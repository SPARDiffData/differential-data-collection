"""Fetching the listed Google Docs: what's asked for, what's saved, and what's refused. A fake
Google that answers every request and records it; no network. Every value is made up."""

import http.client
import io
import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime

import pytest

from diffdata import cli
from diffdata.collect import google_doc, google_fetch, google_signin
from diffdata.collect.google_doc import read_fetches
from diffdata.collect.google_fetch import A_FOLDER, NO_ACCESS, NOT_A_DOC, NOT_FOUND, FetchError
from diffdata.common.config import Config
from diffdata.common.documents import document_dir, documents_dir

DOC = "FAKE-DOC-00000000000001"
OTHER = "FAKE-DOC-00000000000002"
UNLISTED = "FAKE-DOC-00000000000003"
TOKEN = "FAKE-ACCESS-TOKEN-0001"
TITLE = "Distinctive Made-Up Title"
TEXT = "Distinctive made-up text.\n"
NAME = "Alex Distinctive"
WHEN = datetime(2026, 10, 10, 9, 0, tzinfo=UTC)
LATER = datetime(2026, 10, 11, 9, 0, tzinfo=UTC)


def changelog(last: int = 2, skip: int | None = None) -> str:
    entries = []
    for rev in range(1, last + 1):
        if rev != skip:
            op = {"ty": "is", "ibi": rev, "s": TEXT[rev - 1]}
            entries.append([op, 1791500000000 + rev, "111", rev, "A", rev])
    return ")]}'\n" + json.dumps({"chunkedSnapshot": [], "changelog": entries})


def slice_of(text: str, query: dict) -> str:
    """The revisions a changelog request asks for, as Google answers it."""
    if not text.startswith(")]}'"):
        return text
    found = json.loads(text.removeprefix(")]}'\n"))
    wanted = range(int(query["start"]), int(query["end"]) + 1)
    found["changelog"] = [e for e in found["changelog"] if e[3] in wanted]
    return ")]}'\n" + json.dumps(found)


class FakeGoogle:
    """Answers like Drive and Docs from `docs`, by doc ID. Records every request. A doc's
    `fail` maps part of a URL to the HTTP status to answer it with."""

    def __init__(self, **docs):
        self.docs = docs
        self.requests = []
        self.fail: dict[str, list[int]] = {}

    def doc(self, doc_id: str = DOC, **changes) -> dict:
        return {
            "mimeType": google_fetch.GOOGLE_DOC,
            "modifiedTime": "2026-10-09T09:00:00.000Z",
            "comments": [
                {
                    "id": "c1",
                    "createdTime": "2026-10-09T09:00:00.000Z",
                    "author": {"displayName": NAME, "permissionId": "p1"},
                    "content": "A comment.",
                }
            ],
            "revisions": [{"id": "2", "modifiedTime": "2026-10-09T09:00:00.000Z"}],
            "changelog": changelog(),
            **changes,
        }

    def __call__(self, request, timeout=None):
        url = request.full_url
        self.requests.append(request)
        for part, statuses in self.fail.items():
            if part in url and statuses:
                status, body = statuses.pop(0), b"body"
                if isinstance(status, tuple):
                    status, body = status
                raise urllib.error.HTTPError(url, status, "no", {}, io.BytesIO(body))
        parts = urllib.parse.urlparse(url)
        query = dict(urllib.parse.parse_qsl(parts.query))
        doc_id = parts.path.split("/")[4 if "/drive/" in parts.path else 3]
        doc = self.docs.get(doc_id)
        if doc is None:
            raise urllib.error.HTTPError(url, 404, "no", {}, io.BytesIO(b"body"))
        if "/revisions/load" in parts.path:
            return io.BytesIO(slice_of(doc["changelog"], query).encode("utf-8"))
        if parts.path.endswith("/comments"):
            pages = doc.get("comment_pages")
            if pages:
                return io.BytesIO(json.dumps(pages[int(query.get("pageToken", 0))]).encode())
            return io.BytesIO(json.dumps({"comments": doc["comments"]}).encode())
        if parts.path.endswith("/revisions"):
            return io.BytesIO(json.dumps({"revisions": doc["revisions"]}).encode())
        if parts.path.endswith("/export"):
            return io.BytesIO(TEXT.encode("utf-8"))
        meta = {"id": doc_id, "name": TITLE, "createdTime": "2026-10-01T09:00:00.000Z"}
        return io.BytesIO(
            json.dumps(
                meta | {k: doc[k] for k in ("mimeType", "modifiedTime") if k in doc}
            ).encode()
        )

    def urls(self) -> list[str]:
        return [r.full_url for r in self.requests]


