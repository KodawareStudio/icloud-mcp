"""Small OAuth provider for the remote MCP deployment.

The provider is intentionally stateless so it can run on serverless platforms:
client registrations, auth codes, access tokens, and refresh tokens are signed
with `MCP_OAUTH_SIGNING_SECRET` instead of stored in a database.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Any

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

OAUTH_SCOPE = "icloud"
ACCESS_TOKEN_TTL_SECONDS = 60 * 60
AUTH_CODE_TTL_SECONDS = 5 * 60
REFRESH_TOKEN_TTL_SECONDS = 30 * 24 * 60 * 60


def oauth_enabled() -> bool:
    return os.environ.get("MCP_OAUTH_ENABLED", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def public_base_url() -> str:
    base_url = os.environ.get("MCP_PUBLIC_BASE_URL", "").strip().rstrip("/")
    if not base_url:
        raise RuntimeError("MCP_PUBLIC_BASE_URL is required when MCP_OAUTH_ENABLED=1.")
    return base_url


class StatelessOAuthProvider(
    OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]
):
    def __init__(self, signing_secret: str) -> None:
        self.signing_secret = signing_secret.encode()

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        data = self._unsign(client_id, expected_type="client")
        if not data:
            return None
        client = OAuthClientInformationFull.model_validate(data["client"])
        client.client_id = client_id
        return client

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if client_info.scope is None:
            client_info.scope = OAUTH_SCOPE
        data = client_info.model_dump(mode="json", exclude_none=True)
        client_info.client_id = self._sign(
            {
                "type": "client",
                "client": data,
                "iat": int(time.time()),
            }
        )

    async def authorize(
        self,
        client: OAuthClientInformationFull,
        params: AuthorizationParams,
    ) -> str:
        request_token = self._sign(
            {
                "type": "auth_request",
                "client": client.model_dump(mode="json", exclude_none=True),
                "params": params.model_dump(mode="json", exclude_none=True),
                "exp": int(time.time()) + AUTH_CODE_TTL_SECONDS,
            }
        )
        return f"{public_base_url()}/oauth/confirm?request={request_token}"

    async def load_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: str,
    ) -> AuthorizationCode | None:
        data = self._unsign(authorization_code, expected_type="code")
        if not data:
            return None
        if data["client_id"] != client.client_id:
            return None
        return AuthorizationCode(
            code=authorization_code,
            scopes=data["scopes"],
            expires_at=data["exp"],
            client_id=data["client_id"],
            code_challenge=data["code_challenge"],
            redirect_uri=data["redirect_uri"],
            redirect_uri_provided_explicitly=data["redirect_uri_provided_explicitly"],
            resource=data.get("resource"),
        )

    async def exchange_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: AuthorizationCode,
    ) -> OAuthToken:
        return self._token_pair(client.client_id or "", authorization_code.scopes)

    async def load_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: str,
    ) -> RefreshToken | None:
        data = self._unsign(refresh_token, expected_type="refresh")
        if not data or data["client_id"] != client.client_id:
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=data["client_id"],
            scopes=data["scopes"],
            expires_at=data["exp"],
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        return self._token_pair(client.client_id or "", scopes)

    async def load_access_token(self, token: str) -> AccessToken | None:
        data = self._unsign(token, expected_type="access")
        if not data:
            return None
        return AccessToken(
            token=token,
            client_id=data["client_id"],
            scopes=data["scopes"],
            expires_at=data["exp"],
            resource=data.get("resource"),
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        return None

    def create_authorization_code(self, request_token: str) -> str | None:
        data = self._unsign(request_token, expected_type="auth_request")
        if not data:
            return None
        client = OAuthClientInformationFull.model_validate(data["client"])
        params = data["params"]
        return self._sign(
            {
                "type": "code",
                "client_id": client.client_id,
                "scopes": params.get("scopes") or [OAUTH_SCOPE],
                "code_challenge": params["code_challenge"],
                "redirect_uri": params["redirect_uri"],
                "redirect_uri_provided_explicitly": params[
                    "redirect_uri_provided_explicitly"
                ],
                "resource": params.get("resource"),
                "exp": int(time.time()) + AUTH_CODE_TTL_SECONDS,
                "jti": secrets.token_urlsafe(16),
            }
        )

    def redirect_uri_for_code(self, request_token: str, code: str) -> str | None:
        data = self._unsign(request_token, expected_type="auth_request")
        if not data:
            return None
        params = data["params"]
        return construct_redirect_uri(params["redirect_uri"], code=code, state=params.get("state"))

    def _token_pair(self, client_id: str, scopes: list[str]) -> OAuthToken:
        now = int(time.time())
        access_token = self._sign(
            {
                "type": "access",
                "client_id": client_id,
                "scopes": scopes,
                "exp": now + ACCESS_TOKEN_TTL_SECONDS,
                "jti": secrets.token_urlsafe(16),
            }
        )
        refresh_token = self._sign(
            {
                "type": "refresh",
                "client_id": client_id,
                "scopes": scopes,
                "exp": now + REFRESH_TOKEN_TTL_SECONDS,
                "jti": secrets.token_urlsafe(16),
            }
        )
        return OAuthToken(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=ACCESS_TOKEN_TTL_SECONDS,
            scope=" ".join(scopes),
        )

    def _sign(self, payload: dict[str, Any]) -> str:
        raw = _b64(json.dumps(payload, separators=(",", ":")).encode())
        signature = hmac.new(self.signing_secret, raw.encode(), hashlib.sha256).digest()
        return f"{raw}.{_b64(signature)}"

    def _unsign(self, token: str, expected_type: str) -> dict[str, Any] | None:
        try:
            raw, signature = token.rsplit(".", 1)
            expected = _b64(hmac.new(self.signing_secret, raw.encode(), hashlib.sha256).digest())
            if not hmac.compare_digest(signature, expected):
                return None
            data = json.loads(_unb64(raw))
        except Exception:
            return None
        if data.get("type") != expected_type:
            return None
        if exp := data.get("exp"):
            if int(exp) < int(time.time()):
                return None
        return data


def make_oauth_provider() -> StatelessOAuthProvider | None:
    if not oauth_enabled():
        return None
    signing_secret = os.environ.get("MCP_OAUTH_SIGNING_SECRET", "").strip()
    if len(signing_secret) < 32:
        raise RuntimeError("MCP_OAUTH_SIGNING_SECRET must be at least 32 characters.")
    if not os.environ.get("MCP_OAUTH_PASSWORD", "").strip():
        raise RuntimeError("MCP_OAUTH_PASSWORD is required when MCP_OAUTH_ENABLED=1.")
    return StatelessOAuthProvider(signing_secret)


def register_oauth_routes(mcp: Any, provider: StatelessOAuthProvider) -> None:
    @mcp.custom_route("/oauth/confirm", methods=["GET", "POST"], include_in_schema=False)
    async def confirm(request: Request) -> Response:
        if request.method == "GET":
            request_token = request.query_params.get("request", "")
            return HTMLResponse(_confirm_html(request_token))

        form = await request.form()
        request_token = str(form.get("request", ""))
        password = str(form.get("password", ""))
        expected_password = os.environ.get("MCP_OAUTH_PASSWORD", "")
        if not hmac.compare_digest(password.encode(), expected_password.encode()):
            return HTMLResponse(_confirm_html(request_token, error="Incorrect passphrase."), status_code=401)

        code = provider.create_authorization_code(request_token)
        if not code:
            return HTMLResponse("Authorization request expired. Please retry from ChatGPT.", status_code=400)

        redirect_uri = provider.redirect_uri_for_code(request_token, code)
        if not redirect_uri:
            return HTMLResponse("Invalid authorization request.", status_code=400)
        return RedirectResponse(redirect_uri, status_code=302)


def _confirm_html(request_token: str, error: str = "") -> str:
    error_html = f"<p class='error'>{_escape(error)}</p>" if error else ""
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Authorize iCloud MCP</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 0; min-height: 100vh; display: grid; place-items: center; background: #f7f4f8; color: #17131a; }}
    main {{ width: min(420px, calc(100vw - 32px)); }}
    h1 {{ font-size: 24px; margin: 0 0 12px; }}
    p {{ line-height: 1.5; color: #4d4652; }}
    form {{ display: grid; gap: 12px; margin-top: 20px; }}
    input, button {{ font: inherit; padding: 12px 14px; border-radius: 8px; border: 1px solid #cfc7d6; }}
    button {{ border-color: #17131a; background: #17131a; color: white; cursor: pointer; }}
    .error {{ color: #9f1239; }}
  </style>
</head>
<body>
  <main>
    <h1>Authorize iCloud MCP</h1>
    <p>Enter the private passphrase for this deployment to let ChatGPT connect to the iCloud MCP server.</p>
    {error_html}
    <form method="post">
      <input type="hidden" name="request" value="{_escape(request_token)}">
      <input type="password" name="password" autocomplete="current-password" placeholder="Passphrase" required autofocus>
      <button type="submit">Authorize</button>
    </form>
  </main>
</body>
</html>"""


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _unb64(value: str) -> bytes:
    padded = value + "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(padded.encode())


def _escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
