"""Domain-specific exceptions for iCloud MCP.

These wrap underlying caldav / network errors with messages that are
actionable for the calling agent, not just the developer.
"""
from __future__ import annotations


class ICloudError(Exception):
    """Base class for all iCloud MCP errors."""


class AuthenticationError(ICloudError):
    """iCloud rejected the credentials.

    Almost always means: bad app-specific password, or username is missing
    the email domain, or the password was generated for a different Apple ID.
    """


class EventNotFoundError(ICloudError):
    """Requested event UID does not exist in any (searched) calendar."""


class MessageNotFoundError(ICloudError):
    """Requested message UID does not exist in the specified mailbox.

    Most often this means: the UID was stale (e.g. message was moved to another
    folder or deleted), or you passed a UID from one mailbox while specifying a
    different `mailbox`. Refresh with list_messages or search_mail and retry.
    """


class ContactNotFoundError(ICloudError):
    """Requested contact resource does not exist in iCloud Contacts."""


class CalendarNotFoundError(ICloudError):
    """No calendar with the requested name exists on the account."""


class ConflictError(ICloudError):
    """Event was modified by another client between read and write.

    The on-disk event has a newer ETag than what we tried to write against.
    Refetch the event with `get_event` or `list_events` and retry the
    operation against the fresh data.
    """


class NetworkError(ICloudError):
    """Network-level failure communicating with iCloud (timeout, DNS, TLS)."""


class ReadOnlyError(ICloudError):
    """A destructive tool was called while ICLOUD_MCP_READ_ONLY=1."""
