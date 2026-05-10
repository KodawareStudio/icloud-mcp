"""Tests for CardDAV contact parsing and search behavior."""
from __future__ import annotations

from icloud_mcp.contacts.client import _contact_matches, _parse_vcard


_VCARD = """BEGIN:VCARD
VERSION:3.0
UID:abc-123
FN:Alice Example
N:Example;Alice;;;
NICKNAME:Al
ORG:Example Co;Product
TITLE:Director
EMAIL;TYPE=HOME:alice@example.com
EMAIL;TYPE=WORK:alice@work.test
TEL;TYPE=CELL;VALUE=uri:tel:+14155550123
ADR;TYPE=HOME:;;1 Main St;San Francisco;CA;94105;USA
URL:https://example.com
BDAY:1980-01-02
NOTE:Line one\\nLine two
END:VCARD
"""


def test_parse_vcard_extracts_structured_fields() -> None:
    contact = _parse_vcard(
        _VCARD,
        addressbook="Contacts",
        href="https://contacts.icloud.com/card/abc.vcf",
        etag='"123"',
        include_notes=True,
    )

    assert contact.full_name == "Alice Example"
    assert contact.given_name == "Alice"
    assert contact.family_name == "Example"
    assert contact.nickname == "Al"
    assert contact.organization == "Example Co / Product"
    assert contact.title == "Director"
    assert contact.uid == "abc-123"
    assert contact.etag == '"123"'
    assert contact.emails[0].value == "alice@example.com"
    assert contact.emails[0].types == ["home"]
    assert contact.phones[0].value == "+14155550123"
    assert contact.phones[0].types == ["cell"]
    assert contact.addresses[0].city == "San Francisco"
    assert contact.addresses[0].formatted == "1 Main St, San Francisco, CA, 94105, USA"
    assert contact.urls[0].value == "https://example.com"
    assert contact.birthday == "1980-01-02"
    assert contact.note == "Line one\nLine two"


def test_parse_vcard_omits_notes_by_default() -> None:
    contact = _parse_vcard(
        _VCARD,
        addressbook="Contacts",
        href="https://contacts.icloud.com/card/abc.vcf",
        etag=None,
        include_notes=False,
    )

    assert contact.note is None


def test_contact_matches_name_email_phone_and_org() -> None:
    contact = _parse_vcard(
        _VCARD,
        addressbook="Contacts",
        href="https://contacts.icloud.com/card/abc.vcf",
        etag=None,
        include_notes=False,
    )

    assert _contact_matches(contact, "alice")
    assert _contact_matches(contact, "work.test")
    assert _contact_matches(contact, "415555")
    assert _contact_matches(contact, "product")
    assert not _contact_matches(contact, "missing")
