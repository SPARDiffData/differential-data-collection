"""`diffdata push` (STO-1, STO-2, STO-3, STO-9, STO-11), against a fake store. Synthetic data
only: no real bucket, credentials or network."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from diffdata import cli
from diffdata.common.config import Config, ConfigError, load_config
from diffdata.discover.slack_export import build
from diffdata.discover.slack_inbox import opt_out_stamp
from diffdata.scrub import secrets
from diffdata.scrub.run import ScrubError, scrub
from diffdata.scrub.text import CHECKS
from diffdata.store import push as push_module
from diffdata.store.backend import FakeStore, SignInError, StoreError
from diffdata.store.push import PushError, push

PREFIX = "fake-team/not-approved/fake-producer/fake-project"
DAY = "slack/export/CFAKE0001/2026-01-05.md"
DAY_2 = "slack/export/CFAKE0001/2026-01-06.md"
CANVAS = "documents/current/slack-canvas_FFAKE0001.md"
DAY_TEXT = "---\nchannel_id: CFAKE0001\n---\n\n[Person 1]: the plan looks good\n"
DAY_2_TEXT = "---\nchannel_id: CFAKE0001\n---\n\n[Person 2]: agreed, ship it\n"
CANVAS_TEXT = (
    '---\ntitle: "Fake plan"\nsource: slack-canvas\nid: FFAKE0001\n'
    "last_changed: 2026-01-07T10:00:00Z\ncollected_at: 2026-01-08T09:00:00Z\n---\n\n"
    "# Plan\n[Person 1] owns this.\n"
)
# What must never leave the computer. Each private file holds one of these.
SECRETS = ("Fakey Realname", "UFAKE00099", "raw unscrubbed words", "FAKE-RAW-STORE")
# The people list a scrub leaves in private/, which push uses to run the last check again.
PEOPLE = (
    'not_names = []\n\n[[person]]\nplaceholder = "[Person 1]"\n'
    f'slack_ids = ["{SECRETS[1]}"]\nnames = ["{SECRETS[0]}"]\nnicknames = []\n'
)


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def entry(path: str, text: str) -> dict:
    if path.startswith("slack/"):
        _, _, channel, day = path.removesuffix(".md").split("/")
        source, copy_id = "slack", f"{channel}/{day}"
    else:
        source, copy_id = "slack-canvas", path.rsplit("_", 1)[1].removesuffix(".md")
    return {
        "source": source,
        "id": copy_id,
        "path": path,
        "sha256": sha(text),
        "checks": list(CHECKS),
        "flags": 0,
    }


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def make_scrub(data: Path, copies: dict[str, str], held=(), manifest_edit=None) -> None:
    """A data folder as a scrub leaves it: scrubbed/ with its manifest, next to the private and
    raw files that must never be sent."""
    for path, text in copies.items():
        write(data / "scrubbed" / path, text)
    manifest = {
        "tool": "diffdata 0.0.1",
        "approved_for_release": False,
        "note": "Not approved for release.",
        "opt_out_stamp": opt_out_stamp([]),
        "copies": [entry(path, text) for path, text in copies.items()],
        "held_back": [
            {"source": s, "id": i, "why": "the last check found a name"} for s, i in held
        ],
    }
    if manifest_edit:
        manifest_edit(manifest)
    write(data / "scrubbed" / "manifest.json", json.dumps(manifest, indent=2))
    write(data / "private" / "people.toml", PEOPLE)
    write(data / "private" / "flags.md", f"- {SECRETS[0]}\n")
    write(data / "slack" / "left_out.json", json.dumps({SECRETS[1]: SECRETS[0]}))
    write(data / "slack" / "inbox" / "run" / "0001.json", json.dumps({"text": SECRETS[2]}))
    write(data / "slack" / "store" / "CFAKE0001" / "messages.jsonl", json.dumps({"t": SECRETS[3]}))
    write(
        data / "slack" / "export" / "fake-channel" / "2026-01-05.md", f"{SECRETS[0]}: {SECRETS[2]}"
    )


# A made-up service account key: the shape Google gives, with no real value in it.
KEY = {
    "type": "service_account",
    "project_id": "fake-project",
    "client_email": "fake-uploader@fake-project.iam.gserviceaccount.com",
    "private_key": "FAKE-NOT-A-KEY",
}


def config(data: Path, key: dict | None = KEY, **store) -> Config:
    """Settings for a push, with config.toml's folder next to the data folder and the team's
    key saved there, as in ~/.diffdata/."""
    settings = {
        "backend": "gcs",
        "bucket": "fake-pilot-bucket",
        "folder": "fake-team",
        "producer": "fake-producer",
        "project": "fake-project",
        "send_not_approved": "yes",
    } | store
    home = data.parent / "fake-diffdata"
    home.mkdir(parents=True, exist_ok=True)
    if key is not None:
        (home / "store-key.json").write_text(json.dumps(key), encoding="utf-8")
    return Config(
        data_dir=data,
        slack_channels=(),
        # make_scrub's made-up inbox and store folders are read as channels, so each needs an
        # entry (#118).
        slack_notices=(("run", "none"), ("CFAKE0001", "none")),
        path=home / "config.toml",
        **{f"store_{k}": v for k, v in settings.items()},
    )


@pytest.fixture
def data(tmp_path) -> Path:
    data = tmp_path / "data"
    make_scrub(data, {DAY: DAY_TEXT, DAY_2: DAY_2_TEXT, CANVAS: CANVAS_TEXT})
    return data


class Opener:
    """Opens a fake store, and remembers whether the push got that far."""

    def __init__(self, store: FakeStore | None = None, error: Exception | None = None):
        self.store = store if store is not None else FakeStore()
        self.error = error
        self.opened = 0

    def __call__(self, bucket: str, key: dict) -> FakeStore:
        self.opened += 1
        self.key = key
        if self.error:
            raise self.error
        return self.store


def refused(cfg: Config, *words: str) -> str:
    """Push and expect a refusal before the store was opened. Returns the message."""
    opener = Opener()
    with pytest.raises(PushError) as e:
        push(cfg, opener)
    assert opener.opened == 0, "the store was opened before the refusal"
    assert opener.store.objects == {}
    for word in words:
        assert word in str(e.value)
    assert "Nothing was sent." in str(e.value)
    return str(e.value)


def records(store: FakeStore) -> list[dict]:
    return [json.loads(v) for k, v in store.objects.items() if k.startswith(f"{PREFIX}/pushes/")]


def assert_no_secrets(store: FakeStore, *texts: str) -> None:
    sent = (
        b"".join(store.objects.values()).decode("utf-8") + "".join(store.objects) + "".join(texts)
    )
    for secret in SECRETS:
        assert secret not in sent


# What gets sent


def test_sends_each_passed_copy_to_its_place_in_the_layout(data):
    opener = Opener()
    report = push(config(data), opener)
    names = {k for k in opener.store.objects if "/pushes/" not in k}
    assert names == {
        f"{PREFIX}/slack/2026-01-05/CFAKE0001.{sha(DAY_TEXT)[:16]}.md",
        f"{PREFIX}/slack/2026-01-06/CFAKE0001.{sha(DAY_2_TEXT)[:16]}.md",
        f"{PREFIX}/slack-canvas/2026-01-07/FFAKE0001.{sha(CANVAS_TEXT)[:16]}.md",
    }
    sent = {opener.store.objects[n].decode("utf-8") for n in names}
    assert sent == {DAY_TEXT, DAY_2_TEXT, CANVAS_TEXT}
    assert "3 scrubbed copy(ies) added" in report
    assert "not approved for release" in report


def test_private_and_raw_files_are_never_sent_even_next_to_the_copies(data):
    # Files a scrub didn't make, dropped into scrubbed/ itself: not on the manifest, so not sent.
    write(data / "scrubbed" / "slack" / "notes.md", SECRETS[2])
    write(data / "scrubbed" / "people.toml", SECRETS[0])
    write(data / "scrubbed" / "slack" / "left_out.json", SECRETS[1])
    opener = Opener()
    report = push(config(data), opener)
    assert len(opener.store.objects) == 4  # three copies and the record
    assert_no_secrets(opener.store, report)


@pytest.mark.parametrize(
    "path",
    [
        "../private/people.toml",
        "../slack/left_out.json",
        "slack/export/CFAKE0001/../../../private/people.toml",
        "/etc/passwd",
        "C:/Windows/win.ini",
        "manifest.json",
        "slack/export/CFAKE0001/notes.md",
    ],
)
def test_a_manifest_entry_that_isnt_a_scrubbed_copy_stops_the_push(tmp_path, path):
    def edit(manifest):
        manifest["copies"].append(dict(manifest["copies"][0], path=path))

    make_scrub(tmp_path, {DAY: DAY_TEXT}, manifest_edit=edit)
    message = refused(config(tmp_path), "isn't one a scrub makes")
    assert path not in message.replace("scrubbed/manifest.json", "")


def test_a_manifest_entry_whose_id_doesnt_match_its_path_stops_the_push(tmp_path):
    def edit(manifest):
        manifest["copies"][0]["id"] = "CFAKE0002/2026-01-05"

    make_scrub(tmp_path, {DAY: DAY_TEXT}, manifest_edit=edit)
    refused(config(tmp_path), "isn't one a scrub makes")


def test_a_held_back_copy_is_never_sent_even_if_its_file_is_there(tmp_path):
    make_scrub(tmp_path, {DAY: DAY_TEXT}, held=[("slack", "CFAKE0001/2026-01-06")])
    write(tmp_path / "scrubbed" / DAY_2, "a copy the last check held back")  # planted
    opener = Opener()
    report = push(config(tmp_path), opener)
    sent = [k for k in opener.store.objects if "/pushes/" not in k]
    assert sent == [f"{PREFIX}/slack/2026-01-05/CFAKE0001.{sha(DAY_TEXT)[:16]}.md"]
    assert "1 copy(ies) the scrub held back were not sent" in report
    (record,) = records(opener.store)
    assert "held_back" not in record
    assert "why" not in json.dumps(record)


def test_a_copy_listed_as_both_passed_and_held_back_stops_the_push(tmp_path):
    make_scrub(tmp_path, {DAY: DAY_TEXT}, held=[("slack", "CFAKE0001/2026-01-05")])
    refused(config(tmp_path), "both passed and held back")


def test_a_copy_listed_twice_stops_the_push(tmp_path):
    def edit(manifest):
        manifest["copies"].append(dict(manifest["copies"][0]))

    make_scrub(tmp_path, {DAY: DAY_TEXT}, manifest_edit=edit)
    refused(config(tmp_path), "twice")


def test_a_copy_changed_after_the_scrub_stops_the_push(data):
    write(data / "scrubbed" / DAY_2, DAY_2_TEXT.replace("[Person 2]", SECRETS[0]))
    message = refused(config(data), "changed after the scrub", "Copy 2")
    assert SECRETS[0] not in message and DAY_2 not in message


def test_a_listed_copy_thats_missing_stops_the_push(data):
    (data / "scrubbed" / DAY).unlink()
    refused(config(data), "missing")


def test_a_canvas_copy_with_no_date_is_left_out_and_counted(tmp_path):
    undated = CANVAS_TEXT.replace("last_changed: 2026-01-07T10:00:00Z\n", "")
    make_scrub(tmp_path, {DAY: DAY_TEXT, CANVAS: undated})
    opener = Opener()
    report = push(config(tmp_path), opener)
    assert not any("slack-canvas" in k for k in opener.store.objects)
    assert "1 canvas copy(ies) had no last_changed date" in report


# [store] settings


@pytest.mark.parametrize("key", ["backend", "bucket", "folder", "producer", "project"])
def test_an_empty_store_setting_stops_the_push_before_signing_in(data, key):
    refused(config(data, **{key: ""}), key, "empty")


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("backend", "s3"),
        ("bucket", "Fake Bucket"),
        ("bucket", "a"),
        ("folder", "fake/team"),
        ("folder", ".."),
        ("producer", "fake producer"),
        ("project", "../other-team"),
    ],
)
def test_a_wrong_store_setting_stops_the_push_before_signing_in(data, key, value):
    message = refused(config(data, **{key: value}), key)
    if len(value) > 2:  # its value is never quoted back
        assert value not in message.replace("diffdata", "")


def test_store_settings_are_read_from_config_toml(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        f'[local]\ndata_dir = "{(tmp_path / "data").as_posix()}"\n\n[store]\nbackend = "gcs"\n'
        'bucket = "fake-pilot-bucket"\nfolder = "fake-team"\nproducer = "fake-producer"\n'
        'project = "fake-project"\n',
        encoding="utf-8",
    )
    loaded = load_config(path)
    assert (loaded.store_bucket, loaded.store_producer, loaded.store_project) == (
        "fake-pilot-bucket",
        "fake-producer",
        "fake-project",
    )


def test_a_store_setting_that_isnt_text_stops_the_run(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        f'[local]\ndata_dir = "{(tmp_path / "data").as_posix()}"\n\n[store]\nbucket = 5\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match=r"\[store\]"):
        load_config(path)


# The state of the data folder


def test_no_scrub_yet_stops_the_push(tmp_path):
    refused(config(tmp_path / "data"), "Run uv run diffdata scrub first")


def test_a_missing_manifest_stops_the_push(data):
    (data / "scrubbed" / "manifest.json").unlink()
    refused(config(data), "manifest.json")


@pytest.mark.parametrize(
    "text",
    [
        "{not json",
        "[]",
        '{"copies": [], "held_back": []}',
        '{"copies": {}, "held_back": [], "approved_for_release": false}',
        '{"copies": [{"path": "x"}], "held_back": [], "approved_for_release": false}',
        '{"copies": [], "held_back": ["x"], "approved_for_release": false}',
    ],
)
def test_a_manifest_not_in_the_shape_a_scrub_writes_stops_the_push(data, text):
    write(data / "scrubbed" / "manifest.json", text)
    refused(config(data), "manifest.json")


@pytest.mark.parametrize(
    "left, words",
    [
        ("private/scrub.lock", "A scrub or another push is running"),
        ("scrubbed-in-progress/manifest.json", "A scrub stopped without finishing"),
    ],
)
def test_a_scrub_running_or_cut_short_stops_the_push(data, left, words):
    write(data / left, "")
    refused(config(data), words)
    assert (data / left).exists(), "the push removed a lock or folder it didn't make"


def symlink(link: Path, target: Path) -> None:
    try:
        os.symlink(target, link)
    except OSError:
        pytest.skip("this system doesn't allow symlinks here")


def test_a_link_inside_scrubbed_stops_the_push(data):
    symlink(data / "scrubbed" / "slack" / "people.md", data / "private" / "people.toml")
    refused(config(data), "holds a link")


def test_a_copy_that_is_a_link_stops_the_push(data):
    (data / "scrubbed" / DAY).unlink()
    symlink(data / "scrubbed" / DAY, data / "slack" / "export" / "fake-channel" / "2026-01-05.md")
    refused(config(data), "link")


def test_a_listed_copy_made_a_link_after_the_scan_is_still_refused(data, monkeypatch):
    # The scan of scrubbed/ is skipped, as if the link appeared after it ran, and the link's
    # target has the copy's exact bytes, so only the check when the copy is opened stops it.
    monkeypatch.setattr(push_module, "unsafe_entry", lambda folder: False)
    target = data / "elsewhere.md"
    write(target, DAY_TEXT)
    (data / "scrubbed" / DAY).unlink()
    symlink(data / "scrubbed" / DAY, target)
    refused(config(data), "Copy 1", "isn't a plain file")


def test_a_folder_on_a_copys_path_made_a_link_after_the_scan_is_refused(tmp_path, monkeypatch):
    # Another scrub's copies, reached through a link in place of a folder: same bytes and hash.
    monkeypatch.setattr(push_module, "unsafe_entry", lambda folder: False)
    data, other = tmp_path / "data", tmp_path / "other"
    make_scrub(data, {DAY: DAY_TEXT})
    make_scrub(other, {DAY: DAY_TEXT})
    shutil.rmtree(data / "scrubbed" / "slack")
    symlink(data / "scrubbed" / "slack", other / "scrubbed" / "slack")
    refused(config(data), "isn't a plain file")


def hard_link(link: Path, target: Path) -> None:
    try:
        os.link(target, link)
    except OSError:
        pytest.skip("this system doesn't allow hard links here")


def test_a_hard_link_inside_scrubbed_stops_the_push(data):
    hard_link(data / "scrubbed" / "extra.md", data / "private" / "people.toml")
    refused(config(data), "holds a link")


def test_a_listed_copy_with_a_second_hard_link_is_refused_when_opened(data, monkeypatch):
    monkeypatch.setattr(push_module, "unsafe_entry", lambda folder: False)
    hard_link(data / "elsewhere.md", data / "scrubbed" / DAY)
    refused(config(data), "Copy 1", "isn't a plain file")


def test_scrubbed_being_a_link_stops_the_push(tmp_path):
    real = tmp_path / "elsewhere"
    make_scrub(real, {DAY: DAY_TEXT})
    (tmp_path / "data" / "private").mkdir(parents=True)
    symlink(tmp_path / "data" / "scrubbed", real / "scrubbed")
    refused(config(tmp_path / "data"), "is a link")


def test_scrubbed_being_a_file_stops_the_push(tmp_path):
    (tmp_path / "data" / "private").mkdir(parents=True)
    write(tmp_path / "data" / "scrubbed", "not a folder")
    refused(config(tmp_path / "data"), "isn't a folder")


def test_no_scrubbed_folder_with_a_private_folder_stops_the_push(tmp_path):
    (tmp_path / "data" / "private").mkdir(parents=True)
    refused(config(tmp_path / "data"), "no scrubbed copies yet")


@pytest.mark.parametrize("checks", [["anything I like"], [{"a": 1}], "emails"])
def test_a_check_name_the_scrub_doesnt_make_stops_the_push(tmp_path, checks):
    def edit(manifest):
        manifest["copies"][0]["checks"] = checks

    make_scrub(tmp_path / "data", {DAY: DAY_TEXT}, manifest_edit=edit)
    refused(config(tmp_path / "data"), "manifest.json")


def test_the_push_record_leaves_out_the_manifests_note(tmp_path):
    def edit(manifest):
        manifest["note"] = "Fakey Realname"

    make_scrub(tmp_path / "data", {DAY: DAY_TEXT}, manifest_edit=edit)
    store = FakeStore()
    push(config(tmp_path / "data"), Opener(store))
    (record,) = records(store)
    assert "note" not in record
    assert_no_secrets(store)


# Never overwrite


def test_identical_content_already_in_the_store_is_skipped_and_left_alone(data):
    name = f"{PREFIX}/slack/2026-01-05/CFAKE0001.{sha(DAY_TEXT)[:16]}.md"
    store = FakeStore({name: b"what the store already holds"})
    report = push(config(data), Opener(store))
    assert store.objects[name] == b"what the store already holds"
    assert "2 scrubbed copy(ies) added to the store, and 1 skipped" in report
    (record,) = records(store)
    assert [c["result"] for c in record["copies"]].count("exists") == 1


def test_a_second_push_of_the_same_scrub_adds_no_copies(data):
    store = FakeStore()
    push(config(data), Opener(store))
    copies = {k: v for k, v in store.objects.items() if "/pushes/" not in k}
    report = push(config(data), Opener(store))
    assert {k: v for k, v in store.objects.items() if "/pushes/" not in k} == copies
    assert "0 scrubbed copy(ies) added to the store, and 3 skipped" in report
    assert len(records(store)) == 2  # each push leaves its own record, never replacing one


def test_a_changed_copy_is_added_beside_the_old_one(data):
    store = FakeStore()
    push(config(data), Opener(store))
    longer = DAY_TEXT + "[Person 2]: and one more message\n"
    make_scrub(data, {DAY: longer, DAY_2: DAY_2_TEXT, CANVAS: CANVAS_TEXT})
    push(config(data), Opener(store))
    versions = sorted(k for k in store.objects if k.startswith(f"{PREFIX}/slack/2026-01-05/"))
    assert versions == sorted(
        f"{PREFIX}/slack/2026-01-05/CFAKE0001.{sha(t)[:16]}.md" for t in (DAY_TEXT, longer)
    )
    assert store.objects[versions[0]] != store.objects[versions[1]]


# The record of each push


def test_the_push_record_lists_what_was_sent_and_its_release_state(data):
    store = FakeStore()
    push(config(data), Opener(store))
    (record,) = records(store)
    assert record["approved_for_release"] is False
    assert (record["producer"], record["project"]) == ("fake-producer", "fake-project")
    assert sorted(c["id"] for c in record["copies"]) == [
        "CFAKE0001/2026-01-05",
        "CFAKE0001/2026-01-06",
        "FFAKE0001",
    ]
    assert {c["result"] for c in record["copies"]} == {"added"}
    assert all(c["name"] in store.objects for c in record["copies"])


# Sign-in and store errors


def test_a_failed_sign_in_stops_the_push_and_sends_nothing(data):
    opener = Opener(
        error=SignInError("Couldn't sign in. Run gcloud auth application-default login.")
    )
    with pytest.raises(SignInError):
        push(config(data), opener)
    assert opener.store.objects == {}


def test_a_failed_sign_in_shows_the_fix_and_no_token(data, monkeypatch, capsys):
    message = "Couldn't sign in. Run `gcloud auth application-default login`. Nothing was sent."
    opener = Opener(error=SignInError(message))
    monkeypatch.setattr(cli, "load_config", lambda: config(data))
    monkeypatch.setattr("diffdata.store.push.open_gcs", opener)
    assert cli.run_push() == 1
    out = capsys.readouterr()
    assert "gcloud auth application-default login" in out.err
    assert "ya29." not in out.err + out.out  # the shape of a Google access token
    assert opener.store.objects == {}
    assert_no_secrets(opener.store, out.err, out.out)


class SignInFailsAfterOne(FakeStore):
    def add(self, name: str, data: bytes, content_type: str):
        if self.sent:
            raise SignInError("Couldn't sign in. Run gcloud. Nothing was sent.")
        return super().add(name, data, content_type)


def test_a_sign_in_that_fails_partway_doesnt_say_nothing_was_sent(data):
    store = SignInFailsAfterOne()
    with pytest.raises(PushError) as e:
        push(config(data), Opener(store))
    assert "Nothing was sent" not in str(e.value)
    assert "1 copy(ies) were added before it stopped" in str(e.value)


def test_an_unexpected_error_shows_only_its_type(data, monkeypatch, capsys):
    class Leaky(FakeStore):
        def add(self, name, data, content_type):
            raise ValueError(f"Fakey Realname wrote {data!r}")

    monkeypatch.setattr(cli, "load_config", lambda: config(data))
    monkeypatch.setattr("diffdata.store.push.open_gcs", Opener(Leaky()))
    assert cli.run_push() == 1
    out = capsys.readouterr()
    assert "unexpected ValueError" in out.err
    assert "Fakey" not in out.err + out.out and "Person 1" not in out.err + out.out


def test_a_store_that_stops_partway_says_how_many_were_added(data):
    store = FakeStore(fail_after=1)
    with pytest.raises(PushError, match="1 copy\\(ies\\) were added before it stopped") as e:
        push(config(data), Opener(store))
    assert len(store.objects) == 1
    assert records(store) == []
    assert_no_secrets(store, str(e.value))


def test_the_push_never_loads_googles_library_until_it_sends():
    code = "import sys, diffdata.cli, diffdata.store.push; print('google.cloud' in sys.modules)"
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "False"


# The command


def test_diffdata_push_reports_a_refusal_and_fails(tmp_path, monkeypatch, capsys):
    (tmp_path / "config.toml").write_text(
        f'[local]\ndata_dir = "{(tmp_path / "data").as_posix()}"\n\n[store]\nbackend = "gcs"\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    assert cli.main(["push"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("diffdata push: bucket, folder, producer, project under [store]")


def test_diffdata_push_sends_through_the_store(data, tmp_path, monkeypatch, capsys):
    (tmp_path / "config.toml").write_text(
        f'[local]\ndata_dir = "{data.as_posix()}"\n\n'
        '[sources]\nslack_notices = { "run" = "none", CFAKE0001 = "none" }\n\n'
        '[store]\nbackend = "gcs"\n'
        'bucket = "fake-pilot-bucket"\nfolder = "fake-team"\nproducer = "fake-producer"\n'
        'project = "fake-project"\nsend_not_approved = "yes"\n',
        encoding="utf-8",
    )
    (tmp_path / "store-key.json").write_text(json.dumps(KEY), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    opener = Opener()
    monkeypatch.setattr("diffdata.store.push.open_gcs", opener)
    assert cli.main(["push"]) == 0
    assert "3 scrubbed copy(ies) added" in capsys.readouterr().out
    assert len(opener.store.objects) == 4


# End to end, from a real export and scrub of the synthetic fixtures


def test_a_real_scrub_is_pushed_and_nothing_from_outside_scrubbed_goes(
    tmp_path, slack_run, slack_response
):
    fixtures = Path(__file__).parent / "fixtures"
    channel, canvas_id = "C00000001", "F00000003"
    cfg = Config(
        data_dir=tmp_path / "data",
        slack_channels=("example-channel",),
        slack_notices=(("example-channel", "none"),),
        slack_workspace_url="https://example.slack.com",
        slack_canvases=(canvas_id,),
    )
    canvas = (fixtures / "slack" / "read_canvas_v2.json").read_text(encoding="utf-8")
    thread = {"channel_id": channel, "message_ts": "1790864040.000100"}
    people = slack_response(str(fixtures / "scrub" / "read_channel_people.txt"))
    slack_run(
        cfg,
        datetime(2026, 10, 6, 12, 0, tzinfo=UTC),
        [
            ("slack_read_channel", {"channel_id": channel}, slack_response("read_channel.txt")),
            ("slack_read_thread", thread, slack_response("read_thread.txt")),
            ("slack_read_canvas", {"canvas_id": canvas_id}, [{"type": "text", "text": canvas}]),
            ("slack_read_channel", {"channel_id": channel}, people),
        ],
    )
    build(cfg)
    scrub(cfg)
    manifest = json.loads((cfg.data_dir / "scrubbed" / "manifest.json").read_text("utf-8"))
    store = FakeStore()
    push(replace(config(cfg.data_dir), slack_notices=cfg.slack_notices), Opener(store))
    copies = {k: v for k, v in store.objects.items() if "/pushes/" not in k}
    assert len(copies) == len(manifest["copies"]) > 1
    scrubbed = {(cfg.data_dir / "scrubbed" / c["path"]).read_bytes() for c in manifest["copies"]}
    assert set(copies.values()) == scrubbed  # exactly the scrubbed copies, byte for byte
    assert any("/slack-canvas/" in k for k in copies)
    sent = b"".join(store.objects.values()).decode("utf-8")
    for name in ("Alex Example", "Sam Sample", "example.com"):
        assert name not in sent


# #118: the push checks the same left-out stamp as the scrub, late joiners included.

NOTICED = "C00000001/1790900000.000000"  # the notice went up on Oct 1
JORDAN = "U00000009"


def late_joiner_scrub(tmp_path, slack_run, slack_response) -> Config:
    """A real export, build and scrub of #example-channel with a notice, where Jordan (made
    up) joined after it and posted, and Sam joined before it. Returns a config the push can
    use."""
    original = (Path(__file__).parent / "fixtures" / "slack" / "read_channel.txt").read_text(
        encoding="utf-8"
    )
    head, _, rest = original.partition("\n\n")
    joins = "".join(
        f"=== Message from Jordan Join <jordan@example.com> ({JORDAN}) at 2026-10-02 12:00:00 "
        f"CDT ===\nMessage TS: {ts}\n{text}\n\n"
        for ts, text in (
            ("1790990000.000450", "Jordan's late message"),
            ("1790960000.000390", f"<@{JORDAN}|Jordan Join> has joined the channel"),
        )
    )
    # Sam (made up) joined before the notice, so they stay, unless the notice moves earlier.
    joins += (
        "=== Message from Sam Sample <sam@example.com> (U00000002) at 2026-10-02 12:00:00 CDT "
        "===\nMessage TS: 1790800000.000050\n<@U00000002|Sam Sample> has joined the channel\n\n"
    )
    read = tmp_path / "read_channel_late.txt"
    read.write_text(head + "\n\n" + joins + rest, encoding="utf-8")
    cfg = replace(
        config(tmp_path / "data"),
        slack_channels=("example-channel",),
        slack_notices=(("example-channel", NOTICED),),
        slack_workspace_url="https://example.slack.com",
    )
    slack_run(
        cfg,
        datetime(2026, 10, 6, 12, 0, tzinfo=UTC),
        [
            ("slack_read_channel", {"channel_id": "C00000001"}, slack_response(str(read))),
            (
                "slack_read_thread",
                {"channel_id": "C00000001", "message_ts": "1790864040.000100"},
                slack_response("read_thread.txt"),
            ),
        ],
    )
    report = build(cfg)
    # A complete run, so nothing is held back: Jordan is out for joining late, not for that.
    assert "held back" not in report
    assert "1 person(s) joined a channel after its notice" in report
    scrub(cfg)
    return cfg


def test_copies_that_leave_out_a_late_joiner_are_sent(tmp_path, slack_run, slack_response):
    cfg = late_joiner_scrub(tmp_path, slack_run, slack_response)
    store = FakeStore()
    push(cfg, Opener(store))
    sent = b"".join(store.objects.values()).decode("utf-8")
    assert len(store.objects) > 1
    assert "Jordan" not in sent and JORDAN not in sent


def test_must_refuse_a_push_after_the_notice_moved_with_no_build_since(
    tmp_path, slack_run, slack_response
):
    # The notice link was corrected to an earlier post after the scrub: someone else now
    # joined after it, so the copies could hold them.
    cfg = late_joiner_scrub(tmp_path, slack_run, slack_response)
    earlier = replace(cfg, slack_notices=(("example-channel", "C00000001/1790000000.000000"),))
    refused(earlier, "slack_notices")
    # Sam's join (before the old notice) is after the earlier one, so they're late now too.
    assert "2 person(s) joined a channel after its notice" in build(earlier)


def test_must_refuse_a_push_when_a_built_channel_has_no_notice_entry(
    tmp_path, slack_run, slack_response
):
    cfg = late_joiner_scrub(tmp_path, slack_run, slack_response)
    message = refused(replace(cfg, slack_notices=()), "slack_notices", "channel 1")
    assert "example-channel" not in message


# Review fixes after Codex's review of #114.


def test_a_raw_file_with_a_forged_manifest_entry_is_still_refused(tmp_path):
    # The file and its manifest entry agree, so only the last check, run again, stops it.
    data = tmp_path / "data"
    raw = f"---\nchannel_id: CFAKE0001\n---\n\n{SECRETS[0]}: {SECRETS[2]}\n"
    make_scrub(data, {DAY: raw})
    message = refused(config(data), "Copy 1", "still holds something the scrub replaces")
    assert SECRETS[0] not in message


# Built from parts, so the repo never holds a string shaped like a real key.
FAKE_TOKEN = "gh" + "p_" + "aB3dE5fG7hJ9kL1mN3pQ5rS7tU9vW1xY3zA5"


@pytest.mark.parametrize(
    "leftover",
    [
        SECRETS[1],
        "fake.person@example.com",
        "UFAKE12345",
        FAKE_TOKEN,
        "https://meet.google.com/abc-defg-hij",
        "+44 20 7946 0958",
        "the db password: hunter2hunter2",
        "this. Password is fluffy42",
    ],
    ids=[
        "name",
        "email",
        "slack id",
        "secret",
        "meeting link",
        "phone number",
        "password",
        "password is",
    ],
)
def test_a_copy_with_anything_the_scrub_replaces_left_in_it_is_refused(tmp_path, leftover):
    data = tmp_path / "data"
    make_scrub(data, {DAY: f"---\nchannel_id: CFAKE0001\n---\n\n[Person 1]: ask {leftover}\n"})
    refused(config(data), "still holds")


def test_a_missing_or_changed_secret_rules_file_stops_the_push(data, monkeypatch, tmp_path):
    monkeypatch.setattr(secrets, "RULES_FILE", tmp_path / "missing.toml")
    message = refused(config(data), "secret rules file")
    assert "Nothing was sent" in message


def test_a_missing_people_list_stops_the_push(data):
    (data / "private" / "people.toml").unlink()
    refused(config(data), "people.toml")


class LockWatcher(FakeStore):
    """A store that tries to start a scrub while the push is sending."""

    def __init__(self, data: Path):
        super().__init__()
        self.data = data
        self.scrub_refused = None

    def add(self, name: str, data: bytes, content_type: str):
        if self.scrub_refused is None:
            try:
                scrub(config(self.data))
                self.scrub_refused = False
            except ScrubError as e:
                self.scrub_refused = "a push is running" in str(e)
        return super().add(name, data, content_type)


def test_no_scrub_can_start_while_a_push_is_sending(data):
    store = LockWatcher(data)
    push(config(data), Opener(store))
    assert store.scrub_refused is True
    assert not (data / "private" / "scrub.lock").exists(), "the push left its lock behind"


def test_a_refused_push_lets_go_of_the_lock(data):
    write(data / "scrubbed" / DAY, "changed")
    refused(config(data))
    assert not (data / "private" / "scrub.lock").exists()


@pytest.mark.parametrize("name", ["STORAGE_EMULATOR_HOST", "GOOGLE_CLOUD_UNIVERSE_DOMAIN"])
def test_a_setting_that_sends_elsewhere_stops_the_push(data, monkeypatch, name):
    monkeypatch.setenv(name, "http://emulator.example.test:9023")
    message = refused(config(data), name)
    assert "example.test" not in message


def test_a_malformed_held_back_entry_stops_the_push(tmp_path):
    def edit(manifest):
        manifest["held_back"] = [{"id": "CFAKE0001/2026-01-05"}]

    make_scrub(tmp_path / "data", {DAY: DAY_TEXT}, manifest_edit=edit)
    refused(config(tmp_path / "data"), "manifest.json")


def test_a_manifest_with_a_key_given_twice_stops_the_push(data):
    # A second held_back list would otherwise replace the first.
    text = (data / "scrubbed" / "manifest.json").read_text()
    held = '"held_back": [{"source": "slack", "id": "CFAKE0001/2026-01-05", "why": "a name"}],'
    write(
        data / "scrubbed" / "manifest.json", text.replace("{", "{" + held + '"held_back": [],', 1)
    )
    refused(config(data), "manifest.json")


@pytest.mark.parametrize(
    "edit",
    [
        lambda m: m.update(approved_for_release=True),
        lambda m: m["copies"][0].update(checks=["emails"]),
        lambda m: m["copies"][0].update(extra="field"),
        lambda m: m["copies"][0].pop("flags"),
    ],
)
def test_a_manifest_unlike_what_a_scrub_writes_stops_the_push(tmp_path, edit):
    make_scrub(tmp_path / "data", {DAY: DAY_TEXT}, manifest_edit=edit)
    refused(config(tmp_path / "data"), "manifest.json")


@pytest.mark.parametrize(
    "bucket", ["a" * 64, "ab", "goog-fake", "fake-google-bucket", "fake." + "a" * 64]
)
def test_a_bucket_name_google_wouldnt_accept_stops_the_push(data, bucket):
    refused(config(data, bucket=bucket), "bucket")


def test_a_store_setting_with_a_trailing_space_is_refused_not_trimmed(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text(
        f'[local]\ndata_dir = "{(tmp_path / "data").as_posix()}"\n\n'
        '[store]\nbackend = "gcs"\nbucket = "fake-pilot-bucket"\nfolder = "fake-team "\n'
        'producer = "fake-producer"\nproject = "fake-project"\nsend_not_approved = "yes"\n',
        encoding="utf-8",
    )
    (tmp_path / "store-key.json").write_text(json.dumps(KEY), encoding="utf-8")
    make_scrub(tmp_path / "data", {DAY: DAY_TEXT})
    refused(load_config(path), "folder under [store]", "must be one plain name")


def test_a_store_setting_that_isnt_text_is_named(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        f'[local]\ndata_dir = "{(tmp_path / "data").as_posix()}"\n\n[store]\nbucket = 5\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="bucket under"):
        load_config(path)


class StopsOnSecond(FakeStore):
    def __init__(self, error: Exception):
        super().__init__()
        self.error, self.fail = error, True

    def add(self, name: str, data: bytes, content_type: str):
        if self.fail and len(self.sent) == 1:
            raise self.error
        return super().add(name, data, content_type)


def test_an_unexpected_error_partway_still_says_how_many_were_added(data):
    store = StopsOnSecond(ConnectionError("FAKE-SECRET in the message"))
    with pytest.raises(PushError) as e:
        push(config(data), Opener(store))
    assert "1 copy(ies) were added" in str(e.value)
    assert "ConnectionError" in str(e.value) and "FAKE-SECRET" not in str(e.value)
    store.fail = False  # the network is back: the rest are sent, and the first is skipped
    report = push(config(data), Opener(store))
    assert "2 scrubbed copy(ies) added" in report and "1 skipped" in report


class RecordFails(FakeStore):
    def add(self, name: str, data: bytes, content_type: str):
        if "/pushes/" in name:
            raise SignInError("Couldn't sign in. Nothing was sent.")
        return super().add(name, data, content_type)


def test_a_record_that_fails_doesnt_say_nothing_was_sent(data):
    with pytest.raises(PushError) as e:
        push(config(data), Opener(RecordFails()))
    assert "Nothing was sent" not in str(e.value)
    assert "3 copy(ies) were added, but this push's record wasn't" in str(e.value)


def test_two_pushes_in_the_same_second_both_get_a_record(data, monkeypatch):
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 1, 9, 12, 0, 0, tzinfo=UTC)

    monkeypatch.setattr(push_module, "datetime", Frozen)
    store = FakeStore()
    push(config(data), Opener(store))
    push(config(data), Opener(store))  # all skipped this time, so a different record
    assert len(records(store)) == 2


def test_an_unlisted_file_with_a_name_a_scrub_makes_is_never_sent(data):
    stray = "slack/export/CFAKE0001/2026-01-07.md"
    write(data / "scrubbed" / stray, DAY_TEXT.replace("[Person 1]", "[Person 3]"))
    store = FakeStore()
    push(config(data), Opener(store))
    copies = sorted(n for n in store.sent if "/pushes/" not in n)
    assert copies == sorted(
        [
            f"{PREFIX}/slack/2026-01-05/CFAKE0001.{sha(DAY_TEXT)[:16]}.md",
            f"{PREFIX}/slack/2026-01-06/CFAKE0001.{sha(DAY_2_TEXT)[:16]}.md",
            f"{PREFIX}/slack-canvas/2026-01-07/FFAKE0001.{sha(CANVAS_TEXT)[:16]}.md",
        ]
    )


def test_an_error_never_quotes_a_path_from_the_manifest(tmp_path):
    def edit(manifest):
        manifest["copies"][0].update(
            path="slack/export/FAKEYREALNAME/2026-01-05.md", id="FAKEYREALNAME/2026-01-05"
        )

    make_scrub(tmp_path / "data", {DAY: DAY_TEXT}, manifest_edit=edit)
    message = refused(config(tmp_path / "data"), "Copy 1", "missing")
    assert "FAKEYREALNAME" not in message


# The Google Cloud Storage adapter, against a stand-in for Google's client: no bucket,
# credentials or network.


class StubBlob:
    def __init__(self, error: Exception | None):
        self.error, self.calls = error, []

    def upload_from_string(self, data, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error


class StubClient:
    made: list[dict] = []
    error: Exception | None = None

    def __init__(self, **kwargs):
        StubClient.made.append(kwargs)
        self.blobs: list[StubBlob] = []

    def bucket(self, name):
        client = self

        class Bucket:
            def blob(self, blob_name):
                client.blobs.append(StubBlob(StubClient.error))
                return client.blobs[-1]

        return Bucket()


@pytest.fixture
def gcs(monkeypatch):
    from diffdata.store import gcs

    StubClient.made, StubClient.error = [], None
    StubCredentials.error = None
    monkeypatch.setattr(gcs.storage, "Client", StubClient)
    monkeypatch.setattr(
        gcs.service_account.Credentials,
        "from_service_account_info",
        lambda info, scopes: StubCredentials(),
    )
    return gcs


class StubCredentials:
    error: Exception | None = None
    refreshed = 0

    def refresh(self, request):
        StubCredentials.refreshed += 1
        if StubCredentials.error:
            raise StubCredentials.error


def test_the_adapter_uploads_create_only_to_googles_own_address(gcs):
    store = gcs.GCSStore("fake-pilot-bucket", KEY)
    assert store.add("fake/name.md", b"text", "text/markdown") == "added"
    (made,) = StubClient.made
    assert made["client_options"] == {"api_endpoint": "https://storage.googleapis.com"}
    assert made["project"] == "fake-project"  # from the key, so nothing goes looking


def test_the_adapter_sends_the_create_only_condition(gcs, monkeypatch):
    blobs = []
    real_init = StubClient.__init__

    def keep(self, **kwargs):
        real_init(self, **kwargs)
        blobs.append(self)

    monkeypatch.setattr(StubClient, "__init__", keep)
    gcs.GCSStore("fake-pilot-bucket", KEY).add("fake/name.md", b"text", "text/markdown")
    (call,) = blobs[0].blobs[0].calls
    assert call["if_generation_match"] == 0


def test_the_adapter_reports_a_taken_name_as_exists(gcs):
    from google.api_core import exceptions as api

    StubClient.error = api.PreconditionFailed("taken")
    assert (
        gcs.GCSStore("fake-pilot-bucket", KEY).add("fake/name.md", b"t", "text/markdown")
        == "exists"
    )


def test_the_adapter_never_shows_a_token_from_a_sign_in_error(gcs):
    from google.auth import exceptions as auth

    StubClient.error = auth.RefreshError("refresh failed for token ya29.FAKE-TOKEN-1234")
    with pytest.raises(SignInError) as e:
        gcs.GCSStore("fake-pilot-bucket", KEY).add("fake/name.md", b"t", "text/markdown")
    assert "FAKE-TOKEN" not in str(e.value)


def test_the_adapter_shows_only_the_type_of_a_network_error(gcs):
    from google.auth import exceptions as auth

    StubClient.error = auth.TransportError("connection to FAKE-HOST failed")
    with pytest.raises(StoreError) as e:
        gcs.GCSStore("fake-pilot-bucket", KEY).add("fake/name.md", b"t", "text/markdown")
    assert "Couldn't reach Google Cloud Storage" in str(e.value)
    assert "FAKE-HOST" not in str(e.value)


def test_the_adapter_signs_in_before_any_upload(gcs):
    from google.auth import exceptions as auth

    StubCredentials.error = auth.RefreshError("refresh failed for token ya29.FAKE-TOKEN-1234")
    with pytest.raises(SignInError) as e:
        gcs.GCSStore("fake-pilot-bucket", KEY)
    assert StubClient.made == [], "a client was made, so an upload could start"
    assert "FAKE-TOKEN" not in str(e.value) and "Nothing was sent" in str(e.value)


def test_the_adapter_uses_the_checked_sign_in(gcs):
    gcs.GCSStore("fake-pilot-bucket", KEY)
    (made,) = StubClient.made
    assert isinstance(made["credentials"], StubCredentials)


# Review fixes after Codex's second review of #114.


def test_a_forged_copy_with_a_mention_the_scrub_would_replace_is_refused(tmp_path):
    data = tmp_path / "data"
    make_scrub(data, {DAY: "---\nchannel_id: CFAKE0001\n---\n\n[Person 1]: ask @quartzoperator\n"})
    refused(config(data), "Copy 1", "mention")


def test_a_slack_copy_whose_header_names_another_channel_is_refused(tmp_path):
    data = tmp_path / "data"
    make_scrub(data, {DAY: DAY_TEXT.replace("CFAKE0001", "CFAKE0002")})
    refused(config(data), "Copy 1", "doesn't name its channel")


def test_copies_made_before_an_opt_out_are_never_sent(data):
    # The person opted out after the scrub: config.toml lists them, but the copies don't
    # know it. The build and scrub must run again first.
    cfg = Config(
        data_dir=data,
        slack_channels=(),
        slack_notices=(("run", "none"), ("CFAKE0001", "none")),
        slack_opted_out=(SECRETS[1],),
        **{f"store_{k}": v for k, v in vars(push_module.settings(config(data))).items()},
    )
    refused(cfg, "slack_opted_out")


def test_copies_made_before_the_last_build_are_never_sent(data):
    # The manifest and config.toml agree, but the last build applied another list (none), so
    # only the check against the build stops it.
    manifest = json.loads((data / "scrubbed" / "manifest.json").read_text())
    manifest["opt_out_stamp"] = opt_out_stamp([SECRETS[1]])
    write(data / "scrubbed" / "manifest.json", json.dumps(manifest))
    cfg = config(data)
    cfg = Config(**{**vars(cfg), "slack_opted_out": (SECRETS[1],)})
    refused(cfg, "slack_opted_out")


@pytest.mark.parametrize(
    "edit",
    [
        lambda m: m.pop("tool"),
        lambda m: m.pop("note"),
        lambda m: m.pop("opt_out_stamp"),
        lambda m: m.update(extra="field"),
        lambda m: m["copies"][0].update(flags=-1),
        lambda m: m["held_back"].append({"source": "slack", "id": "", "why": "a name"}),
        lambda m: m["held_back"].append({"source": "slack-canvas", "id": "F/1", "why": "x"}),
    ],
)
def test_a_manifest_missing_or_bending_a_field_stops_the_push(tmp_path, edit):
    make_scrub(tmp_path / "data", {DAY: DAY_TEXT}, manifest_edit=edit)
    refused(config(tmp_path / "data"), "manifest.json")


def test_a_bucket_named_like_an_ip_address_stops_the_push(data):
    refused(config(data, bucket="192.0.2.10"), "bucket")


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no named pipes on this system")
def test_a_named_pipe_in_scrubbed_is_refused_and_the_lock_let_go(data):
    # Not on the manifest, so only the scan of scrubbed/ sees it.
    os.mkfifo(data / "scrubbed" / "extra.pipe")
    refused(config(data), "isn't a plain file")
    assert not (data / "private" / "scrub.lock").exists()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no named pipes on this system")
def test_a_named_pipe_reached_after_the_scan_doesnt_hang_the_push(data, monkeypatch):
    monkeypatch.setattr(push_module, "unsafe_entry", lambda folder: False)
    (data / "scrubbed" / "manifest.json").unlink()
    os.mkfifo(data / "scrubbed" / "manifest.json")
    refused(config(data), "manifest.json")


def test_two_identical_pushes_in_the_same_second_both_get_a_record(data, monkeypatch):
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 1, 9, 12, 0, 0, tzinfo=UTC)

    monkeypatch.setattr(push_module, "datetime", Frozen)
    store = FakeStore()
    push(config(data), Opener(store))
    push(config(data), Opener(store))  # all skipped: same results as the next one
    push(config(data), Opener(store))
    assert len(records(store)) == 3


def test_a_record_name_already_taken_is_reported(data, monkeypatch):
    class Taken(FakeStore):
        def add(self, name, data, content_type):
            return "exists" if "/pushes/" in name else super().add(name, data, content_type)

    with pytest.raises(PushError, match="record wasn't"):
        push(config(data), Opener(Taken()))


def test_a_store_setting_error_never_shows_the_config_path(tmp_path):
    cfg = config(tmp_path / "data", producer="")
    cfg = Config(**{**vars(cfg), "path": tmp_path / "Fakey Realname" / "config.toml"})
    make_scrub(tmp_path / "data", {DAY: DAY_TEXT})
    message = refused(cfg, "producer")
    assert "Fakey" not in message


# Paul's review of #114: the team's key, copies not approved for release, the local record
# and clearer messages.


def test_no_store_key_stops_the_push(data):
    message = refused(config(data, key=None), "There's no store key", "store-key.json")
    assert str(data) not in message


@pytest.mark.parametrize(
    "key",
    [
        {**KEY, "type": "authorized_user"},  # a person's own Google login
        {**KEY, "private_key": ""},
        {"type": "service_account"},
        ["not", "a", "key"],
    ],
)
def test_a_file_that_isnt_a_service_account_key_stops_the_push(data, key):
    message = refused(config(data, key=key), "isn't a Google service account key")
    assert "FAKE-NOT-A-KEY" not in message and "fake-uploader" not in message


def test_a_key_file_that_isnt_json_stops_the_push(data):
    cfg = config(data)
    (cfg.path.parent / "store-key.json").write_text("FAKE-NOT-A-KEY {", encoding="utf-8")
    message = refused(cfg, "isn't a Google service account key")
    assert "FAKE-NOT-A-KEY" not in message


def test_a_key_inside_a_git_repository_stops_the_push(data):
    cfg = config(data)
    (cfg.path.parent / ".git").mkdir()
    refused(cfg, "inside a git repository or a synced folder")


def test_a_key_inside_a_synced_folder_stops_the_push(tmp_path):
    data = tmp_path / "Dropbox" / "data"
    make_scrub(data, {DAY: DAY_TEXT})
    refused(config(data), "inside a git repository or a synced folder")


def test_the_key_is_passed_to_the_store_and_never_shown(data, capsys):
    opener = Opener()
    report = push(config(data), opener)
    assert opener.key == KEY
    out = capsys.readouterr()
    stored = b"".join(opener.store.objects.values()).decode("utf-8") + "".join(opener.store.objects)
    kept = "".join(f.read_text("utf-8") for f in (data / "pushes").glob("*.json"))
    for value in (KEY["private_key"], KEY["client_email"]):
        for text in (report, out.out, out.err, stored, kept):
            assert value not in text


def test_copies_not_approved_are_refused_without_the_setting(data):
    for value in ("", "no"):
        message = refused(config(data, send_not_approved=value), "not approved for release")
        assert 'send_not_approved = "yes"' in message


def test_a_send_not_approved_value_other_than_yes_or_no_stops_the_push(data):
    refused(config(data, send_not_approved="true"), 'must be "yes" or "no"')


def test_copies_not_approved_go_to_their_own_part_of_the_folder(data):
    store = FakeStore()
    push(config(data), Opener(store))
    assert store.objects and all(
        name.startswith("fake-team/not-approved/fake-producer/fake-project/")
        for name in store.objects
    )


def test_a_manifest_that_says_approved_stops_the_push(tmp_path):
    # No scrub writes it until #117, so it could only come from an edited manifest.
    make_scrub(
        tmp_path / "data",
        {DAY: DAY_TEXT},
        manifest_edit=lambda m: m.update(approved_for_release=True),
    )
    refused(config(tmp_path / "data"), "manifest.json")


def local_records(data: Path) -> list[dict]:
    return [json.loads(f.read_text("utf-8")) for f in sorted((data / "pushes").glob("*.json"))]


def test_each_push_keeps_its_record_on_this_computer(data):
    store = FakeStore()
    report = push(config(data), Opener(store))
    (kept,) = local_records(data)
    assert kept["complete"] is True and len(kept["copies"]) == 3
    assert kept == records(store)[0]
    assert "pushes/" in report and "in your data folder" in report


def test_a_push_that_stops_partway_still_keeps_its_record(data):
    with pytest.raises(PushError) as e:
        push(config(data), Opener(FakeStore(fail_after=1)))
    (kept,) = local_records(data)
    assert kept["complete"] is False
    # The second may have reached Google before the answer was lost, so it's "unknown".
    assert [c["result"] for c in kept["copies"]] == ["added", "unknown"]
    assert "pushes/" in str(e.value) and "Nothing was sent" not in str(e.value)


def test_a_record_that_fails_to_send_is_still_kept_here(data):
    with pytest.raises(PushError):
        push(config(data), Opener(RecordFails()))
    (kept,) = local_records(data)
    assert kept["complete"] is True


def test_a_refused_push_keeps_no_record(data):
    refused(config(data, key=None))
    assert not (data / "pushes").exists()


def test_a_local_pushes_folder_thats_a_link_stops_the_push(data, tmp_path):
    (tmp_path / "elsewhere").mkdir()
    symlink(data / "pushes", tmp_path / "elsewhere")
    refused(config(data), "pushes/")


def test_the_adapter_reports_a_turned_down_key_with_the_fix(gcs):
    from google.auth import exceptions as auth

    StubClient.error = auth.RefreshError("invalid_grant for FAKE-NOT-A-KEY")
    with pytest.raises(SignInError) as e:
        gcs.GCSStore("fake-pilot-bucket", KEY).add("fake/name.md", b"t", "text/markdown")
    assert "clock" in str(e.value) and "FAKE-NOT-A-KEY" not in str(e.value)


def test_the_adapter_shows_only_the_code_of_a_google_error(gcs):
    from google.api_core import exceptions as api

    StubClient.error = api.ServiceUnavailable("FAKE-DETAIL from Google")
    with pytest.raises(StoreError) as e:
        gcs.GCSStore("fake-pilot-bucket", KEY).add("fake/name.md", b"t", "text/markdown")
    assert "code 503" in str(e.value) and "FAKE-DETAIL" not in str(e.value)


def test_the_adapter_says_the_key_cant_add_to_the_folder(gcs):
    from google.api_core import exceptions as api

    StubClient.error = api.Forbidden("FAKE-DETAIL")
    with pytest.raises(StoreError, match="this team's key can't add files"):
        gcs.GCSStore("fake-pilot-bucket", KEY).add("fake/name.md", b"t", "text/markdown")


def test_the_adapter_refuses_a_key_google_cant_read(gcs, monkeypatch):
    def unreadable(info, scopes):
        raise ValueError("bad key FAKE-NOT-A-KEY")

    monkeypatch.setattr(gcs.service_account.Credentials, "from_service_account_info", unreadable)
    with pytest.raises(SignInError) as e:
        gcs.GCSStore("fake-pilot-bucket", KEY)
    assert "isn't a Google service account key" in str(e.value)
    assert "FAKE-NOT-A-KEY" not in str(e.value)


# Codex's third review of #114.


@pytest.mark.parametrize(
    "routing",
    [
        {"token_uri": "https://auth.example.test/token"},
        {"universe_domain": "example.test"},
    ],
)
def test_a_key_that_would_sign_in_somewhere_else_stops_the_push(data, routing):
    message = refused(config(data, key={**KEY, **routing}), "somewhere other than Google")
    assert "example.test" not in message


def test_a_key_naming_googles_own_token_address_is_accepted(data):
    push(config(data, key={**KEY, "token_uri": "https://oauth2.googleapis.com/token"}), Opener())


def test_a_key_with_a_second_hard_link_stops_the_push(data, tmp_path):
    cfg = config(data)
    hard_link(tmp_path / "copy-in-a-repo.json", cfg.path.parent / "store-key.json")
    refused(cfg, "second hard link")


def test_a_key_that_is_a_link_stops_the_push(data, tmp_path):
    cfg = config(data, key=None)
    (tmp_path / "elsewhere.json").write_text(json.dumps(KEY), encoding="utf-8")
    symlink(cfg.path.parent / "store-key.json", tmp_path / "elsewhere.json")
    refused(cfg, "is a link")


def test_a_pushes_folder_inside_a_git_checkout_stops_the_push(data):
    (data / "pushes" / ".git").mkdir(parents=True)
    refused(config(data), "inside a git repository")


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="needs POSIX permissions")
def test_a_pushes_folder_it_cant_write_stops_the_push_before_signing_in(data):
    (data / "pushes").mkdir()
    (data / "pushes").chmod(0o500)
    try:
        refused(config(data), "Couldn't write this push's record")
    finally:
        (data / "pushes").chmod(0o700)


class Interrupted(FakeStore):
    def add(self, name, data, content_type):
        if self.sent:
            raise KeyboardInterrupt
        return super().add(name, data, content_type)


def test_ctrl_c_partway_still_keeps_the_record(data):
    with pytest.raises(KeyboardInterrupt):
        push(config(data), Opener(Interrupted()))
    (kept,) = local_records(data)
    assert kept["complete"] is False
    assert [c["result"] for c in kept["copies"]] == ["added"]


def test_a_record_the_store_turned_down_is_marked_not_sent_here(data):
    with pytest.raises(PushError):
        push(config(data), Opener(RecordFails()))
    (kept,) = local_records(data)
    assert kept["complete"] is True and kept["record_sent"] is False


def test_the_scrub_stamps_the_list_it_checked_not_one_a_build_wrote_since(tmp_path, monkeypatch):
    # A build that finishes while the scrub runs: the second read of the stamp would see it.
    from diffdata.scrub import run as scrub_run

    data = tmp_path / "data"
    make_scrub(data, {DAY: DAY_TEXT})
    shutil.rmtree(data / "scrubbed")
    shutil.rmtree(data / "slack")
    stamps = iter([opt_out_stamp([]), opt_out_stamp([SECRETS[1]])])
    monkeypatch.setattr(scrub_run, "applied_stamp", lambda folder: next(stamps))
    cfg = config(data)
    scrub(cfg)
    manifest = json.loads((data / "scrubbed" / "manifest.json").read_text("utf-8"))
    assert manifest["opt_out_stamp"] == opt_out_stamp([])
