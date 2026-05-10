"""Smoke test: connect to iCloud CalDAV and dump a 7-day event list.

Run with:
    uv run python scripts/smoke.py

This bypasses your MCP client entirely so you can verify your credentials
and the network round-trip work before wiring the MCP server up.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from icloud_mcp.calendar.client import ICloudCalendarClient
from icloud_mcp.config import Config


def main() -> None:
    cfg = Config.from_env()
    client = ICloudCalendarClient(cfg.icloud_username, cfg.icloud_app_password)

    print(f"Connecting to iCloud as {cfg.icloud_username}...")
    calendars = client.list_calendars()
    print(f"\nFound {len(calendars)} calendars:")
    for c in calendars:
        print(f"  • {c.name}")

    now = datetime.now(timezone.utc)
    end = now + timedelta(days=7)
    print(f"\nFetching events from {now.isoformat()} to {end.isoformat()}...")
    events = client.list_events(now, end)
    print(f"\nFound {len(events)} events in the next 7 days:")
    for e in events[:25]:
        when = e.start.astimezone().strftime("%a %b %d %H:%M")
        loc = f" @ {e.location}" if e.location else ""
        print(f"  • [{when}] {e.title}{loc} ({e.calendar})")
    if len(events) > 25:
        print(f"  ... and {len(events) - 25} more")


if __name__ == "__main__":
    main()
