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
