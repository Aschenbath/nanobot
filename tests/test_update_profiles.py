"""Real gateway runtime identities in isolated profile and installation directories."""

import json
import os
import signal
import socket
import subprocess
import sys
import time
from contextlib import contextmanager, nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from nanobot.installation_lock import (
    InstallationBusyError,
    installation_access,
    installation_state_dir,
    pending_update_path,
)
from nanobot.release_feed import ReleaseTarget
from nanobot.releases import ReleaseArtifact
from nanobot.update_plan import InstallationSnapshot, UpdatePlans, describe_plan
from nanobot.update_profiles import inspect_profiles, require_stopped_profiles

_CHILD = """
import sys
from pathlib import Path
from nanobot.config.home import set_home_path
from nanobot.gateway.runtime import GatewayInstance, GatewayRuntime
from nanobot.installation_lock import installation_access
from nanobot.update_profiles import record_gateway_profile

profile, environment, marker = map(Path, sys.argv[1:])
sys.prefix = str(environment)
set_home_path(profile)
instance = GatewayInstance.resolve(config_path=profile / 'config.json')
runtime = GatewayRuntime(paths=instance.paths)
with installation_access(), runtime.foreground_instance(instance.start_options(port=18790)):
    with record_gateway_profile(instance, runtime, data_dir=profile, workspace=profile / 'user project', port=18790):
        marker.write_text('ready')
        sys.stdin.read(1)
"""


@pytest.fixture
def private_home(tmp_path, monkeypatch):
    root = tmp_path / "private home"
    root.mkdir()
    monkeypatch.setenv("HOME", str(root))
    monkeypatch.setenv("USERPROFILE", str(root))
    return root


