"""Tests for zpotify.auth — PKCE math, token persistence, refresh, and login flow.

No real network calls: the login flow test only talks to the local loopback
callback server, and all token-endpoint interactions are monkeypatched.
"""

from __future__ import annotations

import base64
import hashlib
import threading
import time
import urllib.parse
import urllib.request

import pytest

import zpotify.config as config
from zpotify.auth import Auth, AuthError, NeedsLogin, challenge_for, generate_verifier
from zpotify.config import Config, read_tokens, write_tokens


@pytest.fixture
def tmp_config(tmp_path, monkeypatch):
    """Point config.py's on-disk paths at a scratch dir for this test."""
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(config, "TOKENS_FILE", tmp_path / "tokens.json")
    return tmp_path


# -- PKCE math ----------------------------------------------------------------------


def test_challenge_matches_rfc7636_style_vector():
    # RFC 7636 appendix B example verifier/challenge pair.
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
    expected = expected.rstrip(b"=").decode("ascii")
    assert challenge_for(verifier) == expected
    assert expected == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_generate_verifier_is_url_safe_and_random():
    v1 = generate_verifier()
    v2 = generate_verifier()
    assert v1 != v2
    assert 43 <= len(v1) <= 128
    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
    assert set(v1) <= allowed


# -- token store round-trip ----------------------------------------------------------


def test_token_store_round_trip(tmp_config):
    tokens = {
        "access_token": "AT1",
        "refresh_token": "RT1",
        "expires_at": time.time() + 3600,
        "scope": "user-read-private",
        "token_type": "Bearer",
    }
    write_tokens(tokens)
    assert read_tokens() == tokens
    assert (tmp_config / "tokens.json").exists()


# -- expiry / refresh logic ----------------------------------------------------------


def test_get_access_token_returns_cached_when_fresh(tmp_config):
    write_tokens(
        {
            "access_token": "FRESH",
            "refresh_token": "RT",
            "expires_at": time.time() + 3600,
            "scope": "",
            "token_type": "Bearer",
        }
    )
    a = Auth(Config(client_id="cid"))
    assert a.get_access_token() == "FRESH"


def test_expired_token_triggers_refresh(tmp_config, monkeypatch):
    write_tokens(
        {
            "access_token": "STALE",
            "refresh_token": "RT-old",
            "expires_at": time.time() - 10,  # already expired
            "scope": "",
            "token_type": "Bearer",
        }
    )
    calls = []

    def fake_token_request(self, data):
        calls.append(data)
        return {
            "access_token": "NEW",
            "refresh_token": "RT-new",
            "expires_in": 3600,
            "scope": "user-read-private",
            "token_type": "Bearer",
        }

    monkeypatch.setattr(Auth, "_token_request", fake_token_request)
    a = Auth(Config(client_id="cid"))
    token = a.get_access_token()

    assert token == "NEW"
    assert len(calls) == 1
    assert calls[0]["grant_type"] == "refresh_token"
    assert calls[0]["refresh_token"] == "RT-old"

    stored = read_tokens()
    assert stored["access_token"] == "NEW"
    assert stored["refresh_token"] == "RT-new"  # rotated refresh token persisted
    assert stored["expires_at"] > time.time()


def test_refresh_keeps_old_refresh_token_when_not_rotated(tmp_config, monkeypatch):
    write_tokens(
        {
            "access_token": "STALE",
            "refresh_token": "RT-keep",
            "expires_at": time.time() - 10,
            "scope": "",
            "token_type": "Bearer",
        }
    )

    def fake_token_request(self, data):
        return {"access_token": "NEW", "expires_in": 3600}  # no refresh_token in response

    monkeypatch.setattr(Auth, "_token_request", fake_token_request)
    a = Auth(Config(client_id="cid"))
    a.get_access_token()

    assert read_tokens()["refresh_token"] == "RT-keep"


def test_force_refresh_bypasses_fresh_cache(tmp_config, monkeypatch):
    write_tokens(
        {
            "access_token": "FRESH",
            "refresh_token": "RT",
            "expires_at": time.time() + 3600,
            "scope": "",
            "token_type": "Bearer",
        }
    )
    calls = []

    def fake_token_request(self, data):
        calls.append(data)
        return {"access_token": "FORCED", "refresh_token": "RT", "expires_in": 3600}

    monkeypatch.setattr(Auth, "_token_request", fake_token_request)
    a = Auth(Config(client_id="cid"))
    token = a.get_access_token(force=True)

    assert token == "FORCED"
    assert len(calls) == 1


# -- invalid_grant -> NeedsLogin -------------------------------------------------------


def test_invalid_grant_raises_needs_login_and_clears_tokens(tmp_config, monkeypatch):
    write_tokens(
        {
            "access_token": "STALE",
            "refresh_token": "RT-dead",
            "expires_at": time.time() - 10,
            "scope": "",
            "token_type": "Bearer",
        }
    )

    def fake_token_request(self, data):
        raise AuthError("token request failed (400): invalid_grant: Refresh token revoked")

    monkeypatch.setattr(Auth, "_token_request", fake_token_request)
    a = Auth(Config(client_id="cid"))

    with pytest.raises(NeedsLogin):
        a.get_access_token()

    assert read_tokens() is None


def test_no_tokens_on_disk_raises_needs_login(tmp_config):
    a = Auth(Config(client_id="cid"))
    with pytest.raises(NeedsLogin):
        a.get_access_token()


def test_no_refresh_token_raises_needs_login_and_clears(tmp_config):
    write_tokens(
        {
            "access_token": "STALE",
            "refresh_token": "",
            "expires_at": time.time() - 10,
            "scope": "",
            "token_type": "Bearer",
        }
    )
    a = Auth(Config(client_id="cid"))
    with pytest.raises(NeedsLogin):
        a.get_access_token()
    assert read_tokens() is None


# -- full interactive login flow (loopback only, no real network) ---------------------


def test_login_interactive_completes_via_local_callback(tmp_config, monkeypatch):
    a = Auth(Config(client_id="cid"))
    captured = {}

    def fake_token_request(self, data):
        captured["exchange"] = data
        return {
            "access_token": "AT-login",
            "refresh_token": "RT-login",
            "expires_in": 3600,
            "scope": "user-read-private",
            "token_type": "Bearer",
        }

    monkeypatch.setattr(Auth, "_token_request", fake_token_request)

    def on_url(url: str) -> None:
        captured["url"] = url
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        state = qs["state"][0]

        def hit_callback():
            time.sleep(0.05)
            cb = f"http://127.0.0.1:8898/callback?code=abc123&state={state}"
            with urllib.request.urlopen(cb, timeout=5) as resp:
                assert resp.status == 200

        threading.Thread(target=hit_callback, daemon=True).start()

    a.login_interactive(open_browser=False, on_url=on_url)

    assert captured["exchange"]["code"] == "abc123"
    assert captured["exchange"]["grant_type"] == "authorization_code"
    assert "code_verifier" in captured["exchange"]
    assert read_tokens()["access_token"] == "AT-login"


def test_login_interactive_state_mismatch_raises(tmp_config, monkeypatch):
    a = Auth(Config(client_id="cid"))

    def on_url(url: str) -> None:
        def hit_callback():
            time.sleep(0.05)
            cb = "http://127.0.0.1:8898/callback?code=abc123&state=wrong-state"
            urllib.request.urlopen(cb, timeout=5)

        threading.Thread(target=hit_callback, daemon=True).start()

    with pytest.raises(AuthError):
        a.login_interactive(open_browser=False, on_url=on_url)

    assert read_tokens() is None
