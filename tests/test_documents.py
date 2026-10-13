"""The shared document format: raw, versions, the event log, comments and clean copies.
Synthetic data only."""

import json
import os
import re
from pathlib import Path

import pytest

from diffdata import __version__
from diffdata.common.documents import (
    Actor,
    Anchor,
    Document,
    DocumentError,
    Event,
    Version,
    document_dir,
    save_raw,
    write_document,
)

CANVAS = Document("slack-canvas", "F00000001", "Example plan", tags={"workspace": "example"})
GOOGLE = Document("google-doc", "FAKE-DOC-0001", "Example notes", link="https://example.com/d/1")
ALEX = Actor("U00000001", "Alex Example")
SAM = Actor("U00000002", "Sam Sample")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
COMMENT = "Is this <@U00000002>'s part?"


def day(n: int, hour: int = 12) -> str:
    return f"2026-10-0{n}T{hour:02}:00:00Z"


def show_names(text: str) -> str:
    return text.replace("<@U00000002>", "@Sam Sample")


def canvas_reads(data, texts):
    """One canvas read a day, saved raw, and the snapshots a reader would make of them."""
    versions = []
    for n, text in enumerate(texts, start=1):
        fetch = f"2026100{n}T120000Z"
        save_raw(data, CANVAS.source, CANVAS.id, fetch, "read.json", json.dumps({"text": text}))
        versions.append(Version(text, day(n), f"raw/{fetch}/read.json"))
    return versions


def google_fetches(data):
    """Two native revisions and one comment thread fetched on day 5, then a snapshot of the
    same text and the same comments again on day 6."""
    raws = []
    for fetch in ("20261005T120000Z", "20261006T120000Z"):
        save_raw(data, GOOGLE.source, GOOGLE.id, fetch, "fetch.json", f'{{"fetch": "{fetch}"}}')
        raws.append(f"raw/{fetch}/fetch.json")
    latest = "Draft two, by <@U00000002>\n"
    versions = [
        Version("Draft one\n", day(5), raws[0], "2026-10-01T04:00:00.250-05:00", ALEX, "rev-1"),
        Version(latest, day(5), raws[0], day(3, 9), SAM, "rev-2"),
        Version(latest, day(6), raws[1]),
    ]

    def seen(when, raw):  # in no particular order, as a reader might give them
        def event(what, at, actor, native_id, text=None, **more):
            return Event(what, when, raw, at, actor, native_id, text, **more)

        return [
            event("resolve", day(4, 10), ALEX, "c1-r2", parent="c1"),
            event("comment", day(2, 9), ALEX, "c1", COMMENT, anchor=Anchor("Draft one", "s1")),
            event(
                "suggestion", day(3, 10), SAM, "sg1", "three", kind="insert", anchor=Anchor("two")
            ),
            event("reply", day(3, 11), SAM, "c1-r1", "Yes, mine.", parent="c1"),
        ]

    return versions, seen(day(5), raws[0]) + seen(day(6), raws[1])


def read(data, document, name):
    return (document_dir(data, document.source, document.id) / name).read_text(encoding="utf-8")


def log(data, document):
    return [json.loads(line) for line in read(data, document, "events.jsonl").splitlines()]


def all_files(folder):
    return {
        p.relative_to(folder).as_posix(): p.read_bytes() for p in folder.rglob("*") if p.is_file()
    }


def test_a_canvas_snapshot_is_kept_only_when_its_text_changed(tmp_path):
    counts = write_document(tmp_path, CANVAS, canvas_reads(tmp_path, ["A\n", "A\n", "B\n", "A\n"]))
    folder = document_dir(tmp_path, CANVAS.source, CANVAS.id)
    files = sorted((folder / "versions").glob("*.md"))
    assert [p.read_text(encoding="utf-8") for p in files] == ["A\n", "B\n", "A\n"]
    events = log(tmp_path, CANVAS)
    assert [
        (e["version"], e["origin"], e["at"], e["seen_at"], e["not_before"]) for e in events
    ] == [
        (files[0].name, "snapshot", None, day(1), None),
        (files[1].name, "snapshot", None, day(3), day(2)),
        (files[2].name, "snapshot", None, day(4), day(3)),
    ]
    meta = json.loads(read(tmp_path, CANVAS, "meta.json"))
    assert (meta["last_changed"], meta["last_collected"], meta["workspace"]) == (
        day(4),
        day(4),
        "example",
    )
    assert counts == {"versions": 3, "events": 3, "written": 6, "removed": 0}


