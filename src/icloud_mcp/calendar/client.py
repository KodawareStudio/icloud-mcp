"""iCloud CalDAV client.

Wraps the `caldav` library with iCloud-specific concerns:
- Principal discovery + 5min cache (avoid hammering iCloud on every tool call)
- Defensive iCalendar parsing (one bad event shouldn't kill a list)
- Timezone normalization to UTC at the boundary
- Network timeouts (caldav's default is forever-ish)
- Error wrapping (caldav exceptions → actionable domain errors)
- ETag conflict detection on writes (412 → ConflictError with refetch guidance)
- Transparency/status-aware free/busy (CANCELLED, TRANSPARENT, user-DECLINED don't block)
- iCalendar manipulation helpers factored out so they're testable without network

Recurring event targeting: pass `occurrence_start` to write methods to target
a single instance (the original DTSTART of that instance, available as
`recurrence_id` from list_events). Omit it to target the master/series.
"""
from __future__ import annotations

import copy
import logging
import time
import uuid
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Any, Iterable, Optional

import caldav
from caldav.lib.error import (
    AuthorizationError,
    NotFoundError as CalDAVNotFoundError,
)
from icalendar import Alarm, Calendar as ICalendar, Event as IEvent
from icalendar import vCalAddress, vText

from icloud_mcp.calendar.models import Attendee, Calendar, Event, FreeSlot
from icloud_mcp.errors import (
    AuthenticationError,
    CalendarNotFoundError,
    ConflictError,
    EventNotFoundError,
    NetworkError,
)
from icloud_mcp.util import validate_email as _validate_email

ICLOUD_CALDAV_URL = "https://caldav.icloud.com"
PRODID = "-//icloud-mcp//EN"

logger = logging.getLogger(__name__)


# ============================================================== client class


class _CalendarCache:
    """TTL cache for the principal's calendar list.

    iCloud's principal discovery is the slowest part of every cold call
    (multiple HTTP round-trips). Caching for a few minutes is a big win
    and is safe — calendar lists change rarely.
    """

    def __init__(self, ttl_seconds: float) -> None:
        self._ttl = ttl_seconds
        self._cached: Optional[list[caldav.Calendar]] = None
        self._fetched_at: Optional[float] = None

    def get(self, fetch: Any) -> list[caldav.Calendar]:
        now = time.monotonic()
        if (
            self._cached is None
            or self._fetched_at is None
            or (now - self._fetched_at) > self._ttl
        ):
            self._cached = list(fetch())
            self._fetched_at = now
        return self._cached

    def invalidate(self) -> None:
        self._cached = None
        self._fetched_at = None