def config_for(tmp_path, *docs) -> Config:
    return Config(tmp_path, (), google_docs=docs, path=tmp_path / "config.toml")


def run(tmp_path, google, *docs, now=WHEN) -> str:
    config = config_for(tmp_path, *(docs or (DOC,)))
    return google_fetch.collect(config, opener=google, token=TOKEN, pause=lambda _: None, now=now)


def fetches(tmp_path, doc_id=DOC):
    found, _, _ = read_fetches(document_dir(tmp_path, "google-doc", doc_id))
    return found


def fake() -> FakeGoogle:
    google = FakeGoogle()
    google.docs[DOC] = google.doc()
    return google


def test_a_full_run_saves_the_raw_files_and_builds_the_doc(tmp_path):
    report = run(tmp_path, fake())
    (fetch,) = fetches(tmp_path)
    assert set(fetch.files) == {
        "metadata.json",
        "comments.json",
        "revisions.json",
        "export.txt",
        "changelog.json",
    }
    assert fetch.route == "google-login"
    # Saved as Google sent it, prefix and all.
    assert fetch.files["changelog.json"].read_text("utf-8").startswith(")]}'")
    assert "1 listed doc(s): 1 with new history" in report and "Built 1 doc(s)" in report
    folder = document_dir(tmp_path, "google-doc", DOC)
    assert (folder / "events.jsonl").is_file() and (folder / "comments.md").is_file()
    assert list((documents_dir(tmp_path) / "current").glob(f"*{DOC}.md"))


def test_an_unchanged_doc_gets_only_its_metadata_and_comments(tmp_path):
    google = fake()
    run(tmp_path, google)
    google.requests.clear()
    report = run(tmp_path, google, now=LATER)
    assert len(google.requests) == 2
    assert all("/revisions" not in u and "/export" not in u for u in google.urls())
    newest = fetches(tmp_path)[-1]
    assert set(newest.files) == {"metadata.json", "comments.json"}
    assert "1 unchanged" in report


def test_a_changed_doc_gets_its_history_again(tmp_path):
    google = fake()
    run(tmp_path, google)
    google.docs[DOC] |= {"modifiedTime": "2026-10-10T12:00:00.000Z", "changelog": changelog(3)}
    google.docs[DOC]["revisions"] = [{"id": "3", "modifiedTime": "2026-10-10T12:00:00.000Z"}]
    run(tmp_path, google, now=LATER)
    assert "changelog.json" in fetches(tmp_path)[-1].files


def test_a_list_drive_sends_in_pages_is_saved_a_page_a_file(tmp_path):
    google = fake()
    google.docs[DOC]["comment_pages"] = [
        {"comments": google.docs[DOC]["comments"], "nextPageToken": "1"},
        {"comments": []},
    ]
    run(tmp_path, google)
    assert {"comments.json", "comments-2.json"} <= set(fetches(tmp_path)[0].files)


def test_a_rate_limit_is_tried_again(tmp_path):
    google = fake()
    google.fail["/export"] = [429, 503]
    run(tmp_path, google)
    assert "export.txt" in fetches(tmp_path)[0].files


def test_a_rate_limit_drive_answers_as_403_isnt_no_access(tmp_path):
    google = fake()
    google.fail[f"files/{DOC}"] = [(403, b'{"error": {"reason": "userRateLimitExceeded"}}')]
    report = run(tmp_path, google)
    assert "changelog.json" in fetches(tmp_path)[0].files
    assert "1 with new history" in report


