"""Real offline pip jobs in disposable venvs; no user installation is mutated."""

import asyncio
import base64
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import venv
from dataclasses import replace
from pathlib import Path
from uuid import uuid4
from zipfile import ZipFile

import httpx
import pytest
from packaging.tags import sys_tags

from nanobot.config.home import get_selected_home_path, set_home_path
from nanobot.data_access import data_access
from nanobot.gateway.runtime import GatewayInstance, GatewayRuntime
from nanobot.installation_lock import (
    InstallationBusyError,
    installation_access,
    installation_state_dir,
    pending_update_path,
)
from nanobot.release_feed import ReleaseFeed, ReleaseTarget
from nanobot.releases import PLATFORMS, ReleaseArtifact, release_filenames
from nanobot.update_backup import BackupSource
from nanobot.update_jobs import prepare_job, start_job
from nanobot.update_plan import InstallationSnapshot, UpdatePlan
from nanobot.update_profiles import inspect_profiles, record_gateway_profile
from nanobot.update_worker import (
    JobStatus,
    Phase,
    WorkerSpec,
    cancel_job,
    inspect_environment,
    read_record,
    write_record,
)

_RESTART_PROBE = """
# Synthetic installed handoff for worker/lease routing, not gateway readiness.
import time
from pathlib import Path
import nanobot
from _nanobot_update.data_access import data_access
from _nanobot_update.installation_lock import InstallationBusyError, installation_access, pending_update_path
from _nanobot_update.update_worker import JobStatus, Phase, WorkerSpec, write_record

def run_gateway(directory, participant):
    raise AssertionError('This routing fixture must not launch a gateway')

def run_restarts(directory):
    spec = WorkerSpec.read(directory)
    status = JobStatus.read(directory)
    assert status.phase in {Phase.RESTARTING, Phase.RESTART_FAILED}
    with installation_access(allow_incomplete=True):
        try:
            with installation_access(updating=True):
                raise AssertionError('Runtime bridge has no shared installation lease')
        except InstallationBusyError:
            pass
        with data_access((item.path for item in spec.backup_sources), job_id=spec.plan_id) as data:
            write_record(directory / 'handoff-probe.json', {
                'version': nanobot.__version__, 'package': str(Path(nanobot.__file__).resolve()),
                'attempt': status.attempt, 'phase': status.phase.value,
            })
            if (directory / 'fail-restart').exists():
                raise RuntimeError('Controlled handoff failure')
            JobStatus(spec.plan_id, Phase.FINALIZING, status.attempt, True,
                      'Synthetic routing complete; not real gateway evidence', int(time.time())).save(directory)
            data.release_reservation()
            pending_update_path().unlink()
            JobStatus(spec.plan_id, Phase.SUCCEEDED, status.attempt, True,
                      'Synthetic routing complete; not real gateway evidence', int(time.time())).save(directory)
"""


def wheel(directory, name, version, *, requires=(), platform="any", broken=False, restart=False):
    package = name.replace("-", "_")
    dist = f"{package}-{version}.dist-info"
    files = {
        f"{package if name != 'nanobot-ai' else 'nanobot'}/__init__.py": f"__version__ = '{version}'\n".encode(),
        f"{dist}/METADATA": (f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
                             + "".join(f"Requires-Dist: {requirement}\n" for requirement in requires)).encode(),
        f"{dist}/WHEEL": f"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-{platform}\n".encode(),
    }
    if name == "nanobot-ai":
        if broken:
            files["nanobot/__init__.py"] = b"raise RuntimeError('broken release fixture')\n"
        files["nanobot/web/dist/index.html"] = f"verified UI {version}".encode()
        files["nanobot/tui/bin/fixture"] = b"fixture, not a native execution claim"
        if restart:
            files["nanobot/update_restart.py"] = _RESTART_PROBE.encode()
    records = []
    for filename, data in files.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        records.append(f"{filename},sha256={digest},{len(data)}")
    files[f"{dist}/RECORD"] = ("\n".join(records) + f"\n{dist}/RECORD,,\n").encode()
    path = directory / f"{package}-{version}-py3-none-{platform}.whl"
    with ZipFile(path, "w") as archive:
        for filename, data in files.items():
            archive.writestr(filename, data)
    return path


