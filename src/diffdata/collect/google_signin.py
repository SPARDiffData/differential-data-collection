"""Signing in to Google for the Google Docs fetcher (COL-4), through Google's own file picker.

The producer signs in with their own Google account, on their own computer. Google's
Picker opens during sign-in, the producer picks the docs to share, and the sign-in can
reach only those (the `drive.file` scope). So Google enforces the opt-in, not just our
code. The token stays on this computer, and is sent only to Google.

Two files, in ~/.diffdata/ beside config.toml, outside any repo:
  google-client.json   the app's OAuth client: a "Desktop app" client from the Google
                       Cloud project, downloaded as JSON
  google-token.json    the sign-in, written by `sign_in()`

What this refuses (from the plan's must-refuse list on #52):
  2   any scope but drive.file, in a new token, a refreshed one, or a saved one
  5   keeping the sign-in inside a git repo, or a folder a cloud service syncs
  10  the token in any output or message, or sent anywhere but Google's sign-in server
The standard library does it all: a local page catches Google's answer, and PKCE keeps
the code useless to anyone who sees it.
"""

import base64
import hashlib
import http.server
import json
import os
import secrets
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections.abc import Callable
from pathlib import Path

from diffdata.common.config import user_config_file
from diffdata.common.paths import find_git_root, synced_by

SCOPE = "https://www.googleapis.com/auth/drive.file"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_DOC = "application/vnd.google-apps.document"
CLIENT_FILE = "google-client.json"
TOKEN_FILE = "google-token.json"
# How long to wait for the producer to finish signing in and picking, in seconds.
WAIT = 300
SIGN_IN_AGAIN = "Sign in again with: diffdata collect google-docs sign-in"


class SignInError(Exception):
    """The sign-in can't be made or used. The message never holds the token."""


def signin_home(home: Path | None = None) -> Path:
    """Where the sign-in lives: ~/.diffdata/, the folder config.toml is in. Refused if
    it's inside a git repo or a synced folder, where the token could leave this computer."""
    home = (home or user_config_file().parent).expanduser().resolve()
    if repo := find_git_root(home):
        raise SignInError(
            f"{home} is inside the git repository at {repo}. The Google sign-in must never "
            "be committed, so it can't be kept there."
        )
    if service := synced_by(home):
        raise SignInError(
            f"{home} looks like it's synced by {service}. The Google sign-in must stay on "
            "this computer, so it can't be kept there."
        )
    return home


def load_client(home: Path) -> dict:
    """The app's OAuth client, from google-client.json. It must be a Desktop app client."""
    path = home / CLIENT_FILE
    if not path.is_file():
        raise SignInError(
            f"No {CLIENT_FILE} in {home}. Download the Google Cloud project's Desktop app "
            f"client as JSON and save it there."
        )
    try:
        client = json.loads(path.read_text(encoding="utf-8")).get("installed") or {}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, AttributeError) as e:
        raise SignInError(f"{path} can't be read as a Google client file.") from e
    if not client.get("client_id"):
        raise SignInError(f"{path} isn't a Desktop app client: it has no installed.client_id.")
    return client


def sign_in_link(client: dict, redirect: str, state: str, challenge: str) -> str:
    """Google's sign-in page, asking for drive.file only and opening its Picker on Google
    Docs, so the producer picks the docs the sign-in can reach."""
    params = {
        "client_id": client["client_id"],
        "redirect_uri": redirect,
        "response_type": "code",
        "scope": SCOPE,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
        "trigger_onepick": "true",
        "allow_multiple": "true",
        "mimetypes": GOOGLE_DOC,
    }
    return f"{AUTH_URL}?{urllib.parse.urlencode(params)}"


def check_scope(token: dict) -> None:
    """Refuse a token that can reach anything beyond the picked docs (must-refuse item 2)."""
    if set(str(token.get("scope", "")).split()) != {SCOPE}:
        raise SignInError(f"The Google sign-in isn't limited to the picked docs. {SIGN_IN_AGAIN}")


