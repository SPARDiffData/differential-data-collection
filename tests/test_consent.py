"""The consent record (CON-2) and revoking a source (CON-3). Synthetic data only."""

import json

import pytest

from diffdata import cli
from diffdata.consent import record
from diffdata.consent.record import ConsentError


def give(data, producer="Producer A", sources=("slack:example-channel",), **kwargs):
    return record.grant(
        data,
        producer,
        sources,
        kwargs.get("consumers", ["Example Lab"]),
        kwargs.get("uses", ["research"]),
        at=kwargs.get("at"),
    )


def test_a_grant_is_stored_with_everything_con_2_names(tmp_path):
    given = give(tmp_path, uses=["research", "training"], at="2026-10-21T10:00:00Z")
    line = json.loads(record.ledger_path(tmp_path).read_text(encoding="utf-8"))
    assert line["type"] == "grant"
    assert line["producer"] == "Producer A"
    assert line["sources"] == ["slack:example-channel"]
    assert line["consumers"] == ["Example Lab"]
    assert line["uses"] == ["research", "training"]
    assert line["at"] == "2026-10-21T10:00:00Z"
    assert line["policy_version"] == record.POLICY_VERSION
    assert line["record_id"] == given.record_id


def test_no_ledger_means_no_consent(tmp_path):
    assert record.consents(tmp_path) == {}
    assert not record.allowed(tmp_path, "slack:example-channel")


def test_a_granted_source_is_allowed_and_others_are_not(tmp_path):
    give(tmp_path)
    assert record.allowed(tmp_path, "slack:example-channel")
    assert not record.allowed(tmp_path, "slack:other-channel")


def test_slack_sources_are_matched_the_way_people_type_them(tmp_path):
    give(tmp_path, sources=["Slack:#Example-Channel"])
    assert record.allowed(tmp_path, "slack:example-channel")


def test_revoke_stops_a_source_and_keeps_the_rest(tmp_path):
    give(tmp_path, sources=["slack:example-channel", "gdoc:abc123"])
    assert record.revoke(tmp_path, "Producer A", ["slack:example-channel"]) == (
        "slack:example-channel",
    )
    assert not record.allowed(tmp_path, "slack:example-channel")
    assert record.allowed(tmp_path, "gdoc:abc123")


def test_revoke_adds_a_line_and_leaves_the_grant_in_place(tmp_path):
    give(tmp_path)
    before = record.ledger_path(tmp_path).read_text(encoding="utf-8")
    record.revoke(tmp_path, "Producer A", ["slack:example-channel"])
    after = record.ledger_path(tmp_path).read_text(encoding="utf-8")
    assert after.startswith(before)
    assert [line["type"] for line in record.read_ledger(tmp_path)] == ["grant", "revoke"]


def test_a_new_grant_replaces_the_last_one(tmp_path):
    give(tmp_path, sources=["slack:example-channel"])
    record.revoke(tmp_path, "Producer A", ["slack:example-channel"])
    give(tmp_path, sources=["slack:example-channel", "gdoc:abc123"], consumers=["Other Lab"])
    consent = record.consents(tmp_path)["Producer A"]
    assert consent.sources == ("slack:example-channel", "gdoc:abc123")
    assert consent.grant.consumers == ("Other Lab",)


def test_one_producer_revoking_doesnt_touch_another(tmp_path):
    give(tmp_path, producer="Producer A")
    give(tmp_path, producer="Producer B")
    record.revoke(tmp_path, "Producer A", ["slack:example-channel"])
    assert record.allowed(tmp_path, "slack:example-channel")
    assert record.consents(tmp_path)["Producer A"].sources == ()


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"producer": " "}, "producer"),
        ({"sources": []}, "source"),
        ({"sources": ["example-channel"]}, "<kind>:<id>"),
        ({"consumers": []}, "consumer"),
        ({"uses": []}, "use"),
        ({"uses": ["marketing"]}, "Unknown use"),
    ],
)
def test_an_incomplete_grant_is_refused_and_nothing_is_written(tmp_path, kwargs, message):
    with pytest.raises(ConsentError, match=message):
        give(tmp_path, **kwargs)
    assert not record.ledger_path(tmp_path).exists()


def test_revoking_without_consent_is_refused(tmp_path):
    with pytest.raises(ConsentError, match="no consent on record"):
        record.revoke(tmp_path, "Producer A", ["slack:example-channel"])


def test_revoking_a_source_not_shared_is_refused(tmp_path):
    give(tmp_path)
    with pytest.raises(ConsentError, match="isn't sharing"):
        record.revoke(tmp_path, "Producer A", ["gdoc:abc123"])


def test_a_broken_ledger_line_is_reported_by_number(tmp_path):
    give(tmp_path)
    with record.ledger_path(tmp_path).open("a", encoding="utf-8") as f:
        f.write("not json\n")
    with pytest.raises(ConsentError, match="Line 2"):
        record.consents(tmp_path)


def test_summary_says_what_is_shared_and_revoked(tmp_path):
    give(tmp_path, sources=["slack:example-channel", "gdoc:abc123"])
    record.revoke(tmp_path, "Producer A", ["gdoc:abc123"])
    text = record.summary(tmp_path)
    assert "sharing:   slack:example-channel" in text
    assert "revoked:   gdoc:abc123" in text


def test_cli_gives_shows_and_revokes(tmp_path, monkeypatch, capsys):
    data = (tmp_path / "data").as_posix()
    (tmp_path / "config.toml").write_text(f'[local]\ndata_dir = "{data}"\n', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("diffdata.common.config.user_config_file", lambda: tmp_path / "none")
    who = ["--producer", "Producer A"]
    give_args = [
        "--source",
        "slack:example-channel",
        "--consumer",
        "Example Lab",
        "--use",
        "research",
    ]
    assert cli.main(["consent", "give", *who, *give_args]) == 0
    assert "Recorded consent from Producer A" in capsys.readouterr().out
    assert cli.main(["consent", "revoke", *who, "--source", "slack:example-channel"]) == 0
    assert "Collection stops on the next run" in capsys.readouterr().out
    assert cli.main(["consent", "show"]) == 0
    assert "sharing:   nothing" in capsys.readouterr().out


def test_cli_reports_a_refused_grant(tmp_path, monkeypatch, capsys):
    data = (tmp_path / "data").as_posix()
    (tmp_path / "config.toml").write_text(f'[local]\ndata_dir = "{data}"\n', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("diffdata.common.config.user_config_file", lambda: tmp_path / "none")
    assert cli.main(["consent", "give", "--producer", "Producer A"]) == 1
    assert "at least one" in capsys.readouterr().err