def test_google_revisions_and_comments_go_in_the_log_in_time_order(tmp_path):
    versions, events = google_fetches(tmp_path)
    write_document(tmp_path, GOOGLE, versions, events)
    entries = log(tmp_path, GOOGLE)
    assert [e["type"] for e in entries] == [
        "version",
        "comment",
        "version",
        "suggestion",
        "reply",
        "resolve",
    ]
    first, comment, second, suggestion, reply, resolve = entries
    assert (first["origin"], first["at"], first["native_id"], first["actor"]) == (
        "native",
        "2026-10-01T09:00:00Z",
        "rev-1",
        {"id": "U00000001", "name": "Alex Example"},
    )
    assert "not_before" not in first
    assert (comment["version"], reply["version"], resolve["parent"]) == (
        first["version"],
        second["version"],
        "c1",
    )
    assert comment["anchor"] == {"quote": "Draft one", "section": "s1"}
    assert (suggestion["kind"], suggestion["text"]) == ("insert", "three")
    assert all(e["seen_at"] == day(5) for e in entries)  # day 6 saw nothing new
    assert json.loads(read(tmp_path, GOOGLE, "meta.json")) == {
        "source": "google-doc",
        "id": "FAKE-DOC-0001",
        "title": "Example notes",
        "link": "https://example.com/d/1",
        "created_at": None,
        "last_changed": "2026-10-03T09:00:00Z",
        "last_collected": day(6),
        "versions": 2,
        "events": 6,
        "tool": f"diffdata {__version__}",
    }


def test_building_again_changes_nothing_and_raw_alone_rebuilds_the_rest(tmp_path):
    canvas = canvas_reads(tmp_path, ["A\n", "B\n"])
    versions, events = google_fetches(tmp_path)

    def build():
        return [
            write_document(tmp_path, CANVAS, canvas),
            write_document(tmp_path, GOOGLE, versions, events, show_names),
        ]

    build()
    before = all_files(tmp_path)
    assert [counts["written"] for counts in build()] == [0, 0]
    for name in before:
        if "/raw/" not in name:
            (tmp_path / name).unlink()
    build()
    assert all_files(tmp_path) == before


def test_the_clean_copy_has_a_header_and_the_latest_text_with_names(tmp_path):
    versions, events = google_fetches(tmp_path)
    write_document(tmp_path, GOOGLE, versions, events, show_names)
    current = tmp_path / "documents" / "current"
    assert [p.name for p in current.iterdir()] == ["google-doc_example-notes_FAKE-DOC-0001.md"]
    assert (current / "google-doc_example-notes_FAKE-DOC-0001.md").read_text(encoding="utf-8") == (
        "---\n"
        'title: "Example notes"\n'
        "source: google-doc\n"
        "id: FAKE-DOC-0001\n"
        "link: https://example.com/d/1\n"
        "last_changed: 2026-10-03T09:00:00Z\n"
        "collected_at: 2026-10-06T12:00:00Z\n"
        f"tool: diffdata {__version__}\n"
        "---\n"
        "\n"
        "Draft two, by @Sam Sample\n"
    )
    # The version keeps the text as fetched, so a renamed teammate doesn't make a new one.
    assert b"Draft two, by <@U00000002>\n" in all_files(tmp_path).values()

    renamed = Document(GOOGLE.source, GOOGLE.id, "Renamed: notes!", link=GOOGLE.link)
    assert write_document(tmp_path, renamed, versions, events)["removed"] == 1
    assert [p.name for p in current.iterdir()] == ["google-doc_renamed-notes_FAKE-DOC-0001.md"]


def test_comments_md_shows_each_thread_under_its_quoted_text(tmp_path):
    versions, events = google_fetches(tmp_path)
    write_document(tmp_path, GOOGLE, versions, events, show_names)
    assert read(tmp_path, GOOGLE, "comments.md") == (
        "# Comments: Example notes\n"
        "\n"
        "## Thread 1\n"
        "\n"
        "> Draft one\n"
        "\n"
        "**Alex Example** · 2026-10-02 09:00 UTC\n"
        "Is this @Sam Sample's part?\n"
        "\n"
        "**Sam Sample** · 2026-10-03 11:00 UTC · reply\n"
        "Yes, mine.\n"
        "\n"
        "Resolved by Alex Example · 2026-10-04 10:00 UTC\n"
    )
    current = next((tmp_path / "documents" / "current").iterdir()).read_text(encoding="utf-8")
    assert "Is this" not in current and "Yes, mine" not in current

    write_document(tmp_path, CANVAS, canvas_reads(tmp_path, ["A\n"]))
    assert not (document_dir(tmp_path, CANVAS.source, CANVAS.id) / "comments.md").exists()


def test_a_comment_with_no_time_still_leads_its_thread(tmp_path):
    raw = "raw/1/a.json"
    events = [
        Event("comment", day(5), raw, None, ALEX, "c1", "Whose part?", anchor=Anchor("Plan")),
        Event("reply", day(5), raw, day(3), SAM, "c1-r1", "Mine.", parent="c1"),
    ]
    write_document(tmp_path, CANVAS, [Version("Plan\n", day(1), raw)], events)
    assert read(tmp_path, CANVAS, "comments.md").endswith(
        "## Thread 1\n\n> Plan\n\n**Alex Example** · seen 2026-10-05 12:00 UTC\nWhose part?\n\n"
        "**Sam Sample** · 2026-10-03 12:00 UTC · reply\nMine.\n"
    )


