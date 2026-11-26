"""Google Docs in the scrub and the push (#147): the must-refuse list from the plan, one or more
tests per item. Every doc, name, ID and value here is made up; fake secrets are built from parts."""

import hashlib
import json
import string
from dataclasses import replace
from pathlib import Path

import pytest

from diffdata.collect.google_doc import build, save_fetch
from diffdata.common.config import Config
from diffdata.common.documents import document_dir
from diffdata.discover.slack_inbox import applied_path, opt_out_stamp
from diffdata.scrub import run as scrub_run
from diffdata.scrub.people import People, Person, add_seen, read_people
from diffdata.scrub.run import scrub
from diffdata.scrub.text import Scrubber
from diffdata.store.backend import FakeStore
from diffdata.store.push import PushError, push

DOC = "FAKEDOCAAAAAAAAAAAAAAAAA1"
DOC_2 = "FAKEDOCBBBBBBBBBBBBBBBBB2"
CANVAS = "FFAKECANVAS9"
SLACK_ID, OPTED_OUT_ID = "UFAKE00001", "UFAKE00009"
WHEN = "2026-10-06T09:00:00Z"
CURRENT = f"documents/current/google-doc_{DOC}.md"
COMMENTS = f"documents/comments/google-doc_{DOC}.md"
PREFIX = "fake-team/not-approved/fake-producer/fake-project"
# A GitHub-shaped token, built from parts so the repo never holds one.
TOKEN = "gh" + "p_" + (string.ascii_letters + string.digits)[7:43]
PHONE = "(201) 555-0123"
MEETING = "https://zoom.us/j/12345678901"
# Every made-up value no copy, manifest, report, error or store name may show.
NAMES = ("Quinn", "Fakeperson", "Morgan", "Testname", "Rowan", "Optout", "Jo Shared")
REAL = (*NAMES, "fake-mail.example", TOKEN, "555-0123", "zoom.us", "perm:", "gaia:")
KEY = {
    "type": "service_account",
    "project_id": "fake-project",
    "client_email": "fake-uploader@fake-project.iam.gserviceaccount.com",
    "private_key": "FAKE-NOT-A-KEY",
}
# The people list as an earlier scrub left it: one Slack person, and one who opted out.
PEOPLE = (
    "not_names = []\n\n"
    f'[[person]]\nplaceholder = "[Person 1]"\nslack_ids = ["{SLACK_ID}"]\n'
    'names = ["Morgan Testname"]\nnicknames = []\n\n'
    f'[[person]]\nplaceholder = "[Person 2]"\nslack_ids = ["{OPTED_OUT_ID}"]\n'
    'names = ["Rowan Optout"]\nnicknames = []\n'
)


def write(path: Path, text: str) -> None:
    """Write `text` with \n line endings on every system, as the shared format and the scrub
    write their files (write_text would write \r\n on Windows)."""
    path.write_bytes(text.encode("utf-8"))


def comment(cid, name, content, quote=None, perm="1234567890", replies=()):
    found = {
        "id": cid,
        "createdTime": "2026-10-05T11:00:00.000Z",
        "author": {"displayName": name, "permissionId": perm},
        "content": content,
        "replies": [
            {
                "id": f"{cid}-r{n}",
                "createdTime": "2026-10-05T11:30:00.000Z",
                "author": {"displayName": who, "permissionId": "2234567890"},
                "content": text,
            }
            for n, (who, text) in enumerate(replies, start=1)
        ],
    }
    if quote:
        found["quotedFileContent"] = {"value": quote}
    return found


def add_doc(data: Path, doc_id=DOC, title="Fake notes", text="Plain text.\n", comments=()):
    """A doc as `collect google-docs` leaves it: one fetch, then a build."""
    files = {
        "metadata.json": json.dumps({"id": doc_id, "name": title, "createdTime": WHEN}),
        "export.txt": text,
    }
    if comments:
        files["comments.json"] = json.dumps(list(comments))
    save_fetch(data, doc_id, "20261006T090000Z", WHEN, "google-login", files)
    build(data)


