"""Scrubbed copies (SCR-2): people, Slack IDs and emails replaced, and the must-refuse list
from the plan on Issue #66, one test per item. Synthetic fixtures only."""

import hashlib
import json
import os
import re
import shutil
import sys
import time
import unicodedata
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from diffdata import cli
from diffdata.common.config import Config
from diffdata.discover.slack_export import build
from diffdata.scrub import run as scrub_run
from diffdata.scrub.people import People, Person, read_people, render
from diffdata.scrub.run import ScrubError, scrub
from diffdata.scrub.text import Scrubber

CHANNEL = "C00000001"
PARENT = "1790864040.000100"
CANVAS = "F00000003"
T0 = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
FIXTURES = Path(__file__).parent / "fixtures"
PEOPLE_READ = FIXTURES / "scrub" / "read_channel_people.txt"
DAY = "slack/export/C00000001/2026-10-0{}.md"
CANVAS_COPY = "documents/current/slack-canvas_F00000003.md"
# The invented values in the fixtures that no copy, manifest or printed line may show.
NAMES = ("Alex", "Example", "Sam", "Sample", "Riley", "Placeholder", "Casey", "Silentia")
NAMES += ("José", "Ejemplo", "Jamie", "Tester", "Fakename", "Jordan")
REAL = (*NAMES, "example.com", "U0000", "W0000", "C0000", "F0000", "Kickoff")


@pytest.fixture
def config(tmp_path):
    return Config(
        data_dir=tmp_path / "data",
        slack_channels=("example-channel",),
        slack_workspace_url="https://example.slack.com",
        slack_canvases=(CANVAS,),
    )


@pytest.fixture
def export(config, slack_run, slack_response):
    """One /slack-export run and its build: the channel, its thread and the canvas, then,
    unless told not to, a second channel read with the people these tests need."""

    def run(when=T0, people=True):
        canvas = (FIXTURES / "slack" / "read_canvas_v2.json").read_text(encoding="utf-8")
        thread = {"channel_id": CHANNEL, "message_ts": PARENT}
        reads = [
            ("slack_read_channel", {"channel_id": CHANNEL}, slack_response("read_channel.txt")),
            ("slack_read_thread", thread, slack_response("read_thread.txt")),
            ("slack_read_canvas", {"canvas_id": CANVAS}, [{"type": "text", "text": canvas}]),
        ]
        if people:
            read = slack_response(str(PEOPLE_READ))
            reads.append(("slack_read_channel", {"channel_id": CHANNEL}, read))
        slack_run(config, when, reads)
        build(config)

    return run


@pytest.fixture
def scrubbed(config, export):
    """One export, then one scrub. Returns the scrub's report."""
    export()
    return scrub(config)


def copies(config) -> dict[str, str]:
    folder = config.data_dir / "scrubbed"
    return {
        path.relative_to(folder).as_posix(): path.read_bytes().decode("utf-8")
        for path in sorted(folder.rglob("*"))
        if path.is_file()
    }


def originals(config) -> dict[str, bytes]:
    """Every file in the data folder outside the scrub's own folders, as bytes."""
    data = config.data_dir
    ours = {"scrubbed", "scrubbed-in-progress", "private"}
    return {
        path.relative_to(data).as_posix(): path.read_bytes()
        for path in sorted(data.rglob("*"))
        if path.is_file() and path.relative_to(data).parts[0] not in ours
    }


def people_file(config) -> Path:
    return config.data_dir / "private" / "people.toml"


def flags(config) -> str:
    return (config.data_dir / "private" / "flags.md").read_text(encoding="utf-8")


def placeholders(config) -> dict[str, str]:
    return {p.slack_ids[0]: p.placeholder for p in read_people(people_file(config)).people}


def has_word(name: str, text: str) -> bool:
    """Whether `name` is in `text` as a whole word, in any case."""
    return (
        re.search(rf"(?<![^\W\d_]){re.escape(name)}(?![^\W\d_])", text, re.IGNORECASE) is not None
    )


def write_config(path: Path, data: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"[local]\ndata_dir = '{data.as_posix()}'\n\n[sources]\n"
        'slack_channels = ["example-channel"]\n'
        'slack_workspace_url = "https://example.slack.com"\n'
        f'slack_canvases = ["{CANVAS}"]\n',
        encoding="utf-8",
    )


# What a copy holds


