"""Shared utilities used across calendar and mail subsystems."""
from __future__ import annotations

import re

_EMAIL_RE = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")


def validate_email(email: str) -> str:
    """Validate and normalize a single email address.

    Strips whitespace; raises ValueError if the address fails a basic format
    check. Not RFC-strict, but catches typos before they hit a remote server.
    """
    e = email.strip()
    if not _EMAIL_RE.match(e):
        raise ValueError(f"Invalid email address: {email!r}")
    return e
