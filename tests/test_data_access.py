"""Real shared-data leases across process and installation identities."""

import subprocess
import sys
from contextlib import contextmanager

import pytest

from nanobot.data_access import data_access
from nanobot.installation_lock import InstallationBusyError

_CHILD = """
import sys
from nanobot.data_access import data_access
sys.prefix = sys.argv[1]
with data_access(sys.argv[3:], updating=sys.argv[2] == 'update'):
    print('ready', flush=True)
    sys.stdin.read(1)
"""


@pytest.fixture
def private_home(tmp_path, monkeypatch):
    home = tmp_path / "private home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return home


@contextmanager
def process_access(environment, paths, *, updating=False):
    process = subprocess.Popen(
        [sys.executable, "-c", _CHILD, str(environment), "update" if updating else "runtime", *map(str, paths)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        assert process.stdout.readline().strip() == "ready", process.stderr.read()
        yield process
    finally:
        if process.poll() is None:
            process.stdin.write("x")
            process.stdin.flush()
        process.communicate(timeout=10)


def test_shared_data_blocks_update_across_installations_until_last_writer_exits(private_home, tmp_path):
    store = tmp_path / "shared sessions"
    with process_access(tmp_path / "first environment", [store]) as first:
        with process_access(tmp_path / "second environment", [store]):
            with data_access([store]):
                pass  # Normal runtime sharing remains allowed.
            first.kill()
            first.wait(timeout=10)
            with pytest.raises(InstallationBusyError, match="data is in use"):
                with data_access([store], updating=True):
                    pytest.fail("The other installation's writer was ignored")
        with data_access([store], updating=True):
            pass


@pytest.mark.parametrize("held_parent", [False, True])
def test_overlapping_roots_block_but_siblings_remain_usable(private_home, tmp_path, held_parent):
    parent = tmp_path / "sessions"
    child = parent / "chat.jsonl"
    held, requested = (parent, child) if held_parent else (child, parent)
    with process_access(tmp_path / "another environment", [held], updating=True):
        with pytest.raises(InstallationBusyError):
            with data_access([requested]):
                pytest.fail("An ancestor/descendant path bypassed maintenance")
        with data_access([tmp_path / "separate sessions"]):
            pass


def test_alias_and_partial_acquisition_do_not_bypass_or_leak_leases(private_home, tmp_path):
    store, available = tmp_path / "z-store", tmp_path / "a-store"
    store.mkdir()
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(store, target_is_directory=True)
    except OSError:
        pytest.skip("Directory symlinks are not enabled for this account")
    with process_access(tmp_path / "other environment", [alias]) as writer:
        with pytest.raises(InstallationBusyError):
            with data_access([available, store], updating=True):
                pytest.fail("A symlink alias bypassed the writer lease")
        with data_access([available]):
            pass  # Failed multi-store acquisition released its earlier leases.
        writer.kill()
        writer.wait(timeout=10)
        with data_access([store], updating=True):
            pass
