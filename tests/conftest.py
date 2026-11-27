import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def _w(root, rel, content, mode="w"):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, mode) as fh:
        fh.write(content)
    return p


@pytest.fixture
def fake_sys(tmp_path, monkeypatch):
    """A fake /sys tree; returns a helper to add files."""
    root = str(tmp_path / "sys")
    os.makedirs(root)
    monkeypatch.setenv("UWCTL_SYSFS_ROOT", root)

    class FS:
        path = root

        def add(self, rel, content="", mode="w"):
            return _w(root, rel, content, mode)

        def read(self, rel):
            with open(os.path.join(root, rel)) as fh:
                return fh.read()

    return FS()


@pytest.fixture
def mock_ec():
    from uwctl.ec import MockEC

    return MockEC()


@pytest.fixture
def state_path(tmp_path):
    return str(tmp_path / "state.json")