def test_each_opted_in_day_and_canvas_gets_a_copy_named_by_id_only(config, scrubbed):
    assert set(copies(config)) == {
        *(DAY.format(day) for day in "1235"),
        CANVAS_COPY,
        "manifest.json",
    }


def test_people_are_numbered_in_slack_id_order(config, scrubbed):
    assert placeholders(config) == {
        "U00000001": "[Person 1]",
        "U00000002": "[Person 2]",
        "U00000003": "[Person 3]",
        "U00000004": "[Person 4]",  # known only from a mention's label
        "U00000005": "[Person 5]",
        "U00000006": "[Person 6]",
        "U00000007": "[Person 7]",
        "U00000009": "[Person 8]",  # known only from a canvas mention
    }


def test_every_way_a_name_shows_up_becomes_the_same_placeholder(config, scrubbed):
    found = copies(config)
    first, notice, later = (found[DAY.format(day)] for day in "125")
    assert "**[Person 1]** · 14:14 UTC" in first  # an author
    assert "Added my comments, @[Person 1]." in first  # a mention with its label
    assert "One more thought on this, @[Person 2]." in first  # a mention without one
    assert "[Person 1] made updates to a canvas tab" in notice  # in plain text
    assert "Owner: @[Person 1]." in found[CANVAS_COPY]  # in a canvas
    assert "Thanks [Person 3]!" in later  # a first name on its own, in lowercase
    assert "Looping in @[Person 4]," in later  # someone who never posts
    assert "Ask @[Person 8] about the timing" in found[CANVAS_COPY]
    # An accented name, typed two ways: composed in the author's name, decomposed in text.
    assert unicodedata.normalize("NFD", "José") in PEOPLE_READ.read_text(encoding="utf-8")
    assert "**[Person 6]** · 12:10 UTC" in later and "notes from [Person 6] are" in later


def test_emails_and_slack_ids_are_replaced(config, scrubbed):
    found = copies(config)
    assert "the interview guide go to [email]." in found[DAY.format(1)]
    assert "My old account was [name?]." in found[DAY.format(5)]  # an ID nobody has
    assert "/files/[Person 1]/F00000009/diagram.png" in found[CANVAS_COPY]


def test_channel_file_and_team_ids_are_kept(config, scrubbed):
    found = copies(config)
    assert "channel_id: C00000001" in found[DAY.format(1)]
    assert "/docs/T00000001/F00000002" in found[CANVAS_COPY]
    assert "Files: call-notes.txt (F00000003, text/plain)" in found[DAY.format(1)]


def test_a_name_inside_a_link_is_replaced_until_its_word_is_in_not_names(config, scrubbed):
    # "Example" is Alex's last name and the workspace's, so links hold it.
    link = "[link](https://example.slack.com/archives/C00000001/p1790864040000100)"
    assert "[link](https://[Person 1].slack.com/archives/" in copies(config)[DAY.format(1)]
    text = people_file(config).read_text(encoding="utf-8")
    people_file(config).write_text(
        text.replace("not_names = []", 'not_names = ["example"]'), encoding="utf-8"
    )
    scrub(config)
    found = copies(config)
    assert link in found[DAY.format(1)]
    assert "https://example.slack.com/docs/T00000001/F00000002" in found[CANVAS_COPY]
    assert "# #example-channel, 2026-10-01" in found[DAY.format(1)]
    assert "**[Person 1]** · 14:14 UTC" in found[DAY.format(1)]  # the full name is still found


def test_an_initial_is_not_a_name_on_its_own(config, scrubbed):
    later = copies(config)[DAY.format(5)]
    assert "**[Person 7]** · 12:20 UTC" in later and "Q&A notes" in later


def test_an_outsider_who_shares_a_first_name_gets_that_members_placeholder(config, scrubbed):
    # A known limit, written down in the plan. The name is hidden either way.
    assert "[Person 2] from the other lab says hi." in copies(config)[DAY.format(5)]


def test_slacks_own_mentions_stay(config, scrubbed):
    assert "Monday. @here" in copies(config)[DAY.format(3)]


def test_numbers_stay_the_same_after_new_people_appear(config, export):
    export(people=False)
    scrub(config)
    before = placeholders(config)
    assert before == {
        "U00000001": "[Person 1]",
        "U00000002": "[Person 2]",
        "U00000003": "[Person 3]",
        "U00000009": "[Person 4]",
    }
    export(when=T0 + timedelta(days=1))
    report = scrub(config)
    assert placeholders(config) == before | {
        "U00000004": "[Person 5]",
        "U00000005": "[Person 6]",
        "U00000006": "[Person 7]",
        "U00000007": "[Person 8]",
    }
    assert "The people list has 8 people, 4 of them new." in report


