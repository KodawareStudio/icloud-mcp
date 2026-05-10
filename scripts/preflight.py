"""Pre-flight check: validate everything works end-to-end before wiring into Claude Desktop.

Run with:
    uv run python scripts/preflight.py

Exits 0 on success, 1 on first failure. Each check is self-contained and
produces an actionable error message rather than a stack trace.

Checks (in order):
    1. Environment variables present and well-formed
    2. CalDAV: connection + auth + calendar list
    3. CalDAV: read events from the next 7 days
    4. IMAP: connection + auth + mailbox list
    5. IMAP: list 5 most recent INBOX messages
    6. SMTP: TLS handshake + auth (sends nothing)

If all six pass, the system is ready. If something fails, the message tells
you exactly what to fix.
"""
from __future__ import annotations

import smtplib
import ssl
import sys
from datetime import datetime, timedelta, timezone


# ANSI escapes; Claude Desktop logs may not render these but humans running
# this in a terminal will see colored output.
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
BOLD = "\033[1m"
RESET = "\033[0m"


def ok(msg: str) -> None:
    print(f"  {GREEN}✓{RESET} {msg}")


def fail(msg: str, hint: str = "") -> None:
    print(f"  {RED}✗{RESET} {BOLD}{msg}{RESET}")
    if hint:
        for line in hint.splitlines():
            print(f"    {YELLOW}{line}{RESET}")
    sys.exit(1)


def step(n: int, total: int, label: str) -> None:
    print(f"\n{BOLD}[{n}/{total}]{RESET} {label}")


