"""MCP tool registrations for iCloud Contacts."""
from __future__ import annotations

from typing import Any, Optional

from mcp.types import ToolAnnotations

from icloud_mcp.config import Config
from icloud_mcp.contacts.client import ICloudContactsClient

READ = ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=True)


def register_contacts_tools(
    mcp: Any,
    config: Config,
    client: Optional[ICloudContactsClient] = None,
) -> None:
    """Attach contacts tools to a FastMCP server instance."""
    if client is None:
        client = ICloudContactsClient(
            username=config.icloud_username,
            password=config.icloud_app_password,
        )

    @mcp.tool(annotations=READ)
    def list_contact_addressbooks() -> list[dict]:
        """List all iCloud Contacts CardDAV address books on the account."""
        return [book.model_dump() for book in client.list_addressbooks()]

    @mcp.tool(annotations=READ)
    def list_contacts(
        addressbook_name: Optional[str] = None,
        limit: int = 50,
        include_notes: bool = False,
    ) -> list[dict]:
        """List contacts from iCloud Contacts.

        Args:
            addressbook_name: Optional exact address book name. If omitted,
                contacts from all discovered address books are returned.
            limit: Max contacts to return, 1-500. Default 50.
            include_notes: Include contact notes. Default false because notes can
                contain private free-form context.
        """
        contacts = client.list_contacts(
            addressbook_name=addressbook_name,
            limit=limit,
            include_notes=include_notes,
        )
        return [contact.model_dump(mode="json") for contact in contacts]

    @mcp.tool(annotations=READ)
    def search_contacts(
        query: str,
        addressbook_name: Optional[str] = None,
        limit: int = 25,
        include_notes: bool = False,
    ) -> list[dict]:
        """Search iCloud Contacts by name, organization, email, or phone.

        Search is client-side after fetching vCards from CardDAV.
        """
        contacts = client.search_contacts(
            query=query,
            addressbook_name=addressbook_name,
            limit=limit,
            include_notes=include_notes,
        )
        return [contact.model_dump(mode="json") for contact in contacts]

    @mcp.tool(annotations=READ)
    def get_contact(contact_id: str, include_raw_vcard: bool = False) -> dict:
        """Fetch one iCloud contact by `id` from list_contacts/search_contacts.

        Args:
            contact_id: CardDAV resource URL returned as `id`.
            include_raw_vcard: Include raw vCard text. Default false.
        """
        return client.get_contact(
            contact_id,
            include_raw_vcard=include_raw_vcard,
        ).model_dump(mode="json")