def test_a_nickname_added_by_hand_is_found(config, scrubbed):
    people = read_people(people_file(config))
    people.people[1].nicknames.append("Sammy")  # [Person 2]
    people_file(config).write_text(render(people), encoding="utf-8")
    day = config.data_dir / "slack" / "export" / "example-channel" / "2026-10-03.md"
    day.write_bytes(day.read_bytes() + b"Sammy agrees.\n")
    scrub(config)
    assert "[Person 2] agrees." in copies(config)[DAY.format(3)]
    assert "Sammy" in people_file(config).read_text(encoding="utf-8")  # the list keeps it


def test_a_second_run_writes_the_same_files_byte_for_byte(config, scrubbed):
    before = copies(config), people_file(config).read_bytes(), flags(config)
    scrub(config)
    assert (copies(config), people_file(config).read_bytes(), flags(config)) == before


def test_google_docs_clean_copies_are_skipped(config, export):
    export()
    current = config.data_dir / "documents" / "current"
    other = current / "google-doc_kickoff-notes_FAKE-DOC-0001.md"
    other.write_text("By Alex Example\n", encoding="utf-8")
    report = scrub(config)
    assert not any("google" in path for path in copies(config))
    assert "1 clean copy(ies) from other sources, like Google Docs, skipped" in report


def test_the_manifest_lists_each_copy_and_says_its_not_approved(config, scrubbed):
    found = copies(config)
    manifest = json.loads(found["manifest.json"])
    assert manifest["approved_for_release"] is False
    assert "Not approved for release" in manifest["note"]
    assert [copy["path"] for copy in manifest["copies"]] == [
        *(DAY.format(day) for day in "1235"),
        CANVAS_COPY,
    ]
    later = manifest["copies"][3]
    assert (later["source"], later["id"], later["flags"]) == ("slack", "C00000001/2026-10-05", 3)
    assert later["sha256"] == hashlib.sha256(found[DAY.format(5)].encode("utf-8")).hexdigest()
    assert "last check" in later["checks"]
    assert manifest["held_back"] == []


def test_the_report_gives_counts_only(scrubbed):
    assert scrubbed.splitlines() == [
        "5 scrubbed copy(ies): 4 Slack day file(s) and 1 canvas(es), with 3 flag(s) for review.",
        "What was flagged is in private/flags.md in your data folder.",
        "The people list has 8 people, 8 of them new.",
        "These copies are not approved for release: phone numbers, meeting links and secrets "
        "aren't checked yet.",
    ]


# The people list and the text steps on their own


def test_the_people_list_reads_back_as_it_was_written(tmp_path):
    people = People(
        [
            Person("[Person 1]", ["U00000001"], ['Al "the" Ex\\ample', "Zoë Example"]),
            Person("[Person 2]", [], [], ["Tab\tand\x7fDEL"]),
        ],
        ["Will"],
    )
    path = tmp_path / "people.toml"
    path.write_text(render(people), encoding="utf-8")
    assert read_people(path) == people


def test_a_name_is_matched_whole_and_never_inside_a_placeholder():
    people = People(
        [
            Person("[Person 1]", ["U00000001"], ["Alex Example"]),
            Person("[Person 2]", [], [], ["Person", "Name"]),  # nicknames that are in placeholders
        ]
    )
    text, _ = Scrubber(people).scrub("U00000001 met Person. Alexander isn't Alex's: alex_2.")
    assert text == "[Person 1] met [Person 2]. Alexander isn't [Person 1]'s: [Person 1]_2."


def test_a_name_is_found_however_its_written():
    people = People(
        [
            Person("[Person 1]", ["U00000001"], ["Kim O'Example", "riley.fake"]),
            Person("[Person 2]", ["U00000002"], ["Anne-Marie Sample", "José Ejemplo"]),
        ]
    )
    scrubber = Scrubber(people)
    written = "O’Example and Riley met Anne, then Marie, then Jose and jose ejemplo."
    text, flags = scrubber.scrub(written)
    assert text == (
        "[Person 1] and [Person 1] met [Person 2], then [Person 2], then [Person 2] and [Person 2]."
    )
    assert flags == [] and scrubber.leftovers(written) == ["a name"]


