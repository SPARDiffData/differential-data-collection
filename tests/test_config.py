"""Reading config.toml, and the data folder guards it goes through."""

import pytest

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


def test_data_dir_goes_through_the_repo_guard(tmp_path):
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    data = (tmp_path / "repo" / "data").as_posix()
    with pytest.raises(DataInRepoError):
        load_config(write_config(tmp_path, f'[local]\ndata_dir = "{data}"\n'))


def test_data_dir_goes_through_the_synced_folder_guard(tmp_path):
    data = (tmp_path / "Dropbox" / "data").as_posix()
    with pytest.raises(DataInSyncedFolderError):
        load_config(write_config(tmp_path, f'[local]\ndata_dir = "{data}"\n'))
