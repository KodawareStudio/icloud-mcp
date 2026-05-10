"""ASGI entrypoint for Vercel.

The regular CLI entrypoint in `src/icloud_mcp/server.py` is still used for
local stdio/SSE/HTTP runs. Vercel imports this module and serves `app` as an
ASGI application.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from icloud_mcp.server import mcp  # noqa: E402

app = mcp.streamable_http_app()
