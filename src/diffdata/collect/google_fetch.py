"""Fetch the Google Docs listed in config.toml (COL-4, COL-5), with the producer's own login.

`diffdata collect google-docs sign-in` signs in through Google's Picker (google_signin.py),
so the sign-in reaches only the docs the producer picked. This file fetches each doc that's
listed in `google_docs` too, saves what it got unchanged with `save_fetch()`, and then
`google_doc.build()` does the rest. A doc must be listed and picked: a listed doc that
wasn't picked looks like one the login can't open.

Every run asks for each doc's metadata and comments. It asks for revisions, the changelog
and the text export only when Drive says the doc changed since the last fetch that saved
its history. A revision's own text is fetched only when the whole changelog can't be, as
the reader's fallback history.

What this refuses (from the plan's must-refuse list on #52):
  1   a doc that isn't listed, even if it was picked: only listed IDs are ever asked for
  3   any request that could change a doc: everything here is a GET
  6   a partial changelog: the whole of it from revision 1, or no changelog.json
  7   showing doc content: the report gives counts, and a doc's place in the list
  8   a doc the login can't open: its reason is saved (COL-7), and the run carries on
  9   listing or searching the Drive: there's no such request here
  10  the token going anywhere but Google, by a link or a redirect
  11  a folder, or anything that isn't a Google Doc: refused from its metadata alone
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from diffdata.collect import google_doc, google_signin
from diffdata.collect.google_doc import XSSI_PREFIX, Fetch, GoogleDocError, read_fetches, save_fetch
from diffdata.common.config import Config
from diffdata.common.documents import DocumentError, document_dir, exact

DRIVE = "https://www.googleapis.com/drive/v3"
CHANGELOG = "https://docs.google.com/document/d/{id}/revisions/load?id={id}&start={start}&end={end}"
# The only hosts a token is ever sent to, by this file. Sign-in has its own, in google_signin.
HOSTS = {"www.googleapis.com", "docs.google.com"}
GOOGLE_DOC = google_signin.GOOGLE_DOC
FOLDER = "application/vnd.google-apps.folder"
ROUTE = "google-login"
# Only what the reader uses, so a fetch keeps no more about people than it needs.
PERSON = "displayName,permissionId"
METADATA_FIELDS = "id,name,mimeType,createdTime,modifiedTime"
REVISION_FIELDS = (
    f"nextPageToken,revisions(id,modifiedTime,lastModifyingUser({PERSON}),exportLinks)"
)
COMMENT_FIELDS = (
    f"nextPageToken,comments(id,createdTime,author({PERSON}),content,quotedFileContent,"
    f"replies(id,createdTime,author({PERSON}),content,action))"
)
# Google's rate limits and passing errors, which are worth another try.
RETRY = (429, 502, 503, 504)
ATTEMPTS = 4
CHUNK = 2000
NO_ACCESS, NOT_FOUND = "no access", "not found"
NOT_A_DOC, A_FOLDER = "not a Google Doc", "a folder"


class FetchError(Exception):
    """A request can't be made or failed. Its message holds no URL, ID or response body."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class SameHostRedirects(urllib.request.HTTPRedirectHandler):
    """urllib sends a request's headers on to wherever a redirect points, token included,
    so a redirect is followed only to a host the token may go to."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_host(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


OPENER = urllib.request.build_opener(SameHostRedirects).open


def check_host(url: str) -> None:
    """Refuse a request to anywhere but Google, before the token is sent (must-refuse 10)."""
    parts = urllib.parse.urlparse(url)
    if parts.scheme != "https" or parts.hostname not in HOSTS:
        raise FetchError(
            "A request would have gone somewhere other than Google, so it was refused."
        )


class Google:
    """Read-only requests with the sign-in's token. Each one is a GET to an allowed host."""

    def __init__(self, token: str, opener: Callable = OPENER, pause: Callable = time.sleep):
        self.token, self.opener, self.pause = token, opener, pause

    def get(self, url: str, **params) -> str:
        """The response's text, as Google sent it."""
        check_host(url)
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        for attempt in range(ATTEMPTS):
            request = urllib.request.Request(
                url, method="GET", headers={"Authorization": f"Bearer {self.token}"}
            )
            try:
                with self.opener(request, timeout=120) as response:
                    return response.read().decode("utf-8-sig")
            except urllib.error.HTTPError as e:
                if e.code in RETRY and attempt < ATTEMPTS - 1:
                    self.pause(2**attempt)
                    continue
                raise FetchError(f"Google answered HTTP {e.code}.", e.code) from None
            except (urllib.error.URLError, OSError, ValueError) as e:
                raise FetchError(f"Couldn't reach Google ({type(e).__name__}).") from None
        raise FetchError("Google kept asking to wait.")

    def pages(self, url: str, **params) -> list[str]:
        """Each page of a Drive list, as sent, following nextPageToken."""
        found, token = [], None
        while True:
            page = self.get(url, **params, **({"pageToken": token} if token else {}))
            found.append(page)
            token = parse(page).get("nextPageToken")
            if not token:
                return found


