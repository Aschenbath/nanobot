"""Installation-bound, expiring release plans shared by update entrypoints.

Planning never invokes an installer or changes the running gateway. The approved
record is immutable; an executor must revalidate it before changing any package files.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from filelock import FileLock
from packaging.version import Version
from pydantic import Field, field_validator, model_validator

import nanobot
from nanobot.installation_lock import installation_access, installation_id, installation_state_dir
from nanobot.release_feed import ReleaseFeed, ReleaseTarget
from nanobot.releases import Channel, ReleaseRecord, Sha256
from nanobot.update_profiles import ObservedProfile, inspect_profiles, validate_profiles
from nanobot.utils.helpers import atomic_write_lines

Owner = Literal["venv", "uv", "pipx", "source", "container", "system"]

# Read fresh metadata using this installation's Python, not an executable from PATH
# or this long-lived gateway's cached importlib.metadata state. No config is loaded.
_PROBE = """
import hashlib, json, sys, nanobot
from pathlib import Path
from importlib.metadata import distribution
d = distribution('nanobot-ai')
prefix = Path(sys.prefix).resolve()
records = {}
for name in ('pyvenv.cfg', 'uv-receipt.toml', 'pipx_metadata.json'):
    path = prefix / name
    if path.is_file():
        records[name] = hashlib.sha256(path.read_bytes()).hexdigest()
records['RECORD'] = hashlib.sha256((d.read_text('RECORD') or '').encode()).hexdigest()
records['direct_url.json'] = hashlib.sha256((d.read_text('direct_url.json') or '').encode()).hexdigest()
print(json.dumps(dict(environment=str(prefix), python=str(Path(sys.executable).absolute()),
    installed_version=d.version, package_path=str(Path(nanobot.__file__).resolve()),
    records=records)))