class ICloudCalendarClient:
    """Connects lazily to iCloud CalDAV; safe to construct at startup."""

    def __init__(
        self,
        username: str,
        password: str,
        *,
        user_emails: Optional[Iterable[str]] = None,
        timeout_seconds: float = 30.0,
        cache_ttl_seconds: float = 300.0,
    ) -> None:
        self._username = username
        # Lower-cased emails the user is identified by — primary + aliases.
        # Used to detect the user's PARTSTAT and to target invite responses.
        if user_emails is None:
            self._user_emails: tuple[str, ...] = (username.lower(),)
        else:
            normalized = tuple(e.lower() for e in user_emails if e)
            if username.lower() not in normalized:
                normalized = (username.lower(), *normalized)
            self._user_emails = normalized
        self._client = caldav.DAVClient(
            url=ICLOUD_CALDAV_URL,
            username=username,
            password=password,
            timeout=timeout_seconds,
        )
        self._principal: Optional[caldav.Principal] = None
        self._cache = _CalendarCache(cache_ttl_seconds)

    @property
    def principal(self) -> caldav.Principal:
        if self._principal is None:
            try:
                self._principal = self._client.principal()
            except AuthorizationError as exc:
                raise AuthenticationError(
                    "iCloud authentication failed. Verify ICLOUD_USERNAME is your "
                    "full Apple ID email and ICLOUD_APP_PASSWORD is a valid "
                    "app-specific password generated at appleid.apple.com. "
                    "App-specific passwords can't be reused across Apple IDs."
                ) from exc
            except Exception as exc:
                raise NetworkError(
                    f"Failed to reach iCloud CalDAV at {ICLOUD_CALDAV_URL}: {exc}"
                ) from exc
        return self._principal

    def invalidate_calendar_cache(self) -> None:
        """Force a fresh fetch on next operation."""
        self._cache.invalidate()

    # ------------------------------------------------------------------ reads

    def list_calendars(self) -> list[Calendar]:
        return [
            Calendar(name=cal.name or "Unnamed", url=str(cal.url))
            for cal in self._calendars()
        ]

    def list_events(
        self,
        start: datetime,
        end: datetime,
        calendar_name: Optional[str] = None,
    ) -> list[Event]:
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError("start and end must be timezone-aware datetimes")
        if end <= start:
            raise ValueError("end must be after start")

        calendars = self._select_calendars(calendar_name)
        events: list[Event] = []
        for cal in calendars:
            try:
                results = cal.search(start=start, end=end, event=True, expand=True)
            except Exception as exc:
                logger.warning(
                    "Failed to search calendar %r: %s", cal.name, exc, exc_info=True
                )
                continue
            for dav_event in results:
                events.extend(
                    _safe_parse_event(dav_event, cal.name or "Unnamed", self._username)
                )

        events.sort(key=lambda e: e.start)
        return events

    def get_event(
        self,
        event_uid: str,
        calendar_name: Optional[str] = None,
    ) -> Optional[Event]:
        cal_obj = self._find_event_object(event_uid, calendar_name)
        if cal_obj is None:
            return None
        cal_name = self._calendar_name_of(cal_obj)
        for ev in _safe_parse_event(cal_obj, cal_name, self._user_emails):
            if ev.recurrence_id is None:
                return ev
        return None

    def search_events(
        self,
        query: str,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        calendar_name: Optional[str] = None,
    ) -> list[Event]:
        now = datetime.now(timezone.utc)
        if start is None:
            start = now - timedelta(days=180)
        if end is None:
            end = now + timedelta(days=180)

        events = self.list_events(start, end, calendar_name=calendar_name)
        q = query.lower().strip()
        if not q:
            return events

        def matches(e: Event) -> bool:
            return (
                q in e.title.lower()
                or (e.description is not None and q in e.description.lower())
                or (e.location is not None and q in e.location.lower())
            )

        return [e for e in events if matches(e)]

    def find_free_slots(
        self,
        start: datetime,
        end: datetime,
        duration_minutes: int,
        calendar_names: Optional[list[str]] = None,
    ) -> list[FreeSlot]:
        if duration_minutes <= 0:
            raise ValueError("duration_minutes must be positive")
        if (end - start) > timedelta(days=90):
            raise ValueError(
                "find_free_slots window can't exceed 90 days "
                "(use a tighter window or call repeatedly)"
            )

        if calendar_names:
            events: list[Event] = []
            for name in calendar_names:
                events.extend(self.list_events(start, end, name))
        else:
            events = self.list_events(start, end)

        # Filter to events that actually block time
        blocking = [e for e in events if _is_blocking_event(e)]
        busy = [(e.start, e.end) for e in blocking]
        return _free_slots_from_busy(start, end, busy, duration_minutes)

    # ----------------------------------------------------------------- writes

    def create_event(
        self,
        calendar_name: str,
        title: str,
        start: datetime,
        end: datetime,
        description: Optional[str] = None,
        location: Optional[str] = None,
        attendees: Optional[list[str]] = None,
        alarm_minutes_before: Optional[int] = None,
        all_day: bool = False,
        dry_run: bool = False,
    ) -> dict:
        if attendees is not None:
            attendees = [_validate_email(e) for e in attendees]

        cal = self._require_calendar(calendar_name)
        new_uid = f"{uuid.uuid4()}@icloud-mcp"
        ical_text = _build_create_ical(
            uid=new_uid,
            title=title,
            start=start,
            end=end,
            description=description,
            location=location,
            attendees=attendees,
            alarm_minutes_before=alarm_minutes_before,
            all_day=all_day,
        )

        if dry_run:
            return {"uid": new_uid, "ical": ical_text, "created": False}

        try:
            cal.save_event(ical_text)
        except Exception as exc:
            raise _wrap_write_error(exc, "create_event", new_uid) from exc

        return {"uid": new_uid, "ical": ical_text, "created": True}

    def update_event(
        self,
        event_uid: str,
        calendar_name: Optional[str] = None,
        occurrence_start: Optional[datetime] = None,
        title: Optional[str] = None,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        description: Optional[str] = None,
        location: Optional[str] = None,
        attendees: Optional[list[str]] = None,
        dry_run: bool = False,
    ) -> dict:
        if attendees is not None:
            attendees = [_validate_email(e) for e in attendees]

        cal_obj = self._find_event_object(event_uid, calendar_name)
        if cal_obj is None:
            raise EventNotFoundError(
                f"No event with UID {event_uid!r}. It may have been deleted, "
                "or be in a calendar other than the one specified."
            )

        fields: dict[str, Any] = {}
        if title is not None:
            fields["title"] = title
        if start is not None:
            fields["start"] = start
        if end is not None:
            fields["end"] = end
        if description is not None:
            fields["description"] = description
        if location is not None:
            fields["location"] = location
        if attendees is not None:
            fields["attendees"] = attendees

        if not fields:
            raise ValueError("update_event called with no fields to change")

        new_ical = _apply_update_to_ical(
            cal_obj.data, occurrence_start=occurrence_start, fields=fields
        )

        if dry_run:
            return {"uid": event_uid, "ical": new_ical, "updated": False}

        cal_obj.data = new_ical
        try:
            cal_obj.save()
        except Exception as exc:
            raise _wrap_write_error(exc, "update_event", event_uid) from exc

        return {"uid": event_uid, "ical": new_ical, "updated": True}

    def delete_event(
        self,
        event_uid: str,
        calendar_name: Optional[str] = None,
        occurrence_start: Optional[datetime] = None,
        dry_run: bool = False,
    ) -> dict:
        cal_obj = self._find_event_object(event_uid, calendar_name)
        if cal_obj is None:
            raise EventNotFoundError(
                f"No event with UID {event_uid!r}. It may already have been deleted."
            )

        if occurrence_start is None:
            mode = "series"
            if dry_run:
                return {"uid": event_uid, "mode": mode, "deleted": False}
            try:
                cal_obj.delete()
            except Exception as exc:
                raise _wrap_write_error(exc, "delete_event", event_uid) from exc
            return {"uid": event_uid, "mode": mode, "deleted": True}

        mode = "occurrence"
        new_ical = _apply_delete_occurrence_to_ical(cal_obj.data, occurrence_start)
        if dry_run:
            return {"uid": event_uid, "mode": mode, "deleted": False, "ical": new_ical}
        cal_obj.data = new_ical
        try:
            cal_obj.save()
        except Exception as exc:
            raise _wrap_write_error(exc, "delete_event", event_uid) from exc
        return {"uid": event_uid, "mode": mode, "deleted": True}

    def respond_to_invite(
        self,
        event_uid: str,
        response: str,
        calendar_name: Optional[str] = None,
        dry_run: bool = False,
    ) -> dict:
        """Update the user's PARTSTAT on an event they were invited to.

        iCloud's CalDAV scheduling extension (RFC 6638) typically dispatches
        the iTIP REPLY email to the organizer automatically when the modified
        event is PUT back. If your organizer doesn't receive a notification,
        the SMTP-based fallback can be added in Phase 2 (mail).
        """
        normalized = response.upper().replace(" ", "-")
        if normalized not in ("ACCEPTED", "DECLINED", "TENTATIVE", "NEEDS-ACTION"):
            raise ValueError(
                "response must be one of: accepted, declined, tentative, needs-action"
            )

        cal_obj = self._find_event_object(event_uid, calendar_name)
        if cal_obj is None:
            raise EventNotFoundError(
                f"No event with UID {event_uid!r} to respond to."
            )

        new_ical = _apply_invite_response(cal_obj.data, self._user_emails, normalized)
        if dry_run:
            return {
                "uid": event_uid,
                "response": normalized,
                "responded": False,
                "ical": new_ical,
            }

        cal_obj.data = new_ical
        try:
            cal_obj.save()
        except Exception as exc:
            raise _wrap_write_error(exc, "respond_to_invite", event_uid) from exc

        return {"uid": event_uid, "response": normalized, "responded": True}

    # -------------------------------------------------------------- internals

    def _calendars(self) -> list[caldav.Calendar]:
        return self._cache.get(lambda: self.principal.calendars())

    def _select_calendars(
        self, calendar_name: Optional[str]
    ) -> list[caldav.Calendar]:
        cals = self._calendars()
        if calendar_name is None:
            return cals
        matched = [c for c in cals if (c.name or "") == calendar_name]
        if not matched:
            available = sorted({c.name or "Unnamed" for c in cals})
            raise CalendarNotFoundError(
                f"No calendar named {calendar_name!r}. Available: {available}"
            )
        return matched

    def _require_calendar(self, calendar_name: str) -> caldav.Calendar:
        return self._select_calendars(calendar_name)[0]

    def _find_event_object(
        self,
        event_uid: str,
        calendar_name: Optional[str],
    ) -> Optional[Any]:
        for cal in self._select_calendars(calendar_name):
            try:
                obj = cal.event_by_uid(event_uid)
                if obj is not None:
                    return obj
            except CalDAVNotFoundError:
                continue
            except Exception as exc:
                logger.debug("event_by_uid failed on %r: %s", cal.name, exc)
                continue
        return None

    def _calendar_name_of(self, cal_obj: Any) -> str:
        parent = getattr(cal_obj, "parent", None)
        return (getattr(parent, "name", None) or "Unnamed") if parent else "Unnamed"


