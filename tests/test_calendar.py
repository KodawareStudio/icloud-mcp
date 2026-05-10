"""Unit tests for parsing, iCal manipulation, and free-slot logic.

These tests exercise pure functions only — no network. Run with:
    uv run pytest
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from icalendar import Calendar as ICalendar

from icloud_mcp.calendar.client import (
    _apply_delete_occurrence_to_ical,
    _apply_invite_response,
    _apply_update_to_ical,
    _build_create_ical,
    _free_slots_from_busy,
    _is_blocking_event,
    _safe_parse_event,
    _validate_email,
    _wrap_write_error,
)
from icloud_mcp.calendar.models import Event, FreeSlot
from icloud_mcp.errors import (
    AuthenticationError,
    ConflictError,
    EventNotFoundError,
    NetworkError,
)


class FakeDavEvent:
    def __init__(self, data: bytes | str) -> None:
        self.data = data if isinstance(data, bytes) else data.encode()


def _build_ics(*vevent_blocks: str) -> bytes:
    body = "\r\n".join(vevent_blocks)
    return (
        f"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n{body}\r\nEND:VCALENDAR\r\n"
    ).encode()


# --------------------------------------------------------------- parser tests


def test_basic_event_parses() -> None:
    ics = _build_ics(
        "BEGIN:VEVENT\r\n"
        "UID:abc-123\r\n"
        "SUMMARY:Team standup\r\n"
        "DTSTART:20260506T160000Z\r\n"
        "DTEND:20260506T163000Z\r\n"
        "LOCATION:Zoom\r\n"
        "END:VEVENT"
    )
    events = _safe_parse_event(FakeDavEvent(ics), "Work")
    assert len(events) == 1
    e = events[0]
    assert e.uid == "abc-123"
    assert e.title == "Team standup"
    assert e.location == "Zoom"
    assert e.start == datetime(2026, 5, 6, 16, 0, tzinfo=timezone.utc)
    assert e.end == datetime(2026, 5, 6, 16, 30, tzinfo=timezone.utc)
    assert e.all_day is False


def test_all_day_event() -> None:
    ics = _build_ics(
        "BEGIN:VEVENT\r\n"
        "UID:holiday-1\r\n"
        "SUMMARY:Memorial Day\r\n"
        "DTSTART;VALUE=DATE:20260525\r\n"
        "DTEND;VALUE=DATE:20260526\r\n"
        "END:VEVENT"
    )
    events = _safe_parse_event(FakeDavEvent(ics), "Holidays")
    assert len(events) == 1
    assert events[0].all_day is True


def test_attendees_and_organizer() -> None:
    ics = _build_ics(
        "BEGIN:VEVENT\r\n"
        "UID:mtg-1\r\n"
        "SUMMARY:1:1\r\n"
        "DTSTART:20260506T180000Z\r\n"
        "DTEND:20260506T183000Z\r\n"
        "ORGANIZER;CN=Boss:mailto:boss@example.com\r\n"
        "ATTENDEE;CN=Vibhor;ROLE=REQ-PARTICIPANT;PARTSTAT=ACCEPTED:mailto:v@example.com\r\n"
        "END:VEVENT"
    )
    e = _safe_parse_event(FakeDavEvent(ics), "Work")[0]
    assert e.organizer == "boss@example.com"
    assert len(e.attendees) == 1
    assert e.attendees[0].email == "v@example.com"
    assert e.attendees[0].status == "ACCEPTED"


def test_malformed_event_skipped_not_raised() -> None:
    ics = _build_ics("BEGIN:VEVENT\r\nUID:bad-1\r\nSUMMARY:Broken\r\nEND:VEVENT")
    assert _safe_parse_event(FakeDavEvent(ics), "Work") == []


def test_recurrence_id_parsed() -> None:
    ics = _build_ics(
        "BEGIN:VEVENT\r\n"
        "UID:weekly-1\r\n"
        "SUMMARY:Override\r\n"
        "DTSTART:20260108T170000Z\r\n"
        "DTEND:20260108T173000Z\r\n"
        "RECURRENCE-ID:20260108T160000Z\r\n"
        "END:VEVENT"
    )
    e = _safe_parse_event(FakeDavEvent(ics), "Work")[0]
    assert e.recurrence_id == datetime(2026, 1, 8, 16, 0, tzinfo=timezone.utc)


# -------------------------------------------------------- create ical tests


def test_build_create_ical_basic() -> None:
    text = _build_create_ical(
        uid="new-1",
        title="Coffee",
        start=datetime(2026, 5, 7, 14, 0, tzinfo=timezone.utc),
        end=datetime(2026, 5, 7, 15, 0, tzinfo=timezone.utc),
        location="Cafe",
    )
    cal = ICalendar.from_ical(text)
    [vevent] = list(cal.walk("VEVENT"))
    assert str(vevent["UID"]) == "new-1"
    assert str(vevent["SUMMARY"]) == "Coffee"
    assert str(vevent["LOCATION"]) == "Cafe"


def test_build_create_ical_with_attendees_and_alarm() -> None:
    text = _build_create_ical(
        uid="new-2",
        title="Meeting",
        start=datetime(2026, 5, 7, 14, 0, tzinfo=timezone.utc),
        end=datetime(2026, 5, 7, 15, 0, tzinfo=timezone.utc),
        attendees=["a@example.com", "b@example.com"],
        alarm_minutes_before=15,
    )
    cal = ICalendar.from_ical(text)
    [vevent] = list(cal.walk("VEVENT"))
    attendees = vevent.get("ATTENDEE")
    if not isinstance(attendees, list):
        attendees = [attendees]
    emails = sorted(str(a).replace("mailto:", "").lower() for a in attendees)
    assert emails == ["a@example.com", "b@example.com"]
    [alarm] = list(vevent.walk("VALARM"))
    assert alarm["TRIGGER"].dt == timedelta(minutes=-15)


def test_build_create_ical_rejects_naive_datetimes() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        _build_create_ical(
            uid="x",
            title="Bad",
            start=datetime(2026, 5, 7, 14, 0),
            end=datetime(2026, 5, 7, 15, 0, tzinfo=timezone.utc),
        )


def test_build_create_ical_rejects_inverted_times() -> None:
    with pytest.raises(ValueError, match="end must be after start"):
        _build_create_ical(
            uid="x",
            title="Bad",
            start=datetime(2026, 5, 7, 15, 0, tzinfo=timezone.utc),
            end=datetime(2026, 5, 7, 14, 0, tzinfo=timezone.utc),
        )


# -------------------------------------------------------- update ical tests


_RECURRING_ICS = (
    "BEGIN:VCALENDAR\r\n"
    "VERSION:2.0\r\n"
    "PRODID:-//test//EN\r\n"
    "BEGIN:VEVENT\r\n"
    "UID:weekly-standup\r\n"
    "SUMMARY:Standup\r\n"
    "DTSTART:20260105T160000Z\r\n"
    "DTEND:20260105T163000Z\r\n"
    "RRULE:FREQ=WEEKLY\r\n"
    "DTSTAMP:20260101T000000Z\r\n"
    "END:VEVENT\r\n"
    "END:VCALENDAR\r\n"
)


def test_update_master_changes_all_instances() -> None:
    new_text = _apply_update_to_ical(
        _RECURRING_ICS,
        occurrence_start=None,
        fields={"title": "Daily standup"},
    )
    cal = ICalendar.from_ical(new_text)
    vevents = list(cal.walk("VEVENT"))
    assert len(vevents) == 1  # still just the master
    assert str(vevents[0]["SUMMARY"]) == "Daily standup"
    assert vevents[0]["RRULE"].to_ical().decode() == "FREQ=WEEKLY"  # RRULE preserved


def test_update_with_occurrence_creates_override() -> None:
    occurrence = datetime(2026, 1, 12, 16, 0, tzinfo=timezone.utc)
    new_text = _apply_update_to_ical(
        _RECURRING_ICS,
        occurrence_start=occurrence,
        fields={
            "title": "Standup (rescheduled)",
            "start": datetime(2026, 1, 12, 17, 0, tzinfo=timezone.utc),
            "end": datetime(2026, 1, 12, 17, 30, tzinfo=timezone.utc),
        },
    )
    cal = ICalendar.from_ical(new_text)
    vevents = list(cal.walk("VEVENT"))
    assert len(vevents) == 2

    master = next(v for v in vevents if v.get("RECURRENCE-ID") is None)
    override = next(v for v in vevents if v.get("RECURRENCE-ID") is not None)

    # Master untouched
    assert str(master["SUMMARY"]) == "Standup"
    assert master["RRULE"].to_ical().decode() == "FREQ=WEEKLY"
    # Override has new fields
    assert str(override["SUMMARY"]) == "Standup (rescheduled)"
    assert override["DTSTART"].dt == datetime(2026, 1, 12, 17, 0, tzinfo=timezone.utc)
    assert override["RECURRENCE-ID"].dt.replace(tzinfo=timezone.utc) == occurrence
    # Override must NOT carry the RRULE
    assert "RRULE" not in override


def test_update_existing_override_is_modified_in_place() -> None:
    occurrence = datetime(2026, 1, 12, 16, 0, tzinfo=timezone.utc)
    once = _apply_update_to_ical(
        _RECURRING_ICS,
        occurrence_start=occurrence,
        fields={"title": "First override"},
    )
    twice = _apply_update_to_ical(
        once,
        occurrence_start=occurrence,
        fields={"title": "Second override"},
    )
    cal = ICalendar.from_ical(twice)
    vevents = list(cal.walk("VEVENT"))
    assert len(vevents) == 2  # not 3 — the override was mutated, not re-added
    override = next(v for v in vevents if v.get("RECURRENCE-ID") is not None)
    assert str(override["SUMMARY"]) == "Second override"


# -------------------------------------------------------- delete ical tests


def test_delete_occurrence_adds_exdate() -> None:
    occurrence = datetime(2026, 1, 12, 16, 0, tzinfo=timezone.utc)
    new_text = _apply_delete_occurrence_to_ical(_RECURRING_ICS, occurrence)
    cal = ICalendar.from_ical(new_text)
    [master] = list(cal.walk("VEVENT"))
    exdate = master.get("EXDATE")
    assert exdate is not None
    # EXDATE may be a vDDDLists or single — pull its dt(s)
    dts = []
    items = exdate if isinstance(exdate, list) else [exdate]
    for item in items:
        if hasattr(item, "dts"):
            dts.extend(d.dt for d in item.dts)
        else:
            dts.append(item.dt)
    assert occurrence in [d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d.astimezone(timezone.utc) for d in dts]


def test_delete_occurrence_strips_existing_override() -> None:
    occurrence = datetime(2026, 1, 12, 16, 0, tzinfo=timezone.utc)
    with_override = _apply_update_to_ical(
        _RECURRING_ICS,
        occurrence_start=occurrence,
        fields={"title": "Override"},
    )
    after_delete = _apply_delete_occurrence_to_ical(with_override, occurrence)
    cal = ICalendar.from_ical(after_delete)
    vevents = list(cal.walk("VEVENT"))
    assert len(vevents) == 1  # override removed
    assert vevents[0].get("RECURRENCE-ID") is None


# ----------------------------------------------------------- free slot tests


def _utc(y: int, m: int, d: int, h: int = 0, mi: int = 0) -> datetime:
    return datetime(y, m, d, h, mi, tzinfo=timezone.utc)


def test_free_slots_no_busy() -> None:
    slots = _free_slots_from_busy(_utc(2026, 5, 6, 9), _utc(2026, 5, 6, 17), [], 30)
    assert slots == [FreeSlot(start=_utc(2026, 5, 6, 9), end=_utc(2026, 5, 6, 17))]


def test_free_slots_with_gaps() -> None:
    busy = [
        (_utc(2026, 5, 6, 10), _utc(2026, 5, 6, 11)),
        (_utc(2026, 5, 6, 13), _utc(2026, 5, 6, 14)),
    ]
    slots = _free_slots_from_busy(_utc(2026, 5, 6, 9), _utc(2026, 5, 6, 17), busy, 30)
    assert slots == [
        FreeSlot(start=_utc(2026, 5, 6, 9), end=_utc(2026, 5, 6, 10)),
        FreeSlot(start=_utc(2026, 5, 6, 11), end=_utc(2026, 5, 6, 13)),
        FreeSlot(start=_utc(2026, 5, 6, 14), end=_utc(2026, 5, 6, 17)),
    ]


def test_free_slots_filters_too_short() -> None:
    busy = [
        (_utc(2026, 5, 6, 9, 30), _utc(2026, 5, 6, 12)),  # leaves 0-9:30 then 12+
    ]
    slots = _free_slots_from_busy(
        _utc(2026, 5, 6, 9), _utc(2026, 5, 6, 17), busy, 60
    )
    # 9:00–9:30 is 30min — too short. 12:00–17:00 is 5h — kept.
    assert slots == [FreeSlot(start=_utc(2026, 5, 6, 12), end=_utc(2026, 5, 6, 17))]


def test_free_slots_merges_overlapping_busy() -> None:
    busy = [
        (_utc(2026, 5, 6, 10), _utc(2026, 5, 6, 12)),
        (_utc(2026, 5, 6, 11), _utc(2026, 5, 6, 13)),  # overlaps the first
    ]
    slots = _free_slots_from_busy(_utc(2026, 5, 6, 9), _utc(2026, 5, 6, 17), busy, 30)
    assert slots == [
        FreeSlot(start=_utc(2026, 5, 6, 9), end=_utc(2026, 5, 6, 10)),
        FreeSlot(start=_utc(2026, 5, 6, 13), end=_utc(2026, 5, 6, 17)),
    ]


# =================================================== hardening: status/transp


def test_status_and_transparency_parsed() -> None:
    ics = _build_ics(
        "BEGIN:VEVENT\r\n"
        "UID:cancelled-1\r\n"
        "SUMMARY:Cancelled meeting\r\n"
        "DTSTART:20260506T160000Z\r\n"
        "DTEND:20260506T163000Z\r\n"
        "STATUS:CANCELLED\r\n"
        "TRANSP:TRANSPARENT\r\n"
        "END:VEVENT"
    )
    e = _safe_parse_event(FakeDavEvent(ics), "Work")[0]
    assert e.status == "CANCELLED"
    assert e.transparency == "TRANSPARENT"


def test_user_response_extracted_from_attendee_list() -> None:
    ics = _build_ics(
        "BEGIN:VEVENT\r\n"
        "UID:invite-1\r\n"
        "SUMMARY:Team meeting\r\n"
        "DTSTART:20260506T160000Z\r\n"
        "DTEND:20260506T163000Z\r\n"
        "ATTENDEE;PARTSTAT=ACCEPTED:mailto:me@icloud.com\r\n"
        "ATTENDEE;PARTSTAT=NEEDS-ACTION:mailto:other@example.com\r\n"
        "END:VEVENT"
    )
    e = _safe_parse_event(FakeDavEvent(ics), "Work", user_emails=["me@icloud.com"])[0]
    assert e.user_response == "ACCEPTED"


def test_user_response_none_when_user_not_invited() -> None:
    ics = _build_ics(
        "BEGIN:VEVENT\r\n"
        "UID:notme-1\r\n"
        "SUMMARY:Someone else's meeting\r\n"
        "DTSTART:20260506T160000Z\r\n"
        "DTEND:20260506T163000Z\r\n"
        "ATTENDEE;PARTSTAT=ACCEPTED:mailto:them@example.com\r\n"
        "END:VEVENT"
    )
    e = _safe_parse_event(FakeDavEvent(ics), "Work", user_emails=["me@icloud.com"])[0]
    assert e.user_response is None


# ============================================== hardening: blocking semantics


def _make_event(**overrides: Any) -> Event:
    base = dict(
        uid="x",
        calendar="Work",
        title="Meeting",
        start=_utc(2026, 5, 6, 10),
        end=_utc(2026, 5, 6, 11),
    )
    base.update(overrides)
    return Event(**base)


def test_is_blocking_default_event_blocks() -> None:
    assert _is_blocking_event(_make_event()) is True


def test_is_blocking_cancelled_does_not_block() -> None:
    assert _is_blocking_event(_make_event(status="CANCELLED")) is False


def test_is_blocking_transparent_does_not_block() -> None:
    assert _is_blocking_event(_make_event(transparency="TRANSPARENT")) is False


def test_is_blocking_declined_invite_does_not_block() -> None:
    assert _is_blocking_event(_make_event(user_response="DECLINED")) is False


def test_is_blocking_accepted_invite_blocks() -> None:
    assert _is_blocking_event(_make_event(user_response="ACCEPTED")) is True


# ===================================================== hardening: validation


def test_validate_email_accepts_normal() -> None:
    assert _validate_email("foo@bar.com") == "foo@bar.com"
    assert _validate_email("  spaced@bar.com  ") == "spaced@bar.com"


def test_validate_email_rejects_garbage() -> None:
    with pytest.raises(ValueError, match="Invalid email"):
        _validate_email("not-an-email")
    with pytest.raises(ValueError, match="Invalid email"):
        _validate_email("foo@bar")  # missing TLD
    with pytest.raises(ValueError, match="Invalid email"):
        _validate_email("foo bar@baz.com")  # spaces inside


# =========================================== hardening: error wrapping


def test_wrap_412_to_conflict_error() -> None:
    err = _wrap_write_error(Exception("HTTP 412 Precondition Failed"), "update", "x")
    assert isinstance(err, ConflictError)
    assert "modified by another client" in str(err)


def test_wrap_401_to_auth_error() -> None:
    err = _wrap_write_error(Exception("401 Unauthorized"), "update", "x")
    assert isinstance(err, AuthenticationError)


def test_wrap_404_to_not_found() -> None:
    err = _wrap_write_error(Exception("404 Not Found"), "delete", "x")
    assert isinstance(err, EventNotFoundError)


def test_wrap_timeout_to_network_error() -> None:
    err = _wrap_write_error(Exception("Connection timeout"), "create", "x")
    assert isinstance(err, NetworkError)


# ============================================= hardening: respond_to_invite


_INVITE_ICS = (
    "BEGIN:VCALENDAR\r\n"
    "VERSION:2.0\r\n"
    "PRODID:-//test//EN\r\n"
    "BEGIN:VEVENT\r\n"
    "UID:invite-1\r\n"
    "SUMMARY:All-hands\r\n"
    "DTSTART:20260506T160000Z\r\n"
    "DTEND:20260506T170000Z\r\n"
    "DTSTAMP:20260101T000000Z\r\n"
    "ORGANIZER:mailto:boss@example.com\r\n"
    "ATTENDEE;PARTSTAT=NEEDS-ACTION:mailto:me@icloud.com\r\n"
    "ATTENDEE;PARTSTAT=ACCEPTED:mailto:other@example.com\r\n"
    "END:VEVENT\r\n"
    "END:VCALENDAR\r\n"
)


def test_respond_to_invite_changes_user_partstat() -> None:
    new_text = _apply_invite_response(_INVITE_ICS, ["me@icloud.com"], "ACCEPTED")
    cal = ICalendar.from_ical(new_text)
    [vevent] = list(cal.walk("VEVENT"))
    raw = vevent.get("ATTENDEE")
    items = raw if isinstance(raw, list) else [raw]
    by_email = {
        str(a).replace("mailto:", "").lower(): a.params.get("PARTSTAT")
        for a in items
    }
    assert by_email["me@icloud.com"] == "ACCEPTED"
    # Other attendee untouched
    assert by_email["other@example.com"] == "ACCEPTED"


def test_respond_to_invite_works_for_decline_and_tentative() -> None:
    for resp in ("DECLINED", "TENTATIVE", "NEEDS-ACTION"):
        new_text = _apply_invite_response(_INVITE_ICS, ["me@icloud.com"], resp)
        cal = ICalendar.from_ical(new_text)
        [vevent] = list(cal.walk("VEVENT"))
        raw = vevent.get("ATTENDEE")
        items = raw if isinstance(raw, list) else [raw]
        for a in items:
            email = str(a).replace("mailto:", "").lower()
            if email == "me@icloud.com":
                assert a.params.get("PARTSTAT") == resp


def test_respond_to_invite_errors_when_user_not_invited() -> None:
    with pytest.raises(ValueError, match="are in the attendee list"):
        _apply_invite_response(_INVITE_ICS, ["stranger@elsewhere.com"], "ACCEPTED")


def test_respond_to_invite_errors_when_no_attendees() -> None:
    no_attendees = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n"
        "BEGIN:VEVENT\r\nUID:solo\r\nSUMMARY:Solo work\r\n"
        "DTSTART:20260506T160000Z\r\nDTEND:20260506T170000Z\r\n"
        "END:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    with pytest.raises(ValueError, match="no attendees"):
        _apply_invite_response(no_attendees, ["me@icloud.com"], "ACCEPTED")


# ============================================ hardening: alias matching


def test_user_response_matches_any_alias() -> None:
    """User addressed as @me.com but configured as @icloud.com — should match."""
    ics = _build_ics(
        "BEGIN:VEVENT\r\n"
        "UID:invite-2\r\n"
        "SUMMARY:Team meeting\r\n"
        "DTSTART:20260506T160000Z\r\n"
        "DTEND:20260506T163000Z\r\n"
        "ATTENDEE;PARTSTAT=ACCEPTED:mailto:me@me.com\r\n"
        "END:VEVENT"
    )
    e = _safe_parse_event(
        FakeDavEvent(ics),
        "Work",
        user_emails=["me@icloud.com", "me@me.com", "me@mac.com"],
    )[0]
    assert e.user_response == "ACCEPTED"


def test_respond_to_invite_uses_alias_match() -> None:
    """Invite was addressed to alias; response should still apply."""
    invite_to_alias = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n"
        "BEGIN:VEVENT\r\nUID:inv-3\r\nSUMMARY:Sync\r\n"
        "DTSTART:20260506T160000Z\r\nDTEND:20260506T170000Z\r\n"
        "DTSTAMP:20260101T000000Z\r\n"
        "ATTENDEE;PARTSTAT=NEEDS-ACTION:mailto:me@me.com\r\n"
        "END:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    new_text = _apply_invite_response(
        invite_to_alias,
        ["me@icloud.com", "me@me.com"],
        "ACCEPTED",
    )
    cal = ICalendar.from_ical(new_text)
    [vevent] = list(cal.walk("VEVENT"))
    raw = vevent.get("ATTENDEE")
    items = raw if isinstance(raw, list) else [raw]
    [att] = items
    assert att.params.get("PARTSTAT") == "ACCEPTED"


def test_respond_to_invite_error_mentions_alias_env_var() -> None:
    """Error should hint at ICLOUD_USER_ALIASES so users can fix the issue."""
    ics = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n"
        "BEGIN:VEVENT\r\nUID:x\r\nSUMMARY:y\r\n"
        "DTSTART:20260506T160000Z\r\nDTEND:20260506T170000Z\r\n"
        "ATTENDEE;PARTSTAT=NEEDS-ACTION:mailto:other@example.com\r\n"
        "END:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    with pytest.raises(ValueError, match="ICLOUD_USER_ALIASES"):
        _apply_invite_response(ics, ["me@icloud.com"], "ACCEPTED")


# ================================================= hardening: read_only mode


class _FakeMCP:
    """Minimal stub for FastMCP — captures registered tool functions."""

    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self, **kwargs: Any) -> Any:
        def decorator(func: Any) -> Any:
            self.tools[func.__name__] = func
            return func

        return decorator


def _build_tools(read_only: bool) -> _FakeMCP:
    """Construct a FakeMCP with our tools registered against a stubbed config."""
    from icloud_mcp.calendar.tools import register_calendar_tools
    from icloud_mcp.config import Config

    fake = _FakeMCP()
    config = Config(
        icloud_username="test@icloud.com",
        icloud_app_password="fake-app-pwd",
        read_only=read_only,
    )
    register_calendar_tools(fake, config)
    return fake


def test_read_only_blocks_create_event() -> None:
    fake = _build_tools(read_only=True)
    from icloud_mcp.errors import ReadOnlyError

    with pytest.raises(ReadOnlyError, match="read-only mode"):
        fake.tools["create_event"](
            calendar_name="Work",
            title="Test",
            start="2026-05-06T09:00:00-07:00",
            end="2026-05-06T10:00:00-07:00",
        )


def test_read_only_blocks_update_event() -> None:
    fake = _build_tools(read_only=True)
    from icloud_mcp.errors import ReadOnlyError

    with pytest.raises(ReadOnlyError, match="read-only mode"):
        fake.tools["update_event"](event_uid="x", title="New title")


def test_read_only_blocks_delete_event() -> None:
    fake = _build_tools(read_only=True)
    from icloud_mcp.errors import ReadOnlyError

    with pytest.raises(ReadOnlyError, match="read-only mode"):
        fake.tools["delete_event"](event_uid="x")


def test_read_only_blocks_respond_to_invite() -> None:
    fake = _build_tools(read_only=True)
    from icloud_mcp.errors import ReadOnlyError

    with pytest.raises(ReadOnlyError, match="read-only mode"):
        fake.tools["respond_to_invite"](event_uid="x", response="accepted")