@contextmanager
def gateway(profile, environment):
    profile.mkdir(parents=True, exist_ok=True)
    (profile / "config.json").write_text('{"credential":"must-not-be-in-receipt"}')
    marker = profile / "started"
    marker.unlink(missing_ok=True)
    process = subprocess.Popen([sys.executable, "-c", _CHILD, str(profile), str(environment), str(marker)],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 8
        while not marker.exists():
            if process.poll() is not None:
                pytest.fail(process.stderr.read())
            assert time.monotonic() < deadline, "Runtime registration did not start"
            time.sleep(0.02)
        yield process
    finally:
        if process.poll() is None:
            process.stdin.write("x")
            process.stdin.flush()
        process.communicate(timeout=10)


def test_inventory_retains_profiles_after_shutdown_and_crash(private_home, tmp_path):
    environment = tmp_path / "environment"
    with gateway(tmp_path / "profile one", environment) as first:
        with gateway(tmp_path / "profile two", environment) as second:
            observed = inspect_profiles(str(environment))
            assert len(observed) == 2
            assert {item.state for item in observed} == {"running"}
            assert {item.receipt.pid for item in observed} == {first.pid, second.pid}
            assert {item.receipt.config for item in observed} == {
                str(tmp_path / "profile one/config.json"), str(tmp_path / "profile two/config.json"),
            }
            assert all(item.receipt.launch_mode == "foreground" for item in observed)
            encoded = "".join(path.read_text() for path in (installation_state_dir(environment) / "profiles").glob("*.json"))
            assert "must-not-be-in-receipt" not in encoded
            second.kill()
            second.wait(timeout=5)
            assert sorted(item.state for item in inspect_profiles(str(environment))) == ["running", "stopped"]
    observed = inspect_profiles(str(environment))
    assert len(observed) == 2 and all(item.state == "stopped" for item in observed)
    assert inspect_profiles(str(tmp_path / "another environment")) == []
    if os.name != "nt":
        for path in (installation_state_dir(environment) / "profiles").glob("*.json"):
            assert path.stat().st_mode & 0o777 == 0o600


def test_profile_reused_by_another_installation_is_not_reported_idle(private_home, tmp_path):
    profile, first_env, other_env = (tmp_path / name for name in ("profile", "first", "other"))
    with gateway(profile, first_env):
        assert inspect_profiles(str(first_env))[0].state == "running"
        reviewed = inspect_profiles(str(first_env))
        with pytest.raises(ValueError, match="still in use"):
            require_stopped_profiles(reviewed, str(first_env))
    require_stopped_profiles(reviewed, str(first_env))
    with gateway(profile, other_env):
        assert inspect_profiles(str(first_env))[0].state == "unconfirmed"
        assert inspect_profiles(str(other_env))[0].state == "running"
        with pytest.raises(ValueError, match="unconfirmed"):
            require_stopped_profiles(reviewed, str(first_env))
    assert inspect_profiles(str(first_env))[0].state == "stopped"
    with gateway(profile, first_env):
        with pytest.raises(ValueError, match="profiles changed"):
            require_stopped_profiles(reviewed, str(first_env))


def test_missing_process_identity_cannot_become_pid_only_confirmation(private_home, tmp_path):
    environment = tmp_path / "environment"
    with gateway(tmp_path / "profile", environment):
        observed = inspect_profiles(str(environment))[0]
        path = installation_state_dir(environment) / "profiles" / f"{observed.receipt.id}.json"
        payload = json.loads(path.read_text())
        payload["process_identity"] = None
        path.write_text(json.dumps(payload))
        assert inspect_profiles(str(environment))[0].state == "unconfirmed"
        payload["environment"] = str(tmp_path / "foreign environment")
        path.write_text(json.dumps(payload))
        with pytest.raises(ValueError, match="different installation"):
            inspect_profiles(str(environment))


@pytest.fixture
def startup_profile(private_home, tmp_path, monkeypatch):
    from nanobot.config.schema import Config

    profile = tmp_path / "startup profile"
    monkeypatch.setattr("nanobot.config.home._home_path", profile)
    monkeypatch.setattr("nanobot.config.loader._current_config_path", profile / "config.json")
    config = Config.model_validate({
        "agents": {"defaults": {"workspace": str(profile / "workspace"),
                                "dream": {"enabled": False}}},
        "gateway": {"heartbeat": {"enabled": False}},
    })
    return profile, config


def test_startup_is_visible_and_protected_before_workspace_writes(startup_profile, monkeypatch):
    from nanobot.cli import gateway_runtime
    from nanobot.gateway.runtime import GatewayInstance, GatewayRuntime

    profile, config = startup_profile
    seen = []

    def fail_initialization(workspace):
        observed = inspect_profiles(sys.prefix)
        assert len(observed) == 1 and observed[0].state == "running"
        assert observed[0].receipt.workspace == str(workspace)
        assert observed[0].receipt.pid == os.getpid()
        with pytest.raises(InstallationBusyError, match="installation is in use"):
            with installation_access(updating=True):
                pytest.fail("An update entered while gateway initialization was writing")
        seen.append(observed[0].receipt.run_id)
        raise RuntimeError("workspace initialization failed")

    monkeypatch.setattr(gateway_runtime, "sync_workspace_templates", fail_initialization)
    # The same public runtime entrypoint can be retried after initialization fails.
    for _ in range(2):
        with pytest.raises(RuntimeError, match="workspace initialization failed"):
            gateway_runtime._run_gateway(config, health_server_enabled=False,
                                         webui_static_dist=False)
        observed = inspect_profiles(sys.prefix)
        assert len(observed) == 1 and observed[0].state == "stopped"
        assert not observed[0].receipt.active
        instance = GatewayInstance.resolve(config_path=profile / "config.json")
        assert not GatewayRuntime(paths=instance.paths).status().running
        with installation_access(updating=True):
            pass
    assert seen[0] != seen[1]


def test_duplicate_gateway_cannot_initialize_workspace_before_claim(startup_profile, monkeypatch):
    from nanobot.cli import gateway_runtime
    from nanobot.gateway.runtime import GatewayAlreadyRunningError

    profile, config = startup_profile
    monkeypatch.setattr(gateway_runtime, "sync_workspace_templates",
                        lambda _path: pytest.fail("Duplicate startup touched the workspace"))
    with gateway(profile, sys.prefix):
        original = inspect_profiles(sys.prefix)
        with pytest.raises(GatewayAlreadyRunningError):
            gateway_runtime._run_gateway(config, health_server_enabled=False,
                                         webui_static_dist=False)
        assert inspect_profiles(sys.prefix) == original


@pytest.mark.parametrize("interrupted", [False, True])
def test_gateway_entrypoint_refuses_installation_before_initialization(startup_profile, monkeypatch, interrupted):
    from nanobot.cli import gateway_runtime

    profile, config = startup_profile
    monkeypatch.setattr(gateway_runtime, "sync_workspace_templates",
                        lambda _path: pytest.fail("Blocked startup touched the workspace"))
    if interrupted:
        pending = pending_update_path()
        pending.parent.mkdir(parents=True, exist_ok=True)
        pending.write_text("interrupted")
    with nullcontext() if interrupted else installation_access(updating=True):
        with pytest.raises(InstallationBusyError):
            gateway_runtime._run_gateway(config, health_server_enabled=False,
                                         webui_static_dist=False)
    assert inspect_profiles(sys.prefix) == []
    assert not (profile / "workspace").exists()


@pytest.mark.skipif(os.name == "nt", reason="Graceful SIGTERM proof uses the POSIX gateway handler")
def test_actual_gateway_startup_records_custom_home_and_graceful_exit(private_home, tmp_path):
    import http.client

    # Run the actual gateway lifecycle with no providers/channels or browser. Only
    # tokenizer prefetch is disabled so this test cannot download outside localhost.
    source = """
import sys
from pathlib import Path
from nanobot.config.home import set_home_path
from nanobot.config.loader import set_config_path
from nanobot.config.schema import Config
from nanobot.installation_lock import installation_access
from nanobot.cli import gateway_runtime
from nanobot.agent.loop import AgentLoop
original_from_config = AgentLoop.from_config
def create_agent(config, bus, **kwargs):
    assert kwargs['cron_service'].admission is bus.admission
    return original_from_config(config, bus, **kwargs)
AgentLoop.from_config = create_agent
profile, port = Path(sys.argv[1]), int(sys.argv[2])
set_home_path(profile)
set_config_path(profile / 'config data' / 'config.json')
config = Config.model_validate({'agents': {'defaults': {'workspace': str(profile / 'workspace'),
    'dream': {'enabled': False}}}, 'gateway': {'host': '127.0.0.1', 'port': port,
    'heartbeat': {'enabled': False}}})
gateway_runtime.warmup_token_encoding = lambda: None
with installation_access():
    gateway_runtime._run_gateway(config, webui_static_dist=False, unconfigured_provider_error='No provider for lifecycle test')
"""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    profile = tmp_path / "actual gateway"
    profile.mkdir()
    process = subprocess.Popen([sys.executable, "-c", source, str(profile), str(port)],
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        deadline = time.monotonic() + 20
        while True:
            if process.poll() is not None:
                pytest.fail(process.stdout.read())
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=0.2)
            try:
                connection.request("GET", "/health")
                response = connection.getresponse()
                if response.status == 200 and json.loads(response.read())["ready"]:
                    break
            except (OSError, http.client.HTTPException):
                pass
            finally:
                connection.close()
            assert time.monotonic() < deadline, "Isolated gateway did not become ready"
            time.sleep(0.05)
        observed = inspect_profiles(sys.prefix)
        assert len(observed) == 1 and observed[0].state == "running"
        assert observed[0].receipt.home == str(profile)
        assert observed[0].receipt.config == str(profile / "config data/config.json")
        assert observed[0].receipt.data_dir == str(profile / "config data")
        assert observed[0].receipt.workspace == str(profile / "workspace")
        assert observed[0].receipt.pid == process.pid
        process.send_signal(signal.SIGTERM)
        output, _ = process.communicate(timeout=15)
        assert process.returncode == 0, output
        after = inspect_profiles(sys.prefix)
        assert after[0].state == "stopped" and not after[0].receipt.active
        assert after[0].receipt.run_id == observed[0].receipt.run_id
    finally:
        if process.poll() is None:
            process.terminate()
            process.communicate(timeout=15)


@pytest.mark.asyncio
async def test_plan_lists_profiles_and_rejects_new_instance_before_confirmation(private_home, tmp_path):
    environment = tmp_path / "environment"
    snapshot = InstallationSnapshot(
        environment=str(environment), python=str(environment / "python"), installed_version="0.3.4",
        package_path=str(environment / "nanobot/__init__.py"), running_version="0.3.4",
        running_package=str(environment / "nanobot/__init__.py"), owner="venv", records={},
    )
    filename = "nanobot_ai-0.3.5-py3-none-win_amd64.whl"
    target = ReleaseTarget(version="0.3.5", source="pypi", url=f"https://files.pythonhosted.org/packages/{filename}",
                           artifact=ReleaseArtifact(filename=filename, size=123, sha256="a" * 64))
    feed = SimpleNamespace(latest=AsyncMock(return_value=target), revalidate=AsyncMock())
    plans = UpdatePlans(tmp_path / "plans")
    with gateway(tmp_path / "profile one", environment):
        plan = await plans.prepare(snapshot, "stable", feed, now=100)
        assert len(plan.profiles) == 1
        assert json.loads(describe_plan(plan))["profiles"][0]["state"] == "running"
        await plans.revalidate(plan.id, snapshot, feed, now=101)
        feed.revalidate.reset_mock()
        with gateway(tmp_path / "profile two", environment):
            with pytest.raises(ValueError, match="Gateway profiles changed"):
                await plans.revalidate(plan.id, snapshot, feed, now=102)
            feed.revalidate.assert_not_awaited()
