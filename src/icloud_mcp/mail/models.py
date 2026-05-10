"""Pydantic models for mail entities returned by MCP tools."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class Mailbox(BaseModel):
    """An IMAP folder (e.g. 'INBOX', 'Sent Messages')."""

    name: str
    flags: list[str] = Field(default_factory=list)
    delimiter: str = "/"


class Address(BaseModel):
    """An RFC 5322 address (with optional display name)."""

    email: str
    name: Optional[str] = None


class AttachmentMeta(BaseModel):
    """Metadata about an attachment, without the bytes."""

    filename: str
    content_type: str
    size: int


class MessageHeader(BaseModel):
    """Lightweight message metadata returned by list / search.

    No body content — bodies are fetched on demand via get_message.
    """

    uid: int = Field(description="IMAP UID, unique within the mailbox")
    mailbox: str = Field(description="The folder this message lives in")
    from_addr: Address = Field(description="Sender (RFC 5322 'From' header)")
    to: list[Address] = Field(default_factory=list)
    cc: list[Address] = Field(default_factory=list)
    subject: str = ""
    date: datetime = Field(description="Message date, timezone-aware")
    is_read: bool = False
    is_flagged: bool = False
    size: Optional[int] = Field(default=None, description="Size in bytes if known")
    message_id: Optional[str] = Field(
        default=None,
        description="RFC 5322 Message-ID, used for threading",
    )
    in_reply_to: Optional[str] = Field(
        default=None, description="Parent message's Message-ID, if a reply"
    )
    references: list[str] = Field(
        default_factory=list,
        description="Ancestor Message-IDs from oldest to most recent. Used for thread reconstruction.",
    )


class Message(MessageHeader):
    """Full message including body content and attachment metadata.

    Returned by get_message. Attachment bytes are NOT included — use
    get_attachment to fetch a specific attachment by index.
    """

    body_plain: Optional[str] = Field(
        default=None,
        description=(
            "Plaintext body. Either the original text/plain part, or text "
            "extracted from text/html when no plaintext alternative exists."
        ),
    )
    body_html: Optional[str] = Field(
        default=None,
        description="Raw HTML body, only included when include_html=True was set.",
    )
    has_attachments: bool = False
    attachments: list[AttachmentMeta] = Field(default_factory=list)


class AttachmentData(BaseModel):
    """An attachment with its bytes, base64-encoded for JSON transport."""

    filename: str
    content_type: str
    size: int
    data_base64: str = Field(description="Standard base64 encoding of the bytes")