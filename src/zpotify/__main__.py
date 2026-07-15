"""Entry point: `zpotify` runs the app; subcommands: auth, doctor, demo.

The first run walks through setup in plain terminal mode (client id, Spotify
login, librespot sign-in) before the TUI takes over the screen.
"""

from __future__ import annotations

import sys
import time

from zpotify import config as cfg
from zpotify.auth import Auth, AuthError


def main() -> int:
    args = sys.argv[1:]
    if args[:1] == ["auth"]:
        return cmd_auth()
    if args[:1] == ["doctor"]:
        return cmd_doctor()
    if args[:1] == ["demo"]:
        from zpotify.term.demo import main as demo_main
        demo_main()
        return 0
    if args:
        print("usage: zpotify [auth|doctor|demo]")
        return 2
    return cmd_run()


def cmd_run() -> int:
    config = cfg.Config.load()
    auth = Auth(config)
    if not _setup(config, auth):
        return 1
    from zpotify.ui.app import App
    App(config, auth).run()
    return 0


def _setup(config: cfg.Config, auth: Auth) -> bool:
    """First-run wizard: client id -> Spotify login -> librespot credentials."""
    from zpotify.player.librespot import Librespot, find_librespot

    if find_librespot() is None:
        print("librespot not found. Install it with:  brew install librespot")
        return False

    if not config.client_id:
        print("── zpotify setup ─────────────────────────────────────────────")
        print("zpotify needs a (free) Spotify developer app of your own:")
        print("  1. open https://developer.spotify.com/dashboard")
        print("  2. Create app — any name; check the 'Web API' box")
        print(f"  3. add Redirect URI exactly:  {cfg.REDIRECT_URI}")
        print("  4. paste its Client ID here")
        try:
            client_id = input("Client ID: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        if not client_id:
            print("no client id — aborting")
            return False
        config.client_id = client_id
        config.save()

    if not auth.logged_in:
        print("Opening your browser for Spotify login…")
        try:
            auth.login_interactive(on_url=lambda url: print(f"If it didn't open: {url}"))
        except AuthError as exc:
            print(f"login failed: {exc}")
            return False
        print("Logged in.")

    librespot = Librespot()
    if not librespot.credentials_cached:
        print("One-time librespot (player) sign-in…")
        if not _librespot_signin(librespot):
            return False
    return True


def _librespot_signin(librespot) -> bool:
    """Run librespot until it caches credentials via its OAuth flow.

    librespot 0.8 opens the browser itself and prints the authorize URL to
    *stdout* (the same pipe that later carries PCM — harmless here, since no
    audio can flow before authentication). We sniff stdout for the URL as a
    fallback so the user can open it by hand if the browser didn't launch.
    """
    import threading

    url_seen: list[str] = []

    def announce(url: str) -> None:
        if url and not url_seen:
            url_seen.append(url)
            print(f"If the browser didn't open, sign in here:\n  {url}")

    def on_event(event) -> None:
        if event.kind == "auth_url":
            announce(event.data.get("url", ""))

    def sniff_stdout() -> None:
        stream = librespot.stdout
        if stream is None:
            return
        buffer = b""
        while True:
            try:
                chunk = stream.readline()
            except (OSError, ValueError):
                return
            if not chunk:
                return
            buffer = chunk
            text = buffer.decode("utf-8", "replace")
            start = text.find("https://accounts.spotify.com/")
            if start != -1:
                announce(text[start:].split()[0].strip())
                return

    librespot.on_event = on_event
    librespot.start()
    threading.Thread(target=sniff_stdout, daemon=True).start()
    print("Waiting for player sign-in (a browser window may appear)…")
    try:
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            if librespot.credentials_cached:
                print("Player authorized.")
                return True
            if not librespot.running:
                print("librespot exited before completing sign-in. Log tail:")
                for line in list(librespot.stderr_tail)[-8:]:
                    print(f"  {line}")
                return False
            time.sleep(0.5)
        print("Timed out waiting for librespot sign-in.")
        return False
    finally:
        librespot.stop()


def cmd_auth() -> int:
    config = cfg.Config.load()
    if not config.client_id:
        print("no client id configured — run `zpotify` first")
        return 1
    auth = Auth(config)
    auth.logout()
    try:
        auth.login_interactive(on_url=lambda url: print(f"If the browser didn't open: {url}"))
    except AuthError as exc:
        print(f"login failed: {exc}")
        return 1
    print("Logged in.")
    return 0


def cmd_doctor() -> int:
    from zpotify.player.librespot import Librespot, find_librespot
    config = cfg.Config.load()
    auth = Auth(config)
    ok = True

    binary = find_librespot()
    print(f"librespot        {'ok  ' + binary if binary else 'MISSING — brew install librespot'}")
    ok &= binary is not None

    print(f"client id        {'ok' if config.client_id else 'MISSING — run `zpotify`'}")
    ok &= bool(config.client_id)

    if auth.logged_in:
        try:
            auth.get_access_token()
            print("spotify login    ok")
        except AuthError as exc:
            print(f"spotify login    BROKEN — {exc} (run `zpotify auth`)")
            ok = False
    else:
        print("spotify login    MISSING — run `zpotify auth`")
        ok = False

    print(f"player creds     {'ok' if Librespot().credentials_cached else 'missing (first run will set up)'}")

    try:
        import sounddevice
        device = sounddevice.query_devices(kind="output")
        print(f"audio output     ok  {device['name']}")
    except Exception as exc:
        print(f"audio output     BROKEN — {exc}")
        ok = False

    if auth.logged_in and config.client_id:
        try:
            from zpotify.api import SpotifyAPI
            me = SpotifyAPI(auth).me()
            product = me.get("product", "?")
            name = me.get("display_name") or me.get("id")
            note = "" if product == "premium" else "  (Premium required for playback!)"
            print(f"account          ok  {name} [{product}]{note}")
            ok &= product == "premium"
        except Exception as exc:
            print(f"account          BROKEN — {exc}")
            ok = False

    print("all good — run `zpotify`" if ok else "fix the above, then re-run `zpotify doctor`")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
