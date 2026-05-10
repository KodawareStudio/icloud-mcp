"""Tests for workflow tools (today_brief and friends)."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from icloud_mcp.calendar.models import Event
from icloud_mcp.config import Config
from icloud_mcp.mail.models import Address as MailAddress, MessageHeader
from icloud_mcp.calendar.models import Attendee


class _FakeMCP:
    def __init__(self) -> None:
        self.tools: dict = {}

    def tool(self, **kwargs):
        def decorator(func):
            self.tools[func.__name__] = func
            return func

        return decorator


class _StubCalendar:
    """Stub calendar client returning canned events."""

    def __init__(self, events: list[Event]) -> None:
        self._events = events
        self.last_call: tuple = ()

    def list_events(self, start, end, calendar_name=None):
        self.last_call = (start, end, calendar_name)
        return [e for e in self._events if start <= e.start < end]


class _StubMail:
    """Stub mail client returning canned messages."""

    def __init__(self, messages: list[MessageHeader]) -> None:
        self._messages = messages
        self.last_call: dict = {}

    def list_messages(self, mailbox="INBOX", limit=25, since=None, unread_only=False):
        self.last_call = {
            "mailbox": mailbox,
            "limit": limit,
            "since": since,
            "unread_only": unread_only,
        }
        return self._messages


def _make_event(
    *,
    title: str = "Meeting",
    start: datetime,
    end: datetime,
    status: str | None = None,
    user_response: str | None = None,
) -> Event:
    return Event(
        uid=f"event-{title}",
        calendar="Work",
        title=title,
        start=start,
        end=end,
        status=status,
        user_response=user_response,
    )


def _make_msg(
    *,
    uid: int,
    subject: str = "Hello",
    from_email: str = "alice@example.com",
    is_read: bool = False,
) -> MessageHeader:
    return MessageHeader(
        uid=uid,
        mailbox="INBOX",
        from_addr=MailAddress(email=from_email),
        subject=subject,
        date=datetime.now(timezone.utc),
        is_read=is_read,
    )


def _build_workflow_tools(
    *,
    events: list[Event] | None = None,
    messages: list[MessageHeader] | None = None,
    user_timezone: str | None = None,
) -> tuple[_FakeMCP, _StubCalendar, _StubMail]:
    from icloud_mcp.workflows.tools import register_workflow_tools

    cal = _StubCalendar(events or [])
    mail = _StubMail(messages or [])
    config = Config(
        icloud_username="me@icloud.com",
        icloud_app_password="x",
        read_only=False,
        user_timezone=user_timezone,
    )
    fake = _FakeMCP()
    register_workflow_tools(fake, config, cal, mail)
    return fake, cal, mail


def test_today_brief_basic_shape() -> None:
    fake, _, _ = _build_workflow_tools()
    result = fake.tools["today_brief"](date="2026-05-09", timezone_name="UTC")
    assert result["date"] == "2026-05-09"
    assert result["timezone"] == "UTC"
    assert result["events_count"] == 0
    assert result["unread_count"] == 0
    assert result["pending_invites_count"] == 0


def test_today_brief_filters_cancelled_events() -> None:
    day = datetime(2026, 5, 9, 10, 0, tzinfo=timezone.utc)
    events = [
        _make_event(
            title="Standup",
            start=day,
            end=day + timedelta(minutes=30),
        ),
        _make_event(
            title="Cancelled meeting",
            start=day + timedelta(hours=2),
            end=day + timedelta(hours=3),
            status="CANCELLED",
        ),
    ]
    fake, _, _ = _build_workflow_tools(events=events)
    result = fake.tools["today_brief"](date="2026-05-09", timezone_name="UTC")
    assert result["events_count"] == 1  # cancelled excluded
    assert result["cancelled_count"] == 1
    titles = [e["title"] for e in result["events"]]
    assert "Standup" in titles
    assert "Cancelled meeting" not in titles


def test_today_brief_surfaces_pending_invites() -> None:
    day = datetime(2026, 5, 9, 10, 0, tzinfo=timezone.utc)
    events = [
        _make_event(
            title="Confirmed",
            start=day,
            end=day + timedelta(minutes=30),
            user_response="ACCEPTED",
        ),
        _make_event(
            title="Needs response",
            start=day + timedelta(hours=2),
            end=day + timedelta(hours=3),
            user_response="NEEDS-ACTION",
        ),
    ]
    fake, _, _ = _build_workflow_tools(events=events)
    result = fake.tools["today_brief"](date="2026-05-09", timezone_name="UTC")
    assert result["pending_invites_count"] == 1
    assert result["pending_invites"][0]["title"] == "Needs response"


def test_today_brief_uses_config_timezone_by_default() -> None:
    fake, cal, _ = _build_workflow_tools(user_timezone="America/Los_Angeles")
    result = fake.tools["today_brief"](date="2026-05-09")
    assert result["timezone"] == "America/Los_Angeles"
    # Window should be midnight LA time, not UTC
    start, end, _ = cal.last_call
    assert start.hour == 0
    assert str(start.tzinfo) == "America/Los_Angeles"
    # 24 hours later
    assert (end - start) == timedelta(days=1)


def test_today_brief_explicit_tz_overrides_config() -> None:
    fake, _, _ = _build_workflow_tools(user_timezone="UTC")
    result = fake.tools["today_brief"](
        date="2026-05-09", timezone_name="America/New_York"
    )
    assert result["timezone"] == "America/New_York"


def test_today_brief_rejects_invalid_timezone() -> None:
    fake, _, _ = _build_workflow_tools()
    with pytest.raises(ValueError, match="Unknown timezone"):
        fake.tools["today_brief"](
            date="2026-05-09", timezone_name="Not/A/Real/Zone"
        )


def test_today_brief_rejects_invalid_date() -> None:
    fake, _, _ = _build_workflow_tools()
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        fake.tools["today_brief"](date="not-a-date", timezone_name="UTC")


def test_today_brief_rejects_excessive_lookback() -> None:
    fake, _, _ = _build_workflow_tools()
    with pytest.raises(ValueError, match="between 1 and 168"):
        fake.tools["today_brief"](
            date="2026-05-09", timezone_name="UTC", unread_lookback_hours=200
        )


def test_today_brief_passes_unread_only_to_mail() -> None:
    fake, _, mail = _build_workflow_tools()
    fake.tools["today_brief"](date="2026-05-09", timezone_name="UTC")
    assert mail.last_call["unread_only"] is True
    assert mail.last_call["mailbox"] == "INBOX"


def test_today_brief_lookback_window_passes_to_mail() -> None:
    fake, _, mail = _build_workflow_tools()
    fake.tools["today_brief"](
        date="2026-05-09",
        timezone_name="UTC",
        unread_lookback_hours=72,
    )
    # since should be ~3 days ago (as a date)
    assert mail.last_call["since"] is not None
    today = date.today()
    days_back = (today - mail.last_call["since"]).days
    assert 2 <= days_back <= 4  # 72 hours back ≈ 3 days, with rounding tolerance


def test_today_brief_survives_calendar_failure() -> None:
    """If the calendar fetch raises, the brief still returns mail content."""
    msg = _make_msg(uid=1, subject="Important")

    class FailingCalendar:
        def list_events(self, *args, **kwargs):
            raise RuntimeError("CalDAV is down")

    from icloud_mcp.workflows.tools import register_workflow_tools

    fake = _FakeMCP()
    config = Config(
        icloud_username="me@icloud.com",
        icloud_app_password="x",
        read_only=False,
    )
    register_workflow_tools(fake, config, FailingCalendar(), _StubMail([msg]))

    result = fake.tools["today_brief"](date="2026-05-09", timezone_name="UTC")
    assert result["events_count"] == 0
    assert result["unread_count"] == 1


def test_today_brief_survives_mail_failure() -> None:
    """If the mail fetch raises, the brief still returns calendar content."""
    day = datetime(2026, 5, 9, 10, 0, tzinfo=timezone.utc)
    event = _make_event(
        title="Standup",
        start=day,
        end=day + timedelta(minutes=30),
    )

    class FailingMail:
        def list_messages(self, *args, **kwargs):
            raise RuntimeError("IMAP is down")

    from icloud_mcp.workflows.tools import register_workflow_tools

    fake = _FakeMCP()
    config = Config(
        icloud_username="me@icloud.com",
        icloud_app_password="x",
        read_only=False,
    )
    register_workflow_tools(fake, config, _StubCalendar([event]), FailingMail())

    result = fake.tools["today_brief"](date="2026-05-09", timezone_name="UTC")
    assert result["events_count"] == 1
    assert result["unread_count"] == 0


# ============================================================ prep_for_meeting


class _StubCalendarWithGet:
    def __init__(self, event: Event | None) -> None:
        self._event = event
        self.calls: list = []

    def get_event(self, event_uid, calendar_name=None):
        self.calls.append((event_uid, calendar_name))
        return self._event


class _StubMailWithSearch:
    """Stub mail client that records searches and returns scripted results."""

    def __init__(
        self,
        sent_folder: str = "Sent Messages",
        results_by_query: dict | None = None,
    ) -> None:
        self._sent = sent_folder
        # Keyed by (mailbox, from_addr, to_addr) — None for "any"
        self._results = results_by_query or {}
        self.searches: list[dict] = []

    def sent_folder_name(self) -> str:
        return self._sent

    def search_mail(
        self,
        mailbox="INBOX",
        query=None,
        from_addr=None,
        to_addr=None,
        subject=None,
        since=None,
        before=None,
        unread_only=False,
        limit=25,
    ):
        call = {
            "mailbox": mailbox,
            "from_addr": from_addr,
            "to_addr": to_addr,
            "since": since,
            "limit": limit,
        }
        self.searches.append(call)
        key = (mailbox, from_addr, to_addr)
        return list(self._results.get(key, []))


def _make_event_with_attendees(
    *, attendees: list[Attendee]
) -> Event:
    return Event(
        uid="evt-1",
        calendar="Work",
        title="Project sync",
        start=datetime(2026, 5, 9, 14, 0, tzinfo=timezone.utc),
        end=datetime(2026, 5, 9, 15, 0, tzinfo=timezone.utc),
        attendees=attendees,
    )


def test_prep_for_meeting_excludes_user_from_attendees() -> None:
    """Both primary email and aliases should be filtered out of attendees."""
    event = _make_event_with_attendees(
        attendees=[
            Attendee(email="me@icloud.com"),
            Attendee(email="me@me.com"),  # alias
            Attendee(email="alice@example.com"),
            Attendee(email="bob@example.com"),
        ]
    )

    from icloud_mcp.workflows.tools import register_workflow_tools

    fake = _FakeMCP()
    config = Config(
        icloud_username="me@icloud.com",
        icloud_app_password="x",
        read_only=False,
        user_aliases=("me@me.com",),
    )
    register_workflow_tools(
        fake,
        config,
        _StubCalendarWithGet(event),
        _StubMailWithSearch(),
    )

    result = fake.tools["prep_for_meeting"](event_uid="evt-1")
    other_emails = {a["email"] for a in result["attendees_other_than_me"]}
    assert other_emails == {"alice@example.com", "bob@example.com"}


def test_prep_for_meeting_searches_inbox_and_sent_per_attendee() -> None:
    event = _make_event_with_attendees(
        attendees=[
            Attendee(email="alice@example.com"),
        ]
    )

    from icloud_mcp.workflows.tools import register_workflow_tools

    fake = _FakeMCP()
    config = Config(
        icloud_username="me@icloud.com",
        icloud_app_password="x",
        read_only=False,
    )
    mail = _StubMailWithSearch(sent_folder="Sent Messages")
    register_workflow_tools(
        fake,
        config,
        _StubCalendarWithGet(event),
        mail,
    )

    fake.tools["prep_for_meeting"](event_uid="evt-1")

    # Should have searched INBOX with from_addr=alice, and Sent with to_addr=alice
    inbox_searches = [s for s in mail.searches if s["mailbox"] == "INBOX"]
    sent_searches = [s for s in mail.searches if s["mailbox"] == "Sent Messages"]
    assert len(inbox_searches) == 1
    assert inbox_searches[0]["from_addr"] == "alice@example.com"
    assert inbox_searches[0]["to_addr"] is None
    assert len(sent_searches) == 1
    assert sent_searches[0]["to_addr"] == "alice@example.com"
    assert sent_searches[0]["from_addr"] is None


def test_prep_for_meeting_dedupes_correspondence_by_message_id() -> None:
    """The same message reachable from INBOX and Sent (by Message-ID) should appear once."""
    event = _make_event_with_attendees(
        attendees=[Attendee(email="alice@example.com")]
    )

    shared_id = "<shared-msg@example.com>"
    inbox_msg = MessageHeader(
        uid=1,
        mailbox="INBOX",
        from_addr=MailAddress(email="alice@example.com"),
        subject="Hello",
        date=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
        message_id=shared_id,
    )
    sent_copy = MessageHeader(
        uid=99,  # different UID across folders, but same Message-ID
        mailbox="Sent Messages",
        from_addr=MailAddress(email="me@icloud.com"),
        subject="Hello",
        date=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
        message_id=shared_id,
    )

    mail = _StubMailWithSearch(
        sent_folder="Sent Messages",
        results_by_query={
            ("INBOX", "alice@example.com", None): [inbox_msg],
            ("Sent Messages", None, "alice@example.com"): [sent_copy],
        },
    )

    from icloud_mcp.workflows.tools import register_workflow_tools

    fake = _FakeMCP()
    config = Config(
        icloud_username="me@icloud.com",
        icloud_app_password="x",
        read_only=False,
    )
    register_workflow_tools(fake, config, _StubCalendarWithGet(event), mail)

    result = fake.tools["prep_for_meeting"](event_uid="evt-1")
    correspondence = result["recent_correspondence"]["alice@example.com"]
    assert len(correspondence) == 1


def test_prep_for_meeting_event_not_found() -> None:
    from icloud_mcp.workflows.tools import register_workflow_tools

    fake = _FakeMCP()
    config = Config(
        icloud_username="me@icloud.com",
        icloud_app_password="x",
        read_only=False,
    )
    register_workflow_tools(
        fake, config, _StubCalendarWithGet(None), _StubMailWithSearch()
    )

    with pytest.raises(ValueError, match="No event with UID"):
        fake.tools["prep_for_meeting"](event_uid="bogus-uid")


def test_prep_for_meeting_rejects_invalid_lookback() -> None:
    event = _make_event_with_attendees(attendees=[])

    from icloud_mcp.workflows.tools import register_workflow_tools

    fake = _FakeMCP()
    config = Config(
        icloud_username="me@icloud.com",
        icloud_app_password="x",
        read_only=False,
    )
    register_workflow_tools(
        fake, config, _StubCalendarWithGet(event), _StubMailWithSearch()
    )

    with pytest.raises(ValueError, match="email_lookback_days"):
        fake.tools["prep_for_meeting"](event_uid="evt-1", email_lookback_days=200)


def test_prep_for_meeting_survives_email_search_failure() -> None:
    """If a per-attendee search blows up, the response still returns."""
    event = _make_event_with_attendees(
        attendees=[Attendee(email="alice@example.com")]
    )

    class ExplodingMail:
        def sent_folder_name(self):
            return "Sent Messages"

        def search_mail(self, *args, **kwargs):
            raise RuntimeError("IMAP went away")

    from icloud_mcp.workflows.tools import register_workflow_tools

    fake = _FakeMCP()
    config = Config(
        icloud_username="me@icloud.com",
        icloud_app_password="x",
        read_only=False,
    )
    register_workflow_tools(
        fake, config, _StubCalendarWithGet(event), ExplodingMail()
    )

    result = fake.tools["prep_for_meeting"](event_uid="evt-1")
    assert len(result["attendees_other_than_me"]) == 1
    assert result["recent_correspondence"]["alice@example.com"] == []


# ============================================================ Config aliases


def test_config_parses_aliases_from_env(monkeypatch) -> None:
    monkeypatch.setenv("ICLOUD_USERNAME", "me@icloud.com")
    monkeypatch.setenv("ICLOUD_APP_PASSWORD", "fake")
    monkeypatch.setenv("ICLOUD_USER_ALIASES", "me@me.com, me@mac.com")

    cfg = Config.from_env()
    assert cfg.user_aliases == ("me@me.com", "me@mac.com")
    assert cfg.all_user_emails == ("me@icloud.com", "me@me.com", "me@mac.com")


def test_config_aliases_drops_primary_duplicate(monkeypatch) -> None:
    monkeypatch.setenv("ICLOUD_USERNAME", "me@icloud.com")
    monkeypatch.setenv("ICLOUD_APP_PASSWORD", "fake")
    monkeypatch.setenv("ICLOUD_USER_ALIASES", "me@icloud.com, me@me.com")

    cfg = Config.from_env()
    # Primary should not appear twice
    assert cfg.all_user_emails == ("me@icloud.com", "me@me.com")


def test_config_rejects_invalid_alias(monkeypatch) -> None:
    monkeypatch.setenv("ICLOUD_USERNAME", "me@icloud.com")
    monkeypatch.setenv("ICLOUD_APP_PASSWORD", "fake")
    monkeypatch.setenv("ICLOUD_USER_ALIASES", "me@me.com, not-an-email")

    with pytest.raises(RuntimeError, match="invalid entry"):
        Config.from_env()
