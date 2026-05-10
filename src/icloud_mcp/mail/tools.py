"""MCP tool registrations for iCloud Mail."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Optional

from mcp.types import ToolAnnotations

from icloud_mcp.config import Config
from icloud_mcp.errors import ReadOnlyError
from icloud_mcp.mail.client import ICloudMailClient

READ = ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=True)
WRITE_IDEMPOTENT = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)
WRITE_NEW = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=True,
)
WRITE_DESTRUCTIVE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=True,
    openWorldHint=True,
)


def register_mail_tools(
    mcp: Any,
    config: Config,
    client: Optional[ICloudMailClient] = None,
) -> None:
    """Attach mail tools to a FastMCP server instance.

    If `client` is None, constructs one from config. Pass an existing client
    when sharing it across tool subsystems.
    """
    if client is None:
        client = ICloudMailClient(
            username=config.icloud_username,
            password=config.icloud_app_password,
            user_emails=config.all_user_emails,
        )

    def _require_writable() -> None:
        if config.read_only:
            raise ReadOnlyError(
                "Server is in read-only mode (ICLOUD_MCP_READ_ONLY=1). "
                "Restart with ICLOUD_MCP_READ_ONLY=0 to enable mail writes."
            )

    def _parse_date(value: str, name: str) -> date:
        """Accept ISO date or datetime strings; return a date."""
        try:
            if "T" in value:
                return datetime.fromisoformat(value).date()
            return date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(
                f"{name} must be ISO-8601 date or datetime, e.g. '2026-05-01' "
                f"or '2026-05-01T00:00:00Z'. Got: {exc}"
            ) from exc

    @mcp.tool(annotations=READ)
    def list_mailboxes() -> list[dict]:
        """List all IMAP folders on the connected iCloud Mail account.

        iCloud's standard folders are INBOX, "Sent Messages",
        "Deleted Messages", "Drafts", "Junk", "Archive". Names are case
        sensitive when used in other tools.
        """
        return [m.model_dump() for m in client.list_mailboxes()]

    @mcp.tool(annotations=READ)
    def list_messages(
        mailbox: str = "INBOX",
        limit: int = 25,
        since: Optional[str] = None,
        unread_only: bool = False,
    ) -> list[dict]:
        """List recent message headers from a folder, newest first.

        Returns headers only — no body content. To read a message, call
        get_message with the uid + mailbox.

        For each message you get: uid, mailbox, from_addr (email + display
        name), to, cc, subject, date, is_read, is_flagged, message_id,
        in_reply_to, references. Threading IDs (message_id, in_reply_to,
        references) are included so clients can reconstruct conversations
        without re-fetching.

        Args:
            mailbox: Folder name (case-sensitive). Default INBOX.
            limit: Max messages, 1-100. Default 25.
            since: Optional ISO date or datetime — only return messages on or
                after this date. Server-side IMAP SINCE filter.
            unread_only: If true, return only messages without the \\Seen flag.
        """
        since_date = _parse_date(since, "since") if since else None
        msgs = client.list_messages(
            mailbox=mailbox,
            limit=limit,
            since=since_date,
            unread_only=unread_only,
        )
        return [m.model_dump(mode="json") for m in msgs]

    @mcp.tool(annotations=READ)
    def get_message(
        uid: int,
        mailbox: str,
        include_html: bool = False,
    ) -> dict:
        """Fetch a single message with body and attachment metadata.

        Returns plaintext body in `body_plain` (preferred), with HTML stripped
        to text as fallback for HTML-only messages. Attachment bytes are NOT
        included — use `get_attachment` with an index to fetch one.

        Args:
            uid: IMAP UID from list_messages or search_mail.
            mailbox: Folder containing the message (UIDs are scoped per folder).
            include_html: If true, also include the raw HTML body. Default
                false because HTML can be large and is rarely needed for
                reading content.

        Returns:
            Message dict with fields from MessageHeader plus `body_plain`,
            `body_html` (only when include_html=true), `has_attachments`, and
            `attachments` (list of {filename, content_type, size}).
        """
        msg = client.get_message(uid, mailbox, include_html=include_html)
        return msg.model_dump(mode="json")

    @mcp.tool(annotations=READ)
    def search_mail(
        mailbox: str = "INBOX",
        query: Optional[str] = None,
        from_addr: Optional[str] = None,
        to_addr: Optional[str] = None,
        subject: Optional[str] = None,
        since: Optional[str] = None,
        before: Optional[str] = None,
        unread_only: bool = False,
        limit: int = 25,
    ) -> list[dict]:
        """Server-side IMAP search over a single mailbox.

        At least one content filter (`query`, `from_addr`, `to_addr`,
        `subject`) must be provided. For pure date/unread browsing without a
        content match, use list_messages instead.

        All content filters are substring matches (IMAP semantics). The IMAP
        protocol's TEXT search (used by `query`) covers headers + body.

        Args:
            mailbox: Folder to search (case-sensitive). Default INBOX.
            query: Substring matched against the full message.
            from_addr: Substring matched against From header.
            to_addr: Substring matched against To header.
            subject: Substring matched against Subject header.
            since: Optional ISO date — messages on or after.
            before: Optional ISO date — messages before (exclusive).
            unread_only: If true, only unread messages.
            limit: Max results, 1-100. Default 25.
        """
        since_date = _parse_date(since, "since") if since else None
        before_date = _parse_date(before, "before") if before else None
        msgs = client.search_mail(
            mailbox=mailbox,
            query=query,
            from_addr=from_addr,
            to_addr=to_addr,
            subject=subject,
            since=since_date,
            before=before_date,
            unread_only=unread_only,
            limit=limit,
        )
        return [m.model_dump(mode="json") for m in msgs]

    @mcp.tool(annotations=READ)
    def get_thread(
        uid: int,
        mailbox: str,
        additional_mailboxes: Optional[list[str]] = None,
    ) -> list[dict]:
        """Reconstruct the conversation containing a given message.

        Searches the seed's mailbox plus any `additional_mailboxes` for
        related messages by walking Message-ID / In-Reply-To / References
        headers. Returns headers sorted oldest-first (conversation order),
        deduplicated across folders by Message-ID.

        Real conversations span INBOX (incoming) + Sent Messages (your replies).
        For full bidirectional context, pass `additional_mailboxes=["Sent Messages"]`
        — or use the locale-correct name from your folder list if iCloud
        delivers it differently.

        Args:
            uid: IMAP UID of any message in the thread.
            mailbox: Folder containing that message.
            additional_mailboxes: Optional extra folders to scan for thread
                members. Common: `["Sent Messages"]`.

        Returns:
            List of MessageHeader dicts in conversation order. Each carries
            the `mailbox` it was found in, so the caller can chain to
            get_message correctly.
        """
        headers = client.get_thread(
            uid, mailbox, additional_mailboxes=additional_mailboxes
        )
        return [h.model_dump(mode="json") for h in headers]

    @mcp.tool(annotations=READ)
    def get_attachment(
        uid: int,
        mailbox: str,
        attachment_index: int,
    ) -> dict:
        """Fetch a single attachment's bytes by index.

        The `attachment_index` is the 0-based position of the attachment in
        the `attachments` list returned by get_message.

        Returns the attachment as base64. For a 5MB attachment that's about
        6.7MB on the wire — large attachments may be slow.

        Args:
            uid: IMAP UID of the message.
            mailbox: Folder containing the message.
            attachment_index: 0-based index into Message.attachments.

        Returns:
            Dict with `filename`, `content_type`, `size`, and `data_base64`.
        """
        att = client.get_attachment(uid, mailbox, attachment_index)
        return att.model_dump(mode="json")

    # ----------------------------------------------------------------- writes

    @mcp.tool(annotations=WRITE_NEW)
    def send_mail(
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
        """Send a new email via SMTP and save a copy to the Sent folder.

        Recommended workflow: call once with `dry_run=true` to render the
        full RFC822 message, summarize it back to the user for confirmation,
        then re-run with `dry_run=false`.

        For replies, fetch the parent with `get_message` first, then pass:
          - `in_reply_to=parent.message_id`
          - `references=parent.references + [parent.message_id]`
          - `subject="Re: " + parent.subject` (no auto-prefix)
        This produces RFC-compliant threading that mail clients render as
        a proper conversation.

        Attachments take the same shape as `get_attachment` returns, so a
        forward-attachment workflow can chain them naturally:
          [{filename, content_type, data_base64}]

        Args:
            to: List of recipient emails (at least 1 required).
            subject: Subject line (may be empty, but discouraged).
            body: Plaintext body. Required if `html` not provided.
            cc, bcc: Additional recipients. BCC are not in the headers but are
                in the SMTP envelope, so they receive the message.
            html: Optional HTML alternative. When set, the message is multipart;
                clients display the HTML, fall back to plaintext.
            in_reply_to: Message-ID of the parent (with angle brackets).
            references: Ancestor Message-IDs in conversation order (oldest first).
                The parent's Message-ID is appended automatically if `in_reply_to`
                is set.
            attachments: List of dicts with `filename`, `content_type`, `data_base64`.
                Total attachment size capped at 15MB.
            dry_run: If true, returns rendered RFC822 without sending.

        Returns:
            Dict with `message_id`, `sent`, `saved_to_sent`, `recipients`,
            `rfc822_size`, and optionally `warning` (if Sent-folder save failed
            after a successful send) or `rfc822` (when dry_run=true).
        """
        _require_writable()
        return client.send_mail(
            to=to,
            subject=subject,
            body=body,
            cc=cc,
            bcc=bcc,
            html=html,
            in_reply_to=in_reply_to,
            references=references,
            attachments=attachments,
            dry_run=dry_run,
        )

    @mcp.tool(annotations=WRITE_IDEMPOTENT)
    def mark_read(uids: list[int], mailbox: str) -> dict:
        """Mark messages as read (set the IMAP \\Seen flag).

        Idempotent — re-marking already-read messages is a no-op.

        Args:
            uids: List of UIDs to mark. Empty list returns immediately.
            mailbox: Folder containing the messages.
        """
        _require_writable()
        n = client.mark_read(uids, mailbox)
        return {"updated": n, "mailbox": mailbox}

    @mcp.tool(annotations=WRITE_IDEMPOTENT)
    def mark_unread(uids: list[int], mailbox: str) -> dict:
        """Mark messages as unread (remove the IMAP \\Seen flag).

        Idempotent.

        Args:
            uids: List of UIDs to mark.
            mailbox: Folder containing the messages.
        """
        _require_writable()
        n = client.mark_unread(uids, mailbox)
        return {"updated": n, "mailbox": mailbox}

    @mcp.tool(annotations=WRITE_IDEMPOTENT)
    def flag_messages(uids: list[int], mailbox: str) -> dict:
        """Add a star/flag to messages (the IMAP \\Flagged flag).

        Args:
            uids: List of UIDs to flag.
            mailbox: Folder containing the messages.
        """
        _require_writable()
        n = client.flag_messages(uids, mailbox)
        return {"updated": n, "mailbox": mailbox}

    @mcp.tool(annotations=WRITE_IDEMPOTENT)
    def unflag_messages(uids: list[int], mailbox: str) -> dict:
        """Remove the star/flag from messages.

        Args:
            uids: List of UIDs to unflag.
            mailbox: Folder containing the messages.
        """
        _require_writable()
        n = client.unflag_messages(uids, mailbox)
        return {"updated": n, "mailbox": mailbox}

    @mcp.tool(annotations=WRITE_IDEMPOTENT)
    def move_message(
        uid: int,
        source_mailbox: str,
        target_mailbox: str,
    ) -> dict:
        """Move a single message between folders.

        Verifies the target folder exists before attempting the move.

        Args:
            uid: UID of the message in `source_mailbox`.
            source_mailbox: Folder currently containing the message.
            target_mailbox: Destination folder. Must exist.
        """
        _require_writable()
        return client.move_message(uid, source_mailbox, target_mailbox)

    @mcp.tool(annotations=WRITE_DESTRUCTIVE)
    def delete_message(
        uid: int,
        mailbox: str,
        permanent: bool = False,
    ) -> dict:
        """Delete a message.

        Default behavior is soft-delete: the message moves to the Trash
        ("Deleted Messages") folder, where it can be recovered. Pass
        `permanent=True` to skip Trash and EXPUNGE immediately. ⚠️ Permanent
        deletes cannot be undone — confirm with the user before using.

        If the message is already in Trash, soft-delete escalates to
        permanent automatically.

        Args:
            uid: UID of the message to delete.
            mailbox: Folder containing the message.
            permanent: If True, hard-delete (no Trash). Default False.

        Returns:
            Dict with `uid`, `mailbox`, `permanent`, and `moved_to` (when
            soft-deleted).
        """
        _require_writable()
        return client.delete_message(uid, mailbox, permanent=permanent)