def decide(home: Path, *rows: tuple[str, str]) -> None:
    """Made-up decisions in the approvals file next to config.toml. Only ever in tmp_path."""
    with (home / "approvals.jsonl").open("a", encoding="utf-8") as f:
        for found, decision in rows:
            row = {"key": found, "decision": decision, "at": WHEN, "answer": "x"}
            f.write(json.dumps(row) + "\n")


@pytest.fixture
def home(tmp_path) -> Path:
    home = tmp_path / "fake-diffdata"
    home.mkdir()
    write((home / "store-key.json"), json.dumps(KEY))
    return home


@pytest.fixture
def config(tmp_path, home) -> Config:
    data = tmp_path / "data"
    (data / "private").mkdir(parents=True)
    write((data / "private" / "people.toml"), PEOPLE)
    team = data / "slack" / "team_opted_out.json"
    team.parent.mkdir(parents=True)
    body = {"format": "diffdata-team-opted-out", "slack_opted_out": []}
    write(team, json.dumps(body | {"bucket": "fake-bucket", "folder": "fake-team"}))
    # As the last Slack build leaves it, having applied the opt-out list.
    write(applied_path(data), json.dumps({"applied": opt_out_stamp([OPTED_OUT_ID])}))
    return Config(
        data_dir=data,
        slack_channels=(),
        google_docs=(DOC,),
        slack_opted_out=(OPTED_OUT_ID,),
        path=home / "config.toml",
        store_backend="gcs",
        store_bucket="fake-bucket",
        store_folder="fake-team",
        store_producer="fake-producer",
        store_project="fake-project",
        store_send_not_approved="yes",
    )


def copies(config) -> dict[str, str]:
    folder = config.data_dir / "scrubbed"
    return {
        p.relative_to(folder).as_posix(): p.read_text(encoding="utf-8")
        for p in sorted(folder.rglob("*"))
        if p.is_file()
    }


def manifest(config) -> dict:
    return json.loads(copies(config)["manifest.json"])


def held(config) -> dict[tuple[str, str], str]:
    return {(h["source"], h["id"]): h["why"] for h in manifest(config)["held_back"]}


def placeholder_of(config, name: str) -> str:
    people = read_people(config.data_dir / "private" / "people.toml").people
    return next(p.placeholder for p in people if name in p.names)


def sent(config) -> FakeStore:
    store = FakeStore()
    push(config, lambda bucket, key: store)
    return store


def no_real_values(*texts: str) -> None:
    joined = "\n".join(texts)
    for value in REAL:
        assert value not in joined, value


# 1. A doc that isn't opted in gets no copy


@pytest.mark.parametrize(
    ("listed", "decision", "copied"),
    [
        (True, None, True),
        (False, "approved", True),
        (False, "skipped", False),
        (False, "withdrawn", False),
        (False, None, False),
    ],
)
def test_must_refuse_1_only_a_listed_or_approved_doc_gets_a_copy(
    config, home, listed, decision, copied
):
    add_doc(config.data_dir)
    if decision:
        decide(home, (f"google:{DOC}", decision))
    report = scrub(replace(config, google_docs=(DOC,) if listed else ()))
    assert (CURRENT in copies(config)) is copied
    if not copied:
        assert "clean copy(ies) skipped: canvases or docs not listed" in report


def test_must_refuse_1_with_the_approvals_file_unreadable_only_listed_docs_get_a_copy(config, home):
    add_doc(config.data_dir)
    add_doc(config.data_dir, DOC_2)
    decide(home, (f"google:{DOC_2}", "approved"))
    write((home / "approvals.jsonl"), "not JSON\n")
    report = scrub(config)
    assert CURRENT in copies(config)
    assert not any(DOC_2 in path for path in copies(config))
    assert "Couldn't read the approvals file" in report


# 2. A withdrawn doc's old copy is gone


def test_must_refuse_2_a_withdrawn_docs_earlier_copy_is_gone_after_the_next_scrub(config, home):
    add_doc(config.data_dir, comments=[comment("c1", "Quinn Fakeperson", "Looks fine.")])
    decide(home, (f"google:{DOC}", "approved"))
    unlisted = replace(config, google_docs=())
    scrub(unlisted)
    assert {CURRENT, COMMENTS} <= copies(config).keys()
    decide(home, (f"google:{DOC}", "withdrawn"))
    scrub(unlisted)
    assert not any(DOC in path for path in copies(config))
    assert not any(DOC in c["id"] for c in manifest(config)["copies"])