# =================================================================== helpers
# Pure functions below — no network, fully testable.


def _wrap_write_error(exc: Exception, operation: str, uid: str) -> Exception:
    """Convert a caldav write failure into one of our domain errors."""
    msg = str(exc).lower()
    # caldav doesn't expose a clean 412 exception type, so detect via message.
    if "412" in msg or "precondition" in msg:
        return ConflictError(
            f"{operation} for event {uid!r} failed because the event was modified "
            "by another client (e.g. iPhone Calendar) since you fetched it. Refetch "
            "with get_event or list_events and retry the operation against the "
            "fresh data."
        )
    if "401" in msg or "unauthorized" in msg or "forbidden" in msg:
        return AuthenticationError(
            f"{operation} for event {uid!r} was rejected by iCloud as unauthorized."
        )
    if "404" in msg or "not found" in msg:
        return EventNotFoundError(
            f"{operation} for event {uid!r} failed: event not found on the server."
        )
    if "timeout" in msg or "timed out" in msg or "connection" in msg:
        return NetworkError(
            f"{operation} for event {uid!r} failed due to a network error: {exc}"
        )
    # Unrecognized: re-raise as generic NetworkError so the caller gets context
    return NetworkError(f"{operation} for event {uid!r} failed: {exc}")


def _safe_parse_event(
    dav_event: Any,
    calendar_name: str,
    user_emails: Optional[Iterable[str]] = None,
) -> list[Event]:
    """Parse a CalDAV event into one or more Event models, swallowing errors."""
    try:
        ical = ICalendar.from_ical(dav_event.data)
    except Exception as exc:
        logger.warning("Skipping event with unparseable iCal data: %s", exc)
        return []

    parsed: list[Event] = []
    for component in ical.walk("VEVENT"):
        try:
            parsed.append(_vevent_to_event(component, calendar_name, user_emails))
        except Exception as exc:
            uid = component.get("UID", "<no-uid>")
            logger.warning("Skipping malformed VEVENT %s: %s", uid, exc)
    return parsed


