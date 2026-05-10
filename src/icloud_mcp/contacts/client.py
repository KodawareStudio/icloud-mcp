"""iCloud CardDAV client.

Uses WebDAV/CardDAV directly because the existing CalDAV dependency does not
include CardDAV address book helpers.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable, Optional
from urllib.parse import urljoin
from xml.etree import ElementTree as ET

import niquests

from icloud_mcp.contacts.models import (
    Contact,
    ContactAddressBook,
    ContactField,
    PostalAddress,
)
from icloud_mcp.errors import AuthenticationError, ContactNotFoundError, NetworkError

logger = logging.getLogger(__name__)

ICLOUD_CARDDAV_URL = "https://contacts.icloud.com"

NS_DAV = "DAV:"
NS_CARD = "urn:ietf:params:xml:ns:carddav"
NS = {"d": NS_DAV, "card": NS_CARD}


@dataclass
class _ContactResource:
    href: str
    etag: Optional[str]
    vcard: str


class ICloudContactsClient:
    """Connects lazily to iCloud Contacts over CardDAV."""

    def __init__(
        self,
        username: str,
        password: str,
        base_url: str = ICLOUD_CARDDAV_URL,
        timeout: int = 30,
    ) -> None:
        self._username = username
        self._password = password
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._session = niquests.Session()
        self._session.auth = (username, password)
        self._addressbooks_cache: Optional[list[ContactAddressBook]] = None

    def list_addressbooks(self) -> list[ContactAddressBook]:
        """Discover CardDAV address books for the iCloud account."""
        if self._addressbooks_cache is not None:
            return self._addressbooks_cache

        principal_href = self._current_user_principal()
        home_href = self._addressbook_home_set(principal_href)
        home_url = self._absolute_url(home_href)
        body = """<?xml version="1.0" encoding="utf-8"?>
<d:propfind xmlns:d="DAV:" xmlns:card="urn:ietf:params:xml:ns:carddav">
  <d:prop>
    <d:displayname />
    <d:resourcetype />
  </d:prop>
</d:propfind>"""
        root = self._xml_request("PROPFIND", home_url, body, depth="1")
        books: list[ContactAddressBook] = []
        for response in root.findall("d:response", NS):
            href = _text(response.find("d:href", NS))
            if not href:
                continue
            resource_types = response.findall(".//d:resourcetype/*", NS)
            is_addressbook = any(child.tag == f"{{{NS_CARD}}}addressbook" for child in resource_types)
            if not is_addressbook:
                continue
            display_name = _text(response.find(".//d:displayname", NS)) or _last_path_part(href)
            books.append(ContactAddressBook(name=display_name, url=self._absolute_url(href)))

        self._addressbooks_cache = books
        return books

    def list_contacts(
        self,
        addressbook_name: Optional[str] = None,
        limit: int = 50,
        include_notes: bool = False,
    ) -> list[Contact]:
        """List contacts from one or all address books."""
        limit = _clamp_limit(limit)
        contacts: list[Contact] = []
        for book in self._matching_addressbooks(addressbook_name):
            for resource in self._addressbook_resources(book.url):
                contacts.append(_parse_vcard(resource.vcard, book.name, resource.href, resource.etag, include_notes))
                if len(contacts) >= limit:
                    return contacts
        return contacts

    def search_contacts(
        self,
        query: str,
        addressbook_name: Optional[str] = None,
        limit: int = 25,
        include_notes: bool = False,
    ) -> list[Contact]:
        """Search contacts client-side across names, orgs, email, and phone."""
        needle = query.casefold().strip()
        if not needle:
            raise ValueError("query must not be empty")
        matches: list[Contact] = []
        for book in self._matching_addressbooks(addressbook_name):
            for resource in self._addressbook_resources(book.url):
                contact = _parse_vcard(resource.vcard, book.name, resource.href, resource.etag, include_notes)
                if _contact_matches(contact, needle):
                    matches.append(contact)
                    if len(matches) >= _clamp_limit(limit):
                        return matches
        return matches

    def get_contact(self, contact_id: str, include_raw_vcard: bool = False) -> Contact:
        """Fetch a single contact by CardDAV resource URL."""
        url = self._absolute_url(contact_id)
        response = self._request("GET", url, headers={"Accept": "text/vcard,text/x-vcard,*/*"})
        if response.status_code == 404:
            raise ContactNotFoundError(f"No iCloud contact exists at {contact_id!r}.")
        self._raise_for_status(response, url)
        book_name = self._book_name_for_url(url)
        contact = _parse_vcard(response.text, book_name, url, response.headers.get("ETag"), include_notes=True)
        if include_raw_vcard:
            contact.raw_vcard = response.text
        return contact

    def _matching_addressbooks(self, name: Optional[str]) -> Iterable[ContactAddressBook]:
        books = self.list_addressbooks()
        if name is None:
            return books
        matches = [book for book in books if book.name == name]
        if not matches:
            names = ", ".join(book.name for book in books) or "(none)"
            raise ValueError(f"No iCloud address book named {name!r}. Available: {names}")
        return matches

    def _current_user_principal(self) -> str:
        body = """<?xml version="1.0" encoding="utf-8"?>
