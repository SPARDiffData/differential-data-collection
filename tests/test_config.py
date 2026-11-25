"""Reading config.toml, and the data folder guards it goes through."""

from pathlib import Path

import pytest

from diffdata import cli
from diffdata.common import config as config_module
from diffdata.common.config import ConfigError, load_config
from diffdata.common.paths import DataInRepoError, DataInSyncedFolderError


def write_config(folder, text):
    path = folder / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_reads_data_dir_and_channels(tmp_path):
    data = (tmp_path / "data").as_posix()
    channels = '["#Example-Channel", "other"]'
    path = write_config(
        tmp_path, f'[sources]\nslack_channels = {channels}\n[local]\ndata_dir = "{data}"\n'
    )
    config = load_config(path)
    assert config.data_dir == (tmp_path / "data").resolve()
    assert config.slack_channels == ("example-channel", "other")


def test_reads_config_from_the_current_folder(tmp_path, monkeypatch):
    write_config(tmp_path, f'[local]\ndata_dir = "{(tmp_path / "data").as_posix()}"\n')
    monkeypatch.chdir(tmp_path)
    assert load_config().slack_channels == ()


def test_missing_config_says_how_to_make_one(tmp_path):
    with pytest.raises(ConfigError, match="config.example.toml"):
        load_config(tmp_path / "config.toml")


def test_broken_toml_is_refused(tmp_path):
    with pytest.raises(ConfigError, match="TOML"):
        load_config(write_config(tmp_path, "[local\n"))


def test_missing_data_dir_is_refused(tmp_path):
    with pytest.raises(ConfigError, match="data_dir"):
        load_config(write_config(tmp_path, '[sources]\nslack_channels = ["example-channel"]\n'))


def test_channels_must_be_a_list_of_names(tmp_path):
    data = (tmp_path / "data").as_posix()
    path = write_config(
        tmp_path, f'[sources]\nslack_channels = "x"\n[local]\ndata_dir = "{data}"\n'
    )
    with pytest.raises(ConfigError, match="list"):
        load_config(path)


def test_workspace_url_must_start_with_https(tmp_path):
    data = (tmp_path / "data").as_posix()
    path = write_config(
        tmp_path,
        f'[sources]\nslack_workspace_url = "http://example.slack.com"\n'
        f'[local]\ndata_dir = "{data}"\n',
    )
    with pytest.raises(ConfigError, match="https://"):
        load_config(path)


def test_workspace_url_loses_its_trailing_slash(tmp_path):
    data = (tmp_path / "data").as_posix()
    path = write_config(
        tmp_path,
        f'[sources]\nslack_workspace_url = "https://example.slack.com/"\n'
        f'[local]\ndata_dir = "{data}"\n',
    )
    assert load_config(path).slack_workspace_url == "https://example.slack.com"


def canvases_config(tmp_path, canvases: str):
    data = (tmp_path / "data").as_posix()
    return write_config(
        tmp_path, f'[sources]\nslack_canvases = {canvases}\n[local]\ndata_dir = "{data}"\n'
    )


def test_canvases_are_listed_by_id_or_link(tmp_path):
    link = "https://example.slack.com/docs/T00000001/F00000004"
    path = canvases_config(tmp_path, f'["F00000003", " {link}/ "]')
    assert load_config(path).slack_canvases == ("F00000003", "F00000004")


@pytest.mark.parametrize(
    "second", ["F00000003", "https://example.slack.com/docs/T00000001/F00000003"]
)
def test_a_canvas_listed_twice_is_refused_by_place_not_id(tmp_path, second):
    # A repeat is often a paste of the wrong line, so the canvas meant there would be missing.
    path = canvases_config(tmp_path, f'["F00000003", "F00000004", "{second}"]')
    with pytest.raises(ConfigError, match="Entries 1 and 3 in slack_canvases") as refused:
        load_config(path)
    assert "F00000003" not in str(refused.value)


def test_no_canvases_by_default(tmp_path):
    write_config(tmp_path, f'[local]\ndata_dir = "{(tmp_path / "data").as_posix()}"\n')
    assert load_config(tmp_path / "config.toml").slack_canvases == ()


@pytest.mark.parametrize(
    "canvases",
    [
        '["C00000001"]',  # a channel, not a canvas
        '["https://example.com/docs/T00000001/F00000003"]',  # not a Slack link
        '["https://example.slack.com/files/U00000001/F00000003/a.png"]',  # a file, not a canvas
        '["project plan"]',
        '"F00000003"',  # not a list
    ],
)
def test_anything_else_in_canvases_is_refused(tmp_path, canvases):
    with pytest.raises(ConfigError, match="slack_canvases") as refused:
        load_config(canvases_config(tmp_path, canvases))
    assert "F00000003" not in str(refused.value)  # the place in the list, not the ID