def ask_for_token(opener: Callable, fields: dict) -> dict:
    """Send one form to Google's sign-in server, the only place this sends anything to."""
    request = urllib.request.Request(
        TOKEN_URL,
        data=urllib.parse.urlencode(fields).encode(),
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with opener(request, timeout=60) as response:
            token = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # Google's error body isn't shown: an error message never repeats what was sent.
        raise SignInError(f"Google refused the sign-in (HTTP {e.code}). {SIGN_IN_AGAIN}") from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise SignInError(f"Couldn't reach Google's sign-in server ({type(e).__name__}).") from None
    if not isinstance(token, dict) or not token.get("access_token"):
        raise SignInError(f"Google's answer held no sign-in. {SIGN_IN_AGAIN}")
    return token


def save_token(home: Path, token: dict, client: dict, now: float) -> Path:
    """Write the sign-in to google-token.json, readable only by this user where the system
    allows. A temporary file first, so a save cut short leaves the old one."""
    saved = {
        "access_token": token["access_token"],
        "refresh_token": token.get("refresh_token"),
        "scope": token["scope"],
        "expires_at": now + int(token.get("expires_in", 3600)) - 60,
        "client_id": client["client_id"],
    }
    home.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=home, prefix=".saving-")
    with os.fdopen(handle, "w", encoding="utf-8") as f:
        json.dump(saved, f)
    os.chmod(temporary, 0o600)
    path = home / TOKEN_FILE
    os.replace(temporary, path)
    return path


def wait_for_answer(server: http.server.HTTPServer, timeout: float) -> dict:
    """Google's answer: the one request the browser sends back to the local page."""
    answer: dict = {}

    class Page(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 (the name http.server calls)
            query = urllib.parse.urlparse(self.path).query
            answer.update({k: v[0] for k, v in urllib.parse.parse_qs(query).items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"<h3>diffdata: signed in. You can close this tab.</h3>")

        def log_message(self, *args):
            pass  # the request line holds the code, so it's never printed

    server.RequestHandlerClass = Page
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    thread.join(timeout)
    return answer


def sign_in(
    home: Path | None = None,
    opener: Callable = urllib.request.urlopen,
    browser: Callable[[str], object] = webbrowser.open,
    timeout: float = WAIT,
    now: Callable[[], float] = time.time,
) -> str:
    """Sign in through Google's Picker and save the token. Returns a report with counts only."""
    home = signin_home(home)
    client = load_client(home)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    challenge = challenge.rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    server = http.server.HTTPServer(("127.0.0.1", 0), http.server.BaseHTTPRequestHandler)
    try:
        redirect = f"http://127.0.0.1:{server.server_port}"
        link = sign_in_link(client, redirect, state, challenge)
        print(f"Opening Google's sign-in in your browser. If it doesn't open, visit:\n{link}")
        browser(link)
        answer = wait_for_answer(server, timeout)
    finally:
        server.server_close()
    if answer.get("state") != state:
        raise SignInError("The sign-in page's answer didn't match this sign-in, so it was refused.")
    if "code" not in answer:
        raise SignInError(f"The sign-in was cancelled or timed out. {SIGN_IN_AGAIN}")
    token = ask_for_token(
        opener,
        {
            "code": answer["code"],
            "client_id": client["client_id"],
            "client_secret": client.get("client_secret", ""),
            "redirect_uri": redirect,
            "grant_type": "authorization_code",
            "code_verifier": verifier,
        },
    )
    check_scope(token)
    path = save_token(home, token, client, now())
    picked = [i for i in answer.get("picked_file_ids", "").split(",") if i]
    return f"Signed in. Docs picked: {len(picked)}. The sign-in is saved in {path}."


def access_token(
    home: Path | None = None,
    opener: Callable = urllib.request.urlopen,
    now: Callable[[], float] = time.time,
) -> str:
    """The saved sign-in's access token, refreshed when it has run out. The scope is checked
    every time, so a token edited by hand or changed by Google is refused."""
    home = signin_home(home)
    path = home / TOKEN_FILE
    if not path.is_file():
        raise SignInError(f"Not signed in to Google yet. {SIGN_IN_AGAIN}")
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise SignInError(f"{path} can't be read. {SIGN_IN_AGAIN}") from e
    check_scope(saved)
    if now() < saved.get("expires_at", 0):
        return saved["access_token"]
    if not saved.get("refresh_token"):
        raise SignInError(f"The Google sign-in has run out. {SIGN_IN_AGAIN}")
    client = load_client(home)
    token = ask_for_token(
        opener,
        {
            "client_id": client["client_id"],
            "client_secret": client.get("client_secret", ""),
            "refresh_token": saved["refresh_token"],
            "grant_type": "refresh_token",
        },
    )
    token.setdefault("scope", saved["scope"])
    token.setdefault("refresh_token", saved["refresh_token"])
    check_scope(token)
    save_token(home, token, client, now())
    return token["access_token"]