def test_a_rate_limit_that_never_lifts_fails_the_doc_and_isnt_no_access(tmp_path):
    google = fake()
    limited = (403, b'{"error": {"reason": "rateLimitExceeded"}}')
    google.fail[f"files/{DOC}"] = [limited] * google_fetch.ATTEMPTS
    report = run(tmp_path, google)
    assert "Entry 1: Google kept asking to wait." in report
    assert "0 couldn't be read" in report
    assert not (document_dir(tmp_path, "google-doc", DOC) / "raw").exists()


def test_a_connection_cut_while_reading_a_403_fails_that_doc_only(tmp_path):
    google = fake()
    google.docs[OTHER] = google.doc()

    class CutBody(io.BytesIO):
        def read(self, *args):
            raise http.client.IncompleteRead(b"partial")

    def cut(request, timeout=None):
        if request.full_url.split("?")[0].endswith(DOC):
            raise urllib.error.HTTPError(request.full_url, 403, "no", {}, CutBody())
        return google(request, timeout)

    report = google_fetch.collect(
        config_for(tmp_path, DOC, OTHER), opener=cut, token=TOKEN, pause=lambda _: None, now=WHEN
    )
    assert "Entry 1: Google kept asking to wait." in report
    assert fetches(tmp_path) == []
    assert "changelog.json" in fetches(tmp_path, OTHER)[0].files


def test_a_changelog_too_big_for_one_request_is_joined_from_chunks(tmp_path, monkeypatch):
    # The one changelog saved that Google didn't send whole, so it's checked by replaying it.
    monkeypatch.setattr(google_fetch, "CHUNK", 2)
    google = fake()
    google.docs[DOC] |= {"changelog": changelog(5), "revisions": [{"id": "5"}]}
    google.fail["start=1&end=5"] = [400]
    run(tmp_path, google)
    (fetch,) = fetches(tmp_path)
    joined = json.loads(fetch.files["changelog.json"].read_text("utf-8"))
    assert [entry[3] for entry in joined["changelog"]] == [1, 2, 3, 4, 5]
    counts = google_doc.build(tmp_path)
    assert counts["replay warnings"] == 0 and counts["failed"] == 0
    (clean,) = (documents_dir(tmp_path) / "current").glob(f"*{DOC}.md")
    assert TEXT[:5] in clean.read_text("utf-8")


def test_a_changelog_that_failed_is_tried_again_though_the_doc_hasnt_changed(tmp_path):
    google = fake()
    google.fail["/revisions/load"] = [403]
    report = run(tmp_path, google)
    assert "changelog.json" not in fetches(tmp_path)[0].files
    assert "no whole changelog, so it has no suggestions yet" in report
    report = run(tmp_path, google, now=LATER)
    assert "changelog.json" in fetches(tmp_path)[-1].files
    assert "1 with new history" in report


def test_a_changelog_that_keeps_failing_doesnt_grow_raw_until_the_doc_changes(tmp_path):
    google = fake()
    google.docs[DOC]["revisions"] = [
        {
            "id": "2",
            "exportLinks": {"text/plain": f"https://docs.google.com/document/d/{DOC}/export"},
        }
    ]
    google.fail["/revisions/load"] = [403] * 3
    run(tmp_path, google)
    first = fetches(tmp_path)[0]
    assert {"export.txt", "revisions.json", "revision-2.txt"} <= set(first.files)

    report = run(tmp_path, google, now=LATER)
    assert set(fetches(tmp_path)[-1].files) == {"metadata.json", "comments.json"}
    assert "no whole changelog, so it has no suggestions yet" in report

    google.docs[DOC]["modifiedTime"] = "2026-10-11T12:00:00.000Z"
    run(tmp_path, google, now=datetime(2026, 10, 12, 9, 0, tzinfo=UTC))
    assert "export.txt" in fetches(tmp_path)[-1].files
    assert "changelog.json" not in fetches(tmp_path)[-1].files


