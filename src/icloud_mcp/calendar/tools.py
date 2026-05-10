"""MCP tool registrations for iCloud Calendar."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from mcp.types import ToolAnnotations

from icloud_mcp.calendar.client import ICloudCalendarClient
from icloud_mcp.config import Config
from icloud_mcp.errors import ReadOnlyError

# Annotation presets — keep tool registrations terse.
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


def register_calendar_tools(
    mcp: Any,
    config: Config,
    client: Optional[ICloudCalendarClient] = None,
) -> None:
    """Attach calendar tools to a FastMCP server instance.

    If `client` is None, constructs one from config. Pass an existing client
    when sharing it across tool subsystems (e.g. for workflow tools that
    combine calendar + mail).
    """
    if client is None:
        client = ICloudCalendarClient(
            username=config.icloud_username,
            password=config.icloud_app_password,
            user_emails=config.all_user_emails,
        )

    def _require_writable() -> None:
        if config.read_only:
            raise ReadOnlyError(
                "Server is in read-only mode (ICLOUD_MCP_READ_ONLY=1). "
                "Restart with ICLOUD_MCP_READ_ONLY=0 to enable writes."
            )

    def _parse_dt(value: str, name: str) -> datetime:
        try:
            dt = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(
                f"{name} must be ISO-8601 with timezone offset, "
                f"e.g. '2026-05-06T09:00:00-07:00'. Got: {exc}"
            ) from exc
        if dt.tzinfo is None:
            raise ValueError(
                f"{name} must include a timezone offset (e.g. -07:00 or +00:00)"
            )
        return dt

    # ------------------------------------------------------------------ reads

    @mcp.tool(annotations=READ)
    def list_calendars() -> list[dict]:
        """List all iCloud calendars on the connected account.

        Use this to discover calendar names before calling tools that take a
        `calendar_name` filter. Returns name and CalDAV URL for each calendar.
        """
        return [c.model_dump() for c in client.list_calendars()]

    @mcp.tool(annotations=READ)
    def list_events(
        start: str,
        end: str,
        calendar_name: Optional[str] = None,
    ) -> list[dict]:
        """List calendar events within a time window.

        Recurring events are expanded: each occurrence in the window is
        returned as its own item. The `recurrence_id` field identifies the
        specific instance and is required as `occurrence_start` for
        update/delete of a single occurrence.

        Each event also includes:
          - status: CONFIRMED / TENTATIVE / CANCELLED
          - transparency: OPAQUE (blocks time) / TRANSPARENT (doesn't)
          - user_response: ACCEPTED / DECLINED / TENTATIVE / NEEDS-ACTION (your PARTSTAT)
          - timezone: original TZID for display localization
        Times in the response are UTC.

        Args:
            start: ISO-8601 datetime with timezone offset, e.g.
                "2026-05-06T00:00:00-07:00". Inclusive lower bound.
            end: ISO-8601 datetime with timezone offset. Exclusive upper bound.
            calendar_name: Optional case-sensitive calendar name filter.
        """
        start_dt = _parse_dt(start, "start")
        end_dt = _parse_dt(end, "end")
        events = client.list_events(start_dt, end_dt, calendar_name=calendar_name)
        return [e.model_dump(mode="json") for e in events]

    @mcp.tool(annotations=READ)
    def get_event(
        event_uid: str,
        calendar_name: Optional[str] = None,
    ) -> Optional[dict]:
        """Fetch a single event by UID, returning the master VEVENT.

        For a recurring event this returns the master with its RRULE — to get
        a specific instance with all overrides applied, use `list_events`
        with a tight window covering that instance.

        Args:
            event_uid: UID returned from list_events / search_events.
            calendar_name: Optional. If known, speeds up the lookup; otherwise
                all calendars are searched.

        Returns:
            Event dict, or null if no event with that UID exists.
        """
        event = client.get_event(event_uid, calendar_name=calendar_name)
        return event.model_dump(mode="json") if event else None

    @mcp.tool(annotations=READ)
    def search_events(
        query: str,
        start: Optional[str] = None,
        end: Optional[str] = None,
        calendar_name: Optional[str] = None,
    ) -> list[dict]:
        """Substring search across event title, description, and location.

        Filtering is client-side after fetching events in the window. Default
        window is roughly ±180 days around now — pass a tighter `start`/`end`
        on busy calendars.

        Args:
            query: Case-insensitive substring to match.
            start: Optional ISO-8601 datetime with offset. Default: 180 days ago.
            end: Optional ISO-8601 datetime with offset. Default: 180 days ahead.
            calendar_name: Optional case-sensitive calendar name filter.
        """
        start_dt = _parse_dt(start, "start") if start else None
        end_dt = _parse_dt(end, "end") if end else None
        events = client.search_events(
            query, start=start_dt, end=end_dt, calendar_name=calendar_name
        )
        return [e.model_dump(mode="json") for e in events]

    @mcp.tool(annotations=READ)
    def find_free_slots(
        start: str,
        end: str,
        duration_minutes: int,
        calendar_names: Optional[list[str]] = None,
    ) -> list[dict]:
        """Find free time slots of at least `duration_minutes` in a window.

        Treats events as blocking *only if* they're CONFIRMED, OPAQUE
        (transparency), and the user hasn't DECLINED them. Cancelled events,
        events marked TRANSPARENT (e.g. flights, "available" events), and
        invites the user has declined are correctly skipped.

        All-day events still block their full UTC day, which can clip
        otherwise-free time across timezone boundaries — narrow
        `calendar_names` to skip noisy "all-day" calendars (Birthdays,
        Holidays) when looking for meeting slots.

        Window is capped at 90 days; call repeatedly for longer ranges.

        Args:
            start: ISO-8601 datetime with offset for window start.
            end: ISO-8601 datetime with offset for window end.
            duration_minutes: Minimum slot length to return. Must be > 0.
            calendar_names: Optional list of calendars to consider. Default: all.
        """
        start_dt = _parse_dt(start, "start")
        end_dt = _parse_dt(end, "end")
        slots = client.find_free_slots(
            start_dt, end_dt, duration_minutes, calendar_names=calendar_names
        )
        return [s.model_dump(mode="json") for s in slots]

    # ----------------------------------------------------------------- writes

    @mcp.tool(annotations=WRITE_NEW)
    def create_event(
        calendar_name: str,
        title: str,
        start: str,
        end: str,
        description: Optional[str] = None,
        location: Optional[str] = None,
        attendees: Optional[list[str]] = None,
        alarm_minutes_before: Optional[int] = None,
        all_day: bool = False,
        dry_run: bool = False,
    ) -> dict:
        """Create a new (non-recurring) calendar event.

        Recommended workflow: call once with `dry_run=true` to render the iCal,
        confirm the details with the user, then re-run with `dry_run=false`.

        Args:
            calendar_name: Target calendar (case-sensitive). Get from list_calendars.
            title: Event summary.
            start: ISO-8601 datetime with offset.
            end: ISO-8601 datetime with offset; must be after start.
            description: Optional notes.
            location: Optional location string.
            attendees: Optional list of email addresses to invite.
            alarm_minutes_before: Optional reminder N minutes before start.
            all_day: If true, the date portions of start/end are used.
            dry_run: If true, returns the rendered iCal without saving.

        Returns:
            Dict with `uid`, `ical`, `created` (bool).
        """
        _require_writable()
        start_dt = _parse_dt(start, "start")
        end_dt = _parse_dt(end, "end")
        return client.create_event(
            calendar_name=calendar_name,
            title=title,
            start=start_dt,
            end=end_dt,
            description=description,
            location=location,
            attendees=attendees,
            alarm_minutes_before=alarm_minutes_before,
            all_day=all_day,
            dry_run=dry_run,
        )

    @mcp.tool(annotations=WRITE_IDEMPOTENT)
    def update_event(
        event_uid: str,
        calendar_name: Optional[str] = None,
        occurrence_start: Optional[str] = None,
        title: Optional[str] = None,
        start: Optional[str] = None,
        end: Optional[str] = None,
        description: Optional[str] = None,
        location: Optional[str] = None,
        attendees: Optional[list[str]] = None,
        dry_run: bool = False,
    ) -> dict:
        """Modify an existing event.

        Targeting rules:
          • Omit `occurrence_start` → updates the master event. For a recurring
            series this changes ALL non-overridden occurrences.
          • Provide `occurrence_start` (the original DTSTART of an instance,
            available as `recurrence_id` from list_events) → creates or
            updates an override for that single occurrence only.

        Pass only the fields you want to change. Setting `attendees` replaces
        the entire attendee list; pass `[]` to remove all attendees.

        If iCloud rejects the write because someone else (e.g. iPhone Calendar)
        modified the event between your fetch and write, you'll get a
        ConflictError — refetch with get_event and retry.

        Args:
            event_uid: UID from list_events / search_events.
            calendar_name: Optional hint to speed up lookup.
            occurrence_start: ISO-8601 datetime with offset — set to target a
                single instance of a recurring event.
            title, start, end, description, location, attendees: Optional new values.
            dry_run: If true, returns the rendered iCal without saving.

        Returns:
            Dict with `uid`, `ical` (post-update), `updated` (bool).
        """
        _require_writable()
        return client.update_event(
            event_uid=event_uid,
            calendar_name=calendar_name,
            occurrence_start=_parse_dt(occurrence_start, "occurrence_start")
            if occurrence_start
            else None,
            title=title,
            start=_parse_dt(start, "start") if start else None,
            end=_parse_dt(end, "end") if end else None,
            description=description,
            location=location,
            attendees=attendees,
            dry_run=dry_run,
        )

    @mcp.tool(annotations=WRITE_DESTRUCTIVE)
    def delete_event(
        event_uid: str,
        calendar_name: Optional[str] = None,
        occurrence_start: Optional[str] = None,
        dry_run: bool = False,
    ) -> dict:
        """Delete an event.

        Targeting rules:
          • Omit `occurrence_start` → deletes the entire event. ⚠️ For a
            recurring series this removes ALL instances permanently. Confirm
            with the user explicitly before doing this.
          • Provide `occurrence_start` → cancels just that occurrence via
            EXDATE; the rest of the series is untouched.

        Args:
            event_uid: UID from list_events / search_events.
            calendar_name: Optional hint to speed up lookup.
            occurrence_start: ISO-8601 datetime with offset of the instance
                to cancel. Required to limit the delete to one occurrence.
            dry_run: If true, returns what would have been deleted without
                actually deleting.

        Returns:
            Dict with `uid`, `mode` ("series" or "occurrence"), `deleted` (bool).
        """
        _require_writable()
        return client.delete_event(
            event_uid=event_uid,
            calendar_name=calendar_name,
            occurrence_start=_parse_dt(occurrence_start, "occurrence_start")
            if occurrence_start
            else None,
            dry_run=dry_run,
        )

    @mcp.tool(annotations=WRITE_IDEMPOTENT)
    def respond_to_invite(
        event_uid: str,
        response: str,
        calendar_name: Optional[str] = None,
        dry_run: bool = False,
    ) -> dict:
        """Accept, decline, or tentatively accept a meeting invite.

        Updates the user's PARTSTAT on the event. iCloud's CalDAV scheduling
        extension typically dispatches the iTIP REPLY email to the organizer
        automatically when the modified event is saved — but if the organizer
        doesn't receive a notification, that mechanism may be disabled and an
        SMTP-based fallback (Phase 2) would be needed.

        Errors if the user (the connected ICLOUD_USERNAME) isn't in the
        attendee list — you can't respond to an invite you weren't sent.

        Args:
            event_uid: UID from list_events / search_events.
            response: One of "accepted", "declined", "tentative", "needs-action".
            calendar_name: Optional hint to speed up lookup.
            dry_run: If true, returns the rendered iCal without saving.

        Returns:
            Dict with `uid`, `response`, `responded` (bool).
        """
        _require_writable()
        return client.respond_to_invite(
            event_uid=event_uid,
            response=response,
            calendar_name=calendar_name,
            dry_run=dry_run,
        )