def parse(text: str) -> dict:
    """A response's JSON, which must be an object."""
    try:
        found = json.loads(text.removeprefix(XSSI_PREFIX))
    except ValueError:
        raise FetchError("Google's answer wasn't the JSON it should be.") from None
    if not isinstance(found, dict):
        raise FetchError("Google's answer wasn't the JSON it should be.")
    return found


def saved_pages(stem: str, pages: list[str]) -> dict[str, str]:
    """Page files as the reader takes them: comments.json, comments-2.json and so on."""
    names = [f"{stem}.json", *(f"{stem}-{n}.json" for n in range(2, len(pages) + 1))]
    return dict(zip(names, pages, strict=True))


def complete(changelog: dict, last: int) -> bool:
    """Whether a changelog holds every revision from 1 to `last`, each once and in order: the
    reader replays only the newest one, from an empty doc (must-refuse 6)."""
    entries = changelog.get("changelog")
    if not isinstance(entries, list) or not entries:
        return False
    if not all(isinstance(e, list) and len(e) > 3 for e in entries):
        return False
    return [e[3] for e in entries] == list(range(1, last + 1))


def whole_changelog(google: Google, doc_id: str, last: int) -> str | None:
    """The changelog from revision 1 to `last`, or None if all of it can't be had. In one
    request as Google sends it, else in chunks, joined in Google's own shape."""
    url = CHANGELOG.format(id=doc_id, start=1, end=last)
    try:
        text = google.get(url)
        if complete(parse(text), last):
            return text
    except FetchError as e:
        if e.status in (401, 403):
            return None
    merged: dict = {"changelog": [], "chunkedSnapshot": None}
    try:
        for start in range(1, last + 1, CHUNK):
            end = min(start + CHUNK - 1, last)
            part = parse(google.get(CHANGELOG.format(id=doc_id, start=start, end=end)))
            if merged["chunkedSnapshot"] is None:
                merged["chunkedSnapshot"] = part.get("chunkedSnapshot")
            merged["changelog"] += part.get("changelog") or []
    except FetchError:
        return None
    return json.dumps(merged, ensure_ascii=False) if complete(merged, last) else None


def last_history(folder: Path) -> str | None:
    """Drive's modified time when this doc's history was last saved, or None if it wasn't."""
    fetches, _, _ = read_fetches(folder)
    with_history: list[Fetch] = [
        f for f in fetches if {"changelog.json", "export.txt"} & set(f.files)
    ]
    for fetch in reversed(with_history):
        try:
            modified = (fetch.json("metadata.json") or {}).get("modifiedTime")
        except (OSError, UnicodeDecodeError, ValueError, AttributeError):
            continue
        if modified:
            return modified
    return None


def last_reason(folder: Path) -> str | None:
    """The reason in this doc's newest fetch, if that fetch only says it couldn't be read."""
    fetches, _, _ = read_fetches(folder)
    if not fetches or "access.json" not in fetches[-1].files:
        return None
    try:
        return (fetches[-1].json("access.json") or {}).get("reason")
    except (OSError, UnicodeDecodeError, ValueError, AttributeError):
        return None


def changed_since(modified: str | None, before: str | None) -> bool:
    if not modified or not before:
        return True
    try:
        return exact(modified) > exact(before)
    except DocumentError:
        return True


