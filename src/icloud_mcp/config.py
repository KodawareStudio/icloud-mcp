"""Configuration loaded from environment variables.

Validates that required credentials are present at startup so the server
fails fast with a clear error rather than blowing up on the first tool call.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

from dotenv import load_dotenv

if not os.environ.get("VERCEL"):
    load_dotenv()


@dataclass(frozen=True)
class Config:
    icloud_username: str
    icloud_app_password: str
    read_only: bool
    user_aliases: tuple[str, ...] = field(default_factory=tuple)
    user_timezone: Optional[str] = None

    @property
    def all_user_emails(self) -> tuple[str, ...]:
        """All emails the user is identified by, including the primary username.

        Used wherever we need to recognize "messages addressed to me" or
        "the user's attendee record" — Apple often delivers invites and mail
        to the @me.com or @mac.com aliases of an @icloud.com account, so a
        single username isn't sufficient.
        """
        primary = self.icloud_username.lower()
        return (primary, *self.user_aliases)

    @classmethod
    def from_env(cls) -> Config:
        username = os.environ.get("ICLOUD_USERNAME", "").strip()
        password = os.environ.get("ICLOUD_APP_PASSWORD", "").strip()

        if not username or not password:
            raise RuntimeError(
                "Missing iCloud credentials. Set ICLOUD_USERNAME (full email) and "
                "ICLOUD_APP_PASSWORD (an app-specific password from "
                "appleid.apple.com → Sign-In and Security → App-Specific Passwords) "
                "before starting the server."
            )

        if "@" not in username:
            raise RuntimeError(
                f"ICLOUD_USERNAME must be a full email address (got: {username!r})."
            )

        read_only = os.environ.get("ICLOUD_MCP_READ_ONLY", "").lower() in (
            "1",
            "true",
            "yes",
        )

        aliases_raw = os.environ.get("ICLOUD_USER_ALIASES", "").strip()
        alias_list: list[str] = []
        if aliases_raw:
            for raw in aliases_raw.split(","):
                a = raw.strip().lower()
                if not a:
                    continue
                if "@" not in a:
                    raise RuntimeError(
                        f"ICLOUD_USER_ALIASES contains invalid entry {raw!r}: "
                        "each alias must be a full email address."
                    )
                if a == username.lower():
                    continue  # primary already covered
                alias_list.append(a)

        timezone = os.environ.get("ICLOUD_USER_TIMEZONE", "").strip() or None

        return cls(
            icloud_username=username,
            icloud_app_password=password,
            read_only=read_only,
            user_aliases=tuple(alias_list),
            user_timezone=timezone,
        )


config = Config.from_env()
