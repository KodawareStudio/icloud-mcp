"""Pydantic models for iCloud Contacts entities returned by MCP tools."""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class ContactAddressBook(BaseModel):
    """A CardDAV address book."""

    name: str
    url: str


class ContactField(BaseModel):
    """A typed contact value such as an email address or phone number."""

    value: str
    types: list[str] = Field(default_factory=list)
    label: Optional[str] = None


class PostalAddress(BaseModel):
    """A structured vCard ADR value."""

    formatted: str
    types: list[str] = Field(default_factory=list)
    po_box: Optional[str] = None
    extended: Optional[str] = None
    street: Optional[str] = None
    city: Optional[str] = None
    region: Optional[str] = None
    postal_code: Optional[str] = None
    country: Optional[str] = None


class Contact(BaseModel):
    """A parsed iCloud contact."""

    id: str = Field(description="CardDAV resource URL for get_contact.")
    addressbook: str
    full_name: str
    given_name: Optional[str] = None
    family_name: Optional[str] = None
    nickname: Optional[str] = None
    organization: Optional[str] = None
    title: Optional[str] = None
    emails: list[ContactField] = Field(default_factory=list)
    phones: list[ContactField] = Field(default_factory=list)
    addresses: list[PostalAddress] = Field(default_factory=list)
    urls: list[ContactField] = Field(default_factory=list)
    birthday: Optional[str] = None
    note: Optional[str] = None
    uid: Optional[str] = None
    etag: Optional[str] = None
    raw_vcard: Optional[str] = Field(
        default=None,
        description="Raw vCard data, only included when explicitly requested.",
    )
