"""`diffdata push` (STO-1, STO-2, STO-3, STO-9, STO-11), against a fake store. Synthetic data
only: no real bucket, credentials or network."""

import hashlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from diffdata import cli
from diffdata.common.config import Config, ConfigError, load_config
from diffdata.discover.slack_export import build
from diffdata.scrub.run import scrub
from diffdata.store.backend import FakeStore, SignInError
from diffdata.store.push import PushError, push

PREFIX = "fake-team/fake-producer/fake-project"
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
        "checks": ["emails", "Slack IDs", "known names"],
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
        "copies": [entry(path, text) for path, text in copies.items()],
        "held_back": [
            {"source": s, "id": i, "why": "the last check found a name"} for s, i in held
        ],
    }
    if manifest_edit:
        manifest_edit(manifest)
    write(data / "scrubbed" / "manifest.json", json.dumps(manifest, indent=2))
    write(data / "private" / "people.toml", f'[[person]]\nnames = ["{SECRETS[0]}"]\n')
    write(data / "private" / "flags.md", f"- {SECRETS[0]}\n")
    write(data / "slack" / "left_out.json", json.dumps({SECRETS[1]: SECRETS[0]}))
    write(data / "slack" / "inbox" / "run" / "0001.json", json.dumps({"text": SECRETS[2]}))
    write(data / "slack" / "store" / "CFAKE0001" / "messages.jsonl", json.dumps({"t": SECRETS[3]}))
    write(
        data / "slack" / "export" / "fake-channel" / "2026-01-05.md", f"{SECRETS[0]}: {SECRETS[2]}"
    )


def config(data: Path, **store) -> Config:
    settings = {
        "backend": "gcs",
        "bucket": "fake-pilot-bucket",
        "folder": "fake-team",
        "producer": "fake-producer",
        "project": "fake-project",
    } | store
    return Config(
        data_dir=data, slack_channels=(), **{f"store_{k}": v for k, v in settings.items()}
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

    def __call__(self, bucket: str) -> FakeStore:
        self.opened += 1
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
    message = refused(config(data), "changed after the scrub", DAY_2)
    assert SECRETS[0] not in message


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


@pytest.mark.parametrize("left", ["private/scrub.lock", "scrubbed-in-progress/manifest.json"])
def test_a_scrub_running_or_cut_short_stops_the_push(data, left):
    write(data / left, "")
    refused(config(data), "A scrub is running")


def symlink(link: Path, target: Path) -> None:
    try:
        os.symlink(target, link)
    except OSError:
        pytest.skip("this system doesn't allow symlinks here")


def test_a_link_inside_scrubbed_stops_the_push(data):
    symlink(data / "scrubbed" / "slack" / "people.md", data / "private" / "people.toml")
    refused(config(data), "holds a link")


def test_a_copy_that_is_a_link_stops_the_push(data):
    # The manifest's hash matches what the link points at, so only the link check stops it.
    (data / "scrubbed" / DAY).unlink()
    symlink(data / "scrubbed" / DAY, data / "slack" / "export" / "fake-channel" / "2026-01-05.md")
    refused(config(data), "link")


def test_scrubbed_being_a_link_stops_the_push(tmp_path):
    real = tmp_path / "elsewhere"
    make_scrub(real, {DAY: DAY_TEXT})
    (tmp_path / "data").mkdir()
    symlink(tmp_path / "data" / "scrubbed", real / "scrubbed")
    refused(config(tmp_path / "data"), "is a link")


def test_scrubbed_being_a_file_stops_the_push(tmp_path):
    (tmp_path / "data").mkdir()
    write(tmp_path / "data" / "scrubbed", "not a folder")
    refused(config(tmp_path / "data"))


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
        f'[local]\ndata_dir = "{data.as_posix()}"\n\n[store]\nbackend = "gcs"\n'
        'bucket = "fake-pilot-bucket"\nfolder = "fake-team"\nproducer = "fake-producer"\n'
        'project = "fake-project"\n',
        encoding="utf-8",
    )
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
    push(config(cfg.data_dir), Opener(store))
    copies = {k: v for k, v in store.objects.items() if "/pushes/" not in k}
    assert len(copies) == len(manifest["copies"]) > 1
    scrubbed = {(cfg.data_dir / "scrubbed" / c["path"]).read_bytes() for c in manifest["copies"]}
    assert set(copies.values()) == scrubbed  # exactly the scrubbed copies, byte for byte
    assert any("/slack-canvas/" in k for k in copies)
    sent = b"".join(store.objects.values()).decode("utf-8")
    for name in ("Alex Example", "Sam Sample", "example.com"):
        assert name not in sent