def _vevent_to_event(
    vevent: Any,
    calendar_name: str,
    user_emails: Optional[Iterable[str]] = None,
) -> Event:
    uid = str(vevent.get("UID", ""))
    title = str(vevent.get("SUMMARY", "")).strip()

    dtstart = vevent.get("DTSTART")
    dtend = vevent.get("DTEND")
    if dtstart is None:
        raise ValueError("VEVENT missing DTSTART")

    start_raw = dtstart.dt
    end_raw = dtend.dt if dtend else start_raw

    all_day = isinstance(start_raw, date) and not isinstance(start_raw, datetime)
    start_dt = _to_utc(start_raw)
    end_dt = _to_utc(end_raw)

    rrule = vevent.get("RRULE")
    rrule_str: Optional[str] = None
    if rrule is not None:
        try:
            rrule_str = rrule.to_ical().decode()
        except Exception:
            rrule_str = None

    recurrence_id: Optional[datetime] = None
    rid = vevent.get("RECURRENCE-ID")
    if rid is not None:
        try:
            recurrence_id = _to_utc(rid.dt)
        except Exception:
            recurrence_id = None

    tz_name = None
    if hasattr(dtstart, "params") and "TZID" in dtstart.params:
        tz_name = str(dtstart.params["TZID"])

    status = _str_or_none(vevent.get("STATUS"))
    if status:
        status = status.upper()
    transparency = _str_or_none(vevent.get("TRANSP"))
    if transparency:
        transparency = transparency.upper()

    attendees = _parse_attendees(vevent.get("ATTENDEE"))
    user_response = _user_response_for(attendees, user_emails)

    return Event(
        uid=uid,
        calendar=calendar_name,
        title=title,
        start=start_dt,
        end=end_dt,
        all_day=all_day,
        location=_str_or_none(vevent.get("LOCATION")),
        description=_str_or_none(vevent.get("DESCRIPTION")),
        attendees=attendees,
        organizer=_parse_organizer(vevent.get("ORGANIZER")),
        status=status,
        transparency=transparency,
        user_response=user_response,
        rrule=rrule_str,
        recurrence_id=recurrence_id,
        timezone=tz_name,
    )