# 3. Push refuses a doc copy withdrawn since the scrub


def test_must_refuse_3_push_refuses_a_doc_withdrawn_since_the_scrub(config, home):
    add_doc(config.data_dir, title="Quinn Fakeperson notes")
    decide(home, (f"google:{DOC}", "approved"))
    unlisted = replace(config, google_docs=())
    scrub(unlisted)
    decide(home, (f"google:{DOC}", "withdrawn"))
    store = FakeStore()
    with pytest.raises(PushError) as e:
        push(unlisted, lambda bucket, key: store)
    assert "may have been withdrawn" in str(e.value) and "Nothing was sent." in str(e.value)
    assert store.objects == {}
    no_real_values(str(e.value))
    assert DOC not in str(e.value)


def test_must_refuse_3_push_refuses_an_unlisted_doc_when_approvals_cant_be_read(config, home):
    add_doc(config.data_dir)
    decide(home, (f"google:{DOC}", "approved"))
    unlisted = replace(config, google_docs=())
    scrub(unlisted)
    write((home / "approvals.jsonl"), "not JSON\n")
    store = FakeStore()
    with pytest.raises(PushError, match="can't be read"):
        push(unlisted, lambda bucket, key: store)
    assert store.objects == {}


def test_must_refuse_3_a_listed_doc_still_goes_with_approvals_unreadable(config, home):
    add_doc(config.data_dir)
    write((home / "approvals.jsonl"), "not JSON\n")
    scrub(config)
    names = [k for k in sent(config).objects if "/pushes/" not in k]
    assert len(names) == 1 and names[0].startswith(f"{PREFIX}/google-doc/")


# 4. A doc copy whose header doesn't name its doc


def test_must_refuse_4_a_clean_copy_whose_header_names_another_doc_is_held_back(config):
    add_doc(config.data_dir)
    path = next((config.data_dir / "documents" / "current").glob(f"*_{DOC}.md"))
    write(path, path.read_text("utf-8").replace(f"id: {DOC}", f"id: {DOC_2}"))
    scrub(config)
    assert CURRENT not in copies(config)
    assert held(config)[("google-doc", DOC)] == "its header doesn't name its doc"


def test_must_refuse_4_two_clean_copies_of_one_doc_are_held_back(config):
    add_doc(config.data_dir)
    current = config.data_dir / "documents" / "current"
    first = next(current.glob(f"*_{DOC}.md"))
    (current / f"google-doc_old-title_{DOC}.md").write_bytes(first.read_bytes())
    scrub(config)
    assert CURRENT not in copies(config)
    assert held(config)[("google-doc", DOC)] == "two originals would make this one copy"


# 5. A commenter's name never reaches a copy


def test_must_refuse_5_a_commenter_not_in_slack_gets_a_placeholder_everywhere(config):
    add_doc(
        config.data_dir,
        title="Notes for Quinn Fakeperson",
        text="Quinn Fakeperson wrote this part.\n",
        comments=[
            comment("c1", "Quinn Fakeperson", "My section.", quote="Quinn Fakeperson wrote"),
            comment("c2", "Morgan Testname", "Thanks Quinn."),
        ],
    )
    report = scrub(config)
    found = copies(config)
    quinn = placeholder_of(config, "Quinn Fakeperson")
    assert quinn == "[Person 3]"
    assert f"**{quinn}** ·" in found[COMMENTS]
    assert f"{quinn} wrote this part." in found[CURRENT]
    assert f"Notes for {quinn}" in found[CURRENT] and f"Thanks {quinn}." in found[COMMENTS]
    no_real_values(*found.values(), report)


@pytest.mark.parametrize("name", ["Q", "Fakeword"])
def test_must_refuse_5_a_one_letter_or_not_names_author_still_becomes_a_placeholder(config, name):
    people = config.data_dir / "private" / "people.toml"
    write(people, PEOPLE.replace("not_names = []", 'not_names = ["Fakeword"]'))
    add_doc(config.data_dir, comments=[comment("c1", name, "A comment.")])
    scrub(config)
    text = copies(config)[COMMENTS]
    assert f"**{placeholder_of(config, name)}** ·" in text
    assert f"**{name}**" not in text


