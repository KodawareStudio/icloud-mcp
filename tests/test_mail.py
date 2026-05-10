"""Unit tests for mail header parsing.

Tests the pure helper functions and model conversion without touching IMAP.
For end-to-end IMAP verification, use scripts/smoke_mail.py.
"""
from __future__ import annotations

from collections import namedtuple
from datetime import datetime, timezone

import pytest

from icloud_mcp.mail.client import (
    _address_from_values,
    _addresses_from_values,
    _build_email,
    _build_thread_criteria,
    _extract_ids,
    _first_id,
    _header_str,
    _imap_quote,
    _strip_html,
    _to_header,
    _to_message,
)
from icloud_mcp.mail.models import Address


# imap-tools' Address namedtuple-like stand-in
FakeAddr = namedtuple("FakeAddr", ["name", "email"])


# ============================================================ Message-ID parsing


def test_first_id_returns_bracketed_id() -> None:
    assert _first_id("<abc@example.com>") == "<abc@example.com>"
    assert _first_id("<abc@example.com> <ignored@example.com>") == "<abc@example.com>"


def test_first_id_returns_none_for_empty() -> None:
    assert _first_id(None) is None
    assert _first_id("") is None
    assert _first_id("no brackets") is None


def test_extract_ids_returns_all() -> None:
    refs = "<one@x.com> <two@x.com>\r\n  <three@x.com>"
    assert _extract_ids(refs) == ["<one@x.com>", "<two@x.com>", "<three@x.com>"]


def test_extract_ids_handles_tuple_header() -> None:
    # Some IMAP servers return headers as tuples
    assert _extract_ids(("<a@x.com>", "<b@x.com>")) == ["<a@x.com>", "<b@x.com>"]


def test_header_str_coerces_various_types() -> None:
    assert _header_str("foo") == "foo"
    assert _header_str(("foo", "bar")) == "foo bar"
    assert _header_str(["foo", "bar"]) == "foo bar"


# ============================================================ Address parsing


def test_address_from_values_uses_namedtuple() -> None:
    addr = _address_from_values(FakeAddr(name="Alice", email="alice@example.com"), None)
    assert addr.email == "alice@example.com"
    assert addr.name == "Alice"


def test_address_from_values_lowercases_email() -> None:
    addr = _address_from_values(FakeAddr(name="", email="Alice@Example.COM"), None)
    assert addr.email == "alice@example.com"
    assert addr.name is None  # empty name becomes None


def test_address_from_values_falls_back_to_email_string() -> None:
    addr = _address_from_values(None, "fallback@example.com")
    assert addr.email == "fallback@example.com"
    assert addr.name is None


def test_address_from_values_returns_empty_when_nothing() -> None:
    addr = _address_from_values(None, None)
    assert addr.email == ""


def test_addresses_from_values_filters_empty_emails() -> None:
    values = [
        FakeAddr(name="A", email="a@x.com"),
        FakeAddr(name="", email=""),  # skipped
        FakeAddr(name="C", email="c@x.com"),
    ]
    addrs = _addresses_from_values(values, None)
    assert len(addrs) == 2
    assert addrs[0] == Address(email="a@x.com", name="A")
    assert addrs[1] == Address(email="c@x.com", name="C")


# ====================================================== _to_header end-to-end


class FakeMessage:
    """Stand-in for imap_tools.MailMessage with the attributes _to_header reads."""

    def __init__(
        self,
        *,
        uid: str = "1234",
        from_: str = "alice@example.com",
        from_values: object = FakeAddr(name="Alice", email="alice@example.com"),
        to: tuple = ("me@icloud.com",),
        to_values: tuple = (FakeAddr(name="Me", email="me@icloud.com"),),
        cc: tuple = (),
        cc_values: tuple = (),
        subject: str = "Hello",
        date: datetime = datetime(2026, 5, 9, 14, 0, tzinfo=timezone.utc),
        flags: tuple = (),
        size: int = 1234,
        headers: dict | None = None,
    ) -> None:
        self.uid = uid
        self.from_ = from_
        self.from_values = from_values
        self.to = to
        self.to_values = to_values
        self.cc = cc
        self.cc_values = cc_values
        self.subject = subject
        self.date = date
        self.flags = flags
        self.size = size
        self.headers = headers or {}


def test_to_header_basic() -> None:
    msg = FakeMessage(flags=("\\Seen",))
    h = _to_header(msg, "INBOX")
    assert h.uid == 1234
    assert h.mailbox == "INBOX"
    assert h.from_addr.email == "alice@example.com"
    assert h.from_addr.name == "Alice"
    assert h.subject == "Hello"
    assert h.is_read is True
    assert h.is_flagged is False


