"""Workflow tools combining calendar + mail."""
from __future__ import annotations

import logging
from datetime import date as _date, datetime, time, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from mcp.types import ToolAnnotations

from icloud_mcp.calendar.client import ICloudCalendarClient
from icloud_mcp.config import Config
from icloud_mcp.mail.client import ICloudMailClient

READ = ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=True)

logger = logging.getLogger(__name__)


def register_workflow_tools(
    mcp: Any,
    config: Config,
    calendar_client: ICloudCalendarClient,
    mail_client: ICloudMailClient,
) -> None:
    """Attach workflow tools to a FastMCP server instance."""

    @mcp.tool(annotations=READ)
    def today_brief(
        date: Optional[str] = None,
        timezone_name: Optional[str] = None,
        unread_lookback_hours: int = 24,
    ) -> dict:
        """Single-call summary combining today's calendar + recent unread mail.

        Composes `list_events` for the day's window and `list_messages` for
        recent unread INBOX. Also surfaces calendar invites still awaiting
        a response (PARTSTAT=NEEDS-ACTION) so they don't get buried.

        Cancelled events are excluded from `events` and `pending_invites`,
        but appear in counts as a hint that the day was fuller before
        cancellations.

        Time-of-day handling: "today" means [00:00, 24:00) in the resolved
        timezone — local midnight, not UTC midnight. Provide
        `timezone_name` (or set `ICLOUD_USER_TIMEZONE`) so the boundaries
        match the user's lived experience of "today."

        Args:
            date: Optional ISO date YYYY-MM-DD. Defaults to today in the
                resolved timezone.
            timezone_name: IANA timezone (e.g. 'America/Los_Angeles').
                Falls back to config.user_timezone, then UTC.
            unread_lookback_hours: How far back to look for unread mail.
                Default 24 hours. Capped at 168 (one week).

        Returns:
            Dict with: date, timezone, events (active only),
            cancelled_count, pending_invites, unread_mail.
        """
        if unread_lookback_hours < 1 or unread_lookback_hours > 168:
            raise ValueError(
                "unread_lookback_hours must be between 1 and 168 (one week)"
            )

        # Resolve timezone
        tz_name = timezone_name or config.user_timezone or "UTC"
        try:
            tz = ZoneInfo(tz_name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(
                f"Unknown timezone {tz_name!r}. Use an IANA name like "
                "'America/Los_Angeles' or 'UTC'."
            ) from exc

        # Resolve target date
        if date:
            try:
                d = _date.fromisoformat(date)
            except ValueError as exc:
                raise ValueError(
                    f"date must be YYYY-MM-DD. Got: {date!r} ({exc})"
                ) from exc
        else:
            d = datetime.now(tz).date()

        # Day boundaries in the user's local time
        day_start = datetime.combine(d, time.min, tzinfo=tz)
        day_end = day_start + timedelta(days=1)

        # Calendar
        try:
            all_events = calendar_client.list_events(day_start, day_end)
        except Exception as exc:
            logger.warning("today_brief: calendar fetch failed: %s", exc)
            all_events = []

        active_events = [e for e in all_events if e.status != "CANCELLED"]
        cancelled_count = len(all_events) - len(active_events)
        pending_invites = [
            e for e in active_events if e.user_response == "NEEDS-ACTION"
        ]

        # Mail
        unread_since = (
            datetime.now(timezone.utc) - timedelta(hours=unread_lookback_hours)
        ).date()
        try:
            unread = mail_client.list_messages(
                mailbox="INBOX",
                limit=50,
                since=unread_since,
                unread_only=True,
            )
        except Exception as exc:
            logger.warning("today_brief: mail fetch failed: %s", exc)
            unread = []

        return {
            "date": d.isoformat(),
            "timezone": tz_name,
            "window_start_utc": day_start.astimezone(timezone.utc).isoformat(),
            "window_end_utc": day_end.astimezone(timezone.utc).isoformat(),
            "events_count": len(active_events),
            "cancelled_count": cancelled_count,
            "events": [e.model_dump(mode="json") for e in active_events],
            "pending_invites_count": len(pending_invites),
            "pending_invites": [e.model_dump(mode="json") for e in pending_invites],
            "unread_count": len(unread),
            "unread_mail": [m.model_dump(mode="json") for m in unread],
        }

    @mcp.tool(annotations=READ)
    def prep_for_meeting(
        event_uid: str,
        calendar_name: Optional[str] = None,
        email_lookback_days: int = 14,
        per_attendee_limit: int = 10,
    ) -> dict:
        """Bundle context for an upcoming or recent meeting.

        Returns the event details plus, for each attendee other than the
        user, the recent bidirectional email correspondence — incoming
        from them in INBOX *and* outgoing to them in Sent Messages — sorted
        newest-first and deduplicated across folders by Message-ID.

        Designed to answer "walk me through this meeting" in one call.
        The MCP client can summarize from the structured response without further
        round-trips.

        Args:
            event_uid: UID from list_events / search_events.
            calendar_name: Optional hint to speed up event lookup.
            email_lookback_days: How far back to search email per attendee.
                1-90, default 14.
            per_attendee_limit: Max emails returned per attendee (after
                bidirectional merge + dedupe). 1-50, default 10.

        Returns:
            Dict with `event`, `attendees_other_than_me`, plus
            `recent_correspondence` keyed by attendee email. Each value is
            a list of MessageHeader dicts. If a per-attendee email search
            fails, that attendee is still listed but with an empty list and
            a logged warning (the rest of the response still returns).
        """
        if email_lookback_days < 1 or email_lookback_days > 90:
            raise ValueError("email_lookback_days must be between 1 and 90")
        if per_attendee_limit < 1 or per_attendee_limit > 50:
            raise ValueError("per_attendee_limit must be between 1 and 50")

        event = calendar_client.get_event(event_uid, calendar_name=calendar_name)
        if event is None:
            raise ValueError(
                f"No event with UID {event_uid!r}. Refresh from list_events."
            )

        # Filter out the user's own emails (primary + aliases) from attendees
        user_emails_lower = {e.lower() for e in config.all_user_emails}
        other_attendees = [
            a for a in event.attendees
            if a.email and a.email.lower() not in user_emails_lower
        ]

        since_d = (
            datetime.now(timezone.utc) - timedelta(days=email_lookback_days)
        ).date()

        # Resolve the Sent folder once (locale-aware via SPECIAL-USE flags)
        try:
            sent_folder = mail_client.sent_folder_name()
        except Exception as exc:
            logger.warning("prep_for_meeting: could not resolve Sent folder: %s", exc)
            sent_folder = None

        recent_correspondence: dict[str, list] = {}
        for attendee in other_attendees:
            email = attendee.email.lower()
            collected = []

            # Incoming from this attendee
            try:
                collected.extend(
                    mail_client.search_mail(
                        mailbox="INBOX",
                        from_addr=email,
                        since=since_d,
                        limit=per_attendee_limit * 2,
                    )
                )
            except Exception as exc:
                logger.warning(
                    "prep_for_meeting: INBOX search for %s failed: %s",
                    email,
                    exc,
                )

            # Outgoing to this attendee (only if we resolved the Sent folder)
            if sent_folder:
                try:
                    collected.extend(
                        mail_client.search_mail(
                            mailbox=sent_folder,
                            to_addr=email,
                            since=since_d,
                            limit=per_attendee_limit * 2,
                        )
                    )
                except Exception as exc:
                    logger.warning(
                        "prep_for_meeting: Sent search for %s failed: %s",
                        email,
                        exc,
                    )

            # Dedupe by message_id (preserves first occurrence — newest first
            # after the sort below) and cap at per_attendee_limit.
            collected.sort(key=lambda m: m.date, reverse=True)
            seen_ids: set[str] = set()
            unique = []
            for m in collected:
                if m.message_id and m.message_id in seen_ids:
                    continue
                if m.message_id:
                    seen_ids.add(m.message_id)
                unique.append(m)
                if len(unique) >= per_attendee_limit:
                    break

            recent_correspondence[email] = unique

        return {
            "event": event.model_dump(mode="json"),
            "attendees_other_than_me": [
                a.model_dump(mode="json") for a in other_attendees
            ],
            "email_lookback_days": email_lookback_days,
            "recent_correspondence": {
                email: [m.model_dump(mode="json") for m in msgs]
                for email, msgs in recent_correspondence.items()
            },
        }