def refresh_config(tmp_path, days: str):
    data = (tmp_path / "data").as_posix()
    return write_config(
        tmp_path, f'[sources]\ncanvas_refresh_days = {days}\n[local]\ndata_dir = "{data}"\n'
    )


def test_canvases_are_read_again_every_7_days_by_default(tmp_path):
    write_config(tmp_path, f'[local]\ndata_dir = "{(tmp_path / "data").as_posix()}"\n')
    assert load_config(tmp_path / "config.toml").canvas_refresh_days == 7


def test_canvas_refresh_days_can_be_set(tmp_path):
    assert load_config(refresh_config(tmp_path, "3")).canvas_refresh_days == 3


@pytest.mark.parametrize("days", ["0", "-1", "1.5", '"7"', "true"])
def test_canvas_refresh_days_must_be_a_whole_number_of_days(tmp_path, days):
    with pytest.raises(ConfigError, match="canvas_refresh_days"):
        load_config(refresh_config(tmp_path, days))


def test_data_dir_goes_through_the_repo_guard(tmp_path):
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    data = (tmp_path / "repo" / "data").as_posix()
    with pytest.raises(DataInRepoError):
        load_config(write_config(tmp_path, f'[local]\ndata_dir = "{data}"\n'))


def test_data_dir_goes_through_the_synced_folder_guard(tmp_path):
    data = (tmp_path / "Dropbox" / "data").as_posix()
    with pytest.raises(DataInSyncedFolderError):
        load_config(write_config(tmp_path, f'[local]\ndata_dir = "{data}"\n'))


def config_text(tmp_path, channels="[]"):
    data = (tmp_path / "data").as_posix()
    return f'[sources]\nslack_channels = {channels}\n[local]\ndata_dir = "{data}"\n'


def test_tests_never_read_the_real_config(no_real_config):
    assert config_module.user_config_file() == no_real_config
    assert no_real_config != Path.home() / ".diffdata" / "config.toml"


def test_reads_config_from_the_diffdata_folder_first(tmp_path, monkeypatch, no_real_config):
    no_real_config.parent.mkdir(parents=True)
    path = write_config(no_real_config.parent, config_text(tmp_path, '["outside"]'))
    monkeypatch.chdir(tmp_path)  # no config.toml here
    config = load_config()
    assert config.slack_channels == ("outside",)
    assert config.path.samefile(path)


def test_falls_back_to_the_current_folder(tmp_path, monkeypatch):
    path = write_config(tmp_path, config_text(tmp_path))
    monkeypatch.chdir(tmp_path)
    assert load_config().path.samefile(path)


def test_two_config_files_are_refused(tmp_path, monkeypatch, no_real_config):
    no_real_config.parent.mkdir(parents=True)
    write_config(no_real_config.parent, config_text(tmp_path))
    write_config(tmp_path, config_text(tmp_path))
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ConfigError, match="two config.toml"):
        load_config()


def test_no_config_says_where_to_put_it(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ConfigError, match=r"\.diffdata.*config\.example\.toml"):
        load_config()


def test_channels_lists_the_opted_in_channels(tmp_path, monkeypatch, capsys):
    write_config(tmp_path, config_text(tmp_path, '["#Example-Channel", "other"]'))
    monkeypatch.chdir(tmp_path)
    assert cli.main(["discover", "slack", "channels"]) == 0
    first, *names = capsys.readouterr().out.splitlines()
    assert first.startswith("Opted-in channels, from ") and first.endswith("config.toml:")
    assert names == ["example-channel", "other"]


def test_channels_stops_when_none_are_listed(tmp_path, monkeypatch, capsys):
    write_config(tmp_path, config_text(tmp_path))
    monkeypatch.chdir(tmp_path)
    assert cli.main(["discover", "slack", "channels"]) == 1
    assert "slack_channels" in capsys.readouterr().err


DOC_ID = "FAKE-DOC-00000000000001"


def docs_config(folder, docs):
    data = (folder / "data").as_posix()
    text = f'[sources]\ngoogle_docs = {docs}\n[local]\ndata_dir = "{data}"\n'
    return write_config(folder, text)


@pytest.mark.parametrize(
    "listed",
    [
        DOC_ID,
        f"https://docs.google.com/document/d/{DOC_ID}/edit?tab=t.0",
        f"https://docs.google.com/document/u/1/d/{DOC_ID}",
        f"https://drive.google.com/open?id={DOC_ID}",
    ],
)
def test_a_google_doc_is_listed_by_its_id_or_its_link(tmp_path, listed):
    assert load_config(docs_config(tmp_path, f'["{listed}"]')).google_docs == (DOC_ID,)


def test_google_docs_are_optional(tmp_path):
    path = write_config(tmp_path, f'[local]\ndata_dir = "{(tmp_path / "d").as_posix()}"\n')
    assert load_config(path).google_docs == ()