def main() -> None:
    print(f"{BOLD}icloud-mcp pre-flight check{RESET}\n")

    total = 6

    # ---- 1. env ----
    step(1, total, "Environment configuration")
    try:
        from icloud_mcp.config import Config

        cfg = Config.from_env()
    except RuntimeError as e:
        fail(
            "Configuration invalid",
            str(e),
        )
        return

    ok(f"Username: {cfg.icloud_username}")
    if cfg.user_aliases:
        ok(f"Aliases: {', '.join(cfg.user_aliases)}")
    if cfg.user_timezone:
        ok(f"Timezone: {cfg.user_timezone}")
    else:
        print(
            f"  {YELLOW}!{RESET} No ICLOUD_USER_TIMEZONE set — today_brief will use UTC."
        )
    if cfg.read_only:
        print(
            f"  {YELLOW}!{RESET} ICLOUD_MCP_READ_ONLY=1 — write tools will be disabled."
        )

    # ---- 2. CalDAV connect ----
    step(2, total, "CalDAV connection (calendar)")
    try:
        from icloud_mcp.calendar.client import ICloudCalendarClient
        from icloud_mcp.errors import AuthenticationError, NetworkError

        cal = ICloudCalendarClient(
            username=cfg.icloud_username,
            password=cfg.icloud_app_password,
            user_emails=cfg.all_user_emails,
        )
        calendars = cal.list_calendars()
    except AuthenticationError as e:
        fail(
            "CalDAV authentication failed",
            f"{e}\n\n"
            "Most common causes:\n"
            "  • The app-specific password expired or was revoked.\n"
            "  • You generated the password under a different Apple ID.\n"
            "  • The username isn't the full email (must include @icloud.com / @me.com / @mac.com).\n"
            "Generate a fresh password at appleid.apple.com → Sign-In and Security → "
            "App-Specific Passwords, then update ICLOUD_APP_PASSWORD.",
        )
        return
    except NetworkError as e:
        fail(
            "Could not reach iCloud CalDAV",
            f"{e}\n\n"
            "Check your network connection. iCloud CalDAV is at caldav.icloud.com:443.",
        )
        return
    except Exception as e:
        fail("Unexpected CalDAV error", f"{type(e).__name__}: {e}")
        return

    ok(f"Connected. Found {len(calendars)} calendar(s):")
    for c in calendars:
        print(f"      • {c.name}")

    # ---- 3. CalDAV read ----
    step(3, total, "Read events from the next 7 days")
    try:
        now = datetime.now(timezone.utc)
        events = cal.list_events(now, now + timedelta(days=7))
    except Exception as e:
        fail("Event fetch failed", f"{type(e).__name__}: {e}")
        return

    if events:
        ok(f"Read {len(events)} event(s). Sample: {events[0].title!r} at {events[0].start.isoformat()}")
    else:
        ok("Read 0 events (your next 7 days are clear, or no events visible).")

    # ---- 4. IMAP connect ----
    step(4, total, "IMAP connection (mail)")
    try:
        from icloud_mcp.mail.client import ICloudMailClient

        mail = ICloudMailClient(
            username=cfg.icloud_username,
            password=cfg.icloud_app_password,
            user_emails=cfg.all_user_emails,
        )
        mailboxes = mail.list_mailboxes()
    except AuthenticationError as e:
        fail(
            "IMAP authentication failed",
            f"{e}\n\n"
            "Calendar worked but mail didn't — that's unusual. Verify:\n"
            "  • The same app-specific password is being used.\n"
            "  • Mail is enabled on the iCloud account at icloud.com.\n"
            "  • The account hasn't hit a rate limit (try again in a few minutes).",
        )
        return
    except NetworkError as e:
        fail(
            "Could not reach iCloud IMAP",
            f"{e}\n\nIMAP is at imap.mail.me.com:993 (TLS).",
        )
        return
    except Exception as e:
        fail("Unexpected IMAP error", f"{type(e).__name__}: {e}")
        return

    ok(f"Connected. Found {len(mailboxes)} folder(s).")
    standard = {"INBOX", "Sent Messages", "Deleted Messages", "Drafts", "Junk", "Archive"}
    found = {m.name for m in mailboxes}
    missing = standard - found
    if missing:
        print(f"      {YELLOW}!{RESET} Standard folders missing: {sorted(missing)}")
        print(f"      {YELLOW} {RESET} (Probably fine — your iCloud may use different names.)")

    # ---- 5. IMAP read ----
    step(5, total, "Read recent messages from INBOX")
    try:
        msgs = mail.list_messages(mailbox="INBOX", limit=5)
    except Exception as e:
        fail("Message fetch failed", f"{type(e).__name__}: {e}")
        return

    ok(f"Read {len(msgs)} most-recent message(s).")
    for m in msgs[:3]:
        unread = "●" if not m.is_read else " "
        sender = m.from_addr.name or m.from_addr.email
        date = m.date.astimezone().strftime("%b %d %H:%M")
        print(f"      {unread} [{date}] {sender[:25]:25s}  {m.subject[:50]}")

    # Cleanly close the IMAP connection so SMTP isn't competing for attention
    mail.close()

    # ---- 6. SMTP handshake ----
    step(6, total, "SMTP authentication (sends nothing)")
    try:
        with smtplib.SMTP("smtp.mail.me.com", 587, timeout=15) as s:
            s.starttls(context=ssl.create_default_context())
            s.login(cfg.icloud_username, cfg.icloud_app_password)
            # NOOP is a cheap "are we still here" — no message is sent.
            code, _ = s.noop()
            if code != 250:
                raise RuntimeError(f"SMTP NOOP returned unexpected code: {code}")
    except smtplib.SMTPAuthenticationError as e:
        fail(
            "SMTP authentication failed",
            f"{e}\n\n"
            "Calendar and IMAP worked but SMTP didn't. Verify:\n"
            "  • The same app-specific password is being used (it should work for SMTP).\n"
            "  • There isn't an outbound block on TCP 587 from your network.",
        )
        return
    except (smtplib.SMTPException, OSError) as e:
        fail(
            "SMTP connection failed",
            f"{type(e).__name__}: {e}\n\n"
            "SMTP is at smtp.mail.me.com:587 (STARTTLS). "
            "Some networks/VPNs block outbound 587 — try from a different network.",
        )
        return

    ok("SMTP login successful. send_mail will work.")

    # ---- All passed ----
    print(f"\n{GREEN}{BOLD}All checks passed.{RESET}")
    print()
    print("Next: install the .mcpb in Claude Desktop, or wire it up manually:")
    print()
    print("  ~/Library/Application Support/Claude/claude_desktop_config.json")
    print()
    print("  See README.md for the JSON snippet.")


if __name__ == "__main__":
    main()
