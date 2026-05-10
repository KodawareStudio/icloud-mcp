"""iCloud MCP server entrypoint.

Run as a module:
    uv run python -m icloud_mcp.server

Or via the installed script:
    uv run icloud-mcp
"""
from __future__ import annotations

import logging
import os
import sys
from typing import Literal

from mcp.server.fastmcp import FastMCP
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from icloud_mcp.calendar.client import ICloudCalendarClient
from icloud_mcp.calendar.tools import register_calendar_tools
from icloud_mcp.config import config
from icloud_mcp.mail.client import ICloudMailClient
from icloud_mcp.mail.tools import register_mail_tools
from icloud_mcp.oauth import (
    OAUTH_SCOPE,
    make_oauth_provider,
    oauth_enabled,
    public_base_url,
    register_oauth_routes,
)
from icloud_mcp.workflows.tools import register_workflow_tools

# Log to stderr so it doesn't pollute the MCP stdio channel
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,
)

oauth_provider = make_oauth_provider()
auth_settings = None
if oauth_enabled():
    base_url = public_base_url()
    auth_settings = AuthSettings(
        issuer_url=base_url,
        resource_server_url=f"{base_url}/mcp",
        required_scopes=[OAUTH_SCOPE],
        client_registration_options=ClientRegistrationOptions(
            enabled=True,
            valid_scopes=[OAUTH_SCOPE],
            default_scopes=[OAUTH_SCOPE],
        ),
    )

mcp = FastMCP(
    "icloud",
    auth=auth_settings,
    auth_server_provider=oauth_provider,
)
if oauth_provider is not None:
    register_oauth_routes(mcp, oauth_provider)


@mcp.custom_route("/health", methods=["GET"], include_in_schema=False)
async def health(_: Request) -> Response:
    return JSONResponse(
        {
            "ok": True,
            "oauth_enabled": oauth_enabled(),
            "auth_configured": auth_settings is not None,
            "read_only": config.read_only,
        }
    )

# Construct clients once and share across subsystems so workflow tools can
# reuse the same connections (and the calendar principal cache).
calendar_client = ICloudCalendarClient(
    username=config.icloud_username,
    password=config.icloud_app_password,
    user_emails=config.all_user_emails,
)
mail_client = ICloudMailClient(
    username=config.icloud_username,
    password=config.icloud_app_password,
    user_emails=config.all_user_emails,
)

register_calendar_tools(mcp, config, calendar_client)
register_mail_tools(mcp, config, mail_client)
register_workflow_tools(mcp, config, calendar_client, mail_client)


Transport = Literal["stdio", "sse", "streamable-http"]


def _transport_from_env() -> Transport:
    transport = os.environ.get("MCP_TRANSPORT", "stdio").strip().lower()
    if transport not in {"stdio", "sse", "streamable-http"}:
        raise ValueError(
            "MCP_TRANSPORT must be one of: stdio, sse, streamable-http "
            f"(got {transport!r})."
        )
    return transport  # type: ignore[return-value]


def _configure_http_settings() -> None:
    """Apply HTTP/SSE hosting settings from env before FastMCP starts."""
    mcp.settings.host = os.environ.get("MCP_HOST", mcp.settings.host)

    port = os.environ.get("MCP_PORT") or os.environ.get("PORT")
    if port:
        mcp.settings.port = int(port)

    if allowed_hosts := os.environ.get("MCP_ALLOWED_HOSTS"):
        mcp.settings.transport_security.allowed_hosts = [
            item.strip() for item in allowed_hosts.split(",") if item.strip()
        ]

    if allowed_origins := os.environ.get("MCP_ALLOWED_ORIGINS"):
        mcp.settings.transport_security.allowed_origins = [
            item.strip() for item in allowed_origins.split(",") if item.strip()
        ]


def main() -> None:
    transport = _transport_from_env()
    if transport != "stdio":
        _configure_http_settings()
    mcp.run(transport=transport)


if __name__ == "__main__":
    main()
