"""OAuth 2.0 Authorization Code with PKCE, hand-rolled against urllib/http.server.

No third-party HTTP libraries: token exchange goes through urllib.request and
the local redirect catcher is a one-shot http.server.HTTPServer.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from typing import Callable

from zpotify.config import REDIRECT_PORT, REDIRECT_URI, SCOPES, Config, clear_tokens, read_tokens, write_tokens

AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"

# Refresh a bit before actual expiry so a token never goes stale mid-request.
EXPIRY_SLACK_SECONDS = 60

_CALLBACK_PAGE = """\
<!doctype html>
<html><head><meta charset="utf-8"><title>zpotify</title>
<style>
  body {{
    background: #121212; color: #e6e6e6; font-family: -apple-system, sans-serif;
    display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0;
  }}
  div {{ text-align: center; }}
  h1 {{ color: #1db954; font-weight: 600; }}
</style></head>
<body><div><h1>zpotify</h1><p>{message}</p><p>You can close this tab.</p></div></body></html>
"""


class AuthError(Exception):
    """Raised when the OAuth flow or a token request fails."""


class NeedsLogin(AuthError):
    """Raised when there is no valid token and interactive login is required."""


def generate_verifier() -> str:
    """Return a random PKCE code verifier (64 url-safe characters)."""
    return secrets.token_urlsafe(64)[:64]


def challenge_for(verifier: str) -> str:
    """Derive the S256 PKCE code challenge for a given verifier."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


class _CallbackResult:
    """Shared box the callback handler drops its outcome into."""

    def __init__(self) -> None:
        self.code: str | None = None
        self.error: str | None = None
        self.state: str | None = None
        self.event = threading.Event()


def _make_handler(result: _CallbackResult) -> type[http.server.BaseHTTPRequestHandler]:
    class CallbackHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server API
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != "/callback":
                self.send_response(404)
                self.end_headers()
                return
            qs = urllib.parse.parse_qs(parsed.query)
            result.code = qs.get("code", [None])[0]
            result.error = qs.get("error", [None])[0]
            result.state = qs.get("state", [None])[0]

            if result.error:
                message = f"Login failed: {result.error}"
            else:
                message = "Login complete."
            body = _CALLBACK_PAGE.format(message=message).encode("utf-8")

            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            result.event.set()

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            pass  # a TUI may own stdout/stderr; stay silent

    return CallbackHandler


class Auth:
    """Manages PKCE login and access-token refresh for a single Spotify user."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._lock = threading.Lock()

    @property
    def logged_in(self) -> bool:
        """Whether tokens exist on disk (not necessarily still valid)."""
        return read_tokens() is not None

    def get_access_token(self, force: bool = False) -> str:
        """Return a currently-valid access token, refreshing if needed.

        Pass force=True to refresh unconditionally (e.g. after the API layer
        sees a 401 despite a cache that looked fresh). Thread-safe: safe to
        call concurrently from UI worker threads.
        """
        with self._lock:
            tokens = read_tokens()
            if tokens is None:
                raise NeedsLogin("no tokens on disk; call login_interactive() first")
            if not force and tokens.get("expires_at", 0) - time.time() > EXPIRY_SLACK_SECONDS:
                return tokens["access_token"]
            return self._refresh(tokens)

    def login_interactive(
        self,
        open_browser: bool = True,
        on_url: Callable[[str], None] | None = None,
    ) -> None:
        """Run the full PKCE authorization flow and persist the resulting tokens."""
        verifier = generate_verifier()
        challenge = challenge_for(verifier)
        state = secrets.token_urlsafe(16)

        params = {
            "client_id": self._config.client_id,
            "response_type": "code",
            "redirect_uri": REDIRECT_URI,
            "scope": SCOPES,
            "code_challenge_method": "S256",
            "code_challenge": challenge,
            "state": state,
        }
        url = f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"

        if on_url is not None:
            on_url(url)
        if open_browser:
            webbrowser.open(url)

        result = _CallbackResult()
        server = http.server.HTTPServer(("127.0.0.1", REDIRECT_PORT), _make_handler(result))
        server.timeout = 300

        server_thread = threading.Thread(target=server.handle_request, daemon=True)
        server_thread.start()
        finished = result.event.wait(timeout=300)
        server.server_close()

        if not finished:
            raise AuthError("timed out waiting for the Spotify login redirect")
        if result.error:
            raise AuthError(f"Spotify authorization failed: {result.error}")
        if result.state != state:
            raise AuthError("OAuth state mismatch; possible CSRF, aborting")
        if not result.code:
            raise AuthError("no authorization code in callback")

        tokens = self._token_request(
            {
                "grant_type": "authorization_code",
                "code": result.code,
                "redirect_uri": REDIRECT_URI,
                "client_id": self._config.client_id,
                "code_verifier": verifier,
            }
        )
        self._store(tokens)

    def logout(self) -> None:
        """Forget stored tokens."""
        clear_tokens()

    def _refresh(self, tokens: dict) -> str:
        """Exchange a refresh token for a new access token; persist the result."""
        refresh_token = tokens.get("refresh_token")
        if not refresh_token:
            clear_tokens()
            raise NeedsLogin("no refresh token available")

        try:
            new_tokens = self._token_request(
                {
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": self._config.client_id,
                }
            )
        except AuthError as exc:
            if "invalid_grant" in str(exc):
                clear_tokens()
                raise NeedsLogin("refresh token expired or revoked; login again") from exc
            raise

        # Spotify may omit refresh_token on rotation-less responses; keep the old one.
        new_tokens.setdefault("refresh_token", refresh_token)
        self._store(new_tokens)
        return new_tokens["access_token"]

    def _store(self, tokens: dict) -> None:
        expires_in = tokens.get("expires_in", 3600)
        record = {
            "access_token": tokens["access_token"],
            "refresh_token": tokens.get("refresh_token", ""),
            "expires_at": time.time() + float(expires_in),
            "scope": tokens.get("scope", ""),
            "token_type": tokens.get("token_type", "Bearer"),
        }
        write_tokens(record)

    def _token_request(self, data: dict) -> dict:
        """POST to the token endpoint and return the parsed JSON response."""
        body = urllib.parse.urlencode(data).encode("ascii")
        req = urllib.request.Request(
            TOKEN_URL,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            message = _error_message(exc)
            raise AuthError(f"token request failed ({exc.code}): {message}") from exc
        except urllib.error.URLError as exc:
            raise AuthError(f"token request failed: {exc.reason}") from exc


def _error_message(exc: urllib.error.HTTPError) -> str:
    """Best-effort extraction of an error description from a token-endpoint HTTPError."""
    try:
        payload = json.loads(exc.read().decode("utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return exc.reason or "unknown error"
    error = payload.get("error", "unknown_error")
    description = payload.get("error_description", "")
    return f"{error}: {description}" if description else error