<d:propfind xmlns:d="DAV:">
  <d:prop><d:current-user-principal /></d:prop>
</d:propfind>"""
        for url in (f"{self._base_url}/.well-known/carddav", f"{self._base_url}/"):
            root = self._xml_request("PROPFIND", url, body, depth="0")
            href = _text(root.find(".//d:current-user-principal/d:href", NS))
            if href:
                return href
        raise NetworkError("iCloud CardDAV did not return current-user-principal.")

    def _addressbook_home_set(self, principal_href: str) -> str:
        body = """<?xml version="1.0" encoding="utf-8"?>
<d:propfind xmlns:d="DAV:" xmlns:card="urn:ietf:params:xml:ns:carddav">
  <d:prop><card:addressbook-home-set /></d:prop>
</d:propfind>"""
        root = self._xml_request("PROPFIND", self._absolute_url(principal_href), body, depth="0")
        href = _text(root.find(".//card:addressbook-home-set/d:href", NS))
        if not href:
            raise NetworkError("iCloud CardDAV did not return addressbook-home-set.")
        return href

    def _addressbook_resources(self, addressbook_url: str) -> list[_ContactResource]:
        body = """<?xml version="1.0" encoding="utf-8"?>
<card:addressbook-query xmlns:d="DAV:" xmlns:card="urn:ietf:params:xml:ns:carddav">
  <d:prop>
    <d:getetag />
    <card:address-data />
  </d:prop>