def _to_utc(value: Any) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, date):
        return datetime.combine(value, dtime.min, tzinfo=timezone.utc)
    raise TypeError(f"Cannot convert {type(value).__name__} to UTC datetime")


def _str_or_none(value: Any) -> Optional[str]:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _parse_attendees(raw: Any) -> list[Attendee]:
    if raw is None:
        return []
    items: Iterable[Any] = raw if isinstance(raw, list) else [raw]
    out: list[Attendee] = []
    for a in items:
        try:
            email = (
                str(a)
                .replace("mailto:", "")
                .replace("MAILTO:", "")
                .strip()
                .lower()
            )
            params = getattr(a, "params", {}) or {}
            out.append(
                Attendee(
                    email=email,
                    name=params.get("CN"),
                    role=params.get("ROLE"),
                    status=params.get("PARTSTAT"),
                )
            )
        except Exception as exc:
            logger.debug("Skipping malformed attendee: %s", exc)
    return out


def _parse_organizer(raw: Any) -> Optional[str]:
    if raw is None:
        return None
    try:
        return (
            str(raw).replace("mailto:", "").replace("MAILTO:", "").strip().lower()
        )
    except Exception:
        return None


def _user_response_for(
    attendees: list[Attendee], user_emails: Optional[Iterable[str]]
) -> Optional[str]:
    """Return the PARTSTAT for the first attendee whose email matches the user.

    Matches against any of the user's emails (primary username + aliases).
    Returns None if the user isn't in the attendee list.
    """
    if not user_emails:
        return None
    targets = {e.lower() for e in user_emails if e}
    for a in attendees:
        if a.email in targets:
            return a.status
    return None


def _is_blocking_event(event: Event) -> bool:
    """Whether this event should count as 'busy' for free/busy calculation."""
    if event.status == "CANCELLED":
        return False
    if event.transparency == "TRANSPARENT":
        return False
    if event.user_response == "DECLINED":
        return False
    return True


# ---------------------------------------------------- iCal building/mutation


