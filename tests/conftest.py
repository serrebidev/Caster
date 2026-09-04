# Copyright (c) serrebidev and contributors
# This file is part of Caster
# SPDX-License-Identifier: MIT
"""Shared fixtures.

The default suite is offline and device-free: it must pass on a machine with
no network, no Chromecast and no receiver. Anything that needs the real world
is marked `live` and deselected unless you ask for it.

    py -m pytest                 # offline suite
    py -m pytest -m live         # only the tests that touch real devices
    py -m pytest -m ""           # everything
"""
from __future__ import annotations

import os
import socket
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "live: needs the real network, a real device, or ffmpeg")


def pytest_collection_modifyitems(config, items):
    if config.getoption("-m"):
        return                      # the user asked for something specific
    skip = pytest.mark.skip(reason="needs real devices; run with -m live")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def no_network(monkeypatch):
    """Make any accidental socket use fail loudly rather than hang a test."""
    class Blocked(socket.socket):
        def connect(self, *a, **k):
            raise AssertionError("test tried to open a network connection")

        def connect_ex(self, *a, **k):
            raise AssertionError("test tried to open a network connection")

    monkeypatch.setattr(socket, "socket", Blocked)


@pytest.fixture
def frame():
    """A MainFrame with no wx behind it.

    Casting logic lives on MainFrame but almost none of it needs a window, so
    the object is built without running __init__ and given only the
    attributes the method under test reads. Anything else raising
    AttributeError is the test's fault, not the app's.
    """
    import caster
    obj = caster.MainFrame.__new__(caster.MainFrame)
    obj._targets = []
    obj._sources = []
    obj._stop_flag = False
    obj._mc_restore = {}
    obj._mc_grouped = []
    obj._mc_epoch = 0
    obj._updating_slider = False
    obj._muted = False
    obj.cast = None
    obj.atv = None
    obj._relay = None
    obj._cast_zc = None
    return obj


@pytest.fixture
def settings(tmp_path):
    """A Settings instance writing to a temp file, never the real profile."""
    from caster_config import Settings
    return Settings(path=str(tmp_path / "settings.json"))


@pytest.fixture
def fake_status():
    """Build a Chromecast media status the way pychromecast reports one."""
    def make(state, idle_reason=None, session=1):
        return types.SimpleNamespace(player_state=state,
                                     idle_reason=idle_reason,
                                     media_session_id=session)
    return make


@pytest.fixture
def status_sequence(fake_status):
    """A media controller that walks a fixed list of statuses."""
    class MC:
        def __init__(self, states):
            self._states = states
            self.calls = 0

        @property
        def status(self):
            state = self._states[min(self.calls, len(self._states) - 1)]
            self.calls += 1
            return state
    return MC