@pytest.mark.parametrize(
    "written, scrubbed, flagged",
    [
        ("Ask @alex.newperson.", "Ask @[name?].", ["alex.newperson"]),  # holds a known name
        ("Ask @_newperson.", "Ask @[name?].", ["_newperson"]),
        ("cc:@newperson", "cc:@[name?]", ["newperson"]),
        ("Thanks @Alex Example, today", "Thanks @[Person 1], today", []),
        ("Thanks @alex.", "Thanks @[Person 1].", []),
        # Only starts with a known name, so it isn't given to anyone.
        ("Ask @Alex Newperson.", "Ask @[name?].", ["Alex Newperson"]),
        ("Ask @Will Newperson.", "Ask @[name?].", ["Will Newperson"]),  # Will is in not_names
        ("Ask @Will.", "Ask @Will.", []),
        ("See @Alex Example's doc", "See @[Person 1]'s doc", []),  # in the possessive
        ("See @alex’s doc", "See @[Person 1]’s doc", []),
        ("See @Jordan's doc", "See @[name?]'s doc", ["Jordan"]),
        ("Ask @Alex Šample.", "Ask @[name?].", ["Alex Šample"]),  # a capital beyond A to Z
        ("Ask @alex please", "Ask @[Person 1] please", []),
        ("@here Meeting moved, @Channel", "@here Meeting moved, @Channel", []),
    ],
)
def test_a_mention_is_read_whole(written, scrubbed, flagged):
    people = People([Person("[Person 1]", [], ["Alex Example"])], ["Will"])
    text, flags = Scrubber(people).scrub(written)
    assert text == scrubbed and [flag.text for flag in flags] == flagged


@pytest.mark.parametrize(
    "email", ["contact@[192.0.2.1]", "new.person'alias@example.com", '"new person"@example.com']
)
def test_every_shape_of_email_is_replaced_and_checked(email):
    scrubber = Scrubber(People())
    assert scrubber.scrub(f"Write to {email}, please.") == ("Write to [email], please.", [])
    assert scrubber.leftovers(f"Write to {email}, please.") == ["an email"]


@pytest.mark.parametrize("at", ["@", "%40"])  # a link can spell the @ as %40
def test_an_email_in_a_link_takes_only_itself(at):
    scrubber = Scrubber(People())
    link = f"https://example.org/join?user=new.person{at}example.com&x=1"
    assert scrubber.scrub(link)[0] == "https://example.org/join?user=[email]&x=1"
    assert scrubber.leftovers(link) == ["an email"]


def test_a_mention_hidden_in_a_link_isnt_taken_for_an_email():
    scrubber = Scrubber(People())
    text, _ = scrubber.scrub("https://blog.example.com/@newperson/post")
    assert text == "https://blog.example.com/@[name?]/post" and scrubber.leftovers(text) == []


@pytest.mark.parametrize(
    "name, written",
    [
        ("Kim Weiß", "WEISS replied."),  # one letter folds to two
        ("Kim Weiss", "Weiß replied."),
        ("李", "李 replied."),  # one letter, in a script without capitals
    ],
)
def test_a_name_is_found_in_any_case_and_any_length(name, written):
    scrubber = Scrubber(People([Person("[Person 1]", [], [name])]))
    assert scrubber.scrub(written) == ("[Person 1] replied.", [])
    assert scrubber.leftovers(written) == ["a name"]


def test_an_initial_is_never_a_name_but_a_one_letter_nickname_is():
    scrubber = Scrubber(People([Person("[Person 1]", [], ["Jamie Q. Tester"], ["Z"])]))
    assert scrubber.scrub("Q and Z")[0] == "Q and [Person 1]"


def test_a_long_run_of_letters_is_read_quickly():
    # Like an image pasted into a canvas. Read once per letter, this takes about a minute.
    blob = "data:image/png;base64," + "A" * 100_000
    people = People([Person("[Person 1]", ["U00000001"], ["Alex Example"])])
    started = time.perf_counter()
    assert Scrubber(people).scrub(blob + " alex@example.com") == (blob + " [email]", [])
    assert time.perf_counter() - started < 5


def test_only_user_ids_are_replaced():
    text, flags = Scrubber(People()).scrub("USLACKBOT C00000001 F00000002 T00000000 U00000009")
    assert text == "USLACKBOT C00000001 F00000002 T00000000 [name?]"
    assert [flag.text for flag in flags] == ["U00000009"]


