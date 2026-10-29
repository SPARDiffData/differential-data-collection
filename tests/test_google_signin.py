"""Signing in to Google through its Picker: what's asked for, where the sign-in is kept, and
what's refused. A fake Google and a fake browser; no network. Every value is made up."""

import io
import json
import threading
import urllib.error
import urllib.parse
import urllib.request

import pytest

from diffdata import cli
from diffdata.collect import google_signin
from diffdata.collect.google_signin import SCOPE, SignInError, access_token, sign_in

CLIENT = {"installed": {"client_id": "FAKE-CLIENT.apps.example", "client_secret": "FAKE-SECRET"}}
TOKEN = "FAKE-ACCESS-TOKEN-0001"
REFRESH = "FAKE-REFRESH-TOKEN-0001"
READONLY = "https://www.googleapis.com/auth/drive.readonly"


@pytest.fixture
def home(tmp_path):
    """~/.diffdata/ with a client file in it, outside any repo."""
    folder = tmp_path / "home" / ".diffdata"
    folder.mkdir(parents=True)
    (folder / "google-client.json").write_text(json.dumps(CLIENT), encoding="utf-8")
    return folder


class FakeGoogle:
    """Answers token requests with the given token, and records every request."""

    def __init__(self, token: dict | None = None, error: int | None = None):
        self.token = token if token is not None else {}
        self.error = error
        self.requests: list[urllib.request.Request] = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        if self.error:
            body = io.BytesIO(json.dumps({"error": "invalid_grant", "echo": TOKEN}).encode())
            raise urllib.error.HTTPError(request.full_url, self.error, "refused", {}, body)
        return io.BytesIO(json.dumps(self.token).encode())

    def form(self, n: int = -1) -> dict:
        return dict(urllib.parse.parse_qsl(self.requests[n].data.decode()))


def granted(scope: str = SCOPE) -> dict:
    return {"access_token": TOKEN, "refresh_token": REFRESH, "expires_in": 3600, "scope": scope}


def browser(answer: dict | None = None, state: str | None = None):
    """A fake browser: it reads the sign-in link, and sends Google's answer back to the local
    page, as Google does after the producer signs in and picks. Keeps the link it opened."""
    opened = []

    def open_link(link: str) -> None:
        opened.append(link)
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(link).query))
        reply = {"state": state or query["state"], **(answer or {})}
        url = f"{query['redirect_uri']}/?{urllib.parse.urlencode(reply)}"
        direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        threading.Thread(target=lambda: direct.open(url, timeout=10).read(), daemon=True).start()

    open_link.opened = opened
    return open_link


PICKED = {"code": "FAKE-CODE", "picked_file_ids": "FAKE-DOC-0001,FAKE-DOC-0002"}


def test_the_sign_in_link_asks_for_drive_file_only_and_opens_the_picker():
    link = google_signin.sign_in_link(
        CLIENT["installed"], "http://127.0.0.1:1", "FAKE-STATE", "FAKE-CHALLENGE"
    )
    assert link.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(link).query))
    assert query["scope"] == SCOPE
    assert query["trigger_onepick"] == "true"
    assert query["mimetypes"] == "application/vnd.google-apps.document"
    assert (query["code_challenge_method"], query["prompt"]) == ("S256", "consent")


def test_signing_in_saves_the_token_beside_config_and_reports_counts_only(home, capsys):
    google = FakeGoogle(granted())
    report = sign_in(home, opener=google, browser=browser(PICKED), timeout=10)
    assert report.startswith("Signed in. Docs picked: 2.")
    saved = json.loads((home / "google-token.json").read_text("utf-8"))
    assert (saved["access_token"], saved["scope"]) == (TOKEN, SCOPE)
    # One request in all: the code swapped for a token, at Google's sign-in server.
    (request,) = google.requests
    assert (request.get_method(), request.host) == ("POST", "oauth2.googleapis.com")
    assert google.form()["code"] == "FAKE-CODE" and google.form()["code_verifier"]
    # The token is never shown (item 10), and neither are the picked docs' IDs.
    shown = report + capsys.readouterr().out
    assert TOKEN not in shown and REFRESH not in shown and "FAKE-DOC-0001" not in shown


@pytest.mark.parametrize("scope", [READONLY, f"{SCOPE} {READONLY}", ""])
def test_a_token_that_reaches_more_than_the_picked_docs_is_refused(home, scope):
    # Must-refuse item 2: drive.file only, never alongside anything else.
    google = FakeGoogle(granted(scope))
    with pytest.raises(SignInError, match="isn't limited to the picked docs"):
        sign_in(home, opener=google, browser=browser(PICKED), timeout=10)
    assert not (home / "google-token.json").exists()


