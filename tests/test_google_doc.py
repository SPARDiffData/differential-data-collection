"""Google Docs reader: every way of fetching a doc ends up in the shared document format.
Synthetic data only."""

import json
import re

import pytest

from diffdata.collect.google_doc import GoogleDocError, build, save_fetch
from diffdata.common.documents import document_dir, documents_dir, exact

DOC = "FAKE-DOC-0001"
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
FIRST = "Draft one\nMore.\n"
SECOND = FIRST + "Edit by B.\n"
FINAL = SECOND + "Done.\n"


def ms(when: str) -> int:
    return int(exact(when).timestamp() * 1000)


def entry(op: dict, when: str, user: str, rev: int, session: str) -> list:
    return [op, ms(when), user, rev, session, rev]


def changelog() -> str:
    """Session A writes two lines. Session B adds a line, suggests "Very early " in two
    fragments and suggests deleting "Draft", then comes back three hours later."""
    entries = [
        entry({"ty": "is", "ibi": 1, "s": "Draft one\n"}, "2026-10-05T10:00:00Z", "111", 1, "A"),
        entry({"ty": "is", "ibi": 11, "s": "More.\n"}, "2026-10-05T10:01:00Z", "111", 2, "A"),
        entry({"ty": "is", "ibi": 17, "s": "Edit by B.\n"}, "2026-10-05T12:00:00Z", "222", 3, "B"),
        entry(
            {"ty": "mlti", "mts": [{"ty": "iss", "ibi": 1, "s": "Very ", "sugid": "sug-1"}]},
            "2026-10-05T12:01:00Z",
            "222",
            4,
            "B",
        ),
        entry(
            {"ty": "iss", "ibi": 6, "s": "early ", "sugid": "sug-1"},
            "2026-10-05T12:01:30Z",
            "222",
            5,
            "B",
        ),
        entry(
            {"ty": "msfd", "si": 12, "ei": 16, "sugid": "sug-2"},
            "2026-10-05T12:02:00Z",
            "222",
            6,
            "B",
        ),
        entry({"ty": "is", "ibi": 39, "s": "Done.\n"}, "2026-10-05T15:00:00Z", "222", 7, "B"),
    ]
    # As Google sends it, with the prefix that stops a browser running it.
    return ")]}'\n" + json.dumps({"chunkedSnapshot": [], "changelog": entries})


def comments(sam_name: str = "Sam Sample") -> str:
    return json.dumps(
        [
            {
                "id": "c1",
                "createdTime": "2026-10-05T11:00:00.000Z",
                "author": {"displayName": "Alex Example", "permissionId": "p1"},
                "content": "Is this right?",
                "quotedFileContent": {"value": "Draft &amp; one"},
                "replies": [
                    {
                        "id": "c1-r1",
                        "createdTime": "2026-10-05T11:30:00.000Z",
                        "author": {"displayName": sam_name, "permissionId": "p2"},
                        "content": "Yes.",
                    },
                    {
                        "id": "c1-r2",
                        "createdTime": "2026-10-05T11:31:00.000Z",
                        "author": {"displayName": "Alex Example", "permissionId": "p1"},
                        "content": "",
                        "action": "resolve",
                    },
                ],
            }
        ]
    )


def metadata(name: str = "Example notes") -> str:
    return json.dumps({"id": DOC, "name": name, "createdTime": "2026-10-05T09:00:00Z"})


def full_fetch(data, fetch_id="20261006T090000Z", when="2026-10-06T09:00:00Z"):
    """Everything the producer's own Google login can get."""
    save_fetch(
        data,
        DOC,
        fetch_id,
        when,
        "google-login",
        {
            "metadata.json": metadata(),
            "comments.json": comments(),
            "changelog.json": changelog(),
            # Drive's text export: a byte order mark, Windows line endings, and a blank line
            # between paragraphs, so it never matches the replay exactly.
            "export.txt": chr(0xFEFF) + "Draft one\r\n\r\nMore.\r\n\r\nEdit by B.\r\n\r\nDone.\r\n",
        },
    )