# Must refuse (plan on Issue #66)


def test_must_refuse_1_an_original_is_never_changed(config, export, tmp_path):
    export()
    # The people list is a hard link to a file elsewhere. Updating the list replaces the
    # link, so the file it points to stays as it was.
    elsewhere = tmp_path / "elsewhere.toml"
    elsewhere.write_text(
        'not_names = []\n\n[[person]]\nplaceholder = "[Person 1]"\nslack_ids = ["U00000001"]\n',
        encoding="utf-8",
    )
    linked = elsewhere.read_bytes()
    people_file(config).parent.mkdir()
    os.link(elsewhere, people_file(config))
    before = originals(config)
    scrub(config)
    scrub(replace(config, slack_channels=()))
    assert originals(config) == before
    assert elsewhere.read_bytes() == linked
    assert "U00000009" in people_file(config).read_text(encoding="utf-8")


def test_must_refuse_2_a_run_deletes_nothing_it_didnt_make(config, scrubbed):
    data = config.data_dir
    beside = [
        data / "scrubbed-notes.md",
        data / "private-notes.md",
        data / "private" / "my-notes.md",
        data / "notes" / "scrubbed" / "keep.md",
    ]
    for path in beside:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("keep", encoding="utf-8")
    scrub(config)
    people_file(config).write_text("not_names = [", encoding="utf-8")
    with pytest.raises(ScrubError):
        scrub(config)
    assert [path.read_text(encoding="utf-8") for path in beside] == ["keep"] * len(beside)


@pytest.mark.parametrize("folder", ["scrubbed", "scrubbed-in-progress"])
@pytest.mark.parametrize(
    "name",
    ["notes.md", "slack/export/C00000001/notes.md", "notes/.writing-draft", "notes/empty/"],
)
def test_must_refuse_2_a_folder_the_scrub_replaces_with_someone_elses_file_is_left_alone(
    config, scrubbed, folder, name
):
    theirs = config.data_dir / folder / name
    if name.endswith("/"):
        theirs.mkdir(parents=True)
    else:
        theirs.parent.mkdir(parents=True, exist_ok=True)
        theirs.write_text("keep", encoding="utf-8")
    before = copies(config)
    with pytest.raises(ScrubError, match=f"{folder}/ in your data folder holds a file or folder"):
        scrub(config)
    assert theirs.is_dir() if name.endswith("/") else theirs.read_text(encoding="utf-8") == "keep"
    assert copies(config) == before


def test_what_finder_and_file_explorer_add_doesnt_stop_a_run(config, scrubbed):
    for name in (".DS_Store", "slack/export/C00000001/Thumbs.db", "documents/desktop.ini"):
        (config.data_dir / "scrubbed" / name).write_text("", encoding="utf-8")
    scrub(config)
    assert not (config.data_dir / "scrubbed" / ".DS_Store").exists()


def make_link(link: Path, target: Path, kind: str) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    if kind == "junction":
        if sys.platform != "win32":
            pytest.skip("junctions are Windows only")
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError:
            pytest.skip("this computer doesn't let tests make symlinks")


@pytest.mark.parametrize(
    "where, kind",
    [
        ("scrubbed", "junction"),
        ("scrubbed", "symlink"),
        ("scrubbed/slack", "junction"),
        ("scrubbed-in-progress", "symlink"),
        ("private", "junction"),
        ("private/notes", "symlink"),
    ],
)
def test_must_refuse_3_a_link_where_the_scrub_writes_is_refused(
    config, export, tmp_path, where, kind
):
    export()
    target = tmp_path / "elsewhere"
    target.mkdir()
    (target / "keep.md").write_text("keep", encoding="utf-8")
    make_link(config.data_dir / where, target, kind)
    with pytest.raises(ScrubError, match="is a link, holds one"):
        scrub(config)
    assert [(p.name, p.read_bytes()) for p in target.iterdir()] == [("keep.md", b"keep")]
    if not where.startswith("private"):
        assert not (config.data_dir / "private").exists()