# 6. Same person, same placeholder


def test_must_refuse_6_a_slack_persons_full_name_as_author_gets_their_number(config):
    add_doc(
        config.data_dir,
        text="Ask Morgan Testname.\n",
        comments=[comment("c1", "MORGAN TESTNAME", "Done.")],
    )
    scrub(config)
    found = copies(config)
    assert "**[Person 1]** ·" in found[COMMENTS]
    assert "Ask [Person 1]." in found[CURRENT]
    # No second person was added for the same name.
    people = read_people(config.data_dir / "private" / "people.toml").people
    assert len(people) == 2


# 7. A shared name isn't given to one person


def test_must_refuse_7_an_author_name_two_people_have_becomes_name_and_is_flagged(config):
    people = PEOPLE.replace('names = ["Morgan Testname"]', 'names = ["Morgan Testname"]')
    people += (
        '\n[[person]]\nplaceholder = "[Person 3]"\nslack_ids = ["UFAKE00003"]\n'
        'names = ["Jo Shared"]\nnicknames = []\n\n'
        '[[person]]\nplaceholder = "[Person 4]"\nslack_ids = ["UFAKE00004"]\n'
        'names = ["Jo Shared"]\nnicknames = []\n'
    )
    write((config.data_dir / "private" / "people.toml"), people)
    add_doc(config.data_dir, comments=[comment("c1", "Jo Shared", "Hello.")])
    scrub(config)
    assert "**[name]** ·" in copies(config)[COMMENTS]
    flags = (config.data_dir / "private" / "flags.md").read_text(encoding="utf-8")
    assert '"Jo Shared", shared by [Person 3] and [Person 4]' in flags


# 8. No email or Google ID in a copy


def test_must_refuse_8_emails_and_google_ids_never_reach_a_copy(config):
    mail = "quinn@fake-mail.example"
    add_doc(
        config.data_dir,
        title=f"From {mail}",
        text=f"Write to {mail}.\n",
        comments=[
            comment("c1", "someone@fake-mail.example", f"Or {mail}", quote=f"to {mail}"),
        ],
    )
    scrub(config)
    found = copies(config)
    assert "**[no name]** ·" in found[COMMENTS]
    assert found[CURRENT].count("[email]") == 2 and found[COMMENTS].count("[email]") == 4
    no_real_values(*found.values())


@pytest.mark.parametrize("found", ["gaia:1234567890", "perm:p1", "gaia:111", "PERM%3A12ab"])
def test_must_refuse_8_a_google_person_id_in_the_text_becomes_name_maybe(config, found):
    add_doc(config.data_dir, text=f"Edited by {found} today.\n")
    scrub(config)
    assert "Edited by [name?] today." in copies(config)[CURRENT]
    flags = (config.data_dir / "private" / "flags.md").read_text(encoding="utf-8")
    assert "## A Google person ID" in flags and "## A Slack ID" not in flags


def test_must_refuse_8_the_last_check_holds_back_a_copy_with_a_google_id(config, monkeypatch):
    add_doc(config.data_dir, text="Edited by gaia:1234567890 today.\n")
    monkeypatch.setattr(Scrubber, "slack_ids", lambda self, text, flags: text)
    scrub(config)
    assert CURRENT not in copies(config)
    assert held(config)[("google-doc", DOC)] == "the last check found a Google person ID"


# 9. Secrets, phone numbers and meeting links in a doc


def test_must_refuse_9_secrets_phones_and_meeting_links_are_replaced_everywhere(config):
    add_doc(
        config.data_dir,
        title=f"Call {PHONE}",
        text=f"Token {TOKEN}\nJoin {MEETING}\n",
        comments=[comment("c1", "Morgan Testname", f"Ring {PHONE} or {MEETING}", quote=TOKEN)],
    )
    scrub(config)
    found = copies(config)
    assert "[secret]" in found[CURRENT] and "[meeting link]" in found[CURRENT]
    assert "Call [phone]" in found[CURRENT]
    assert "[phone]" in found[COMMENTS] and "[meeting link]" in found[COMMENTS]
    assert "> [secret]" in found[COMMENTS]
    no_real_values(*found.values())