def _build_create_ical(
    *,
    uid: str,
    title: str,
    start: datetime,
    end: datetime,
    description: Optional[str] = None,
    location: Optional[str] = None,
    attendees: Optional[list[str]] = None,
    alarm_minutes_before: Optional[int] = None,
    all_day: bool = False,
) -> str:
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("start and end must be timezone-aware datetimes")
    if end <= start:
        raise ValueError("end must be after start")

    ical = ICalendar()
    ical.add("prodid", PRODID)
    ical.add("version", "2.0")

    vevent = IEvent()
    vevent.add("uid", uid)
    vevent.add("summary", title)
    vevent.add("dtstamp", datetime.now(timezone.utc))

    if all_day:
        vevent.add("dtstart", start.date())
        vevent.add("dtend", end.date())
    else:
        vevent.add("dtstart", start.astimezone(timezone.utc))
        vevent.add("dtend", end.astimezone(timezone.utc))

    if description:
        vevent.add("description", description)
    if location:
        vevent.add("location", location)

    for email in attendees or []:
        addr = vCalAddress(f"mailto:{email}")
        addr.params["ROLE"] = vText("REQ-PARTICIPANT")
        addr.params["PARTSTAT"] = vText("NEEDS-ACTION")
        addr.params["RSVP"] = vText("TRUE")
        vevent.add("attendee", addr, encode=0)

    if alarm_minutes_before is not None:
        if alarm_minutes_before < 0:
            raise ValueError("alarm_minutes_before must be non-negative")
        alarm = Alarm()
        alarm.add("action", "DISPLAY")
        alarm.add("trigger", timedelta(minutes=-alarm_minutes_before))
        alarm.add("description", title)
        vevent.add_component(alarm)

    ical.add_component(vevent)
    return ical.to_ical().decode()


def _apply_update_to_ical(
    ical_text: str | bytes,
    *,
    occurrence_start: Optional[datetime],
    fields: dict[str, Any],
) -> str:
    ical = ICalendar.from_ical(ical_text)
    master = _find_master_vevent(ical)
    if master is None:
        raise ValueError("No master VEVENT found in calendar object")

    if occurrence_start is None:
        _apply_fields_to_vevent(master, fields)
    else:
        occurrence_utc = _to_utc(occurrence_start)
        target = _find_override_vevent(ical, occurrence_utc)
        if target is None:
            target = _build_override_vevent(master, occurrence_utc)
            ical.add_component(target)
        _apply_fields_to_vevent(target, fields)

    return ical.to_ical().decode()


def _apply_delete_occurrence_to_ical(
    ical_text: str | bytes,
    occurrence_start: datetime,
) -> str:
    ical = ICalendar.from_ical(ical_text)
    master = _find_master_vevent(ical)
    if master is None:
        raise ValueError("No master VEVENT found in calendar object")

    occurrence_utc = _to_utc(occurrence_start)
    master.add("EXDATE", occurrence_utc)

    keep = []
    for sub in ical.subcomponents:
        if sub.name == "VEVENT":
            rid = sub.get("RECURRENCE-ID")
            if rid is not None:
                try:
                    if _to_utc(rid.dt) == occurrence_utc:
                        continue
                except Exception:
                    pass
        keep.append(sub)
    ical.subcomponents = keep

    return ical.to_ical().decode()


def _apply_invite_response(
    ical_text: str | bytes,
    user_emails: Iterable[str],
    response: str,
) -> str:
    ical = ICalendar.from_ical(ical_text)
    master = _find_master_vevent(ical)
    if master is None:
        raise ValueError("No master VEVENT found in calendar object")

    raw = master.get("ATTENDEE")
    if raw is None:
        raise ValueError(
            "Event has no attendees, so there's nothing to respond to. "
            "(You may be the organizer or this isn't a meeting.)"
        )

    items = raw if isinstance(raw, list) else [raw]
    targets = {e.lower() for e in user_emails if e}
    if not targets:
        raise ValueError("No user emails configured to match against")

    found = False
    for attendee in items:
        addr = (
            str(attendee)
            .replace("mailto:", "")
            .replace("MAILTO:", "")
            .strip()
            .lower()
        )
        if addr in targets:
            attendee.params["PARTSTAT"] = vText(response)
            attendee.params["RSVP"] = vText("FALSE")
            found = True
            break

    if not found:
        attendee_list = sorted(
            str(a).replace("mailto:", "").replace("MAILTO:", "").strip().lower()
            for a in items
        )
        targets_display = sorted(targets)
        raise ValueError(
            f"None of the configured user emails {targets_display} are in the "
            f"attendee list ({attendee_list}). Can't respond to an invite you "
            "weren't sent. If you receive invites under a different alias, set "
            "ICLOUD_USER_ALIASES in your env."
        )

    # Bump DTSTAMP so iCloud sees this as a fresh edit
    if "DTSTAMP" in master:
        del master["DTSTAMP"]
    master.add("DTSTAMP", datetime.now(timezone.utc))

    return ical.to_ical().decode()