def test_must_refuse_4_a_data_folder_inside_a_repo_stops_the_scrub(
    tmp_path, no_real_config, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    data = tmp_path / "repo" / "data"
    (data / "slack").mkdir(parents=True)
    write_config(no_real_config, data)
    assert cli.main(["scrub"]) == 1
    assert "is inside the git repository" in capsys.readouterr().err
    assert [path.name for path in data.iterdir()] == ["slack"]


def test_must_refuse_5_a_source_taken_off_the_list_loses_its_copy(config, scrubbed):
    before = originals(config)
    scrub(replace(config, slack_channels=()))
    assert set(copies(config)) == {CANVAS_COPY, "manifest.json"}
    scrub(replace(config, slack_channels=(), slack_canvases=()))
    assert set(copies(config)) == {"manifest.json"}
    assert originals(config) == before


def test_must_refuse_6_a_copy_with_anything_left_is_held_back(config, export, monkeypatch):
    export()
    monkeypatch.setattr(Scrubber, "slack_ids", lambda self, text, flags: text)  # a broken step
    report = scrub(config)
    found = copies(config)
    assert DAY.format(5) not in found and CANVAS_COPY not in found and DAY.format(1) in found
    why = "the last check found a Slack ID"
    assert json.loads(found["manifest.json"])["held_back"] == [
        {"source": "slack", "id": "C00000001/2026-10-05", "why": why},
        {"source": "slack-canvas", "id": CANVAS, "why": why},
    ]
    assert "2 copy(ies) held back, with no file written" in report


def test_must_refuse_7_a_name_shared_or_unknown_isnt_given_to_anyone(config, scrubbed):
    later = copies(config)[DAY.format(5)]
    assert "Hi all, [name] here." in later  # Alex Example and Alex Fakename
    assert "Ping @[name?] about the venue." in later
    listed = flags(config)
    assert '- "Alex", shared by [Person 1] and [Person 5]: ' + DAY.format(5) + " (1)" in listed
    assert '- "@Jordan": ' + DAY.format(5) + " (1)" in listed
    assert '- "W00000099": ' + DAY.format(5) + " (1)" in listed


def test_must_refuse_8_no_private_file_or_value_is_in_a_scrubbed_copy(config, scrubbed):
    found = copies(config)
    assert not {"people.toml", "flags.md", "scrub.lock"} & {Path(p).name for p in found}
    people = read_people(people_file(config))
    values = {v for p in people.people for v in [*p.slack_ids, *p.names]}
    values |= {word for p in people.people for name in p.names for word in name.split()}
    values = {v for v in values if len(v) > 2}  # not the initial "Q."
    for path, text in found.items():
        assert not [v for v in values if has_word(v, text)], path
        assert "@" not in re.sub(r"@\[|@here", "", text), path  # no email or unknown mention
        assert "W00000099" not in text


def test_must_refuse_9_success_prints_counts_only(
    config, export, no_real_config, monkeypatch, capsys, tmp_path
):
    monkeypatch.chdir(tmp_path)
    export()
    write_config(no_real_config, config.data_dir)
    assert cli.main(["scrub"]) == 0
    printed = "".join(capsys.readouterr())
    assert "4 Slack day file(s)" in printed
    assert not [value for value in REAL if value in printed]


@pytest.mark.parametrize("failure", ["people list", "lock", "link", "crash", "early crash"])
def test_must_refuse_9_a_failure_prints_no_real_values(
    config, scrubbed, no_real_config, monkeypatch, capsys, tmp_path, failure
):
    monkeypatch.chdir(tmp_path)
    write_config(no_real_config, config.data_dir)
    secret = "Alex Example U00000001 riley@example.com Kickoff"
    if failure == "people list":
        people_file(config).write_text(f'not_names = ["{secret}"\n', encoding="utf-8")
    elif failure == "lock":
        (config.data_dir / "private" / "scrub.lock").touch()
    elif failure == "link":
        shutil.rmtree(config.data_dir / "scrubbed")
        (tmp_path / "elsewhere").mkdir()
        kind = "junction" if sys.platform == "win32" else "symlink"
        make_link(config.data_dir / "scrubbed", tmp_path / "elsewhere", kind)
    else:
        broken = "refuse_links" if failure == "early crash" else "make_copies"

        def crash(*args):
            raise ValueError(secret)

        monkeypatch.setattr(scrub_run, broken, crash)
    assert cli.main(["scrub"]) == 1
    printed = "".join(capsys.readouterr())
    assert printed.startswith("diffdata scrub: ")
    assert not [value for value in REAL if value in printed]
    assert "Traceback" not in printed
    if "crash" in failure:
        assert "unexpected ValueError" in printed


LIST = b"not_names = []\n\n"  # the start of every list diffdata writes
BROKEN_LISTS = {
    "can't be read as TOML (at": b'not_names = ["Alex Example"\n',
    "isn't UTF-8": b'not_names = ["Alex \xe9"]\n',
    "a setting diffdata doesn't know": b'Alex = "Example"\n',
    "emptied or cut short": b"",
    "may have been emptied": b"# The people the scrub knows: Alex Example\n",
    "same placeholder": LIST + b'[[person]]\nplaceholder = "[Person 1]"\n\n'
    b'[[person]]\nplaceholder = "[Person 1]"\n',
    "same Slack ID": LIST + b'[[person]]\nplaceholder = "[Person 1]"\nslack_ids = ["U00000001"]\n'
    b'\n[[person]]\nplaceholder = "[Person 2]"\nslack_ids = ["U00000001"]\n',
    "must be a list of words": LIST + b'[[person]]\nplaceholder = "[Person 1]"\nnames = "Alex"\n',
    "none of them empty": b'not_names = [" "]\n',
    'a placeholder like "[Person 1]"': LIST + b'[[person]]\nplaceholder = "[Alex Example]"\n',
    "a field diffdata doesn't know": LIST + b'[[person]]\nplaceholder = "[Person 1]"\nnick = []\n',
    "a Slack ID that isn't one": LIST + b'[[person]]\nplaceholder = "[Person 1]"\n'
    b'slack_ids = ["Alex"]\n',
    "must start with [[person]]": LIST + b'person = "Alex"\n',
}


@pytest.mark.parametrize("problem", BROKEN_LISTS)
def test_must_refuse_10_a_broken_people_list_stops_the_scrub(config, scrubbed, problem):
    people_file(config).write_bytes(BROKEN_LISTS[problem])
    with pytest.raises(ScrubError) as stop:
        scrub(config)
    assert problem in str(stop.value) and "Alex" not in str(stop.value)
    assert people_file(config).read_bytes() == BROKEN_LISTS[problem]
    assert not (config.data_dir / "scrubbed").exists()


def test_must_refuse_10_a_missing_people_list_stops_the_scrub(config, scrubbed):
    people_file(config).unlink()
    with pytest.raises(ScrubError, match="is missing, but private/ is there"):
        scrub(config)
    assert not people_file(config).exists()
    assert not (config.data_dir / "scrubbed").exists()
    shutil.rmtree(config.data_dir / "private")  # starting a new list on purpose
    scrub(config)
    assert placeholders(config)["U00000001"] == "[Person 1]"


def test_must_refuse_10_an_update_cut_short_leaves_the_old_list_whole(config, export, monkeypatch):
    export(people=False)
    scrub(config)
    before = people_file(config).read_bytes()
    export(when=T0 + timedelta(days=1))  # new people, so the list is updated

    def cut_short(source, target):
        raise OSError("disk full")

    monkeypatch.setattr(scrub_run.os, "replace", cut_short)
    with pytest.raises(ScrubError):
        scrub(config)
    assert people_file(config).read_bytes() == before
    assert [path.name for path in people_file(config).parent.iterdir()] == ["people.toml"]


def test_must_refuse_11_a_failure_partway_leaves_no_scrubbed_folder(config, scrubbed, monkeypatch):
    before = originals(config)
    write_file, written = scrub_run.write_file, []

    def fail_on_the_fourth_copy(path, text):
        written.append(path)
        if len(written) == 4:  # the list hasn't changed, so the first writes are the copies
            raise OSError("disk full")
        write_file(path, text)

    monkeypatch.setattr(scrub_run, "write_file", fail_on_the_fourth_copy)
    with pytest.raises(ScrubError, match="No scrubbed copies are left"):
        scrub(config)
    assert all("scrubbed-in-progress" in path.parts for path in written)
    data = config.data_dir
    assert not (data / "scrubbed").exists() and not (data / "scrubbed-in-progress").exists()
    assert not (data / "private" / "flags.md").exists()
    assert originals(config) == before


def test_must_refuse_12_a_second_scrub_stops_while_the_lock_is_taken(config, scrubbed):
    before = copies(config)
    lock = config.data_dir / "private" / "scrub.lock"
    lock.touch()
    with pytest.raises(ScrubError, match="delete private/scrub.lock"):
        scrub(config)
    assert lock.exists() and copies(config) == before