@pytest.fixture
def installation(tmp_path, monkeypatch, request):
    state_home = tmp_path / "private home"
    state_home.mkdir()
    monkeypatch.setenv("HOME", str(state_home))
    monkeypatch.setenv("USERPROFILE", str(state_home))
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    environment = tmp_path / "venv with spaces"
    venv.EnvBuilder(with_pip=True).create(environment)
    python = str(environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))
    monkeypatch.setenv("PIP_CONFIG_FILE", os.devnull)
    monkeypatch.setenv("PIP_NO_INDEX", "1")
    monkeypatch.setenv("PIP_FIND_LINKS", str(artifacts))
    for version in ("1.0.0", "2.0.0"):
        wheel(artifacts, "fixture-update-dep", version)
    previous = wheel(artifacts, "nanobot-ai", "0.3.4", requires=["fixture-update-dep==1.0.0"])
    subprocess.run([python, "-I", "-m", "pip", "install", "--disable-pip-version-check", str(previous)],
                   check=True, capture_output=True, timeout=60)
    platform = next(tag.platform for tag in sys_tags() if tag.platform in PLATFORMS.values())
    candidate = wheel(artifacts, "nanobot-ai", "0.3.5", requires=["fixture-update-dep>=2.0.0"], platform=platform,
                      broken=getattr(request, "param", False) is True,
                      restart=getattr(request, "param", False) == "restart")
    baseline = inspect_environment(python)
    site = Path(subprocess.check_output([python, "-I", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"], text=True).strip())
    snapshot = InstallationSnapshot(
        environment=str(environment.resolve()), python=python, installed_version="0.3.4", running_version="0.3.4",
        package_path=str(site / "nanobot/__init__.py"), running_package=str(site / "nanobot/__init__.py"),
        owner="venv", records={"pyvenv.cfg": baseline.receipt_sha, **baseline.app_records},
    )
    profile, workspace = tmp_path / "gateway data", tmp_path / "user project"
    (profile / "sessions").mkdir(parents=True)
    workspace.mkdir()
    (profile / "config.json").write_text('{"fixture":"private credential"}')
    (profile / "sessions/chat.jsonl").write_text('{"content":"original conversation"}\n')
    (workspace / "private-project.py").write_text("not nanobot state")
    selected_home = get_selected_home_path()
    try:
        set_home_path(profile)
        instance = GatewayInstance.resolve(config_path=profile / "config.json", workspace=str(workspace))
        runtime = GatewayRuntime(paths=instance.paths)
        # Synthetic package fixture, real runtime receipt lifetime. Full gateway
        # registration is independently tested in test_update_profiles.py.
        with monkeypatch.context() as patch:
            patch.setattr(sys, "prefix", str(environment))
            with runtime.foreground_instance(instance.start_options(port=18790)):
                with record_gateway_profile(instance, runtime, data_dir=profile, workspace=workspace, port=18790):
                    pass
    finally:
        set_home_path(selected_home)
    profiles = inspect_profiles(str(environment))
    restart_profiles = []
    if getattr(request, "param", False) == "restart":
        # Synthetic process metadata selects the installed routing stub above;
        # test_update_restart.py separately runs the actual background gateway.
        receipt = profiles[0].receipt.model_copy(update={"launch_mode": "background"})
        write_record(installation_state_dir(str(environment)) / "profiles" / f"{receipt.id}.json",
                     receipt.model_dump(mode="json"))
        profiles = inspect_profiles(str(environment))
        restart_profiles = [receipt.id]
    target = ReleaseTarget(version="0.3.5", source="pypi",
                           url=f"https://files.pythonhosted.org/packages/{candidate.name}",
                           artifact=ReleaseArtifact(filename=candidate.name, size=candidate.stat().st_size,
                                                    sha256=hashlib.sha256(candidate.read_bytes()).hexdigest()))
    now = int(time.time())
    plan = UpdatePlan(id=str(uuid4()), installation=snapshot, target=target, extras=[], channel="stable",
                      profiles=profiles, restart_profiles=restart_profiles,
                      created_at=now, expires_at=now + 900)
    requests = []

    def response(request):
        requests.append(str(request.url))
        if request.url.host == "files.pythonhosted.org":
            return httpx.Response(200, content=candidate.read_bytes())
        assert request.url.path == "/pypi/nanobot-ai/0.3.5/json", "execution re-resolved latest"
        files = []
        for filename in release_filenames("0.3.5"):
            if filename.endswith((".whl", ".tar.gz")):
                selected = filename == candidate.name
                files.append({"filename": filename, "size": target.artifact.size if selected else 1,
                              "url": f"https://files.pythonhosted.org/packages/{filename}", "yanked": False,
                              "digests": {"sha256": target.artifact.sha256 if selected else "a" * 64}})
        return httpx.Response(200, json={"info": {"name": "nanobot-ai", "version": "0.3.5", "requires_python": ">=3.11"}, "urls": files})

    return plan, candidate, site, requests, httpx.MockTransport(response)


async def wait_worker(process):
    code = await asyncio.to_thread(process.wait, 40)
    assert code == 0


@pytest.mark.parametrize("installation", ["restart"], indirect=True)
async def test_installed_runtime_failure_repairs_handoff_without_reinstalling(installation, tmp_path, monkeypatch):
    plan, _, site, requests, transport = installation
    async with ReleaseFeed(tmp_path / "feed", transport=transport) as feed:
        directory = await prepare_job(plan, feed)
        failure = directory / "fail-restart"
        failure.touch()
        process = await start_job(directory, feed)
        assert await asyncio.to_thread(process.wait, 40) != 0
        status = JobStatus.read(directory)
        assert status.phase == Phase.RESTART_FAILED and status.attempt == 1
        assert status.mutation_started
        probe = read_record(directory / "handoff-probe.json")
        assert probe == {"version": "0.3.5", "package": str((site / "nanobot/__init__.py").resolve()),
                         "attempt": 1, "phase": "restarting"}
        assert inspect_environment(plan.installation.python).app_version == "0.3.5"
        backup = (directory / "backup/data.zip").read_bytes()
        record = (site / "nanobot_ai-0.3.5.dist-info/RECORD").read_bytes()
        with pytest.raises(InstallationBusyError):
            with installation_access(environment=plan.installation.environment):
                pytest.fail("Runtime failure released the installation fence")
        with pytest.raises(InstallationBusyError):
            with data_access([plan.profiles[0].receipt.config]):
                pytest.fail("Runtime failure released the data reservation")

        async def offline(*_args, **_kwargs):
            pytest.fail("Already-installed handoff repair must not depend on a release feed")

        monkeypatch.setattr(feed, "revalidate", offline)
        requests.clear()
        with pytest.raises(ValueError, match="explicit repair"):
            await start_job(directory, feed)
        failure.unlink()
        await wait_worker(await start_job(directory, feed, repair=True))
    assert not requests
    assert JobStatus.read(directory).phase == Phase.SUCCEEDED
    assert JobStatus.read(directory).attempt == 1
    assert read_record(directory / "handoff-probe.json")["phase"] == "restart_failed"
    assert (directory / "backup/data.zip").read_bytes() == backup
    assert (site / "nanobot_ai-0.3.5.dist-info/RECORD").read_bytes() == record
    with installation_access(environment=plan.installation.environment), data_access([plan.profiles[0].receipt.config]):
        pass


def test_update_record_size_is_bounded_before_replacement(tmp_path):
    path = tmp_path / "record.json"
    record = {"message": "配置实例"}
    write_record(path, record)
    original = path.read_bytes()
    assert read_record(path, max_bytes=len(original)) == record
    write_record(path, record, max_bytes=len(original))
    with pytest.raises(ValueError, match="size limit"):
        write_record(path, record, max_bytes=len(original) - 1)
    assert path.read_bytes() == original
    with pytest.raises(ValueError, match="size limit"):
        read_record(path, max_bytes=len(original) - 1)


@pytest.mark.asyncio
async def test_expanded_profile_request_is_readable_by_the_standalone_worker(installation, tmp_path):
    plan, _, _, _, transport = installation
    async with ReleaseFeed(tmp_path / "feed", transport=transport) as feed:
        directory = await prepare_job(plan, feed)
    spec = WorkerSpec.read(directory)
    # A multi-profile inventory expands into many explicit named stores. It may
    # outgrow the journal limit even when the saved plan is comfortably bounded.
    sources = tuple(BackupSource(str(tmp_path / f"profile-{i}" / ("nested-" * 16) / "sessions")) for i in range(8192))
    expanded = replace(spec, backup_sources=sources)
    expanded.save(directory)
    assert (directory / "request.json").stat().st_size > 1024 * 1024
    assert WorkerSpec.read(directory) == expanded
    result = subprocess.run([spec.baseline.base_python, "-I", str(directory / "worker.pyz"), "--status", str(directory)],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["phase"] == "ready"
    # Earlier saved requests had no backup contract. Reject them explicitly;
    # their own immutable worker remains available for inspection/recovery.
    spec.save(directory)
    legacy = read_record(directory / "request.json")
    legacy["schema_version"] = 1
    legacy.pop("backup_sources")
    write_record(directory / "request.json", legacy)
    with pytest.raises(ValueError, match="Unsupported update worker request"):
        WorkerSpec.read(directory)
    assert inspect_environment(plan.installation.python).app_version == "0.3.4"


@pytest.mark.asyncio
async def test_real_job_stages_then_installs_exact_app_and_dependency(installation, monkeypatch, tmp_path):
    plan, _, site, requests, transport = installation
    async with ReleaseFeed(tmp_path / "feed", transport=transport) as feed:
        directory = await prepare_job(plan, feed)
        assert inspect_environment(plan.installation.python).app_version == "0.3.4"
        assert not (directory / "approval.json").exists()
        spec = WorkerSpec.read(directory)
        # The detached archive may load the target runtime only after acquiring
        # its installation lease. Its frozen helpers must remain separate from
        # the real installed nanobot package, not supply an empty __init__.
        probe_command = [
            plan.installation.python, "-I", "-c", """
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from _nanobot_update.installation_lock import installation_access
from _nanobot_update.update_worker import JobStatus
with installation_access():
    import nanobot
    print(json.dumps([nanobot.__version__, nanobot.__file__, JobStatus.read(Path(sys.argv[2])).phase.value]))
""", str(directory / "worker.pyz"), str(directory),
        ]
        probe = subprocess.run(probe_command, check=True, capture_output=True, text=True, timeout=20)
        loaded_version, loaded_path, phase = json.loads(probe.stdout)
        assert loaded_version == "0.3.4" and phase == "ready"
        assert Path(loaded_path).resolve() == (site / "nanobot/__init__.py").resolve()
        assert {(item.name, item.version) for item in spec.wheels} == {
            ("nanobot-ai", "0.3.5"), ("fixture-update-dep", "2.0.0"),
        }
        assert await prepare_job(plan, feed) == directory
        # Installer configuration cannot redirect the actual write to another env.
        unintended = tmp_path / "not-the-approved-environment"
        monkeypatch.setenv("PIP_TARGET", str(unintended))
        await wait_worker(await start_job(directory, feed))
    status = JobStatus.read(directory)
    assert status.phase == Phase.INSTALLED and status.attempt == 1 and status.mutation_started
    assert "not yet verified" in status.message
    assert inspect_environment(plan.installation.python).app_version == "0.3.5"
    probe = subprocess.run(probe_command, check=True, capture_output=True, text=True, timeout=20)
    loaded_version, loaded_path, phase = json.loads(probe.stdout)
    assert loaded_version == "0.3.5" and phase == "installed"
    assert Path(loaded_path).resolve() == (site / "nanobot/__init__.py").resolve()
    assert (site / "nanobot/web/dist/index.html").read_text() == "verified UI 0.3.5"
    assert not unintended.exists()
    with ZipFile(directory / "backup/data.zip") as archive:
        entries = json.loads(archive.read("manifest.json"))["entries"]
        configs = [item for item in entries if item["path"] == plan.profiles[0].receipt.config]
        assert len(configs) == 1
        assert archive.read(configs[0]["member"]) == b'{"fixture":"private credential"}'
        assert not any(item["path"].endswith("private-project.py") for item in entries)
    assert not pending_update_path(plan.installation.environment).exists()
    # Recover the reachable crash gap between saving INSTALLED and clearing the
    # startup fence. The completed job must verify/clear it, not install again.
    write_record(pending_update_path(plan.installation.environment),
                 {"schema_version": 2, "job_id": plan.id, "version": "0.3.5"})
    result = subprocess.run([spec.baseline.base_python, "-I", str(directory / "worker.pyz"), str(directory)],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert JobStatus.read(directory).attempt == 1
    assert not pending_update_path(plan.installation.environment).exists()
    assert requests and all("/0.3.5/" in url or "/packages/" in url for url in requests)


@pytest.mark.asyncio
async def test_changed_artifact_or_owner_receipt_never_installs(installation, tmp_path):
    plan, _, _, _, transport = installation
    async with ReleaseFeed(tmp_path / "feed", transport=transport) as feed:
        directory = await prepare_job(plan, feed)
        app = directory / "wheels" / plan.target.artifact.filename
        original = app.read_bytes()
        app.write_bytes(original + b"changed")
        with pytest.raises(ValueError, match="size"):
            await start_job(directory, feed)
        assert not pending_update_path(plan.installation.environment).exists()
        app.write_bytes(original)
        receipt = Path(plan.installation.environment) / "pyvenv.cfg"
        receipt.write_text(receipt.read_text() + "\n# changed by another owner\n")
        with pytest.raises(ValueError, match="ownership"):
            await start_job(directory, feed)
    assert inspect_environment(plan.installation.python).app_version == "0.3.4"


@pytest.mark.asyncio
async def test_stale_plan_is_rejected_before_staging(installation, tmp_path):
    plan, _, _, requests, transport = installation
    # A real environment change, not a hypothetical internal flag.
    receipt = Path(plan.installation.environment) / "pyvenv.cfg"
    receipt.write_text(receipt.read_text() + "\n# changed\n")
    async with ReleaseFeed(tmp_path / "feed", transport=transport) as feed:
        with pytest.raises(ValueError, match="changed since"):
            await prepare_job(plan, feed)
    assert not requests


@pytest.mark.asyncio
async def test_worker_survives_coordinator_loss_without_unlocking_live_pip(installation, tmp_path):
    plan, _, site, _, transport = installation
    async with ReleaseFeed(tmp_path / "feed", transport=transport) as feed:
        directory = await prepare_job(plan, feed)
        ready, release = tmp_path / "pip-ready", tmp_path / "pip-continue"
        pip_entry = site / "pip/__main__.py"
        original = pip_entry.read_text()
        # Hold the real pip CLI immediately before it runs; only the disposable
        # fixture's launcher is wrapped, and its original body still executes.
        wrapper = (
            f"from pathlib import Path\nimport os,time\nPath({str(ready)!r}).write_text(str(os.getpid()))\n"
            f"deadline=time.monotonic()+20\nwhile not Path({str(release)!r}).exists():\n"
            "    if time.monotonic()>deadline: raise SystemExit(99)\n    time.sleep(0.02)\n"
            f"exec(compile({original!r}, __file__, 'exec'))\n"
        )
        pip_entry.write_text(wrapper)
        process = await start_job(directory, feed)
        try:
            for _ in range(200):
                if ready.exists():
                    break
                assert process.poll() is None, JobStatus.read(directory)
                await asyncio.sleep(0.025)
            assert ready.exists()
            process.kill()
            process.wait(timeout=10)
            assert JobStatus.read(directory).phase == Phase.INSTALLING
            with pytest.raises(InstallationBusyError):
                with installation_access(updating=True, environment=plan.installation.environment):
                    pytest.fail("Live pip lost its installation lease with the coordinator")
            with pytest.raises(InstallationBusyError, match="data is in use"):
                with data_access([Path(plan.profiles[0].receipt.data_dir) / "sessions"]):
                    pytest.fail("Live pip lost its data lease with the coordinator")
            with pytest.raises(InstallationBusyError):
                await start_job(directory, feed, repair=True)
        finally:
            release.touch()
            if process.poll() is None:
                process.wait(timeout=30)
        # Wait for the actual pip lease to disappear, not just the dead parent's PID.
        for _ in range(400):
            try:
                with installation_access(updating=True, environment=plan.installation.environment):
                    break
            except InstallationBusyError:
                await asyncio.sleep(0.025)
        else:
            pytest.fail("Pip did not release its lease")
        assert inspect_environment(plan.installation.python).app_version == "0.3.5"
        with pytest.raises(InstallationBusyError, match="reserved"):
            with data_access([Path(plan.profiles[0].receipt.data_dir) / "sessions"]):
                pytest.fail("Pip exit lifted the interrupted coordinator's data reservation")
        backup = (directory / "backup/data.zip").read_bytes()
        # A repair must retain the original data even if a migration or an
        # operator changed the live profile after the interrupted installation.
        Path(plan.profiles[0].receipt.config).write_text('{"fixture":"changed after install"}')
        pip_entry.write_text(original)
        retained = directory / "retained-original-backup"
        (directory / "backup").rename(retained)
        refused = await start_job(directory, feed, repair=True)
        assert await asyncio.to_thread(refused.wait, 30) != 0
        assert JobStatus.read(directory).mutation_started
        assert JobStatus.read(directory).attempt == 1
        assert "original pre-update backup is missing" in JobStatus.read(directory).message
        assert not (directory / "backup").exists()
        retained.rename(directory / "backup")
        (site / "nanobot/web/dist/index.html").unlink()
        with pytest.raises(ValueError, match="explicit repair"):
            await start_job(directory, feed)
        await wait_worker(await start_job(directory, feed, repair=True))
        assert (directory / "backup/data.zip").read_bytes() == backup
    assert JobStatus.read(directory).attempt == 2
    assert JobStatus.read(directory).phase == Phase.INSTALLED
    assert (site / "nanobot/web/dist/index.html").read_text() == "verified UI 0.3.5"
    with data_access([Path(plan.profiles[0].receipt.data_dir) / "sessions"]):
        pass  # Successful repair releases both the live lease and persistent fence.


@pytest.mark.asyncio
async def test_other_installation_writer_refuses_job_before_approval_or_backup(installation, tmp_path):
    plan, _, _, _, transport = installation
    async with ReleaseFeed(tmp_path / "feed", transport=transport) as feed:
        directory = await prepare_job(plan, feed)
        # This test process uses a different Python environment from the pip job.
        with data_access([Path(plan.profiles[0].receipt.workspace) / "sessions"]):
            with pytest.raises(InstallationBusyError, match="data is in use"):
                await start_job(directory, feed)
        assert not (directory / "approval.json").exists()
        assert not (directory / "backup").exists()
        assert JobStatus.read(directory).phase == Phase.READY
        assert inspect_environment(plan.installation.python).app_version == "0.3.4"
        await wait_worker(await start_job(directory, feed))
        assert JobStatus.read(directory).phase == Phase.INSTALLED


@pytest.mark.asyncio
async def test_cancel_interrupted_handoff_releases_data_without_installing(installation, tmp_path, monkeypatch):
    from nanobot import update_jobs

    plan, _, _, _, transport = installation
    ready = tmp_path / "coordinator-ready"
    original_popen = subprocess.Popen
    wrapper = """
import sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from _nanobot_update import update_worker
original_run = update_worker.subprocess.run
def run(command, *args, **kwargs):
    if '--pip' in command:
        Path(sys.argv[3]).write_text('ready')
        time.sleep(15)
        raise RuntimeError('Handoff test deadline exceeded')
    return original_run(command, *args, **kwargs)
update_worker.subprocess.run = run
update_worker.run_job(Path(sys.argv[2]))
"""

    def launch(command, **kwargs):
        if len(command) == 4 and str(command[2]).endswith("worker.pyz"):
            command = [command[0], "-I", "-c", wrapper, command[2], command[3], str(ready)]
        return original_popen(command, **kwargs)

    async with ReleaseFeed(tmp_path / "feed", transport=transport) as feed:
        directory = await prepare_job(plan, feed)
        monkeypatch.setattr(update_jobs.subprocess, "Popen", launch)
        process = await start_job(directory, feed)
        try:
            for _ in range(200):
                if ready.exists():
                    break
                assert process.poll() is None, JobStatus.read(directory)
                await asyncio.sleep(0.025)
            assert ready.exists()
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)
        status = JobStatus.read(directory)
        assert status.phase == Phase.BACKING_UP and not status.mutation_started
        assert inspect_environment(plan.installation.python).app_version == "0.3.4"
        store = Path(plan.profiles[0].receipt.data_dir) / "sessions"
        with pytest.raises(InstallationBusyError, match="reserved"):
            with data_access([store]):
                pytest.fail("A dead coordinator lost its maintenance reservation")
        cancel_job(directory)
        assert JobStatus.read(directory).phase == Phase.CANCELLED
        assert not pending_update_path(plan.installation.environment).exists()
        with data_access([store]):
            pass
        with pytest.raises(ValueError, match="already finished"):
            await start_job(directory, feed)


@pytest.mark.asyncio
async def test_prepared_worker_has_no_execution_authority(installation, tmp_path):
    plan, _, _, _, transport = installation
    async with ReleaseFeed(tmp_path / "feed", transport=transport) as feed:
        with pytest.raises(ValueError, match="confirmed extras"):
            await prepare_job(plan.model_copy(update={"extras": ["not-provided"]}), feed)
        directory = await prepare_job(plan, feed)
    spec = WorkerSpec.read(directory)
    result = subprocess.run([spec.baseline.base_python, "-I", str(directory / "worker.pyz"), str(directory)],
                            capture_output=True, timeout=20)
    assert result.returncode != 0
    assert inspect_environment(plan.installation.python).app_version == "0.3.4"
    assert not pending_update_path(plan.installation.environment).exists()
    assert JobStatus.read(directory).phase == Phase.FAILED
    assert not JobStatus.read(directory).mutation_started
    # A saved but expired approval cannot authorize a delayed launch.
    now = int(time.time())
    write_record(directory / "approval.json", {
        "plan_id": plan.id, "request_sha256": hashlib.sha256((directory / "request.json").read_bytes()).hexdigest(),
        "created_at": now - 1000, "expires_at": now - 100,
    })
    result = subprocess.run([spec.baseline.base_python, "-I", str(directory / "worker.pyz"), str(directory)],
                            capture_output=True, timeout=20)
    assert result.returncode != 0
    assert "expired" in JobStatus.read(directory).message
    cancel_job(directory)
    result = subprocess.run([spec.baseline.base_python, "-I", str(directory / "worker.pyz"), str(directory)],
                            capture_output=True, timeout=20)
    assert result.returncode != 0
    assert JobStatus.read(directory).phase == Phase.CANCELLED


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="Symlink creation requires separate Windows privileges")
async def test_real_worker_refuses_installation_when_scoped_backup_fails(installation, tmp_path):
    plan, _, _, _, transport = installation
    async with ReleaseFeed(tmp_path / "feed", transport=transport) as feed:
        directory = await prepare_job(plan, feed)
        external = tmp_path / "external-secret"
        external.write_text("outside the approved stores")
        link = Path(plan.profiles[0].receipt.data_dir) / "sessions/external"
        link.symlink_to(external)
        process = await start_job(directory, feed)
        assert await asyncio.to_thread(process.wait, 30) != 0
        status = JobStatus.read(directory)
        assert status.phase == Phase.FAILED and not status.mutation_started
        assert "symlink" in status.message
        assert inspect_environment(plan.installation.python).app_version == "0.3.4"
        assert not pending_update_path(plan.installation.environment).exists()
        assert not (directory / "backup").exists()
        link.unlink()
        await wait_worker(await start_job(directory, feed))
    assert JobStatus.read(directory).phase == Phase.INSTALLED


@pytest.mark.asyncio
@pytest.mark.parametrize("installation", [True], indirect=True)
async def test_broken_release_keeps_recovery_record_and_never_overwrites_an_external_upgrade(installation, tmp_path):
    plan, candidate, _, _, transport = installation
    async with ReleaseFeed(tmp_path / "feed", transport=transport) as feed:
        directory = await prepare_job(plan, feed)
        process = await start_job(directory, feed)
        assert await asyncio.to_thread(process.wait, 40) != 0
        state = JobStatus.read(directory)
        assert state.phase == Phase.FAILED and state.mutation_started
        assert "does not import" in state.message
        assert pending_update_path(plan.installation.environment).exists()
        spec = WorkerSpec.read(directory)
        # The status command is usable even though importing nanobot now fails.
        result = subprocess.run([spec.baseline.base_python, "-I", str(directory / "worker.pyz"), "--status", str(directory)],
                                capture_output=True, text=True, timeout=20)
        assert result.returncode == 0
        assert json.loads(result.stdout)["phase"] == "failed"
        with pytest.raises(ValueError, match="not cancellation"):
            cancel_job(directory)
        newer = wheel(candidate.parent, "nanobot-ai", "0.3.6", requires=["fixture-update-dep>=2.0.0"])
        subprocess.run([plan.installation.python, "-I", "-m", "pip", "install", "--disable-pip-version-check", str(newer)],
                       capture_output=True, check=True, timeout=30)
        with pytest.raises(ValueError, match="cannot overwrite"):
            await start_job(directory, feed, repair=True)
    assert inspect_environment(plan.installation.python).app_version == "0.3.6"


@pytest.mark.asyncio
async def test_cancelled_preparation_keeps_lease_until_package_manager_exits(installation, monkeypatch, tmp_path):
    from nanobot import update_jobs

    plan, _, _, _, transport = installation
    entered, finish = threading.Event(), threading.Event()
    original = update_jobs._prepare_wheels

    def held_preparation(*args):
        entered.set()
        assert finish.wait(20)
        return original(*args)

    monkeypatch.setattr(update_jobs, "_prepare_wheels", held_preparation)
    async with ReleaseFeed(tmp_path / "feed", transport=transport) as feed:
        preparing = asyncio.create_task(prepare_job(plan, feed))
        try:
            assert await asyncio.to_thread(entered.wait, 10)
            preparing.cancel()
            await asyncio.sleep(0)
            assert not preparing.done()
            with pytest.raises(BlockingIOError):
                await prepare_job(plan, feed)
        finally:
            finish.set()
            with pytest.raises(asyncio.CancelledError):
                await preparing
        directory = await prepare_job(plan, feed)
    assert JobStatus.read(directory).phase == Phase.READY
    assert inspect_environment(plan.installation.python).app_version == "0.3.4"