def test_revisions_made_in_the_same_second_keep_their_order(tmp_path):
    def revision(text, ms):
        made = f"2026-10-01T09:00:00.{ms}Z"
        return Version(text, day(5), "raw/1/a.json", made, native_id=f"rev-{ms}")

    newest_first = [revision("D\n", 900), revision("A\n", 700), revision("B\n", 400)]
    write_document(tmp_path, GOOGLE, [*newest_first, revision("A\n", 100)])
    entries = log(tmp_path, GOOGLE)
    assert [e["native_id"] for e in entries] == ["rev-100", "rev-400", "rev-700", "rev-900"]
    # Text A comes back within the same second, and still gets its own file and event ID.
    assert len({e["version"] for e in entries}) == len({e["event_id"] for e in entries}) == 4
    current = next((tmp_path / "documents" / "current").iterdir()).read_text(encoding="utf-8")
    assert current.endswith("---\n\nD\n")


def test_a_raw_file_is_never_changed(tmp_path):
    path = save_raw(tmp_path, "slack-canvas", "F00000001", "fetch-1", "read.json", '{"n": 1}')
    assert (
        save_raw(tmp_path, "slack-canvas", "F00000001", "fetch-1", "read.json", '{"n": 1}') == path
    )
    with pytest.raises(DocumentError, match="never changed"):
        save_raw(tmp_path, "slack-canvas", "F00000001", "fetch-1", "read.json", '{"n": 2}')
    assert path.read_text(encoding="utf-8") == '{"n": 1}'


def test_a_raw_save_cut_short_leaves_nothing_and_never_replaces_another(tmp_path, monkeypatch):
    folder = document_dir(tmp_path, "slack-canvas", "F00000001") / "raw" / "fetch-1"
    real_link = os.link

    def cut_short(source, target):
        raise OSError("the disk is full")

    monkeypatch.setattr(os, "link", cut_short)
    with pytest.raises(OSError):
        save_raw(tmp_path, "slack-canvas", "F00000001", "fetch-1", "read.json", '{"n": 1}')
    assert list(folder.iterdir()) == []  # no partial file, and no temporary one

    def another_save_first(source, target):
        Path(target).write_text('{"n": 2}', encoding="utf-8")
        real_link(source, target)

    monkeypatch.setattr(os, "link", another_save_first)
    with pytest.raises(DocumentError, match="never changed"):
        save_raw(tmp_path, "slack-canvas", "F00000001", "fetch-1", "read.json", '{"n": 1}')
    assert [p.read_text(encoding="utf-8") for p in folder.iterdir()] == ['{"n": 2}']


def test_bad_input_is_refused_before_anything_is_written(tmp_path):
    raw = "raw/1/a.json"
    one = [Version("A\n", day(1), raw)]
    clashing = Document("slack-canvas", "F00000001", "Plan", tags={"title": "Other"})
    attempts = {
        "no time zone": lambda: write_document(
            tmp_path, CANVAS, [Version("A\n", "2026-10-01T12:00:00", raw)]
        ),
        "at least one version": lambda: write_document(tmp_path, CANVAS, []),
        "standard ones": lambda: write_document(tmp_path, clashing, one),
        "isn't an event type": lambda: write_document(
            tmp_path, CANVAS, one, [Event("like", day(1), raw)]
        ),
        # A source with _ could share a clean copy's name with another document.
        "can't name": lambda: save_raw(tmp_path, "slack_canvas", "F00000001", "1", "a.json", "{}"),
    }
    for message, attempt in attempts.items():
        with pytest.raises(DocumentError, match=message):
            attempt()
    assert list(tmp_path.rglob("*")) == []


@pytest.mark.parametrize("bad", ["..", "../F00000001", "a/b", "a\\b", ""])
def test_a_name_that_could_leave_the_documents_folder_is_refused(tmp_path, bad):
    data = tmp_path / "data"
    attempts = [
        lambda: save_raw(data, bad, "F00000001", "fetch-1", "read.json", "{}"),
        lambda: save_raw(data, "slack-canvas", bad, "fetch-1", "read.json", "{}"),
        lambda: save_raw(data, "slack-canvas", "F00000001", bad, "read.json", "{}"),
        lambda: save_raw(data, "slack-canvas", "F00000001", "fetch-1", bad, "{}"),
        lambda: write_document(
            data, Document("slack-canvas", bad, "Plan"), [Version("A\n", day(1), "raw/1/a.json")]
        ),
    ]
    for attempt in attempts:
        with pytest.raises(DocumentError):
            attempt()
    assert list(tmp_path.rglob("*")) == []


def test_no_email_in_an_actor_or_tag_reaches_the_log_or_meta(tmp_path):
    with pytest.raises(DocumentError, match="email"):
        Actor("U00000001", "Alex Example <alex@example.com>")
    with pytest.raises(DocumentError, match="email"):
        Document("slack-canvas", "F00000001", "Example plan", tags={"owner": "alex@example.com"})
    versions, events = google_fetches(tmp_path)
    write_document(tmp_path, GOOGLE, versions, events)
    for name in ("events.jsonl", "meta.json"):
        assert not EMAIL.search(read(tmp_path, GOOGLE, name))