"""


class _Probe(ReleaseRecord):
    environment: str
    python: str
    installed_version: str
    package_path: str
    records: dict[str, Sha256]


class InstallationSnapshot(_Probe):
    running_version: str
    running_package: str
    owner: Owner

    @field_validator("installed_version", "running_version")
    @classmethod
    def valid_version(cls, value: str) -> str:
        Version(value)
        return value

    @property
    def installation_id(self) -> str:
        return installation_id(self.environment)

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()


def inspect_installation() -> InstallationSnapshot:
    probe = subprocess.run([sys.executable, "-I", "-c", _PROBE], check=True,
                           capture_output=True, text=True, timeout=10)
    fresh = _Probe.model_validate_json(probe.stdout)
    if Path(fresh.environment) != Path(sys.prefix).resolve():
        raise RuntimeError("The update interpreter belongs to a different installation")
    root = Path(nanobot.__file__).resolve().parent.parent
    owner: Owner = "venv"
    if Path("/.dockerenv").exists() or os.environ.get("RENDER") == "true":
        owner = "container"
    elif (root / "pyproject.toml").is_file() and (root / "tui/package.json").is_file():
        owner = "source"
    elif "uv-receipt.toml" in fresh.records:
        owner = "uv"
    elif "pipx_metadata.json" in fresh.records:
        owner = "pipx"
    elif sys.prefix == sys.base_prefix:
        owner = "system"
    return InstallationSnapshot(**fresh.model_dump(), running_version=nanobot.__version__,
                                running_package=str(Path(nanobot.__file__).resolve()), owner=owner)


class UpdatePlan(ReleaseRecord):
    schema_version: Literal[1] = 1
    id: str
    installation: InstallationSnapshot
    channel: Channel
    target: ReleaseTarget
    extras: list[str] | None = None
    profiles: list[ObservedProfile] = Field(default_factory=list, max_length=1024)
    # Explicit consent to stop exact running instances, separate from restart.
    stop_profiles: list[str] = Field(default_factory=list, max_length=1024)
    # Inventory alone is not authority to start a previously stopped instance.
    restart_profiles: list[str] = Field(default_factory=list, max_length=1024)
    created_at: int = Field(ge=0)
    expires_at: int = Field(gt=0)

    @field_validator("id")
    @classmethod
    def canonical_id(cls, value: str) -> str:
        if str(UUID(value)) != value:
            raise ValueError("Invalid update plan identifier")
        return value

    @field_validator("extras")
    @classmethod
    def explicit_extras(cls, value: list[str] | None) -> list[str] | None:
        import re

        if value is not None and (len(value) > 50 or len(value) != len(set(value)) or any(
            not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", extra) for extra in value
        )):
            raise ValueError("Extras must be explicit, unique normalized names")
        return value

    @model_validator(mode="after")
    def valid_plan(self) -> UpdatePlan:
        if not self.created_at < self.expires_at <= self.created_at + 15 * 60:
            raise ValueError("Update plan must expire within fifteen minutes")
        if self.channel == "stable" and Version(self.target.version).is_prerelease:
            raise ValueError("A stable plan cannot install a preview")
        profiles = {item.receipt.id: item.receipt for item in self.profiles}
        if len(set(self.stop_profiles)) != len(self.stop_profiles):
            raise ValueError("Shutdown participants must be unique")
        for participant in self.stop_profiles:
            profile = profiles.get(participant)
            if (profile is None or profile.environment != self.installation.environment
                    or profile.launch_mode != "background" or profile.launch is None
                    or profile.launch.lifetime != "explicit" or profile.stop_protocol != 1
                    or not any(item.receipt.id == participant and item.state == "running" for item in self.profiles)):
                raise ValueError("Shutdown requires a reviewed running gateway with cooperative stop support")
        if len(set(self.restart_profiles)) != len(self.restart_profiles):
            raise ValueError("Restart participants must be unique")
        for participant in self.restart_profiles:
            profile = profiles.get(participant)
            if (profile is None or profile.environment != self.installation.environment
                    or profile.launch_mode != "background" or profile.launch is None
                    or profile.launch.lifetime != "explicit" or not profile.port):
                raise ValueError("Restart requires a reviewed, explicitly managed background gateway")
        return self

    @property
    def action(self) -> Literal["upgrade", "downgrade", "restart", "none"]:
        target = Version(self.target.version)
        installed = Version(self.installation.installed_version)
        running = Version(self.installation.running_version)
        if target < max(installed, running):
            return "downgrade"
        if target > installed:
            return "upgrade"
        if running != installed:
            return "restart"
        return "none"

    def validate_for(self, installation: InstallationSnapshot, *, now: int | None = None) -> None:
        checked_at = int(time.time()) if now is None else now
        if checked_at < self.created_at or checked_at >= self.expires_at:
            raise ValueError("Update plan expired; check again before confirming")
        if self.installation.fingerprint != installation.fingerprint:
            raise ValueError("Installation changed since this plan; check again before confirming")


class UpdatePlans:
    """Persist plans outside replaceable package files, with a small caller Interface."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def save(self, plan: UpdatePlan) -> UpdatePlan:
        encoded = plan.model_dump_json()
        if len(encoded.encode("utf-8")) + 1 > 1024 * 1024:
            raise ValueError("Update plan exceeds its size limit")
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.directory / f"{plan.id}.json"
        with FileLock(str(self.directory / "plans.lock"), timeout=5):
            if path.exists():
                if self.load(plan.id) != plan:
                    raise ValueError("An approved update plan cannot be replaced")
                return plan
            atomic_write_lines(path, [encoded])
            path.chmod(0o600)
        return plan

    def load(self, plan_id: str) -> UpdatePlan:
        UpdatePlan.canonical_id(plan_id)
        path = self.directory / f"{plan_id}.json"
        if path.is_symlink() or path.stat().st_size > 1024 * 1024:
            raise ValueError("Invalid saved update plan")
        plan = UpdatePlan.model_validate_json(path.read_bytes())
        if plan.id != plan_id:
            raise ValueError("Saved update plan identity differs")
        return plan

    async def prepare(self, installation: InstallationSnapshot, channel: Channel, feed: ReleaseFeed,
                      *, now: int | None = None) -> UpdatePlan:
        target = await feed.latest(channel)
        created_at = int(time.time()) if now is None else now
        profiles = inspect_profiles(installation.environment)
        plan = UpdatePlan(id=str(uuid4()), installation=installation, channel=channel, target=target, profiles=profiles,
                          created_at=created_at, expires_at=created_at + 15 * 60)
        return self.save(plan)

    async def revalidate(self, plan_id: str, installation: InstallationSnapshot, feed: ReleaseFeed,
                         *, now: int | None = None) -> UpdatePlan:
        plan = self.load(plan_id)
        plan.validate_for(installation, now=now)
        validate_profiles(plan.profiles, installation.environment)
        await feed.revalidate(plan.target)
        # Release discovery can yield to a profile restart or a long network wait.
        plan.validate_for(installation, now=now)
        validate_profiles(plan.profiles, installation.environment)
        return plan


async def preview_release_update(channel: Channel) -> UpdatePlan:
    """Build a read-only installation plan for CLI and future gateway handoff."""
    with installation_access():
        installation = inspect_installation()
        directory = installation_state_dir(installation.environment)
        async with ReleaseFeed(directory / "releases") as feed:
            return await UpdatePlans(directory / "plans").prepare(installation, channel, feed)


def describe_plan(plan: UpdatePlan) -> str:
    """No promised install command until the owner/lifecycle executor is available."""
    return json.dumps({
        **plan.model_dump(mode="json"), "action": plan.action,
        "installation_id": plan.installation.installation_id,
        "profile_notice": "Known profiles only; this does not prove all runtimes or supervisors are accounted for.",
        "notice": "Plan only. No packages were changed or processes restarted.",
    }, indent=2)