def test_must_refuse_9_a_phone_number_only_the_last_check_sees_holds_both_copies_back(
    config, monkeypatch
):
    add_doc(config.data_dir, text=f"Ring {PHONE}\n", comments=[comment("c1", "X Y", PHONE)])
    monkeypatch.setattr(Scrubber, "phone_numbers", lambda self, text, flags: text)
    scrub(config)
    assert not any(DOC in path for path in copies(config))
    assert held(config)[("google-doc", DOC)] == "the last check found a phone number"
    assert held(config)[("google-doc-comments", DOC)] == "the last check found a phone number"


# 10. A title in a path


def test_must_refuse_10_a_title_is_in_no_path_manifest_entry_or_store_name(config):
    add_doc(
        config.data_dir,
        title=f"Quinn Fakeperson {PHONE}",
        comments=[comment("c1", "Quinn Fakeperson", "x")],
    )
    report = scrub(config)
    found = copies(config)
    assert set(found) == {"manifest.json", CURRENT, COMMENTS}
    assert 'title: "[Person 3] [phone]"' in found[CURRENT]
    store = sent(config)
    no_real_values(*found, found["manifest.json"], report, *store.objects)
    assert "quinn" not in "".join(store.objects).lower()


# 11. Comments by an opted-out person


def test_must_refuse_11_comments_by_someone_who_opted_out_are_left_out(config):
    add_doc(
        config.data_dir,
        comments=[
            comment("c1", "Rowan Optout", "My own words.", replies=[("Morgan Testname", "Ok.")]),
            comment("c2", "Morgan Testname", "Fine.", replies=[("rowan optout", "Mine too.")]),
        ],
    )
    report = scrub(config)
    text = copies(config)[COMMENTS]
    assert "My own words." not in text and "Mine too." not in text
    assert "Ok." in text and "Fine." in text
    assert "2 comment(s) or reply(ies) by people who opted out were left out." in text
    assert "2 Google Doc comment(s) or reply(ies) by people who opted out" in report
    no_real_values(text, report)


# 12. Push sends only what a scrub makes


def test_must_refuse_12_push_sends_exactly_the_two_doc_copies(config):
    add_doc(config.data_dir, comments=[comment("c1", "Morgan Testname", "Fine.")])
    scrub(config)
    entries = {c["source"]: c["sha256"][:16] for c in manifest(config)["copies"]}
    store = sent(config)
    names = {k for k in store.objects if "/pushes/" not in k}
    assert names == {
        f"{PREFIX}/google-doc/2026-10-06/{DOC}.{entries['google-doc']}.md",
        f"{PREFIX}/google-doc/2026-10-06/{DOC}.comments.{entries['google-doc-comments']}.md",
    }
    body = b"".join(store.objects.values()).decode("utf-8")
    assert "Plain text." in body and "raw/" not in body and "perm:" not in body


@pytest.mark.parametrize(
    "edit",
    [
        lambda c: c.update(source="slack-canvas"),
        lambda c: c.update(path=f"documents/current/google-doc_{DOC}.txt"),
        lambda c: c.update(id=DOC_2),
    ],
)
def test_must_refuse_12_a_doc_entry_a_scrub_doesnt_make_is_refused(config, edit):
    add_doc(config.data_dir)
    scrub(config)
    path = config.data_dir / "scrubbed" / "manifest.json"
    found = json.loads(path.read_text("utf-8"))
    edit(found["copies"][0])
    write(path, json.dumps(found))
    with pytest.raises(PushError, match="Nothing was sent"):
        sent(config)


def test_must_refuse_12_a_doc_copy_edited_after_the_scrub_is_refused(config):
    add_doc(config.data_dir)
    scrub(config)
    path = config.data_dir / "scrubbed" / CURRENT
    write(path, path.read_text("utf-8") + "Quinn Fakeperson\n")
    with pytest.raises(PushError, match="changed after the scrub"):
        sent(config)


# 13. Nothing quotes data


