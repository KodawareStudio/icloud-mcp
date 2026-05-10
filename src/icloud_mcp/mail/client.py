"""iCloud IMAP + SMTP client.

Wraps `imap-tools` with iCloud-specific concerns:
- Long-lived MailBox connection, lazy-connected, single-instance per client
- Reconnect-once-on-failure for IMAP operations (handles idle drops, network blips)
- Error wrapping (imap-tools/socket exceptions → actionable domain errors)
- Header parsing helpers factored out (testable without network)
- HTML→plaintext fallback for emails with only HTML bodies
- Thread reconstruction via IMAP HEADER searches across References/In-Reply-To
- SMTP send via stdlib smtplib + email.message
- Sent-folder save via IMAP APPEND (iCloud SMTP doesn't auto-save)

iCloud-specific notes:
- IMAP at imap.mail.me.com:993 SSL
- SMTP at smtp.mail.me.com:587 STARTTLS
- Folder names: INBOX, "Sent Messages", "Deleted Messages", "Drafts", "Junk", "Archive"
- Folder discovery uses RFC 6154 SPECIAL-USE flags where available
- UIDs are stable within a folder until UIDVALIDITY changes
- Attachment size limit: ~20MB (iCloud); we cap at 15MB defensively
"""
from __future__ import annotations

import base64
import functools
import logging
import re
import smtplib
import socket
import ssl
from datetime import date, datetime, timezone
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
from html.parser import HTMLParser
from typing import Any, Callable, Iterable, Optional

from imap_tools import AND, MailBox, MailMessage
from imap_tools import errors as imap_errors

from icloud_mcp.errors import (
    AuthenticationError,
    MessageNotFoundError,
    NetworkError,
)
from icloud_mcp.mail.models import (
    Address,
    AttachmentData,
    AttachmentMeta,
    Mailbox,
    Message,
    MessageHeader,
)
from icloud_mcp.util import validate_email

ICLOUD_IMAP_HOST = "imap.mail.me.com"
ICLOUD_IMAP_PORT = 993
ICLOUD_SMTP_HOST = "smtp.mail.me.com"
ICLOUD_SMTP_PORT = 587

# Defensive cap; iCloud's hard limit is ~20MB.
MAX_ATTACHMENT_TOTAL_BYTES = 15 * 1024 * 1024

# RFC 5322 Message-ID extraction: each ID is wrapped in angle brackets.
# References header is a whitespace-separated list of IDs.
_MSG_ID_RE = re.compile(r"<[^<>\s]+>")

# Substrings that indicate the IMAP connection is no longer usable.
# We retry these once with a fresh connection rather than surfacing them.
_STALE_CONNECTION_MARKERS = (
    "bye",
    "disconnected",
    "broken pipe",
    "abort",
    "closed",
    "timeout",
    "timed out",
    "connection reset",
)

logger = logging.getLogger(__name__)


def _reconnect_on_imap_failure(method: Callable) -> Callable:
    """Decorator: catches connection-level errors, reconnects, retries once.

    Wraps a method so that if the underlying IMAP connection has gone stale
    (idle timeout from iCloud, NAT rebind, network blip), the next call
    transparently reconnects rather than failing with a confusing error.

    Only retries once — if reconnect-and-retry also fails, the second
    exception propagates so we don't loop forever.
    """

    @functools.wraps(method)
    def wrapper(self: ICloudMailClient, *args: Any, **kwargs: Any) -> Any:
        try:
            return method(self, *args, **kwargs)
        except (
            BrokenPipeError,
            ConnectionResetError,
            ConnectionAbortedError,
            EOFError,
        ) as exc:
            logger.warning(
                "IMAP connection dropped during %s (%s); reconnecting and retrying",
                method.__name__,
                exc,
            )
            self.close()
            return method(self, *args, **kwargs)
        except imap_errors.ImapToolsError as exc:
            err_msg = str(exc).lower()
            if any(m in err_msg for m in _STALE_CONNECTION_MARKERS):
                logger.warning(
                    "IMAP appears stale during %s (%s); reconnecting and retrying",
                    method.__name__,
                    exc,
                )
                self.close()
                return method(self, *args, **kwargs)
            raise

    return wrapper


