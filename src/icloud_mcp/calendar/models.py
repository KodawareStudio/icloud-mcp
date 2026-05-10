"""Pydantic models for calendar entities returned by MCP tools."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class Calendar(BaseModel):
    """An iCloud calendar (e.g. 'Home', 'Work')."""

    name: str
    url: str


class FreeSlot(BaseModel):
    """A contiguous span with no scheduled events."""

    start: datetime
    end: datetime


class Attendee(BaseModel):
    """A meeting attendee."""

    email: str
    name: Optional[str] = None
    role: Optional[str] = None  # e.g. REQ-PARTICIPANT, OPT-PARTICIPANT, CHAIR
    status: Optional[str] = None  # e.g. ACCEPTED, DECLINED, TENTATIVE, NEEDS-ACTION


class Event(BaseModel):
    """A calendar event, with recurrences expanded into individual instances."""

    uid: str = Field(description="iCalendar UID (stable across recurrences)")
    calendar: str = Field(description="Name of the calendar this event belongs to")
    title: str
    start: datetime = Field(description="Start time, normalized to UTC")
    end: datetime = Field(description="End time, normalized to UTC")
    all_day: bool = False
    location: Optional[str] = None
    description: Optional[str] = None
    attendees: list[Attendee] = Field(default_factory=list)
    organizer: Optional[str] = None
    status: Optional[str] = Field(
        default=None,
        description="CONFIRMED, TENTATIVE, or CANCELLED. Cancelled events still "
        "appear in queries but should be filtered out of free/busy calculations.",
    )
    transparency: Optional[str] = Field(
        default=None,
        description="OPAQUE (blocks time, default) or TRANSPARENT (doesn't block).",
    )
    user_response: Optional[str] = Field(
        default=None,
        description="The connected user's PARTSTAT for this event if they're an "
        "attendee: ACCEPTED, DECLINED, TENTATIVE, NEEDS-ACTION. Null if the user "
        "is the organizer or not invited.",
    )
    recurrence_id: Optional[datetime] = Field(
        default=None,
        description=(
            "If this event is one occurrence of a recurring series, the "
            "RECURRENCE-ID identifying the specific instance. Used to target "
            "a single occurrence in update/delete with scope='this'."
        ),
    )
    rrule: Optional[str] = Field(
        default=None,
        description="Original RRULE string if this is a recurring event",
    )
    timezone: Optional[str] = Field(
        default=None,
        description="Original TZID (e.g. 'America/Los_Angeles') for display localization",
    )