def _find_master_vevent(ical: Any) -> Optional[Any]:
    for component in ical.walk("VEVENT"):
        if component.get("RECURRENCE-ID") is None:
            return component
    return None


def _find_override_vevent(ical: Any, occurrence_utc: datetime) -> Optional[Any]:
    for component in ical.walk("VEVENT"):
        rid = component.get("RECURRENCE-ID")
        if rid is None:
            continue
        try:
            if _to_utc(rid.dt) == occurrence_utc:
                return component
        except Exception:
            continue
    return None


def _build_override_vevent(master: Any, occurrence_utc: datetime) -> Any:
    override = copy.deepcopy(master)
    for prop in ("RRULE", "EXDATE", "RDATE"):
        if prop in override:
            del override[prop]
    if "RECURRENCE-ID" in override:
        del override["RECURRENCE-ID"]
    override.add("RECURRENCE-ID", occurrence_utc)
    if "DTSTAMP" in override:
        del override["DTSTAMP"]
    override.add("DTSTAMP", datetime.now(timezone.utc))
    return override


def _apply_fields_to_vevent(vevent: Any, fields: dict[str, Any]) -> None:
    if "title" in fields:
        _set_property(vevent, "SUMMARY", fields["title"])
    if "start" in fields:
        _set_dt_property(vevent, "DTSTART", fields["start"])
    if "end" in fields:
        _set_dt_property(vevent, "DTEND", fields["end"])
    if "description" in fields:
        _set_property(vevent, "DESCRIPTION", fields["description"])
    if "location" in fields:
        _set_property(vevent, "LOCATION", fields["location"])
    if "attendees" in fields:
        _replace_attendees(vevent, fields["attendees"] or [])
    if "DTSTAMP" in vevent:
        del vevent["DTSTAMP"]
    vevent.add("DTSTAMP", datetime.now(timezone.utc))


def _set_property(component: Any, key: str, value: Optional[str]) -> None:
    if key in component:
        del component[key]
    if value is not None and value != "":
        component.add(key, value)


def _set_dt_property(component: Any, key: str, value: datetime) -> None:
    if value.tzinfo is None:
        raise ValueError(f"{key} update value must be timezone-aware")
    if key in component:
        del component[key]
    component.add(key, value.astimezone(timezone.utc))


def _replace_attendees(vevent: Any, emails: list[str]) -> None:
    while "ATTENDEE" in vevent:
        del vevent["ATTENDEE"]
    for email in emails:
        addr = vCalAddress(f"mailto:{email}")
        addr.params["ROLE"] = vText("REQ-PARTICIPANT")
        addr.params["PARTSTAT"] = vText("NEEDS-ACTION")
        addr.params["RSVP"] = vText("TRUE")
        vevent.add("attendee", addr, encode=0)


# ---------------------------------------------------------------- free slots


def _free_slots_from_busy(
    start: datetime,
    end: datetime,
    busy: list[tuple[datetime, datetime]],
    duration_minutes: int,
) -> list[FreeSlot]:
    if not busy:
        return _slot_if_long_enough(start, end, duration_minutes)

    norm: list[tuple[datetime, datetime]] = []
    for b_start, b_end in busy:
        s = max(b_start, start)
        e = min(b_end, end)
        if e > s:
            norm.append((s, e))
    norm.sort()

    merged: list[tuple[datetime, datetime]] = []
    for s, e in norm:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))

    free: list[FreeSlot] = []
    cursor = start
    for s, e in merged:
        free.extend(_slot_if_long_enough(cursor, s, duration_minutes))
        cursor = e
    free.extend(_slot_if_long_enough(cursor, end, duration_minutes))
    return free


def _slot_if_long_enough(
    s: datetime, e: datetime, duration_minutes: int
) -> list[FreeSlot]:
    if (e - s) >= timedelta(minutes=duration_minutes):
        return [FreeSlot(start=s, end=e)]
    return []