# ============================================================== client class


class ICloudMailClient:
    """Lazy-connecting IMAP client for iCloud Mail.

    Holds a single MailBox instance for the lifetime of the server. Folder
    state is set per operation. Reconnection on dropped connections is not
    implemented in the spike — if a long-running server hits a stale
    connection, restart Claude Desktop.
    """

    def __init__(
        self,
        username: str,
        password: str,
        *,
        user_emails: Optional[Iterable[str]] = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        self._username = username
        self._password = password
        if user_emails is None:
            self._user_emails: tuple[str, ...] = (username.lower(),)
        else:
            normalized = tuple(e.lower() for e in user_emails if e)
            if username.lower() not in normalized:
                normalized = (username.lower(), *normalized)
            self._user_emails = normalized
        self._timeout = timeout_seconds
        self._mailbox: Optional[MailBox] = None
        self._current_folder: Optional[str] = None

    def _imap(self) -> MailBox:
        if self._mailbox is None:
            try:
                mb = MailBox(
                    ICLOUD_IMAP_HOST,
                    port=ICLOUD_IMAP_PORT,
                    timeout=self._timeout,
                )
                mb.login(self._username, self._password)
                self._mailbox = mb
            except imap_errors.MailboxLoginError as exc:
                raise AuthenticationError(
                    "iCloud Mail authentication failed. Verify ICLOUD_USERNAME is "
                    "your full Apple ID email and ICLOUD_APP_PASSWORD is a valid "
                    "app-specific password from appleid.apple.com. Note: the same "
                    "app-specific password works for both Calendar and Mail."
                ) from exc
            except (socket.timeout, socket.gaierror, OSError) as exc:
                raise NetworkError(
                    f"Could not reach {ICLOUD_IMAP_HOST}:{ICLOUD_IMAP_PORT}: {exc}"
                ) from exc
        return self._mailbox

    def _set_folder(self, folder: str) -> MailBox:
        mb = self._imap()
        if self._current_folder != folder:
            try:
                mb.folder.set(folder)
                self._current_folder = folder
            except imap_errors.MailboxFolderSelectError as exc:
                available = sorted(f.name for f in mb.folder.list())
                raise ValueError(
                    f"No folder named {folder!r}. Available: {available}"
                ) from exc
        return mb

    def close(self) -> None:
        """Cleanly logout. Safe to call multiple times."""
        if self._mailbox is not None:
            try:
                self._mailbox.logout()
            except Exception as exc:
                logger.debug("Error during IMAP logout: %s", exc)
            finally:
                self._mailbox = None
                self._current_folder = None

    def sent_folder_name(self) -> str:
        """Resolve the Sent folder name (RFC 6154 \\Sent flag, falls back to 'Sent Messages')."""
        return self._find_special_folder("\\Sent", "Sent Messages")

    def trash_folder_name(self) -> str:
        """Resolve the Trash folder name (RFC 6154 \\Trash flag, falls back to 'Deleted Messages')."""
        return self._find_special_folder("\\Trash", "Deleted Messages")

    @property
    def user_emails(self) -> tuple[str, ...]:
        """All emails the user is identified by — primary username + aliases."""
        return self._user_emails

    # ------------------------------------------------------------------ reads

    @_reconnect_on_imap_failure
    def list_mailboxes(self) -> list[Mailbox]:
        """List all IMAP folders on the account."""
        mb = self._imap()
        try:
            folders = mb.folder.list()
        except Exception as exc:
            raise NetworkError(f"Failed to list mailboxes: {exc}") from exc

        return [
            Mailbox(
                name=f.name,
                flags=list(f.flags) if f.flags else [],
                delimiter=f.delim or "/",
            )
            for f in folders
        ]

    @_reconnect_on_imap_failure
    def list_messages(
        self,
        mailbox: str = "INBOX",
        limit: int = 25,
        since: Optional[date] = None,
        unread_only: bool = False,
    ) -> list[MessageHeader]:
        """List recent messages from a folder, newest first.

        Args:
            mailbox: Folder name (default INBOX).
            limit: Max messages to return; capped at 100.
            since: Optional date filter (server-side IMAP SINCE).
            unread_only: If True, only return messages without the \\Seen flag.

        Returns:
            List of MessageHeader objects sorted newest-first.
        """
        if limit < 1 or limit > 100:
            raise ValueError("limit must be between 1 and 100")

        mb = self._set_folder(mailbox)

        # Build IMAP search criteria
        criteria_kwargs: dict[str, Any] = {}
        if since is not None:
            criteria_kwargs["date_gte"] = (
                since.date() if isinstance(since, datetime) else since
            )
        if unread_only:
            criteria_kwargs["seen"] = False

        if criteria_kwargs:
            criteria = AND(**criteria_kwargs)
        else:
            criteria = "ALL"

        try:
            msgs = list(
                mb.fetch(
                    criteria,
                    headers_only=True,
                    mark_seen=False,
                    reverse=True,  # newest first
                    limit=limit,
                )
            )
        except Exception as exc:
            raise NetworkError(
                f"Failed to fetch messages from {mailbox!r}: {exc}"
            ) from exc

        return [_to_header(m, mailbox) for m in msgs]

    @_reconnect_on_imap_failure
    def get_message(
        self,
        uid: int,
        mailbox: str,
        include_html: bool = False,
    ) -> Message:
        """Fetch a single message with body and attachment metadata."""
        mb = self._set_folder(mailbox)
        try:
            msgs = list(
                mb.fetch(
                    AND(uid=str(uid)),
                    headers_only=False,
                    mark_seen=False,
                    limit=1,
                )
            )
        except Exception as exc:
            raise NetworkError(
                f"Failed to fetch message uid={uid} from {mailbox!r}: {exc}"
            ) from exc

        if not msgs:
            raise MessageNotFoundError(
                f"No message with UID {uid} in {mailbox!r}. The UID may be stale "
                "(message moved or deleted), or wrong mailbox specified."
            )
        return _to_message(msgs[0], mailbox, include_html=include_html)

    @_reconnect_on_imap_failure
    def search_mail(
        self,
        mailbox: str = "INBOX",
        query: Optional[str] = None,
        from_addr: Optional[str] = None,
        to_addr: Optional[str] = None,
        subject: Optional[str] = None,
        since: Optional[date] = None,
        before: Optional[date] = None,
        unread_only: bool = False,
        limit: int = 25,
    ) -> list[MessageHeader]:
        """Server-side IMAP search over a single mailbox.

        At least one content filter (`query`, `from_addr`, `to_addr`, `subject`)
        must be provided — for date/unread-only filtering with no content
        criterion, use list_messages.

        Args:
            mailbox: Folder to search.
            query: Substring matched against the full message (headers + body).
            from_addr: Substring matched against From header.
            to_addr: Substring matched against To header.
            subject: Substring matched against Subject header.
            since: Optional date floor (server-side IMAP SINCE).
            before: Optional date ceiling (server-side IMAP BEFORE).
            unread_only: If true, only unread messages.
            limit: Max results, 1-100.
        """
        if limit < 1 or limit > 100:
            raise ValueError("limit must be between 1 and 100")

        if not any((query, from_addr, to_addr, subject)):
            raise ValueError(
                "search_mail requires at least one of: query, from_addr, to_addr, "
                "subject. For date or unread-only filters with no content match, "
                "use list_messages."
            )

        kwargs: dict[str, Any] = {}
        if query:
            kwargs["text"] = query
        if from_addr:
            kwargs["from_"] = from_addr
        if to_addr:
            kwargs["to"] = to_addr
        if subject:
            kwargs["subject"] = subject
        if since is not None:
            kwargs["date_gte"] = (
                since.date() if isinstance(since, datetime) else since
            )
        if before is not None:
            kwargs["date_lt"] = (
                before.date() if isinstance(before, datetime) else before
            )
        if unread_only:
            kwargs["seen"] = False

        mb = self._set_folder(mailbox)
        try:
            msgs = list(
                mb.fetch(
                    AND(**kwargs),
                    headers_only=True,
                    mark_seen=False,
                    reverse=True,
                    limit=limit,
                )
            )
        except Exception as exc:
            raise NetworkError(
                f"Search failed in {mailbox!r}: {exc}"
            ) from exc

        return [_to_header(m, mailbox) for m in msgs]

    @_reconnect_on_imap_failure
    def get_thread(
        self,
        uid: int,
        mailbox: str,
        additional_mailboxes: Optional[list[str]] = None,
    ) -> list[MessageHeader]:
        """Reconstruct the conversation containing a given message.

        Searches the seed's mailbox plus any `additional_mailboxes` for
        messages whose Message-ID matches any of the seed's References /
        In-Reply-To, OR whose In-Reply-To / References contains the seed's
        Message-ID. Returns headers sorted oldest-first (conversation order),
        deduplicated across folders by Message-ID.

        For typical conversations spanning your inbox and replies you sent,
        pass `additional_mailboxes=["Sent Messages"]` (or use the
        sent_folder_name() helper to get the locale-correct name).
        """
        seed = self.get_message(uid, mailbox, include_html=False)
        seed_header = MessageHeader(**{
            k: v for k, v in seed.model_dump().items()
            if k in MessageHeader.model_fields
        })

        related: set[str] = set()
        if seed_header.message_id:
            related.add(seed_header.message_id)
        if seed_header.in_reply_to:
            related.add(seed_header.in_reply_to)
        related.update(seed_header.references)

        if len(related) <= 1:
            return [seed_header]

        criteria = _build_thread_criteria(
            related, descendant_id=seed_header.message_id
        )

        folders = [mailbox]
        for f in additional_mailboxes or []:
            if f and f != mailbox and f not in folders:
                folders.append(f)

        headers: list[MessageHeader] = []
        seen_message_ids: set[str] = set()
        for folder in folders:
            try:
                mb = self._set_folder(folder)
                msgs = list(
                    mb.fetch(
                        criteria,
                        headers_only=True,
                        mark_seen=False,
                        limit=200,
                    )
                )
            except Exception as exc:
                logger.warning(
                    "Thread fetch in folder %r failed: %s", folder, exc
                )
                continue

            for m in msgs:
                h = _to_header(m, folder)
                if h.message_id:
                    if h.message_id in seen_message_ids:
                        continue
                    seen_message_ids.add(h.message_id)
                headers.append(h)

        # Defensively ensure the seed itself is included.
        if not any(
            h.uid == seed_header.uid and h.mailbox == seed_header.mailbox
            for h in headers
        ):
            headers.append(seed_header)

        headers.sort(key=lambda h: h.date)
        return headers

    @_reconnect_on_imap_failure
    def get_attachment(
        self, uid: int, mailbox: str, attachment_index: int
    ) -> AttachmentData:
        """Fetch a single attachment's bytes by index.

        The attachment_index corresponds to the order in `Message.attachments`
        from get_message.
        """
        mb = self._set_folder(mailbox)
        try:
            msgs = list(
                mb.fetch(
                    AND(uid=str(uid)),
                    headers_only=False,
                    mark_seen=False,
                    limit=1,
                )
            )
        except Exception as exc:
            raise NetworkError(
                f"Failed to fetch message uid={uid} for attachment: {exc}"
            ) from exc

        if not msgs:
            raise MessageNotFoundError(
                f"No message with UID {uid} in {mailbox!r}."
            )

        attachments = list(msgs[0].attachments)
        if attachment_index < 0 or attachment_index >= len(attachments):
            raise ValueError(
                f"attachment_index {attachment_index} out of range; "
                f"message has {len(attachments)} attachment(s)."
            )

        att = attachments[attachment_index]
        payload = att.payload or b""
        return AttachmentData(
            filename=att.filename or f"attachment_{attachment_index}",
            content_type=att.content_type or "application/octet-stream",
            size=len(payload),
            data_base64=base64.b64encode(payload).decode("ascii"),
        )

    # ----------------------------------------------------------------- writes

    def mark_read(self, uids: list[int], mailbox: str) -> int:
        return self._set_flag(uids, mailbox, "\\Seen", True)

    def mark_unread(self, uids: list[int], mailbox: str) -> int:
        return self._set_flag(uids, mailbox, "\\Seen", False)

    def flag_messages(self, uids: list[int], mailbox: str) -> int:
        return self._set_flag(uids, mailbox, "\\Flagged", True)

    def unflag_messages(self, uids: list[int], mailbox: str) -> int:
        return self._set_flag(uids, mailbox, "\\Flagged", False)

    @_reconnect_on_imap_failure
    def _set_flag(
        self, uids: list[int], mailbox: str, flag: str, value: bool
    ) -> int:
        if not uids:
            return 0
        mb = self._set_folder(mailbox)
        try:
            mb.flag([str(u) for u in uids], flag, value)
        except Exception as exc:
            raise NetworkError(
                f"Failed to set {flag} flag on {len(uids)} message(s) in "
                f"{mailbox!r}: {exc}"
            ) from exc
        return len(uids)

    @_reconnect_on_imap_failure
    def move_message(
        self, uid: int, source_mailbox: str, target_mailbox: str
    ) -> dict:
        """Move one message between folders. Verifies the target exists first."""
        if source_mailbox == target_mailbox:
            raise ValueError("source_mailbox and target_mailbox must differ")

        # Verify target folder exists (cheap; cached at the IMAP server).
        available = {f.name for f in self._imap().folder.list()}
        if target_mailbox not in available:
            raise ValueError(
                f"Target folder {target_mailbox!r} doesn't exist. "
                f"Available: {sorted(available)}"
            )

        mb = self._set_folder(source_mailbox)
        try:
            mb.move([str(uid)], target_mailbox)
        except Exception as exc:
            raise NetworkError(
                f"Failed to move uid={uid} from {source_mailbox!r} to "
                f"{target_mailbox!r}: {exc}"
            ) from exc
        return {
            "uid": uid,
            "from_mailbox": source_mailbox,
            "to_mailbox": target_mailbox,
        }

    @_reconnect_on_imap_failure
    def delete_message(
        self, uid: int, mailbox: str, permanent: bool = False
    ) -> dict:
        """Delete a message.

        Default behavior is soft-delete (move to Trash). With permanent=True,
        sets \\Deleted flag and EXPUNGEs. If the message is already in Trash,
        soft-delete escalates to permanent automatically.
        """
        trash_folder = self._find_special_folder("\\Trash", "Deleted Messages")

        if not permanent and mailbox == trash_folder:
            # Already in Trash; escalate to hard delete rather than no-op.
            permanent = True

        if not permanent:
            mb = self._set_folder(mailbox)
            try:
                mb.move([str(uid)], trash_folder)
            except Exception as exc:
                raise NetworkError(
                    f"Failed to move uid={uid} to {trash_folder!r}: {exc}"
                ) from exc
            return {
                "uid": uid,
                "mailbox": mailbox,
                "moved_to": trash_folder,
                "permanent": False,
            }

        mb = self._set_folder(mailbox)
        try:
            mb.delete([str(uid)])
        except Exception as exc:
            raise NetworkError(
                f"Failed to permanently delete uid={uid} from {mailbox!r}: {exc}"
            ) from exc
        return {"uid": uid, "mailbox": mailbox, "permanent": True}

    def send_mail(
        self,
        to: list[str],
        subject: str,
        body: str,
        cc: Optional[list[str]] = None,
        bcc: Optional[list[str]] = None,
        html: Optional[str] = None,
        in_reply_to: Optional[str] = None,
        references: Optional[list[str]] = None,
        attachments: Optional[list[dict]] = None,
        dry_run: bool = False,
    ) -> dict:
        """Send a new email via SMTP and save to the Sent folder via IMAP APPEND.

        Returns a dict with `message_id`, `sent`, `saved_to_sent`, and
        optionally `warning` (set when SMTP succeeds but Sent-folder save fails).
        With `dry_run=True`, returns the rendered RFC822 text without sending.
        """
        msg, recipients, raw_bytes = _build_email(
            from_addr=self._username,
            to=to,
            cc=cc,
            bcc=bcc,
            subject=subject,
            body=body,
            html=html,
            in_reply_to=in_reply_to,
            references=references,
            attachments=attachments,
        )

        if dry_run:
            return {
                "message_id": msg["Message-ID"],
                "sent": False,
                "saved_to_sent": False,
                "rfc822": raw_bytes.decode("utf-8", errors="replace"),
                "rfc822_size": len(raw_bytes),
            }

        # 1. SMTP send
        try:
            with smtplib.SMTP(
                ICLOUD_SMTP_HOST, ICLOUD_SMTP_PORT, timeout=self._timeout
            ) as s:
                s.starttls(context=ssl.create_default_context())
                try:
                    s.login(self._username, self._password)
                except smtplib.SMTPAuthenticationError as exc:
                    raise AuthenticationError(
                        "SMTP authentication failed. The same app-specific "
                        "password used for IMAP should work for SMTP — verify "
                        "ICLOUD_APP_PASSWORD has no stray spaces."
                    ) from exc
                s.send_message(msg, to_addrs=recipients)
        except (socket.timeout, socket.gaierror, OSError) as exc:
            raise NetworkError(
                f"SMTP send failed (could not reach {ICLOUD_SMTP_HOST}): {exc}"
            ) from exc
        except smtplib.SMTPException as exc:
            raise NetworkError(f"SMTP send failed: {exc}") from exc

        # 2. APPEND to Sent folder via IMAP. Failures here are non-fatal —
        #    the message is already sent. The save method handles its own
        #    reconnect-on-failure.
        saved, warning = self._save_to_sent(raw_bytes)

        return {
            "message_id": msg["Message-ID"],
            "sent": True,
            "saved_to_sent": saved,
            "warning": warning,
            "recipients": recipients,
            "rfc822_size": len(raw_bytes),
        }

    @_reconnect_on_imap_failure
    def _save_to_sent(self, raw_bytes: bytes) -> tuple[bool, Optional[str]]:
        """APPEND a sent-message copy to the Sent folder.

        Returns (saved, warning_or_None). Failures are caught and reported
        as warnings rather than raised — the email has already been sent
        successfully via SMTP, so we don't want to mislead the caller into
        thinking the whole operation failed.
        """
        sent_folder = self._find_special_folder("\\Sent", "Sent Messages")
        try:
            mb = self._imap()
            mb.append(raw_bytes, sent_folder, flag_set=("\\Seen",))
            return True, None
        except Exception as exc:
            warning = (
                f"Email was sent successfully but couldn't be saved to "
                f"{sent_folder!r}: {exc}"
            )
            logger.warning(warning)
            return False, warning

    # -------------------------------------------------------------- internals

    def _find_special_folder(self, flag: str, fallback_name: str) -> str:
        """Resolve a folder by RFC 6154 SPECIAL-USE flag with name fallback."""
        try:
            folders = self._imap().folder.list()
        except Exception:
            return fallback_name
        for f in folders:
            if f.flags and flag in f.flags:
                return f.name
        # Try exact match on fallback name as a second-best
        for f in folders:
            if f.name == fallback_name:
                return f.name
        return fallback_name


# =================================================================== helpers
# Pure functions below — testable without network.


def _to_header(msg: MailMessage, mailbox: str) -> MessageHeader:
    """Convert an imap-tools MailMessage into our MessageHeader model."""
    uid = int(msg.uid) if msg.uid else 0
    flags = set(msg.flags or ())

    return MessageHeader(
        uid=uid,
        mailbox=mailbox,
        from_addr=_address_from_values(msg.from_values, msg.from_),
        to=_addresses_from_values(msg.to_values, msg.to),
        cc=_addresses_from_values(msg.cc_values, msg.cc),
        subject=msg.subject or "",
        date=msg.date,
        is_read="\\Seen" in flags,
        is_flagged="\\Flagged" in flags,
        size=msg.size if hasattr(msg, "size") else None,
        message_id=_first_id(msg.headers.get("message-id")),
        in_reply_to=_first_id(msg.headers.get("in-reply-to")),
        references=_extract_ids(msg.headers.get("references")),
    )


def _address_from_values(values: Any, fallback_email: Optional[str]) -> Address:
    """Build an Address from imap-tools `from_values` (an Address namedtuple).

    Falls back to the bare email string if the parsed object is missing.
    """
    if values is not None:
        # imap-tools Address namedtuple has .name and .email fields
        email = getattr(values, "email", "") or ""
        name = getattr(values, "name", "") or ""
        if email:
            return Address(email=email.lower(), name=name or None)
    if fallback_email:
        return Address(email=fallback_email.lower(), name=None)
    return Address(email="", name=None)


def _addresses_from_values(
    values: Iterable[Any] | None, fallback_emails: Iterable[str] | None
) -> list[Address]:
    """Build a list of Address objects from imap-tools values tuples."""
    result: list[Address] = []
    if values:
        for v in values:
            email = getattr(v, "email", "") or ""
            name = getattr(v, "name", "") or ""
            if email:
                result.append(Address(email=email.lower(), name=name or None))
    elif fallback_emails:
        for em in fallback_emails:
            if em:
                result.append(Address(email=em.lower(), name=None))
    return result


def _first_id(header_value: Any) -> Optional[str]:
    """Return the first Message-ID in a header value (with angle brackets)."""
    if header_value is None:
        return None
    s = _header_str(header_value)
    if not s:
        return None
    matches = _MSG_ID_RE.findall(s)
    return matches[0] if matches else None


def _extract_ids(header_value: Any) -> list[str]:
    """Return all Message-IDs in a header value."""
    if header_value is None:
        return []
    s = _header_str(header_value)
    if not s:
        return []
    return _MSG_ID_RE.findall(s)


def _header_str(header_value: Any) -> str:
    """Coerce a header value to a string. Headers can come back as tuples."""
    if isinstance(header_value, str):
        return header_value
    if isinstance(header_value, (tuple, list)):
        return " ".join(str(x) for x in header_value)
    return str(header_value)


# -------------------------------------------------------- full-message support


def _to_message(msg: MailMessage, mailbox: str, *, include_html: bool) -> Message:
    """Convert an imap-tools MailMessage with body into our Message model."""
    header = _to_header(msg, mailbox)

    plain = msg.text or None
    html_raw = msg.html or None

    # If we don't have plaintext but do have HTML, derive plaintext from HTML.
    if not plain and html_raw:
        plain = _strip_html(html_raw)

    attachments_meta = [
        AttachmentMeta(
            filename=a.filename or "",
            content_type=a.content_type or "application/octet-stream",
            size=len(a.payload or b""),
        )
        for a in (msg.attachments or ())
    ]

    return Message(
        **header.model_dump(),
        body_plain=plain,
        body_html=html_raw if include_html else None,
        has_attachments=len(attachments_meta) > 0,
        attachments=attachments_meta,
    )


# ---------------------------------------------------------- HTML→plaintext


class _HTMLToText(HTMLParser):
    """Best-effort HTML stripper for email bodies.

    Drops script/style/head content; inserts breaks at block-level boundaries;
    collapses whitespace. Output is readable, not pretty. Entities are
    handled via convert_charrefs=True.
    """

    _SKIP_TAGS = frozenset({"script", "style", "head", "title"})
    _BLOCK_TAGS = frozenset(
        {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6"}
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
        elif tag in self._BLOCK_TAGS and self._skip_depth == 0:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        elif tag in self._BLOCK_TAGS and self._skip_depth == 0:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self._parts.append(data)

    def get_text(self) -> str:
        text = "".join(self._parts)
        # Collapse runs of newlines to at most two; strip trailing whitespace per line
        text = re.sub(r"\n[ \t]+", "\n", text)
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = re.sub(r"[ \t]{2,}", " ", text)
        return text.strip()


def _strip_html(html_text: str) -> str:
    parser = _HTMLToText()
    try:
        parser.feed(html_text)
        parser.close()
    except Exception:
        # Defensive: if the HTML is so broken HTMLParser raises, fall back
        # to a brute-force regex strip rather than failing the whole tool.
        import html as _html

        return _html.unescape(re.sub(r"<[^>]+>", "", html_text)).strip()
    return parser.get_text()


# ------------------------------------------------------- thread search criteria


def _imap_quote(s: str) -> str:
    """Escape a string for use as an IMAP quoted-string."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _build_thread_criteria(
    message_ids: Iterable[str], *, descendant_id: Optional[str] = None
) -> str:
    """Build raw IMAP search criteria for a thread.

    Matches messages whose Message-ID is in `message_ids` (the ancestor chain),
    plus optionally messages whose In-Reply-To or References header contains
    `descendant_id` (forward-search to find replies).

    IMAP OR is binary, so for N parts we nest as: OR (a) (OR (b) (OR (c) d)).
    """
    parts: list[str] = []
    for mid in sorted(set(message_ids)):
        parts.append(f'HEADER Message-ID "{_imap_quote(mid)}"')
    if descendant_id:
        esc = _imap_quote(descendant_id)
        parts.append(f'HEADER In-Reply-To "{esc}"')
        parts.append(f'HEADER References "{esc}"')

    if not parts:
        return "ALL"
    if len(parts) == 1:
        return parts[0]

    # Nest binary ORs from the right: OR (a) (OR (b) (OR (c) d))
    result = parts[-1]
    for p in reversed(parts[:-1]):
        result = f"OR ({p}) ({result})"
    return result


# ----------------------------------------------------------------- send build


def _build_email(
    *,
    from_addr: str,
    to: list[str],
    cc: Optional[list[str]],
    bcc: Optional[list[str]],
    subject: str,
    body: str,
    html: Optional[str],
    in_reply_to: Optional[str],
    references: Optional[list[str]],
    attachments: Optional[list[dict]],
) -> tuple[EmailMessage, list[str], bytes]:
    """Build an RFC822 message and the SMTP recipient list.

    Returns (msg, recipients, raw_bytes). `recipients` includes To+Cc+Bcc
    (Bcc is intentionally not in the headers but must be in the SMTP envelope).
    """
    if not to:
        raise ValueError("send_mail requires at least one 'to' recipient")
    if not body and not html:
        raise ValueError("send_mail requires either body or html (or both)")

    to_clean = [validate_email(e) for e in to]
    cc_clean = [validate_email(e) for e in (cc or [])]
    bcc_clean = [validate_email(e) for e in (bcc or [])]
    from_clean = validate_email(from_addr)

    # Sanity check: no overlap with To/Cc that would cause duplicate delivery
    seen = set(to_clean)
    cc_clean = [e for e in cc_clean if e not in seen and not seen.add(e)]
    bcc_clean = [e for e in bcc_clean if e not in seen and not seen.add(e)]

    msg = EmailMessage()
    msg["From"] = from_clean
    msg["To"] = ", ".join(to_clean)
    if cc_clean:
        msg["Cc"] = ", ".join(cc_clean)
    msg["Subject"] = subject
    msg["Date"] = format_datetime(datetime.now(timezone.utc))
    msg["Message-ID"] = make_msgid(domain="icloud-mcp")

    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        # References = ancestors + the immediate parent's Message-ID
        ref_chain = list(references or [])
        if in_reply_to not in ref_chain:
            ref_chain.append(in_reply_to)
        msg["References"] = " ".join(ref_chain)
    elif references:
        msg["References"] = " ".join(references)

    # Body: plaintext primary; HTML as alternative if provided.
    msg.set_content(body or "")
    if html:
        msg.add_alternative(html, subtype="html")

    # Attachments
    total_size = 0
    for att in attachments or []:
        if not isinstance(att, dict):
            raise ValueError("Each attachment must be a dict")
        try:
            filename = att["filename"]
            content_type = att.get("content_type", "application/octet-stream")
            data_b64 = att["data_base64"]
        except KeyError as exc:
            raise ValueError(
                f"Attachment missing required key: {exc}. "
                "Expected: filename, content_type, data_base64."
            ) from exc

        try:
            data = base64.b64decode(data_b64, validate=True)
        except Exception as exc:
            raise ValueError(
                f"Attachment {filename!r}: data_base64 is not valid base64: {exc}"
            ) from exc

        total_size += len(data)
        if total_size > MAX_ATTACHMENT_TOTAL_BYTES:
            raise ValueError(
                f"Total attachment size exceeds {MAX_ATTACHMENT_TOTAL_BYTES // (1024 * 1024)}MB cap "
                f"(at {filename!r}, total {total_size:,} bytes). iCloud's hard limit is ~20MB."
            )

        maintype, _, subtype = content_type.partition("/")
        if not subtype:
            maintype, subtype = "application", "octet-stream"
        msg.add_attachment(
            data,
            maintype=maintype,
            subtype=subtype,
            filename=filename,
        )

    raw_bytes = msg.as_bytes()
    recipients = to_clean + cc_clean + bcc_clean
    return msg, recipients, raw_bytes