def test_a_bad_google_docs_entry_is_named_by_its_place_only(tmp_path):
    path = docs_config(tmp_path, f'["{DOC_ID}", "https://example.org/private-name"]')
    with pytest.raises(ConfigError, match="Entry 2 in google_docs") as refused:
        load_config(path)
    assert "private-name" not in str(refused.value)


def test_the_same_doc_listed_twice_is_refused(tmp_path):
    link = f"https://docs.google.com/document/d/{DOC_ID}/edit"
    with pytest.raises(ConfigError, match="Entries 1 and 2 in google_docs"):
        load_config(docs_config(tmp_path, f'["{DOC_ID}", "{link}"]'))


def test_google_docs_must_be_a_list(tmp_path):
    with pytest.raises(ConfigError, match="must be a list"):
        load_config(docs_config(tmp_path, '"one"'))


# The opt-out list (#105): Slack user IDs only, so a typo can't silently leave no one out.


def test_reads_the_opted_out_slack_ids(tmp_path):
    data = (tmp_path / "data").as_posix()
    ids = '["U00000001", " W00000002 "]'
    path = write_config(
        tmp_path, f'[sources]\nslack_opted_out = {ids}\n[local]\ndata_dir = "{data}"\n'
    )
    assert load_config(path).slack_opted_out == ("U00000001", "W00000002")


def test_the_opt_out_list_is_empty_by_default(tmp_path):
    data = (tmp_path / "data").as_posix()
    path = write_config(tmp_path, f'[local]\ndata_dir = "{data}"\n')
    assert load_config(path).slack_opted_out == ()


@pytest.mark.parametrize(
    "value, message",
    [
        ('["alex@example.com"]', "Entry 1 in slack_opted_out"),
        ('["U00000001", "u00000002"]', "Entry 2 in slack_opted_out"),
        ('["C00000001"]', "Entry 1 in slack_opted_out"),
        ('["U0001"]', "Entry 1 in slack_opted_out"),
        ('["UABCDEFGH"]', "Entry 1 in slack_opted_out"),  # no digit, as the scrub requires
        ('["U00000001", "U00000001"]', "Entries 1 and 2 in slack_opted_out"),
        ('"U00000001"', "must be a list of Slack user IDs"),
    ],
)
def test_must_refuse_6_an_opt_out_entry_that_isnt_a_user_id_stops_the_run(tmp_path, value, message):
    data = (tmp_path / "data").as_posix()
    path = write_config(
        tmp_path, f'[sources]\nslack_opted_out = {value}\n[local]\ndata_dir = "{data}"\n'
    )
    with pytest.raises(ConfigError, match=message) as refused:
        load_config(path)
    assert "example.com" not in str(refused.value)  # the entry's place, never its value


@pytest.mark.parametrize(
    "text, where",
    [
        ('[store]\nslack_opted_out = ["U00000001"]\n', "store.slack_opted_out"),
        ('slack_opted_out = ["U00000001"]\n', "slack_opted_out"),
        ('[sources]\nslack_opt_out = ["U00000001"]\n', "sources.slack_opt_out"),
        ('[sources]\nopted_out = ["U00000001"]\n', "sources.opted_out"),
    ],
)
def test_must_refuse_6_an_opt_out_list_anywhere_but_sources_stops_the_run(tmp_path, text, where):
    # A line pasted at the end of the file lands under the last table, [store], and a
    # misspelt key is just unknown: either would silently leave no one out.
    data = (tmp_path / "data").as_posix()
    path = write_config(tmp_path, f'[local]\ndata_dir = "{data}"\n' + text)
    with pytest.raises(
        ConfigError, match="must be slack_opted_out, under \\[sources\\]"
    ) as refused:
        load_config(path)
    assert where in str(refused.value) and "U00000001" not in str(refused.value)


@pytest.mark.parametrize(
    "text, named",
    [
        ('[sources]\nslack_oped_out = ["U00000001"]\n', "slack_oped_out"),
        ('[sources]\nslack_chanels = ["general"]\n', "slack_chanels"),
        ('stray = "value"\n', "a key under the top level that isn't a setting name"),
    ],
)
def test_a_setting_diffdata_doesnt_know_stops_the_run(tmp_path, text, named):
    # A typo in slack_opted_out that doesn't look like it would otherwise be ignored (#105).
    data = (tmp_path / "data").as_posix()
    path = write_config(tmp_path, text + f'[local]\ndata_dir = "{data}"\n')
    with pytest.raises(ConfigError, match="a setting diffdata doesn't know") as refused:
        load_config(path)
    assert named in str(refused.value) and "U00000001" not in str(refused.value)