</card:addressbook-query>"""
        root = self._xml_request("REPORT", addressbook_url, body, depth="1")
        resources: list[_ContactResource] = []
        for response in root.findall("d:response", NS):
            href = _text(response.find("d:href", NS))
            vcard = _text(response.find(".//card:address-data", NS))
            if not href or not vcard:
                continue
            resources.append(
                _ContactResource(
                    href=self._absolute_url(href),
                    etag=_text(response.find(".//d:getetag", NS)),
                    vcard=vcard,
                )
            )
        return resources

    def _book_name_for_url(self, url: str) -> str:
        for book in self.list_addressbooks():
            if url.startswith(book.url.rstrip("/") + "/"):
                return book.name
        return "Contacts"

    def _xml_request(self, method: str, url: str, body: str, depth: str) -> ET.Element:
        response = self._request(
            method,
            url,
            data=body.encode("utf-8"),
            headers={
                "Depth": depth,
                "Content-Type": "application/xml; charset=utf-8",
                "Accept": "application/xml,text/xml,*/*",
            },
        )
        self._raise_for_status(response, url)
        try:
            return ET.fromstring(response.content)
        except ET.ParseError as exc:
            raise NetworkError(f"iCloud CardDAV returned invalid XML from {url}: {exc}") from exc

    def _request(self, method: str, url: str, **kwargs: object) -> niquests.Response:
        try:
            return self._session.request(method, url, timeout=self._timeout, allow_redirects=True, **kwargs)
        except niquests.RequestException as exc:
            raise NetworkError(f"Could not reach iCloud CardDAV at {url}: {exc}") from exc

    def _raise_for_status(self, response: niquests.Response, url: str) -> None:
        if response.status_code in {401, 403}:
            raise AuthenticationError(
                "iCloud Contacts authentication failed. Verify ICLOUD_USERNAME is "
                "your full Apple ID email and ICLOUD_APP_PASSWORD is a valid "
                "app-specific password."
            )
        if response.status_code >= 400:
            raise NetworkError(
                f"iCloud CardDAV request to {url} failed with HTTP {response.status_code}: "
                f"{response.text[:300]}"
            )

    def _absolute_url(self, href: str) -> str:
        return urljoin(self._base_url + "/", href)


def _parse_vcard(
    text: str,
    addressbook: str,
    href: str,
    etag: Optional[str],
    include_notes: bool,
) -> Contact:
    props = _vcard_properties(text)
    name_parts = _split_structured(_first_value(props, "N") or "")
    full_name = _first_value(props, "FN") or " ".join(part for part in [name_parts[1], name_parts[0]] if part) or "(No Name)"
    note = _first_value(props, "NOTE") if include_notes else None
    return Contact(
        id=href,
        addressbook=addressbook,
        full_name=full_name,
        family_name=name_parts[0] or None,
        given_name=name_parts[1] or None,
        nickname=_first_value(props, "NICKNAME"),
        organization=_first_org(props),
        title=_first_value(props, "TITLE"),
        emails=_fields(props, "EMAIL"),
        phones=_fields(props, "TEL", strip_tel_uri=True),
        addresses=_addresses(props),
        urls=_fields(props, "URL"),
        birthday=_first_value(props, "BDAY"),
        note=note,
        uid=_first_value(props, "UID"),
        etag=etag,
    )


def _vcard_properties(text: str) -> list[tuple[str, dict[str, list[str]], str]]:
    lines = _unfold_vcard_lines(text)
    props: list[tuple[str, dict[str, list[str]], str]] = []
    for line in lines:
        if not line or ":" not in line:
            continue
        head, raw_value = line.split(":", 1)
        parts = head.split(";")
        name = parts[0].upper()
        params: dict[str, list[str]] = {}
        for part in parts[1:]:
            if "=" in part:
                key, value = part.split("=", 1)
                params.setdefault(key.upper(), []).extend(v.strip('"') for v in value.split(","))
            else:
                params.setdefault("TYPE", []).append(part.strip('"'))
        props.append((name, params, _unescape(raw_value)))
    return props


def _unfold_vcard_lines(text: str) -> list[str]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines: list[str] = []
    for line in normalized.split("\n"):
        if line.startswith((" ", "\t")) and lines:
            lines[-1] += line[1:]
        else:
            lines.append(line)
    return lines


def _first_value(props: list[tuple[str, dict[str, list[str]], str]], name: str) -> Optional[str]:
    for prop_name, _, value in props:
        if prop_name == name and value:
            return value
    return None


def _first_org(props: list[tuple[str, dict[str, list[str]], str]]) -> Optional[str]:
    value = _first_value(props, "ORG")
    if not value:
        return None
    parts = [part for part in _split_structured(value) if part]
    return " / ".join(parts) if parts else value


def _fields(
    props: list[tuple[str, dict[str, list[str]], str]],
    name: str,
    strip_tel_uri: bool = False,
) -> list[ContactField]:
    fields: list[ContactField] = []
    for prop_name, params, value in props:
        if prop_name != name or not value:
            continue
        if strip_tel_uri and value.lower().startswith("tel:"):
            value = value[4:]
        fields.append(
            ContactField(
                value=value,
                types=[t.lower() for t in params.get("TYPE", []) if t],
                label=_first_param(params, "X-ABLABEL"),
            )
        )
    return fields


def _addresses(props: list[tuple[str, dict[str, list[str]], str]]) -> list[PostalAddress]:
    addresses: list[PostalAddress] = []
    for prop_name, params, value in props:
        if prop_name != "ADR" or not value:
            continue
        parts = _split_structured(value)
        padded = [*parts, *[""] * (7 - len(parts))]
        formatted = ", ".join(part for part in padded[:7] if part)
        addresses.append(
            PostalAddress(
                formatted=formatted,
                types=[t.lower() for t in params.get("TYPE", []) if t],
                po_box=padded[0] or None,
                extended=padded[1] or None,
                street=padded[2] or None,
                city=padded[3] or None,
                region=padded[4] or None,
                postal_code=padded[5] or None,
                country=padded[6] or None,
            )
        )
    return addresses


def _contact_matches(contact: Contact, needle: str) -> bool:
    haystack = [
        contact.full_name,
        contact.given_name or "",
        contact.family_name or "",
        contact.nickname or "",
        contact.organization or "",
        contact.title or "",
        *(field.value for field in contact.emails),
        *(field.value for field in contact.phones),
    ]
    return any(needle in value.casefold() for value in haystack if value)


def _split_structured(value: str) -> list[str]:
    return [_unescape(part).strip() for part in _split_escaped(value, ";")]


def _split_escaped(value: str, separator: str) -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    escaped = False
    for char in value:
        if escaped:
            current.append("\\" + char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == separator:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    parts.append("".join(current))
    return parts


def _unescape(value: str) -> str:
    return (
        value.replace("\\n", "\n")
        .replace("\\N", "\n")
        .replace("\\,", ",")
        .replace("\\;", ";")
        .replace("\\\\", "\\")
    )


def _first_param(params: dict[str, list[str]], name: str) -> Optional[str]:
    values = params.get(name)
    return values[0] if values else None


def _text(element: Optional[ET.Element]) -> Optional[str]:
    if element is None or element.text is None:
        return None
    return element.text.strip()


def _last_path_part(href: str) -> str:
    return href.rstrip("/").rsplit("/", 1)[-1] or "Contacts"


def _clamp_limit(limit: int) -> int:
    if limit < 1:
        raise ValueError("limit must be at least 1")
    return min(limit, 500)