def test_to_header_marks_unread_when_no_seen_flag() -> None:
    msg = FakeMessage(flags=())
    h = _to_header(msg, "INBOX")
    assert h.is_read is False


def test_to_header_extracts_threading_headers() -> None:
    msg = FakeMessage(
        headers={
            "message-id": "<this@x.com>",
            "in-reply-to": "<parent@x.com>",
            "references": "<grandparent@x.com> <parent@x.com>",
        }
    )
    h = _to_header(msg, "INBOX")
    assert h.message_id == "<this@x.com>"
    assert h.in_reply_to == "<parent@x.com>"
    assert h.references == ["<grandparent@x.com>", "<parent@x.com>"]


def test_to_header_handles_missing_threading_headers() -> None:
    msg = FakeMessage(headers={})
    h = _to_header(msg, "INBOX")
    assert h.message_id is None
    assert h.in_reply_to is None
    assert h.references == []


def test_to_header_recognizes_flagged() -> None:
    msg = FakeMessage(flags=("\\Seen", "\\Flagged"))
    h = _to_header(msg, "INBOX")
    assert h.is_flagged is True


def test_to_header_serializes_to_json() -> None:
    msg = FakeMessage(flags=("\\Seen",))
    h = _to_header(msg, "INBOX")
    j = h.model_dump(mode="json")
    assert j["uid"] == 1234
    assert j["mailbox"] == "INBOX"
    assert j["from_addr"]["email"] == "alice@example.com"
    assert j["is_read"] is True
    assert j["date"].startswith("2026-05-09T14:00:00")


# ============================================================== HTML stripping


def test_strip_html_basic_paragraphs() -> None:
    out = _strip_html("<p>Hello</p><p>World</p>")
    assert "Hello" in out
    assert "World" in out
    assert out.count("\n\n") >= 1  # paragraph break preserved


def test_strip_html_drops_script_and_style() -> None:
    out = _strip_html(
        "<p>Visible</p><script>alert('hidden')</script>"
        "<style>.x { color: red; }</style><p>Also visible</p>"
    )
    assert "Visible" in out
    assert "Also visible" in out
    assert "alert" not in out
    assert "color: red" not in out


def test_strip_html_decodes_entities() -> None:
    out = _strip_html("<p>Tom &amp; Jerry &lt;3 &nbsp;you</p>")
    assert "Tom & Jerry" in out
    assert "<3" in out
    assert "&amp;" not in out


def test_strip_html_collapses_whitespace() -> None:
    out = _strip_html("<p>Lots\n\n\n\nof   spaces\n\n\n</p>")
    # No more than 2 consecutive newlines, runs of spaces collapsed
    assert "\n\n\n" not in out
    assert "   " not in out


def test_strip_html_handles_broken_html() -> None:
    # Unclosed tags, weird structure — should not raise
    out = _strip_html("<p>Start<div><span>middle</p></div>end")
    assert "Start" in out
    assert "middle" in out
    assert "end" in out


def test_strip_html_handles_empty_string() -> None:
    assert _strip_html("") == ""


# ====================================================== _to_message conversion


class FakeAttachment:
    def __init__(
        self,
        *,
        filename: str = "doc.pdf",
        content_type: str = "application/pdf",
        payload: bytes = b"PDFBYTES",
    ) -> None:
        self.filename = filename
        self.content_type = content_type
        self.payload = payload