def events(data) -> list[dict]:
    path = document_dir(data, "google-doc", DOC) / "events.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def of_type(data, kind: str) -> list[dict]:
    return [event for event in events(data) if event["type"] == kind]


def meta(data) -> dict:
    return json.loads((document_dir(data, "google-doc", DOC) / "meta.json").read_text("utf-8"))


def test_changelog_gives_a_version_per_stretch_of_editing(tmp_path):
    full_fetch(tmp_path)
    counts = build(tmp_path)
    assert counts["docs"] == 1 and counts["history: changelog"] == 1
    versions = of_type(tmp_path, "version")
    # A new session (A to B), then three hours idle. The export matches the replay, so it
    # isn't counted as another version.
    assert [v["native_id"] for v in versions] == ["rev-2", "rev-6", "rev-7"]
    assert {v["origin"] for v in versions} == {"native"}
    assert [v["actor"]["id"] for v in versions] == ["gaia:111", "gaia:222", "gaia:222"]
    folder = document_dir(tmp_path, "google-doc", DOC)
    texts = [(folder / "versions" / v["version"]).read_text("utf-8") for v in versions]
    # Versions hold the text without suggestions: those are events.
    assert texts == [FIRST, SECOND, FINAL]
    assert meta(tmp_path)["history"] == "changelog"
    assert meta(tmp_path)["title"] == "Example notes"


def test_suggestions_are_joined_by_suggestion_id(tmp_path):
    full_fetch(tmp_path)
    build(tmp_path)
    suggestions = {s["native_id"]: s for s in of_type(tmp_path, "suggestion")}
    assert suggestions["sug-1"]["kind"] == "insert"
    assert suggestions["sug-1"]["text"] == "Very early "
    assert suggestions["sug-1"]["at"] == "2026-10-05T12:01:00Z"
    assert suggestions["sug-2"]["kind"] == "delete"
    assert suggestions["sug-2"]["text"] == "Draft"


def test_comment_threads_become_comment_reply_and_resolve(tmp_path):
    full_fetch(tmp_path)
    build(tmp_path)
    (comment,) = of_type(tmp_path, "comment")
    assert comment["anchor"]["quote"] == "Draft & one"
    assert comment["actor"] == {"id": "perm:p1", "name": "Alex Example"}
    # Made after the first version and before the second.
    assert comment["version"] == of_type(tmp_path, "version")[0]["version"]
    (reply,) = of_type(tmp_path, "reply")
    (resolve,) = of_type(tmp_path, "resolve")
    assert reply["parent"] == resolve["parent"] == "c1"
    readable = (document_dir(tmp_path, "google-doc", DOC) / "comments.md").read_text("utf-8")
    assert "Resolved by Alex Example" in readable


def test_snapshots_alone_still_show_the_doc_changing(tmp_path):
    # The Drive connector route: only the current text, at each pull.
    for n, text in enumerate(["One\n", "One\nTwo\n", "One\r\nTwo\r\n"], start=1):
        when = f"2026-10-0{n}T12:00:00Z"
        save_fetch(tmp_path, DOC, f"2026100{n}T120000Z", when, "connector", {"export.txt": text})
    assert build(tmp_path)["history: snapshots"] == 1
    versions = of_type(tmp_path, "version")
    # The third pull differs only in line endings, so it isn't a change.
    assert len(versions) == 2
    assert {v["origin"] for v in versions} == {"snapshot"}
    assert versions[1]["not_before"] == "2026-10-01T12:00:00Z"
    assert meta(tmp_path)["title"] == DOC  # no metadata.json, so the ID stands in


def test_a_snapshot_after_the_changelog_is_kept_only_if_the_text_changed(tmp_path):
    full_fetch(tmp_path)
    save_fetch(
        tmp_path,
        DOC,
        "20261007T090000Z",
        "2026-10-07T09:00:00Z",
        "connector",
        {"export.txt": FINAL.replace("\n", "\r\n")},
    )
    build(tmp_path)
    assert len(of_type(tmp_path, "version")) == 3
    save_fetch(
        tmp_path,
        DOC,
        "20261008T090000Z",
        "2026-10-08T09:00:00Z",
        "connector",
        {"export.txt": FINAL + "Later.\n"},
    )
    build(tmp_path)
    versions = of_type(tmp_path, "version")
    assert len(versions) == 4 and versions[-1]["origin"] == "snapshot"