def test_must_refuse_13_no_report_error_or_manifest_quotes_doc_data(config, monkeypatch):
    add_doc(
        config.data_dir,
        title="Quinn Fakeperson plan",
        text=f"Morgan Testname: {TOKEN} {PHONE}\n",
        comments=[comment("c1", "Rowan Optout", "secret words"), comment("c2", "Q P", MEETING)],
    )
    add_doc(config.data_dir, DOC_2, title="Quinn Fakeperson again", text=f"Ring {PHONE}\n")
    monkeypatch.setattr(Scrubber, "phone_numbers", lambda self, text, flags: text)
    report = scrub(replace(config, google_docs=(DOC, DOC_2)))
    found = copies(config)
    no_real_values(report, found["manifest.json"])
    assert "secret words" not in report + found["manifest.json"]


# 14. Approved canvases


def add_canvas(data: Path) -> None:
    path = data / "documents" / "current" / f"slack-canvas_fake-canvas_{CANVAS}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    write(
        path,
        f'---\ntitle: "Fake canvas"\nsource: slack-canvas\nid: {CANVAS}\n'
        f"last_changed: 2026-10-06T09:00:00Z\n---\n\nPlan.\n",
    )


def test_must_refuse_14_an_approved_canvas_gets_a_copy_and_a_withdrawn_one_doesnt(config, home):
    add_canvas(config.data_dir)
    canvas_copy = f"documents/current/slack-canvas_{CANVAS}.md"
    decide(home, (f"slack-file:{CANVAS}", "approved"))
    scrub(config)
    assert canvas_copy in copies(config)
    decide(home, (f"slack-file:{CANVAS}", "withdrawn"))
    scrub(config)
    assert canvas_copy not in copies(config)


def test_must_refuse_14_push_refuses_a_canvas_withdrawn_since_the_scrub(config, home):
    add_canvas(config.data_dir)
    decide(home, (f"slack-file:{CANVAS}", "approved"))
    scrub(config)
    decide(home, (f"slack-file:{CANVAS}", "withdrawn"))
    with pytest.raises(PushError, match="may have been withdrawn"):
        sent(config)


# And the rest of the plan


def test_a_mention_placeholder_is_never_matched_as_a_name(config):
    people = config.data_dir / "private" / "people.toml"
    write(people, PEOPLE.replace('names = ["Morgan Testname"]', 'names = ["Mention"]'))
    add_doc(config.data_dir, comments=[comment("c1", "A B", "+quinn@fake-mail.example see")])
    scrub(config)
    assert "@[mention] see" in copies(config)[COMMENTS]


def test_a_second_scrub_writes_the_same_doc_copies_and_numbers(config):
    add_doc(config.data_dir, comments=[comment("c1", "Quinn Fakeperson", "x")])
    scrub(config)
    before = copies(config), (config.data_dir / "private" / "people.toml").read_bytes()
    scrub(config)
    assert (copies(config), (config.data_dir / "private" / "people.toml").read_bytes()) == before


def test_opted_in_reads_listed_and_approved_canvases_and_docs(config, home):
    decide(home, (f"google:{DOC_2}", "approved"), (f"slack-file:{CANVAS}", "approved"))
    opted = scrub_run.opted_in(config)
    assert opted.docs == {DOC, DOC_2} and opted.canvases == {CANVAS} and opted.approvals_read


# Fixes from the first Codex review


def test_a_doc_withdrawn_while_push_runs_isnt_sent(config, home):
    add_doc(config.data_dir)
    decide(home, (f"google:{DOC}", "approved"))
    unlisted = replace(config, google_docs=())
    scrub(unlisted)
    store = FakeStore()

    def opener(bucket, key):
        decide(home, (f"google:{DOC}", "withdrawn"))  # during sign-in
        return store

    with pytest.raises(PushError, match="withdrawn while this push ran"):
        push(unlisted, opener)
    assert not any(DOC in name for name in store.objects if "/pushes/" not in name)


def test_a_canvas_clean_copy_whose_header_names_another_canvas_is_held_back(config, home):
    add_canvas(config.data_dir)
    path = config.data_dir / "documents" / "current" / f"slack-canvas_fake-canvas_{CANVAS}.md"
    other = path.with_name("slack-canvas_fake-canvas_FFAKECANVAS1.md")
    path.rename(other)
    decide(home, ("slack-file:FFAKECANVAS1", "approved"))
    scrub(config)
    assert held(config)[("slack-canvas", "FFAKECANVAS1")] == "its header doesn't name its canvas"