def test_a_full_disk_on_one_doc_leaves_the_next_doc_and_the_build_going(tmp_path, monkeypatch):
    google = fake()
    google.docs[OTHER] = google.doc()
    saving = google_fetch.save_fetch

    def full_disk(data, doc_id, *args):
        if doc_id == DOC:
            raise OSError(f"No space left, saving {data / doc_id}")
        return saving(data, doc_id, *args)

    monkeypatch.setattr(google_fetch, "save_fetch", full_disk)
    report = run(tmp_path, google, DOC, OTHER)
    assert "Entry 1: it couldn't be saved." in report and DOC not in report
    assert "changelog.json" in fetches(tmp_path, OTHER)[0].files
    assert "Built 1 doc(s)" in report


def test_a_connection_cut_mid_answer_fails_that_doc_only(tmp_path):
    google = fake()
    google.docs[OTHER] = google.doc()

    def cut(request, timeout=None):
        if f"{DOC}/export" in request.full_url:
            raise http.client.IncompleteRead(b"partial")
        return google(request, timeout)

    report = google_fetch.collect(
        config_for(tmp_path, DOC, OTHER), opener=cut, token=TOKEN, pause=lambda _: None, now=WHEN
    )
    assert "Entry 1: Couldn't reach Google (IncompleteRead)." in report
    assert "changelog.json" in fetches(tmp_path, OTHER)[0].files


def test_must_refuse_1_a_picked_doc_that_isnt_listed_gets_no_request(tmp_path):
    google = fake()
    google.docs[UNLISTED] = google.doc()  # picked: the token could reach it
    run(tmp_path, google, DOC)
    assert not any(UNLISTED in url for url in google.urls())
    assert not document_dir(tmp_path, "google-doc", UNLISTED).exists()


def test_must_refuse_3_every_request_is_a_get_to_google(tmp_path):
    google = fake()
    google.docs[OTHER] = google.doc()
    run(tmp_path, google, DOC, OTHER)
    assert {r.get_method() for r in google.requests} == {"GET"}
    assert {r.host for r in google.requests} == google_fetch.HOSTS
    assert all(r.data is None for r in google.requests)


def test_must_refuse_4_a_data_folder_in_a_repo_stops_before_signing_in_or_fetching(
    tmp_path, monkeypatch, capsys, no_real_config
):
    (tmp_path / ".git").mkdir()
    no_real_config.parent.mkdir(parents=True, exist_ok=True)
    no_real_config.write_text(f'[local]\ndata_dir = "{tmp_path.as_posix()}/data"\n', "utf-8")

    def never(*args, **kwargs):
        raise AssertionError("it went ahead")

    monkeypatch.setattr(google_signin, "sign_in", never)
    monkeypatch.setattr(google_fetch, "collect", never)
    assert cli.main(["collect", "google-docs", "sign-in"]) == 1
    assert cli.main(["collect", "google-docs"]) == 1
    assert "inside the git repository" in capsys.readouterr().err


def test_must_refuse_6_a_changelog_that_fails_halfway_leaves_no_changelog(tmp_path, monkeypatch):
    # One request fails, so it's asked for in chunks, and the second chunk fails too.
    monkeypatch.setattr(google_fetch, "CHUNK", 1)
    google = fake()
    link = f"https://docs.google.com/feeds/download/documents/export/Export?id={DOC}"
    google.docs[DOC]["revisions"] = [
        {"id": "2", "exportLinks": {"text/plain": link}, "modifiedTime": "2026-10-09T09:00:00Z"}
    ]
    google.fail["start=1&end=2"] = [400]
    google.fail["start=2&end=2"] = [400]
    original = FakeGoogle.__call__

    def with_export(self, request, timeout=None):
        if "/feeds/download/" in request.full_url:
            self.requests.append(request)
            return io.BytesIO(b"A revision's text.\n")
        return original(self, request, timeout)

    monkeypatch.setattr(FakeGoogle, "__call__", with_export)
    run(tmp_path, google)
    (fetch,) = fetches(tmp_path)
    assert "changelog.json" not in fetch.files
    assert fetch.files["revision-2.txt"].read_text("utf-8") == "A revision's text.\n"