def test_drive_revision_exports_are_native_versions(tmp_path):
    revisions = [
        {
            "id": "1",
            "modifiedTime": "2026-10-01T10:00:00Z",
            "lastModifyingUser": {"displayName": "Alex Example", "permissionId": "p1"},
        },
        {
            "id": "2",
            "modifiedTime": "2026-10-02T10:00:00Z",
            "lastModifyingUser": {"displayName": "Sam Sample", "permissionId": "p2"},
        },
    ]
    files = {
        "revisions.json": json.dumps(revisions),
        "revision-1.txt": "One\n",
        "revision-2.txt": "One\nTwo\n",
        "export.txt": "One\r\nTwo\r\n",
    }
    save_fetch(tmp_path, DOC, "20261003T120000Z", "2026-10-03T12:00:00Z", "connector", files)
    assert build(tmp_path)["history: revisions"] == 1
    versions = of_type(tmp_path, "version")
    assert [(v["native_id"], v["at"]) for v in versions] == [
        ("1", "2026-10-01T10:00:00Z"),
        ("2", "2026-10-02T10:00:00Z"),
    ]
    assert versions[1]["actor"] == {"id": "perm:p2", "name": "Sam Sample"}


def test_comments_seen_again_are_kept_once_and_rebuilds_change_nothing(tmp_path):
    full_fetch(tmp_path)
    full_fetch(tmp_path, "20261007T090000Z", "2026-10-07T09:00:00Z")
    first = build(tmp_path)
    assert first["fetches"] == 2
    assert len(of_type(tmp_path, "comment")) == 1
    assert len(of_type(tmp_path, "suggestion")) == 2
    assert build(tmp_path)["written"] == 0


def test_an_email_never_leaves_raw(tmp_path):
    # Someone without a Google account shows up with their email as their name.
    save_fetch(
        tmp_path,
        DOC,
        "20261006T090000Z",
        "2026-10-06T09:00:00Z",
        "shared-account",
        {"comments.json": comments("sam@example.com"), "export.txt": "Text\n"},
    )
    build(tmp_path)
    (reply,) = of_type(tmp_path, "reply")
    assert reply["actor"] == {"id": "perm:p2", "name": None}
    built = [p for p in documents_dir(tmp_path).rglob("*") if p.is_file() and "raw" not in p.parts]
    assert [p.name for p in built if EMAIL.search(p.read_text("utf-8"))] == []


def test_a_doc_with_an_unreadable_fetch_is_left_as_it_was(tmp_path):
    full_fetch(tmp_path)
    broken = document_dir(tmp_path, "google-doc", DOC) / "raw" / "20261009T000000Z"
    broken.mkdir()
    (broken / "fetch.json").write_text("{not json", encoding="utf-8")
    counts = build(tmp_path)
    assert counts["skipped"] == 1 and counts["docs"] == 0


def test_a_half_saved_fetch_is_not_built_from_until_it_is_finished(tmp_path):
    first = {"export.txt": "One\n"}
    save_fetch(tmp_path, DOC, "20261001T120000Z", "2026-10-01T12:00:00Z", "connector", first)
    later = {"export.txt": "One\nTwo\n"}
    with pytest.raises(GoogleDocError):  # the second file's name is refused, after export.txt
        save_fetch(
            tmp_path,
            DOC,
            "20261002T120000Z",
            "2026-10-02T12:00:00Z",
            "connector",
            later | {"bad name?.txt": "x"},
        )
    counts = build(tmp_path)
    assert counts["unfinished fetches"] == 1 and counts["docs"] == 1
    assert len(of_type(tmp_path, "version")) == 1
    # Saving it again under the same fetch ID finishes it.
    save_fetch(tmp_path, DOC, "20261002T120000Z", "2026-10-02T12:00:00Z", "connector", later)
    assert build(tmp_path)["unfinished fetches"] == 0
    assert len(of_type(tmp_path, "version")) == 2


