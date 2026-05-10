"""ASGI entrypoint for Vercel.

The regular CLI entrypoint in `src/icloud_mcp/server.py` is still used for
local stdio/SSE/HTTP runs. Vercel imports this module and serves `app` as an
ASGI application.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from icloud_mcp.server import _configure_http_settings, mcp  # noqa: E402

_configure_http_settings()

if vercel_url := os.environ.get("VERCEL_URL"):
    if vercel_url not in mcp.settings.transport_security.allowed_hosts:
        mcp.settings.transport_security.allowed_hosts.append(vercel_url)

for origin in ("https://chatgpt.com", "https://chat.openai.com"):
    if origin not in mcp.settings.transport_security.allowed_origins:
        mcp.settings.transport_security.allowed_origins.append(origin)

app = mcp.streamable_http_app()
