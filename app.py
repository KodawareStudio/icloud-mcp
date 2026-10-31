"""ASGI entrypoint for Vercel.

The regular CLI entrypoint in `src/icloud_mcp/server.py` is still used for
local stdio/SSE/HTTP runs. Vercel imports this module and serves `app` as an
ASGI application.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from icloud_mcp.server import _configure_http_settings, mcp  # noqa: E402

_configure_http_settings()

# Accept the per-deployment host, the stable production host, and whatever
# host MCP_PUBLIC_BASE_URL points at, so the public URL is never rejected.
hosts = [
    os.environ.get("VERCEL_URL"),
    os.environ.get("VERCEL_PROJECT_PRODUCTION_URL"),
    urlparse(os.environ.get("MCP_PUBLIC_BASE_URL", "").strip()).netloc,
]
for host in filter(None, hosts):
    if host not in mcp.settings.transport_security.allowed_hosts:
        mcp.settings.transport_security.allowed_hosts.append(host)

for origin in (
    "https://chatgpt.com",
    "https://chat.openai.com",
    "https://claude.ai",
    "https://claude.com",
):
    if origin not in mcp.settings.transport_security.allowed_origins:
        mcp.settings.transport_security.allowed_origins.append(origin)

app = mcp.streamable_http_app()