@pytest.mark.parametrize(
    ("route", "when", "files"),
    [
        ("carrier-pigeon", "2026-10-06T09:00:00Z", {}),
        ("connector", "2026-10-06T09:00:00", {}),  # no time zone
        ("connector", "2026-10-06T09:00:00Z", {"fetch.json": "{}"}),
    ],
)
def test_save_fetch_refuses_what_the_reader_could_not_trust(tmp_path, route, when, files):
    with pytest.raises(GoogleDocError):
        save_fetch(tmp_path, DOC, "20261006T090000Z", when, route, files)


def test_a_saved_fetch_is_never_changed(tmp_path):
    files = {"export.txt": "One\n"}
    save_fetch(tmp_path, DOC, "20261006T090000Z", "2026-10-06T09:00:00Z", "connector", files)
    with pytest.raises(GoogleDocError):
        save_fetch(
            tmp_path,
            DOC,
            "20261006T090000Z",
            "2026-10-06T09:00:00Z",
            "connector",
            {"export.txt": "Two\n"},
        )


def build_changelog(data, entries: list[list]) -> list[dict]:
    """Save a fetch holding only this changelog, build, and return its events."""
    body = json.dumps({"chunkedSnapshot": [], "changelog": entries})
    when = "2026-10-06T09:00:00Z"
    save_fetch(data, DOC, "20261006T090000Z", when, "browser-file", {"changelog.json": body})
    build(data)
    return events(data)


def test_a_missing_session_or_an_empty_start_is_not_a_version(tmp_path):
    # Google leaves the session out of some entries, and a doc often starts with styling.
    found = build_changelog(
        tmp_path,
        [
            entry({"ty": "as", "st": "text"}, "2026-10-05T10:00:00Z", "111", 1, "A"),
            entry({"ty": "is", "ibi": 1, "s": "One "}, "2026-10-05T10:00:10Z", "111", 2, "B"),
            entry({"ty": "is", "ibi": 5, "s": "two\n"}, "2026-10-05T10:00:20Z", "111", 3, None),
            entry({"ty": "is", "ibi": 9, "s": "x"}, "2026-10-05T10:00:30Z", "111", 4, "B"),
        ],
    )
    versions = [e for e in found if e["type"] == "version"]
    assert [v["native_id"] for v in versions] == ["rev-4"]


def test_a_new_editor_starts_a_new_version(tmp_path):
    found = build_changelog(
        tmp_path,
        [
            entry({"ty": "is", "ibi": 1, "s": "One\n"}, "2026-10-05T10:00:00Z", "111", 1, None),
            entry({"ty": "is", "ibi": 5, "s": "Two\n"}, "2026-10-05T10:00:05Z", "222", 2, None),
        ],
    )
    versions = [e for e in found if e["type"] == "version"]
    assert [v["actor"]["id"] for v in versions] == ["gaia:111", "gaia:222"]


def test_suggestion_text_follows_fixed_typing_and_marks_taken_back(tmp_path):
    plain = {"ty": "is", "ibi": 1, "s": "Plain words\n"}
    found = build_changelog(
        tmp_path,
        [
            entry(plain, "2026-10-05T10:00:00Z", "1", 1, "A"),
            # Suggests "Veery " at the start, then fixes the typo inside the suggestion.
            entry(
                {"ty": "iss", "ibi": 1, "s": "Veery ", "sugid": "sug-1"},
                "2026-10-05T10:01:00Z",
                "2",
                2,
                "B",
            ),
            entry({"ty": "dss", "si": 2, "ei": 2}, "2026-10-05T10:01:05Z", "2", 3, "B"),
            # Marks "Plain words" for deletion, then takes back " words".
            entry(
                {"ty": "msfd", "si": 6, "ei": 16, "sugid": "sug-2"},
                "2026-10-05T10:02:00Z",
                "2",
                4,
                "B",
            ),
            entry({"ty": "usfd", "si": 11, "ei": 16}, "2026-10-05T10:02:05Z", "2", 5, "B"),
        ],
    )
    suggestions = {e["native_id"]: e["text"] for e in found if e["type"] == "suggestion"}
    assert suggestions == {"sug-1": "Very ", "sug-2": "Plain"}


