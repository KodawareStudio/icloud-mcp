"""Smoke test: connect to iCloud Mail and dump folders + recent INBOX headers.

Run with:
    uv run python scripts/smoke_mail.py

This bypasses your MCP client entirely so you can verify your IMAP credentials
and the network round-trip work before wiring the MCP server up.

Note: the same app-specific password works for both Calendar and Mail —
no need to generate a separate one.
"""
from __future__ import annotations

from icloud_mcp.config import Config
from icloud_mcp.mail.client import ICloudMailClient


def main() -> None:
    cfg = Config.from_env()
    client = ICloudMailClient(cfg.icloud_username, cfg.icloud_app_password)

    try:
        print(f"Connecting to iCloud Mail as {cfg.icloud_username}...")
        mailboxes = client.list_mailboxes()
        print(f"\nFound {len(mailboxes)} folders:")
        for mb in mailboxes:
            print(f"  • {mb.name}")

        print("\nFetching most recent 10 messages from INBOX...")
        messages = client.list_messages(mailbox="INBOX", limit=10)
        print(f"Found {len(messages)} messages:\n")

        for m in messages:
            read_marker = " " if m.is_read else "●"  # filled = unread
            flag_marker = "⚑" if m.is_flagged else " "
            sender = m.from_addr.name or m.from_addr.email
            sender_display = sender[:30].ljust(30)
            subject_display = (m.subject or "(no subject)")[:60]
            date_display = m.date.astimezone().strftime("%b %d %H:%M")
            print(
                f"  {read_marker}{flag_marker} [{date_display}] "
                f"{sender_display}  {subject_display}"
            )
    finally:
        client.close()


if __name__ == "__main__":
    main()