@pytest.mark.parametrize(
    "partial",
    [changelog(3, skip=2), changelog(1), ")]}'\n" + json.dumps({"changelog": []}), "not json"],
    ids=["a gap", "cut short", "empty", "not JSON"],
)
def test_must_refuse_6_a_changelog_with_revisions_missing_isnt_saved(tmp_path, partial):
    google = fake()
    google.docs[DOC]["changelog"] = partial
    google.docs[DOC]["revisions"] = [{"id": "3"}]
    run(tmp_path, google)
    assert "changelog.json" not in fetches(tmp_path)[0].files


def test_must_refuse_7_output_never_shows_a_doc_even_when_one_fails(tmp_path, capsys):
    google = fake()
    google.docs[OTHER] = google.doc()
    google.fail[f"{OTHER}/comments"] = [400]
    report = run(tmp_path, google, DOC, OTHER, UNLISTED)
    printed = capsys.readouterr()
    shown = report + printed.out + printed.err
    for value in (DOC, OTHER, UNLISTED, TITLE, TEXT.strip(), NAME, "docs.google.com", "body"):
        assert value not in shown
    assert "Entry 2:" in report and "Entry 3:" in report


@pytest.mark.parametrize("status, reason", [(403, NO_ACCESS), (404, NOT_FOUND)])
def test_must_refuse_8_a_doc_the_login_cant_open_is_recorded_and_the_run_carries_on(
    tmp_path, status, reason
):
    google = fake()
    google.docs[OTHER] = google.doc()
    google.fail[f"files/{DOC}"] = [status]
    report = run(tmp_path, google, DOC, OTHER)
    (fetch,) = fetches(tmp_path)
    assert set(fetch.files) == {"access.json"}
    assert json.loads(fetch.files["access.json"].read_text("utf-8")) == {"reason": reason}
    assert "changelog.json" in fetches(tmp_path, OTHER)[0].files
    assert f"Entry 1: {reason}" in report


def test_a_listed_doc_that_wasnt_picked_is_recorded_once(tmp_path):
    # Google says 404 for a doc the sign-in can't reach. It's asked again on every run, but a
    # new fetch is saved only when the reason changes.
    google = FakeGoogle()
    run(tmp_path, google, DOC)
    run(tmp_path, google, DOC, now=LATER)
    assert len(fetches(tmp_path)) == 1


def test_must_refuse_9_only_the_listed_ids_are_ever_asked_for(tmp_path):
    google = fake()
    run(tmp_path, google)
    for url in google.urls():
        path = urllib.parse.urlparse(url).path
        assert path.startswith((f"/drive/v3/files/{DOC}", f"/document/d/{DOC}/"))


def test_must_refuse_10_the_token_never_shows_even_in_a_failing_run(tmp_path, capsys):
    google = fake()
    google.fail["/comments"] = [400]
    report = run(tmp_path, google)
    printed = capsys.readouterr()
    assert TOKEN not in report + printed.out + printed.err


def test_must_refuse_10_a_link_to_another_host_is_refused_before_its_sent(tmp_path):
    google = fake()
    link = "https://example.org/export"
    google.docs[DOC]["revisions"] = [{"id": "2", "exportLinks": {"text/plain": link}}]
    google.docs[DOC]["changelog"] = "not json"
    report = run(tmp_path, google)
    assert not any("example.org" in url for url in google.urls())
    # That revision's text is left out. What was fetched is still saved, and the run says so.
    (fetch,) = fetches(tmp_path)
    assert "export.txt" in fetch.files and "revision-2.txt" not in fetch.files
    assert "no whole changelog" in report