class FakeFullMessage(FakeMessage):
    """FakeMessage with body and attachments for testing _to_message."""

    def __init__(
        self,
        *,
        text: str | None = None,
        html: str | None = None,
        attachments: tuple = (),
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.text = text
        self.html = html
        self.attachments = attachments


def test_to_message_uses_plaintext_when_available() -> None:
    msg = FakeFullMessage(text="Hello\nWorld", html="<p>Hello</p><p>World</p>")
    m = _to_message(msg, "INBOX", include_html=False)
    assert m.body_plain == "Hello\nWorld"
    assert m.body_html is None  # not requested


def test_to_message_strips_html_when_no_plaintext() -> None:
    msg = FakeFullMessage(text=None, html="<p>Hello</p><p>World</p>")
    m = _to_message(msg, "INBOX", include_html=False)
    assert m.body_plain is not None
    assert "Hello" in m.body_plain
    assert "World" in m.body_plain
    assert "<p>" not in m.body_plain


def test_to_message_includes_html_when_requested() -> None:
    msg = FakeFullMessage(text="plain", html="<p>html</p>")
    m = _to_message(msg, "INBOX", include_html=True)
    assert m.body_plain == "plain"
    assert m.body_html == "<p>html</p>"


def test_to_message_lists_attachments() -> None:
    msg = FakeFullMessage(
        text="see attached",
        attachments=(
            FakeAttachment(filename="a.pdf", payload=b"AAA"),
            FakeAttachment(filename="b.png", content_type="image/png", payload=b"BB"),
        ),
    )
    m = _to_message(msg, "INBOX", include_html=False)
    assert m.has_attachments is True
    assert len(m.attachments) == 2
    assert m.attachments[0].filename == "a.pdf"
    assert m.attachments[0].size == 3
    assert m.attachments[1].content_type == "image/png"


def test_to_message_no_attachments() -> None:
    msg = FakeFullMessage(text="no attachments here")
    m = _to_message(msg, "INBOX", include_html=False)
    assert m.has_attachments is False
    assert m.attachments == []


# =========================================================== thread criteria


def test_imap_quote_escapes_quotes_and_backslashes() -> None:
    assert _imap_quote('hello"world') == 'hello\\"world'
    assert _imap_quote("a\\b") == "a\\\\b"
    assert _imap_quote("normal") == "normal"


def test_thread_criteria_single_id() -> None:
    crit = _build_thread_criteria(["<a@x.com>"])
    assert crit == 'HEADER Message-ID "<a@x.com>"'


def test_thread_criteria_multiple_ids() -> None:
    crit = _build_thread_criteria(["<a@x.com>", "<b@x.com>"])
    # Should use IMAP OR (binary)
    assert crit.startswith("OR (")
    assert "<a@x.com>" in crit
    assert "<b@x.com>" in crit


def test_thread_criteria_with_descendant_search() -> None:
    crit = _build_thread_criteria(
        ["<seed@x.com>"], descendant_id="<seed@x.com>"
    )
    # We expect: MessageID match + In-Reply-To match + References match, OR'd
    assert "Message-ID" in crit
    assert "In-Reply-To" in crit
    assert "References" in crit
    assert crit.count("OR (") == 2  # 3 criteria → 2 ORs


def test_thread_criteria_dedupes_ids() -> None:
    crit = _build_thread_criteria(["<a@x.com>", "<a@x.com>", "<b@x.com>"])
    # Should appear twice total: once for a, once for b
    assert crit.count("<a@x.com>") == 1
    assert crit.count("<b@x.com>") == 1


def test_thread_criteria_empty() -> None:
    assert _build_thread_criteria([]) == "ALL"


def test_thread_criteria_escapes_quoted_id() -> None:
    crit = _build_thread_criteria(['<weird"id@x.com>'])
    assert '\\"' in crit  # quote escaped


# =================================================== send_mail: build helpers


def _parse_built(msg) -> dict:
    """Pull headers off an EmailMessage for assertions."""
    return {h.lower(): msg[h] for h in msg.keys()}


def test_build_email_minimum_required() -> None:
    msg, recipients, raw = _build_email(
        from_addr="me@icloud.com",
        to=["alice@example.com"],
        cc=None,
        bcc=None,
        subject="Hello",
        body="Hi there",
        html=None,
        in_reply_to=None,
        references=None,
        attachments=None,
    )
    h = _parse_built(msg)
    assert h["from"] == "me@icloud.com"
    assert h["to"] == "alice@example.com"
    assert h["subject"] == "Hello"
    assert "<" in h["message-id"] and ">" in h["message-id"]
    assert "date" in h
    assert recipients == ["alice@example.com"]
    assert b"Hi there" in raw


def test_build_email_includes_bcc_in_recipients_not_headers() -> None:
    msg, recipients, _ = _build_email(
        from_addr="me@icloud.com",
        to=["a@x.com"],
        cc=["b@x.com"],
        bcc=["c@x.com"],
        subject="s",
        body="b",
        html=None,
        in_reply_to=None,
        references=None,
        attachments=None,
    )
    h = _parse_built(msg)
    assert h.get("bcc") is None  # MUST not appear in headers
    assert recipients == ["a@x.com", "b@x.com", "c@x.com"]


def test_build_email_dedupes_overlapping_recipients() -> None:
    _, recipients, _ = _build_email(
        from_addr="me@icloud.com",
        to=["a@x.com"],
        cc=["a@x.com", "b@x.com"],  # a is duplicate
        bcc=["b@x.com"],  # b is duplicate
        subject="s",
        body="b",
        html=None,
        in_reply_to=None,
        references=None,
        attachments=None,
    )
    assert recipients == ["a@x.com", "b@x.com"]


def test_build_email_threading_headers() -> None:
    msg, _, _ = _build_email(
        from_addr="me@icloud.com",
        to=["a@x.com"],
        cc=None,
        bcc=None,
        subject="Re: s",
        body="b",
        html=None,
        in_reply_to="<parent@x.com>",
        references=["<grandparent@x.com>"],
        attachments=None,
    )
    h = _parse_built(msg)
    assert h["in-reply-to"] == "<parent@x.com>"
    # References should be grandparent + parent (parent appended)
    assert h["references"] == "<grandparent@x.com> <parent@x.com>"


def test_build_email_threading_with_references_only() -> None:
    msg, _, _ = _build_email(
        from_addr="me@icloud.com",
        to=["a@x.com"],
        cc=None,
        bcc=None,
        subject="s",
        body="b",
        html=None,
        in_reply_to=None,
        references=["<a@x.com>", "<b@x.com>"],
        attachments=None,
    )
    h = _parse_built(msg)
    assert h["references"] == "<a@x.com> <b@x.com>"


def test_build_email_html_alternative() -> None:
    _, _, raw = _build_email(
        from_addr="me@icloud.com",
        to=["a@x.com"],
        cc=None,
        bcc=None,
        subject="s",
        body="plain text",
        html="<p>html text</p>",
        in_reply_to=None,
        references=None,
        attachments=None,
    )
    # Multipart/alternative includes both parts
    assert b"plain text" in raw
    assert b"<p>html text</p>" in raw
    assert b"multipart/alternative" in raw.lower()


def test_build_email_attachment() -> None:
    import base64

    payload = b"PDF-FAKE-BYTES"
    _, _, raw = _build_email(
        from_addr="me@icloud.com",
        to=["a@x.com"],
        cc=None,
        bcc=None,
        subject="s",
        body="see attached",
        html=None,
        in_reply_to=None,
        references=None,
        attachments=[
            {
                "filename": "report.pdf",
                "content_type": "application/pdf",
                "data_base64": base64.b64encode(payload).decode(),
            }
        ],
    )
    assert b"report.pdf" in raw
    assert b"application/pdf" in raw


def test_build_email_rejects_no_recipients() -> None:
    with pytest.raises(ValueError, match="at least one"):
        _build_email(
            from_addr="me@icloud.com",
            to=[],
            cc=None,
            bcc=None,
            subject="s",
            body="b",
            html=None,
            in_reply_to=None,
            references=None,
            attachments=None,
        )


def test_build_email_rejects_no_body_or_html() -> None:
    with pytest.raises(ValueError, match="body or html"):
        _build_email(
            from_addr="me@icloud.com",
            to=["a@x.com"],
            cc=None,
            bcc=None,
            subject="s",
            body="",
            html=None,
            in_reply_to=None,
            references=None,
            attachments=None,
        )


def test_build_email_rejects_invalid_recipient() -> None:
    with pytest.raises(ValueError, match="Invalid email"):
        _build_email(
            from_addr="me@icloud.com",
            to=["not-an-email"],
            cc=None,
            bcc=None,
            subject="s",
            body="b",
            html=None,
            in_reply_to=None,
            references=None,
            attachments=None,
        )


def test_build_email_rejects_oversized_attachments() -> None:
    import base64

    # 16MB > 15MB cap
    huge = b"\x00" * (16 * 1024 * 1024)
    with pytest.raises(ValueError, match="exceeds"):
        _build_email(
            from_addr="me@icloud.com",
            to=["a@x.com"],
            cc=None,
            bcc=None,
            subject="s",
            body="b",
            html=None,
            in_reply_to=None,
            references=None,
            attachments=[
                {
                    "filename": "huge.bin",
                    "content_type": "application/octet-stream",
                    "data_base64": base64.b64encode(huge).decode(),
                }
            ],
        )


def test_build_email_rejects_invalid_base64() -> None:
    with pytest.raises(ValueError, match="not valid base64"):
        _build_email(
            from_addr="me@icloud.com",
            to=["a@x.com"],
            cc=None,
            bcc=None,
            subject="s",
            body="b",
            html=None,
            in_reply_to=None,
            references=None,
            attachments=[
                {
                    "filename": "x.bin",
                    "content_type": "application/octet-stream",
                    "data_base64": "not!valid base64@@@",
                }
            ],
        )


# ============================================== read-only enforcement (mail)


class _FakeMCP:
    def __init__(self) -> None:
        self.tools: dict = {}

    def tool(self, **kwargs):
        def decorator(func):
            self.tools[func.__name__] = func
            return func

        return decorator


def _build_mail_tools(read_only: bool) -> _FakeMCP:
    from icloud_mcp.config import Config
    from icloud_mcp.mail.tools import register_mail_tools

    fake = _FakeMCP()
    config = Config(
        icloud_username="test@icloud.com",
        icloud_app_password="fake-app-pwd",
        read_only=read_only,
    )
    register_mail_tools(fake, config)
    return fake


def test_read_only_blocks_send_mail() -> None:
    fake = _build_mail_tools(read_only=True)
    from icloud_mcp.errors import ReadOnlyError

    with pytest.raises(ReadOnlyError):
        fake.tools["send_mail"](
            to=["a@example.com"], subject="x", body="hi"
        )


def test_read_only_blocks_mark_read() -> None:
    fake = _build_mail_tools(read_only=True)
    from icloud_mcp.errors import ReadOnlyError

    with pytest.raises(ReadOnlyError):
        fake.tools["mark_read"](uids=[1], mailbox="INBOX")


def test_read_only_blocks_delete_message() -> None:
    fake = _build_mail_tools(read_only=True)
    from icloud_mcp.errors import ReadOnlyError

    with pytest.raises(ReadOnlyError):
        fake.tools["delete_message"](uid=1, mailbox="INBOX")


def test_read_only_blocks_move_message() -> None:
    fake = _build_mail_tools(read_only=True)
    from icloud_mcp.errors import ReadOnlyError

    with pytest.raises(ReadOnlyError):
        fake.tools["move_message"](
            uid=1, source_mailbox="INBOX", target_mailbox="Archive"
        )


def test_read_only_blocks_flag() -> None:
    fake = _build_mail_tools(read_only=True)
    from icloud_mcp.errors import ReadOnlyError

    with pytest.raises(ReadOnlyError):
        fake.tools["flag_messages"](uids=[1], mailbox="INBOX")


# ============================================== reconnect-on-failure decorator


def test_reconnect_decorator_retries_on_broken_pipe() -> None:
    from icloud_mcp.mail.client import _reconnect_on_imap_failure

    class FakeClient:
        def __init__(self) -> None:
            self.call_count = 0
            self.close_count = 0

        def close(self) -> None:
            self.close_count += 1

        @_reconnect_on_imap_failure
        def operation(self) -> str:
            self.call_count += 1
            if self.call_count == 1:
                raise BrokenPipeError("connection dropped")
            return "ok"

    c = FakeClient()
    assert c.operation() == "ok"
    assert c.call_count == 2
    assert c.close_count == 1


def test_reconnect_decorator_retries_on_stale_imap_error() -> None:
    from imap_tools import errors as imap_errors

    from icloud_mcp.mail.client import _reconnect_on_imap_failure

    class FakeClient:
        def __init__(self) -> None:
            self.call_count = 0

        def close(self) -> None:
            pass

        @_reconnect_on_imap_failure
        def operation(self) -> str:
            self.call_count += 1
            if self.call_count == 1:
                # Stale-marker substring "bye" in the message → triggers retry
                raise imap_errors.ImapToolsError(
                    "BYE: Connection closed by server"
                )
            return "ok"

    c = FakeClient()
    assert c.operation() == "ok"
    assert c.call_count == 2


def test_reconnect_decorator_does_not_retry_unrelated_errors() -> None:
    from icloud_mcp.mail.client import _reconnect_on_imap_failure

    class FakeClient:
        def __init__(self) -> None:
            self.call_count = 0

        def close(self) -> None:
            pass

        @_reconnect_on_imap_failure
        def operation(self) -> str:
            self.call_count += 1
            raise ValueError("not a connection problem")

    c = FakeClient()
    with pytest.raises(ValueError, match="not a connection"):
        c.operation()
    assert c.call_count == 1  # not retried


def test_reconnect_decorator_propagates_second_failure() -> None:
    from icloud_mcp.mail.client import _reconnect_on_imap_failure

    class FakeClient:
        def __init__(self) -> None:
            self.call_count = 0

        def close(self) -> None:
            pass

        @_reconnect_on_imap_failure
        def operation(self) -> str:
            self.call_count += 1
            raise BrokenPipeError("still broken")

    c = FakeClient()
    with pytest.raises(BrokenPipeError):
        c.operation()
    # Tried twice: original + one retry
    assert c.call_count == 2