def test_an_answer_for_another_sign_in_is_refused_before_asking_for_a_token(home):
    google = FakeGoogle(granted())
    with pytest.raises(SignInError, match="didn't match"):
        sign_in(home, opener=google, browser=browser(PICKED, state="FAKE-OTHER"), timeout=10)
    assert google.requests == [] and not (home / "google-token.json").exists()


def test_a_cancelled_sign_in_saves_nothing(home):
    google = FakeGoogle(granted())
    with pytest.raises(SignInError, match="cancelled"):
        sign_in(home, opener=google, browser=browser({"error": "access_denied"}), timeout=10)
    assert google.requests == [] and not (home / "google-token.json").exists()


def test_a_refused_sign_in_never_shows_what_google_sent_back(home):
    # Item 10: Google's error body here even echoes the token, and it's left out.
    google = FakeGoogle(error=400)
    with pytest.raises(SignInError) as refused:
        sign_in(home, opener=google, browser=browser(PICKED), timeout=10)
    assert "HTTP 400" in str(refused.value) and TOKEN not in str(refused.value)


def test_the_sign_in_is_never_kept_inside_a_repo(tmp_path):
    # Must-refuse item 5: a home folder inside a git repo stops everything.
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".diffdata").mkdir()
    (repo / ".diffdata" / "google-client.json").write_text(json.dumps(CLIENT), "utf-8")
    opened, google = browser(PICKED), FakeGoogle(granted())
    with pytest.raises(SignInError, match="inside the git repository"):
        sign_in(repo / ".diffdata", opener=google, browser=opened, timeout=10)
    assert opened.opened == [] and google.requests == []
    assert not (repo / ".diffdata" / "google-token.json").exists()


def test_the_sign_in_is_never_kept_in_a_synced_folder(tmp_path):
    # Also item 5: a cloud service would copy the token off this computer.
    home = tmp_path / "OneDrive" / ".diffdata"
    home.mkdir(parents=True)
    (home / "google-client.json").write_text(json.dumps(CLIENT), "utf-8")
    opened, google = browser(PICKED), FakeGoogle(granted())
    with pytest.raises(SignInError, match="synced by"):
        sign_in(home, opener=google, browser=opened, timeout=10)
    assert opened.opened == [] and google.requests == []
    assert not (home / "google-token.json").exists()
    # A saved sign-in there isn't used either.
    with pytest.raises(SignInError, match="synced by"):
        access_token(home)


def test_a_missing_client_file_says_where_it_goes(tmp_path):
    with pytest.raises(SignInError, match="No google-client.json"):
        sign_in(tmp_path / ".diffdata", browser=browser(PICKED), timeout=10)


def saved(home, scope=SCOPE, expires_at=2000.0, refresh=REFRESH):
    token = {"access_token": TOKEN, "refresh_token": refresh, "scope": scope}
    (home / "google-token.json").write_text(json.dumps(token | {"expires_at": expires_at}))


def test_a_saved_sign_in_is_used_until_it_runs_out(home):
    saved(home)
    google = FakeGoogle()
    assert access_token(home, opener=google, now=lambda: 1000.0) == TOKEN
    assert google.requests == []


def test_a_run_out_sign_in_is_refreshed_and_still_checked(home):
    saved(home)
    google = FakeGoogle({"access_token": "FAKE-ACCESS-TOKEN-0002", "expires_in": 3600})
    assert access_token(home, opener=google, now=lambda: 3000.0) == "FAKE-ACCESS-TOKEN-0002"
    assert google.form()["grant_type"] == "refresh_token"
    stored = json.loads((home / "google-token.json").read_text("utf-8"))
    assert (stored["scope"], stored["refresh_token"]) == (SCOPE, REFRESH)
    # A refresh that comes back wider than drive.file is refused, and not saved.
    google = FakeGoogle({"access_token": "FAKE-ACCESS-TOKEN-0003", "scope": READONLY})
    with pytest.raises(SignInError, match="isn't limited"):
        access_token(home, opener=google, now=lambda: 9e9)
    assert json.loads((home / "google-token.json").read_text("utf-8"))["scope"] == SCOPE


def test_a_saved_sign_in_with_a_wider_scope_is_refused_before_any_request(home):
    saved(home, scope=READONLY)
    google = FakeGoogle(granted())
    with pytest.raises(SignInError, match="isn't limited"):
        access_token(home, opener=google, now=lambda: 3000.0)
    assert google.requests == []


def test_not_signed_in_yet_says_how(home):
    with pytest.raises(SignInError, match="diffdata collect google-docs sign-in"):
        access_token(home)


def test_the_command_reports_a_refusal_without_a_traceback(monkeypatch, capsys):
    def refuse():
        raise SignInError("No google-client.json in FAKE-HOME.")

    monkeypatch.setattr(google_signin, "sign_in", refuse)
    assert cli.main(["collect", "google-docs", "sign-in"]) == 1
    assert capsys.readouterr().err == "diffdata: No google-client.json in FAKE-HOME.\n"