@pytest.mark.parametrize(
    "url",
    [
        "https://example.org/x",
        "http://www.googleapis.com/x",
        "https://www.googleapis.com.example.org/x",
        "https://evil@example.org/x",
        "http://doc-00.googleusercontent.com/x",
        "https://googleusercontent.com.example.org/x",
    ],
)
def test_must_refuse_10_a_redirect_to_another_host_is_refused(url):
    handler = google_fetch.SameHostRedirects()
    request = urllib.request.Request("https://www.googleapis.com/drive/v3/files/x")
    with pytest.raises(FetchError, match="somewhere other than Google"):
        handler.redirect_request(request, None, 302, "Found", {}, url)


def test_a_revision_exports_signed_link_is_followed_without_the_token():
    handler = google_fetch.SameHostRedirects()
    headers = {"Authorization": f"Bearer {TOKEN}"}
    request = urllib.request.Request("https://docs.google.com/feeds/x", headers=headers)
    url = "https://doc-00-0a-docstext.googleusercontent.com/export/y?sig=FAKE"
    followed = handler.redirect_request(request, None, 307, "Found", {}, url)
    assert followed.full_url == url
    assert not followed.has_header("Authorization")


def test_a_redirect_within_google_keeps_going():
    handler = google_fetch.SameHostRedirects()
    request = urllib.request.Request("https://www.googleapis.com/drive/v3/files/x")
    url = "https://docs.google.com/y"
    assert handler.redirect_request(request, None, 302, "Found", {}, url).full_url == url


@pytest.mark.parametrize(
    "mime_type, reason",
    [
        (google_fetch.FOLDER, A_FOLDER),
        ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", NOT_A_DOC),
        ("application/vnd.google-apps.spreadsheet", NOT_A_DOC),
    ],
)
def test_must_refuse_11_a_folder_or_other_file_gets_its_metadata_read_and_nothing_else(
    tmp_path, mime_type, reason
):
    google = fake()
    google.docs[DOC]["mimeType"] = mime_type
    report = run(tmp_path, google)
    (request,) = google.requests
    assert urllib.parse.urlparse(request.full_url).path == f"/drive/v3/files/{DOC}"
    (fetch,) = fetches(tmp_path)
    assert json.loads(fetch.files["access.json"].read_text("utf-8")) == {"reason": reason}
    assert "couldn't be read" in report and TITLE not in report


def test_a_sign_in_google_no_longer_accepts_stops_the_run(tmp_path):
    google = fake()
    google.fail[f"files/{DOC}"] = [401]
    with pytest.raises(google_signin.SignInError, match="sign-in"):
        run(tmp_path, google)
    assert fetches(tmp_path) == []


def test_a_failure_partway_saves_nothing_for_that_doc(tmp_path):
    google = fake()
    google.fail["/export"] = [500] * google_fetch.ATTEMPTS
    report = run(tmp_path, google)
    assert fetches(tmp_path) == []
    assert "Entry 1: Google answered HTTP 500. Nothing was saved for it." in report


def test_with_nothing_listed_nothing_is_fetched_or_signed_in(tmp_path, monkeypatch):
    def never():
        raise AssertionError("it asked for the sign-in")

    monkeypatch.setattr(google_signin, "access_token", never)
    report = google_fetch.collect(config_for(tmp_path), opener=FakeGoogle())
    assert "nothing was fetched" in report


def test_the_build_step_rebuilds_without_fetching(tmp_path, monkeypatch, no_real_config, capsys):
    run(tmp_path, fake())
    no_real_config.parent.mkdir(parents=True, exist_ok=True)
    no_real_config.write_text(f'[local]\ndata_dir = "{tmp_path.as_posix()}"\n', "utf-8")
    monkeypatch.setattr(google_fetch, "OPENER", None)
    assert cli.main(["collect", "google-docs", "build"]) == 0
    assert "Built 1 doc(s)" in capsys.readouterr().out


def test_a_saved_fetch_reads_back_in_the_reader(tmp_path):
    # What the fetcher saves is what the reader takes: one version, the comment, no failures.
    run(tmp_path, fake())
    counts = google_doc.build(tmp_path)
    assert counts["docs"] == 1 and counts["failed"] == 0 and counts["replay warnings"] == 0