@pytest.mark.parametrize(
    "text, named",
    [
        ('[store]\nslack_opted_ou = ["U00000001"]\n', "a key under store that isn't"),
        ('[store]\nbucket = ""\nextra = 1\n', "a key under store that isn't"),
        ('[store]\nbukcet = ""\n', "store.bukcet"),  # close to a setting: shown
    ],
)
def test_a_setting_local_or_store_doesnt_know_stops_the_run(tmp_path, text, named):
    data = (tmp_path / "data").as_posix()
    path = write_config(tmp_path, f'[local]\ndata_dir = "{data}"\n' + text)
    with pytest.raises(ConfigError, match="a setting diffdata doesn't know") as refused:
        load_config(path)
    assert named in str(refused.value)


def test_a_key_that_looks_like_an_id_or_a_name_is_never_printed(tmp_path):
    data = (tmp_path / "data").as_posix()
    path = write_config(
        tmp_path, f'[sources]\nU0FAKE0001 = "Alex Example"\n[local]\ndata_dir = "{data}"\n'
    )
    with pytest.raises(ConfigError, match="a key that isn't a setting name") as refused:
        load_config(path)
    assert "U0FAKE0001" not in str(refused.value) and "Alex" not in str(refused.value)


def test_a_lowercase_key_that_isnt_close_to_a_setting_is_never_printed(tmp_path):
    # It may be a pasted name, so only the table is named.
    data = (tmp_path / "data").as_posix()
    path = write_config(
        tmp_path, f'[sources]\nalex_example = "U00000001"\n[local]\ndata_dir = "{data}"\n'
    )
    with pytest.raises(ConfigError, match="a key under sources that isn't a setting name") as e:
        load_config(path)
    assert "alex" not in str(e.value)


# Decision 2 on #130: a channel is listed by its name, its ID, or a link to it.
def test_a_channel_can_be_listed_by_its_id_or_its_link(tmp_path):
    data = (tmp_path / "data").as_posix()
    channels = (
        '["example-channel", "C00000002", '
        '"https://example.slack.com/archives/G00000003", '
        '"https://example.slack.com/archives/C00000004/p1700000000000100"]'
    )
    path = write_config(
        tmp_path, f'[sources]\nslack_channels = {channels}\n[local]\ndata_dir = "{data}"\n'
    )
    config = load_config(path)
    assert config.slack_channels == ("example-channel",)
    assert config.slack_channel_ids == ("C00000002", "G00000003", "C00000004")
    assert config.lists_channel("C00000002")
    assert config.lists_channel("C00000009", "Example-Channel")
    assert not config.lists_channel("C00000009", "other")
    assert not config.lists_channel("C00000009")


def test_a_lowercase_name_that_looks_like_an_id_stays_a_name(tmp_path):
    data = (tmp_path / "data").as_posix()
    path = write_config(
        tmp_path, f'[sources]\nslack_channels = ["c00000002"]\n[local]\ndata_dir = "{data}"\n'
    )
    config = load_config(path)
    assert config.slack_channels == ("c00000002",)
    assert config.slack_channel_ids == ()


@pytest.mark.parametrize(
    "listed",
    [
        '["https://example.slack.com/docs/T00000001/F00000001"]',  # a canvas link
        '["https://example.com/archives/C00000002"]',  # not Slack
    ],
)
def test_a_link_that_isnt_to_a_channel_is_refused_without_quoting_it(tmp_path, listed):
    data = (tmp_path / "data").as_posix()
    path = write_config(
        tmp_path, f'[sources]\nslack_channels = {listed}\n[local]\ndata_dir = "{data}"\n'
    )
    with pytest.raises(ConfigError, match="Entry 1 in slack_channels") as e:
        load_config(path)
    assert "example" not in str(e.value).replace(str(path), "")


def test_the_same_channel_id_twice_is_refused(tmp_path):
    data = (tmp_path / "data").as_posix()
    channels = '["C00000002", "x", "https://example.slack.com/archives/C00000002"]'
    path = write_config(
        tmp_path, f'[sources]\nslack_channels = {channels}\n[local]\ndata_dir = "{data}"\n'
    )
    with pytest.raises(ConfigError, match="Entries 1 and 3"):
        load_config(path)


# CON-5, must-refuse 17 on #130: a DM or a group DM is never listed.
@pytest.mark.parametrize(
    "listed",
    [
        '["D00000001"]',
        '["https://example.slack.com/archives/D00000001"]',
        '["mpdm-alex--sam--kim-1"]',
        '["#MPDM-alex--sam-1"]',
    ],
)
def test_a_dm_or_group_dm_in_slack_channels_is_refused(tmp_path, listed):
    data = (tmp_path / "data").as_posix()
    path = write_config(
        tmp_path, f'[sources]\nslack_channels = {listed}\n[local]\ndata_dir = "{data}"\n'
    )
    with pytest.raises(ConfigError, match="CON-5") as e:
        load_config(path)
    assert "D00000001" not in str(e.value) and "alex" not in str(e.value)
