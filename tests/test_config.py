import os
from pathlib import Path

import plaud_poller.config as config
from plaud_poller.config import default_data_dir, expand_path


def test_expand_path_expands_home_and_env(monkeypatch):
    monkeypatch.setenv("PLAUD_TEST_BASE", "~/example")
    expanded = expand_path("$PLAUD_TEST_BASE/path")
    assert expanded == Path.home() / "example" / "path"


def test_default_data_dir_linux_xdg(monkeypatch):
    monkeypatch.setattr(config.sys, "platform", "linux")
    monkeypatch.setattr(config.os, "name", "posix")
    monkeypatch.setenv("XDG_DATA_HOME", "/tmp/xdg-data")
    assert default_data_dir() == Path("/tmp/xdg-data") / "plaud-poller"


def test_default_data_dir_macos(monkeypatch):
    monkeypatch.setattr(config.sys, "platform", "darwin")
    monkeypatch.setattr(config.os, "name", "posix")
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    assert default_data_dir() == Path.home() / "Library" / "Application Support" / "plaud-poller"