def test_push_refuses_a_canvas_copy_whose_header_names_another_canvas(config, home):
    add_canvas(config.data_dir)
    decide(home, (f"slack-file:{CANVAS}", "approved"), ("slack-file:FFAKECANVAS1", "approved"))
    scrub(config)
    folder = config.data_dir / "scrubbed"
    old, new = (
        f"documents/current/slack-canvas_{CANVAS}.md",
        "documents/current/slack-canvas_FFAKECANVAS1.md",
    )
    (folder / old).rename(folder / new)
    found = json.loads((folder / "manifest.json").read_text("utf-8"))
    found["copies"][0].update(path=new, id="FFAKECANVAS1")
    write((folder / "manifest.json"), json.dumps(found))
    with pytest.raises(PushError, match="doesn't name its canvas or doc"):
        sent(config)


def test_push_refuses_a_comments_copy_after_a_commenter_is_found_to_have_opted_out(config):
    add_doc(config.data_dir, comments=[comment("c1", "Quinn Fakeperson", "Mine.")])
    scrub(config)
    people = config.data_dir / "private" / "people.toml"
    write(
        people,
        people.read_text("utf-8").replace(
            'names = ["Rowan Optout"]\nnicknames = []',
            'names = ["Rowan Optout"]\nnicknames = ["Quinn Fakeperson"]',
        ),
    )
    with pytest.raises(PushError, match="isn't what a scrub makes from its doc now"):
        sent(config)


def test_push_refuses_a_comments_copy_made_by_hand_for_a_doc_with_no_comments(config):
    add_doc(config.data_dir)
    scrub(config)
    folder = config.data_dir / "scrubbed"
    forged = f"---\nsource: google-doc-comments\nid: {DOC}\nlast_changed: {WHEN}\n---\nHi.\n"
    (folder / COMMENTS).parent.mkdir(parents=True)
    write((folder / COMMENTS), forged)
    found = json.loads((folder / "manifest.json").read_text("utf-8"))
    entry = dict(found["copies"][0], source="google-doc-comments", path=COMMENTS)
    entry["sha256"] = hashlib.sha256(forged.encode("utf-8")).hexdigest()
    found["copies"].append(entry)
    write((folder / "manifest.json"), json.dumps(found))
    with pytest.raises(PushError, match="isn't what a scrub makes from its doc now"):
        sent(config)


def test_an_opted_out_name_with_no_letters_still_leaves_their_comments_out(config):
    people = config.data_dir / "private" / "people.toml"
    # Only the person who opted out (the last entry) has it.
    write(people, PEOPLE.removesuffix("nicknames = []\n") + 'nicknames = ["007"]\n')
    add_doc(config.data_dir, comments=[comment("c1", "007", "Their words.")])
    scrub(config)
    assert "Their words." not in copies(config)[COMMENTS]


def test_a_line_separator_in_a_comment_doesnt_stop_its_authors_being_learned(config):
    add_doc(
        config.data_dir,
        title="For Quinn Fakeperson",
        comments=[comment("c1", "Quinn Fakeperson", "One\u2028two")],
    )
    scrub(config)
    found = copies(config)
    assert {CURRENT, COMMENTS} <= found.keys()
    no_real_values(*found.values())


def test_a_name_with_quotes_in_a_title_is_replaced(config):
    add_doc(
        config.data_dir,
        title='Notes by J "Q" Fakeperson',
        comments=[comment("c1", 'J "Q" Fakeperson', "x")],
    )
    scrub(config)
    found = copies(config)
    assert 'title: "Notes by [Person 3]"' in found[CURRENT]
    assert 'title: "Notes by [Person 3]"' in found[COMMENTS]
    assert sent(config).objects


def test_a_doc_only_person_keeps_their_number_when_slack_first_sees_them():
    people = People([Person("[Person 1]", [], ["Quinn Fakeperson"], [])])
    assert add_seen(people, {"UFAKE00005": {"Quinn Fakeperson", "quinn.f"}}) == 0
    assert people.people[0].slack_ids == ["UFAKE00005"]
    assert "quinn.f" in people.people[0].names