def fetch_doc(google: Google, data: Path, doc_id: str, now: datetime) -> str:
    """Fetch one listed doc and save it. Returns what happened: "new", "unchanged", or the
    reason it couldn't be read, which is saved too (COL-7)."""
    folder = document_dir(data, google_doc.SOURCE, doc_id)
    fetch_id = now.strftime("%Y%m%dT%H%M%SZ")
    at = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    def refused(reason: str) -> str:
        if last_reason(folder) != reason:  # a new fetch only when something changed
            access = json.dumps({"reason": reason}, indent=2) + "\n"
            save_fetch(data, doc_id, fetch_id, at, ROUTE, {"access.json": access})
        return reason

    try:
        text = google.get(f"{DRIVE}/files/{doc_id}", fields=METADATA_FIELDS)
    except FetchError as e:
        if e.status == 403:
            return refused(NO_ACCESS)
        if e.status == 404:  # also what Google says for a doc that wasn't picked
            return refused(NOT_FOUND)
        raise
    metadata = parse(text)
    # Checked before anything else is read: approving a folder isn't approving its files.
    if metadata.get("mimeType") == FOLDER:
        return refused(A_FOLDER)
    if metadata.get("mimeType") != GOOGLE_DOC:
        return refused(NOT_A_DOC)

    files = {"metadata.json": text}
    comments = google.pages(f"{DRIVE}/files/{doc_id}/comments", fields=COMMENT_FIELDS, pageSize=100)
    files |= saved_pages("comments", comments)
    changed = changed_since(metadata.get("modifiedTime"), last_history(folder))
    if changed:
        pages = google.pages(
            f"{DRIVE}/files/{doc_id}/revisions", fields=REVISION_FIELDS, pageSize=1000
        )
        files |= saved_pages("revisions", pages)
        revisions = [r for page in pages for r in parse(page).get("revisions") or []]
        files["export.txt"] = google.get(f"{DRIVE}/files/{doc_id}/export", mimeType="text/plain")
        numbers = [int(r["id"]) for r in revisions if str(r.get("id", "")).isdigit()]
        changelog = whole_changelog(google, doc_id, max(numbers, default=1))
        if changelog is not None:
            files["changelog.json"] = changelog
        else:
            files |= revision_texts(google, revisions)
    save_fetch(data, doc_id, fetch_id, at, ROUTE, files)
    return "new" if changed else "unchanged"


def revision_texts(google: Google, revisions: list[dict]) -> dict[str, str]:
    """Each revision's text, the reader's history when there's no whole changelog."""
    found = {}
    for revision in revisions:
        link = (revision.get("exportLinks") or {}).get("text/plain")
        if link and str(revision.get("id", "")).isalnum():
            found[f"revision-{revision['id']}.txt"] = google.get(link)
    return found


def collect(
    config: Config,
    opener: Callable = OPENER,
    token: str | None = None,
    pause: Callable = time.sleep,
    now: datetime | None = None,
) -> str:
    """Fetch every listed doc, then build. Returns the report: counts, and places in the list."""
    if not config.google_docs:
        return f"No docs are listed under google_docs in {config.path}, so nothing was fetched."
    google = Google(token or google_signin.access_token(), opener, pause)
    now = now or datetime.now(UTC)
    outcomes: Counter = Counter()
    problems = []
    for number, doc_id in enumerate(config.google_docs, start=1):
        try:
            outcome = fetch_doc(google, config.data_dir, doc_id, now)
        except FetchError as e:
            if e.status == 401:  # the sign-in itself, so every other doc would fail too
                raise google_signin.SignInError(
                    f"Google didn't accept the sign-in. {google_signin.SIGN_IN_AGAIN}"
                ) from None
            outcomes["failed"] += 1
            problems.append(f"Entry {number}: {e} Nothing was saved for it. Run this again.")
            continue
        except (GoogleDocError, DocumentError):
            outcomes["failed"] += 1
            problems.append(f"Entry {number}: it couldn't be saved.")
            continue
        outcomes[outcome] += 1
        if outcome not in ("new", "unchanged"):
            problems.append(
                f"Entry {number}: {outcome}. If it's yours to share, pick it at sign-in."
            )
    report = [
        f"{len(config.google_docs)} listed doc(s): {outcomes['new']} with new history, "
        f"{outcomes['unchanged']} unchanged, {outcomes['failed']} failed, "
        f"{sum(n for k, n in outcomes.items() if k not in ('new', 'unchanged', 'failed'))} "
        "couldn't be read.",
        *problems,
    ]
    return "\n".join([*report, build_report(config)])


def build_report(config: Config) -> str:
    """Write every fetched doc in the shared format. Returns counts only."""
    counts = google_doc.build(config.data_dir)
    lines = [
        f"Built {counts['docs']} doc(s): {counts['versions']} version(s), {counts['events']} "
        f"event(s), {counts['written']} file(s) written."
    ]
    if counts["replay warnings"]:
        lines.append(
            f"{counts['replay warnings']} replay warning(s): check those docs' versions by eye."
        )
    other = {"failed": "failed to build", "skipped": "skipped", "unfinished fetches": "unfinished"}
    lines += [f"{counts[key]} {name}." for key, name in other.items() if counts[key]]
    return "\n".join(lines)