def test_drive_pages_saved_as_sent_and_in_several_files(tmp_path):
    first, second = (
        json.loads(comments()),
        [{"id": "c2", "createdTime": "2026-10-05T13:00:00Z", "content": "Second page."}],
    )
    revisions = {
        "kind": "drive#revisionList",
        "revisions": [{"id": "1", "modifiedTime": "2026-10-01T10:00:00Z"}],
    }
    files = {
        # Drive's comments.list exactly as sent: an object, with the list inside.
        "comments.json": json.dumps(
            {"kind": "drive#commentList", "comments": first, "nextPageToken": "page-2"}
        ),
        "comments-2.json": json.dumps({"kind": "drive#commentList", "comments": second}),
        "revisions.json": json.dumps(revisions),
        "revision-1.txt": "One\n",
    }
    save_fetch(tmp_path, DOC, "20261006T090000Z", "2026-10-06T09:00:00Z", "google-login", files)
    assert build(tmp_path)["docs"] == 1
    comments_seen = of_type(tmp_path, "comment")
    assert [c["native_id"] for c in comments_seen] == ["c1", "c2"]
    assert comments_seen[1]["raw"] == "raw/20261006T090000Z/comments-2.json"
    assert of_type(tmp_path, "version")[0]["at"] == "2026-10-01T10:00:00Z"


def test_a_raw_file_in_a_shape_we_dont_expect_fails_only_that_doc(tmp_path):
    other = "FAKE-DOC-0002"
    when = "2026-10-06T09:00:00Z"
    save_fetch(
        tmp_path,
        DOC,
        "20261006T090000Z",
        when,
        "google-login",
        {"comments.json": '"not a list"', "export.txt": "One\n"},
    )
    save_fetch(tmp_path, other, "20261006T090000Z", when, "connector", {"export.txt": "Two\n"})
    counts = build(tmp_path)
    assert counts["failed"] == 1 and counts["docs"] == 1


def test_fetching_again_keeps_when_old_history_was_first_seen(tmp_path):
    body = json.dumps(
        {
            "chunkedSnapshot": [],
            "changelog": [
                entry({"ty": "is", "ibi": 1, "s": "One\n"}, "2026-10-05T10:00:00Z", "1", 1, "A"),
                entry(
                    {"ty": "iss", "ibi": 5, "s": "Two", "sugid": "sug-1"},
                    "2026-10-05T10:05:00Z",
                    "1",
                    2,
                    "A",
                ),
            ],
        }
    )
    files = {"changelog.json": body}
    save_fetch(tmp_path, DOC, "f1", "2026-10-06T09:00:00Z", "browser-file", files)
    build(tmp_path)
    log = document_dir(tmp_path, "google-doc", DOC) / "events.jsonl"
    before = log.read_bytes()
    # The same history fetched again three days later.
    save_fetch(tmp_path, DOC, "f2", "2026-10-09T09:00:00Z", "browser-file", files)
    build(tmp_path)
    assert log.read_bytes() == before
    (version,) = of_type(tmp_path, "version")
    (suggestion,) = of_type(tmp_path, "suggestion")
    assert (version["seen_at"], version["raw"]) == ("2026-10-06T09:00:00Z", "raw/f1/changelog.json")
    assert suggestion["seen_at"] == "2026-10-06T09:00:00Z"


def test_replay_warnings_are_counted(tmp_path):
    # An insert far past the end of the doc: the replay places it, but warns.
    body = json.dumps(
        {
            "chunkedSnapshot": [],
            "changelog": [
                entry({"ty": "is", "ibi": 1, "s": "One\n"}, "2026-10-05T10:00:00Z", "1", 1, "A"),
                entry({"ty": "is", "ibi": 99, "s": "x"}, "2026-10-05T10:00:05Z", "1", 2, "A"),
            ],
        }
    )
    save_fetch(
        tmp_path, DOC, "f1", "2026-10-06T09:00:00Z", "browser-file", {"changelog.json": body}
    )
    assert build(tmp_path)["replay warnings"] == 1