def test_a_commenter_called_doc_doesnt_break_the_header(config):
    add_doc(config.data_dir, text="Doc wrote this.\n", comments=[comment("c1", "Doc", "Hello.")])
    scrub(config)
    found = copies(config)
    assert "source: google-doc\n" in found[CURRENT] and "[Person 3] wrote this." in found[CURRENT]
    assert "source: google-doc-comments\n" in found[COMMENTS]
    assert len([k for k in sent(config).objects if "/pushes/" not in k]) == 2


# Fixes from the second Codex review


def test_a_password_named_in_the_title_is_replaced_in_the_body_too(config):
    add_doc(config.data_dir, title="Wifi password=kettle42x", text="Type kettle42x to log in.\n")
    scrub(config)
    found = copies(config)[CURRENT]
    assert "kettle42x" not in found and "Type [secret] to log in." in found


def test_a_doc_taken_off_google_docs_during_sign_in_isnt_sent(config, home):
    add_doc(config.data_dir)
    scrub(config)
    store = FakeStore()

    def opener(bucket, key):  # config.toml edited during sign-in: the doc is no longer listed
        data = config.data_dir.as_posix()
        write(
            home / "config.toml", f'[local]\ndata_dir = "{data}"\n\n[sources]\ngoogle_docs = []\n'
        )
        return store

    with pytest.raises(PushError, match="withdrawn while this push ran"):
        push(config, opener)
    assert not any("/google-doc/" in name for name in store.objects)


def test_a_doc_withdrawn_during_sign_in_stops_the_push_before_any_copy(config, home):
    add_doc(config.data_dir, DOC_2)
    add_doc(config.data_dir)
    decide(home, (f"google:{DOC}", "approved"), (f"google:{DOC_2}", "approved"))
    unlisted = replace(config, google_docs=())
    scrub(unlisted)
    store = FakeStore()

    def opener(bucket, key):
        decide(home, (f"google:{DOC_2}", "withdrawn"))
        return store

    with pytest.raises(PushError, match="0 copy\\(ies\\) were added"):
        push(unlisted, opener)
    assert not any("/google-doc/" in name for name in store.objects)


def test_push_refuses_when_another_doc_has_a_commenter_the_list_doesnt_have(config):
    add_doc(config.data_dir, title="Notes for Quinn Fakeperson")
    scrub(config)
    add_doc(config.data_dir, DOC_2, comments=[comment("c1", "Quinn Fakeperson", "Hi.")])
    with pytest.raises(PushError, match="commenter the people list doesn't have yet"):
        sent(replace(config, google_docs=(DOC, DOC_2)))


def test_a_doc_with_no_events_file_is_held_back(config):
    add_doc(config.data_dir)
    (document_dir(config.data_dir, "google-doc", DOC) / "events.jsonl").unlink()
    scrub(config)
    assert held(config)[("google-doc", DOC)] == "its comments can't be read"


def test_push_refuses_a_doc_with_two_clean_copies_left_after_the_scrub(config):
    add_doc(config.data_dir)
    scrub(config)
    current = config.data_dir / "documents" / "current"
    first = next(current.glob(f"*_{DOC}.md"))
    (current / f"google-doc_old-title_{DOC}.md").write_bytes(first.read_bytes())
    with pytest.raises(PushError, match="isn't what a scrub makes from its doc now"):
        sent(config)


def test_a_google_permission_id_with_no_digits_is_replaced(config):
    add_doc(config.data_dir, text="Shared with perm:abcXYZ today.\n")
    scrub(config)
    assert "Shared with [name?] today." in copies(config)[CURRENT]


def test_two_new_slack_people_with_a_doc_only_persons_name_both_get_new_numbers():
    people = People([Person("[Person 1]", [], ["Quinn Fakeperson"], [])])
    seen = {"UFAKE00005": {"Quinn Fakeperson"}, "UFAKE00006": {"Quinn Fakeperson"}}
    assert add_seen(people, seen) == 2
    assert people.people[0].slack_ids == []


def test_an_author_with_no_name_isnt_given_to_a_person_called_someone(config):
    write(config.data_dir / "private" / "people.toml", PEOPLE.replace("Morgan Testname", "Someone"))
    add_doc(config.data_dir, comments=[comment("c1", "", "Anonymous.")])
    scrub(config)
    assert "**[no name]** ·" in copies(config)[COMMENTS]
