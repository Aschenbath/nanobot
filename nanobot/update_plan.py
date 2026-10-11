"""Installation-bound, expiring release plans shared by update entrypoints.

Planning never invokes an installer or changes the running gateway. The approved
record is immutable; an executor must revalidate it before changing any package files.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import platform
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
from nanobot.update_manager import ManagerPolicy, read_manager_policy
from nanobot.update_process import inspect_python_environment
from nanobot.update_profiles import (
    ObservedProfile,
    ProfileReceipt,
    inspect_profiles,
    validate_profiles,
)
from nanobot.utils.helpers import atomic_write_lines

Owner = Literal["venv", "uv", "pipx", "source", "container", "system"]


def _managed_background(profile: ProfileReceipt, environment: str) -> bool:
    return (profile.environment == environment and profile.launch_mode == "background"
            and profile.launch is not None and profile.launch.lifetime == "explicit")

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
    manager: ManagerPolicy | None = None

    @model_validator(mode="after")
    def matching_manager(self) -> InstallationSnapshot:
        if self.manager is not None and self.manager.manager != self.owner:
            raise ValueError("Package-manager settings must belong to this installation owner")
        return self

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
    probe = inspect_python_environment(sys.executable, str(Path(sys.prefix).resolve()), _PROBE, timeout=10)
    probe.check_returncode()
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
    manager = None
    if owner == "uv" or owner == "pipx":
        receipt = "uv-receipt.toml" if owner == "uv" else "pipx_metadata.json"
        manager = read_manager_policy(Path(fresh.environment), owner, fresh.records[receipt])
    return InstallationSnapshot(**fresh.model_dump(), running_version=nanobot.__version__,
                                running_package=str(Path(nanobot.__file__).resolve()), owner=owner, manager=manager)


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
        if self.installation.manager is not None and self.extras != self.installation.manager.extras:
            raise ValueError("An update must preserve the installation owner's recorded extras")
        profiles = {item.receipt.id: item.receipt for item in self.profiles}
        if len(set(self.stop_profiles)) != len(self.stop_profiles):
            raise ValueError("Shutdown participants must be unique")
        for participant in self.stop_profiles:
            profile = profiles.get(participant)
            if (profile is None or not _managed_background(profile, self.installation.environment)
                    or profile.stop_protocol != 1
                    or not any(item.receipt.id == participant and item.state == "running" for item in self.profiles)):
                raise ValueError("Shutdown requires a reviewed running gateway with cooperative stop support")
        if len(set(self.restart_profiles)) != len(self.restart_profiles):
            raise ValueError("Restart participants must be unique")
        for participant in self.restart_profiles:
            profile = profiles.get(participant)
            if (profile is None or not _managed_background(profile, self.installation.environment)
                    or not profile.port):
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

    @property
    def owner_restriction(self) -> str | None:
        manager = self.installation.manager
        return manager.restriction(self.target.version) if manager is not None else None

    def validate_for(self, installation: InstallationSnapshot, *, now: int | None = None) -> None:
        checked_at = int(time.time()) if now is None else now
        if checked_at < self.created_at or checked_at >= self.expires_at:
            raise ValueError("Update plan expired; check again before confirming")
        if self.installation.fingerprint != installation.fingerprint:
            raise ValueError("Installation changed since this plan; check again before confirming")


class UpdatePlanNotFoundError(FileNotFoundError):
    """The requested saved plan is absent, not an installation-probe failure."""


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
        try:
            if path.is_symlink() or path.stat().st_size > 1024 * 1024:
                raise ValueError("Invalid saved update plan")
            encoded = path.read_bytes()
        except FileNotFoundError as exc:
            raise UpdatePlanNotFoundError("The saved update plan is no longer available") from exc
        plan = UpdatePlan.model_validate_json(encoded)
        if plan.id != plan_id:
            raise ValueError("Saved update plan identity differs")
        return plan

    async def prepare(self, installation: InstallationSnapshot, channel: Channel, feed: ReleaseFeed,
                      *, now: int | None = None, restart_running: bool = False,
                      extras: list[str] | None = None) -> UpdatePlan:
        target = await feed.latest(channel)
        created_at = int(time.time()) if now is None else now
        profiles = inspect_profiles(installation.environment)
        if installation.manager is not None:
            if extras is not None and extras != installation.manager.extras:
                raise ValueError("The plan cannot change the installation owner's recorded extras")
            extras = installation.manager.extras
        participants: list[str] = []
        if restart_running:
            for profile in profiles:
                if profile.state == "unconfirmed":
                    raise ValueError("Confirm every gateway's process owner before planning a restart")
                if profile.state == "running":
                    if (not _managed_background(profile.receipt, installation.environment) or not profile.receipt.port
                            or profile.receipt.stop_protocol != 1):
                        raise ValueError("A running gateway needs its owner's external restart workflow")
                    participants.append(profile.receipt.id)
        plan = UpdatePlan(id=str(uuid4()), installation=installation, channel=channel, target=target, extras=extras, profiles=profiles,
                          stop_profiles=participants, restart_profiles=participants,
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


class UpdatePlanRequest(ReleaseRecord):
    """An intended change to review, never installation/shutdown permission."""

    channel: Channel = "stable"
    restart_running: bool = False
    extras: list[str] | None = None

    @field_validator("extras")
    @classmethod
    def explicit_extras(cls, value: list[str] | None) -> list[str] | None:
        return UpdatePlan.explicit_extras(value)


class ProfileReview(ReleaseRecord):
    id: str
    workspace: str
    reviewed_state: Literal["running", "stopped", "unconfirmed"]
    stop: bool
    restart: bool


class UpdatePlanReview(ReleaseRecord):
    """Client-safe facts, not execution approval or fresh release eligibility.

    Validity describes the locally observed installation and review lifetime.
    Reading a review never re-resolves its target or checks the release feed.
    """

    id: str
    installation_id: str
    host: str
    owner: Owner
    running_version: str
    installed_version: str
    target_version: str
    channel: Channel
    action: Literal["upgrade", "downgrade", "restart", "none"]
    created_at: int
    expires_at: int
    validity: Literal["current", "expired", "installation_changed", "profiles_changed"]
    artifact_size: int
    extras: list[str] | None
    owner_restriction: str | None
    profiles: list[ProfileReview]


def review_plan(plan: UpdatePlan, installation: InstallationSnapshot, *, now: int | None = None,
                profiles: list[ObservedProfile] | None = None) -> UpdatePlanReview:
    checked_at = int(time.time()) if now is None else now
    validity: Literal["current", "expired", "installation_changed", "profiles_changed"] = "current"
    if plan.installation.fingerprint != installation.fingerprint:
        validity = "installation_changed"
    elif not plan.created_at <= checked_at < plan.expires_at:
        validity = "expired"
    elif profiles is not None and profiles != plan.profiles:
        validity = "profiles_changed"
    return UpdatePlanReview(
        id=plan.id, installation_id=plan.installation.installation_id, host=platform.node(),
        owner=plan.installation.owner, running_version=installation.running_version,
        installed_version=installation.installed_version, target_version=plan.target.version,
        channel=plan.channel, action=plan.action, created_at=plan.created_at, expires_at=plan.expires_at,
        validity=validity, artifact_size=plan.target.artifact.size, extras=plan.extras,
        owner_restriction=plan.owner_restriction, profiles=[ProfileReview(
            id=item.receipt.id, workspace=item.receipt.workspace, reviewed_state=item.state,
            stop=item.receipt.id in plan.stop_profiles, restart=item.receipt.id in plan.restart_profiles,
        ) for item in plan.profiles],
    )


def _inspect_with_lease() -> InstallationSnapshot:
    # Keep the complete snapshot (including owner receipt parsing) protected
    # after request cancellation. The native child also holds its own lease,
    # so loss of this process cannot leave site/import/exit cleanup unprotected.
    with installation_access():
        return inspect_installation()


async def preview_release_update(channel: Channel, *, restart_running: bool = False,
                                 extras: list[str] | None = None) -> UpdatePlan:
    """Build a read-only installation plan for CLI and future gateway handoff."""
    with installation_access():
        installation = await asyncio.to_thread(_inspect_with_lease)
        directory = installation_state_dir(installation.environment)
        async with ReleaseFeed(directory / "releases") as feed:
            plan = await UpdatePlans(directory / "plans").prepare(
                installation, channel, feed, restart_running=restart_running, extras=extras,
            )
        # External package managers do not participate in our lease. Discovery
        # can be slow: do not return a freshly valid review of a replaced install.
        plan.validate_for(await asyncio.to_thread(_inspect_with_lease))
        return plan


async def read_release_plan(plan_id: str) -> UpdatePlanReview:
    """Recover the exact review after refresh; do not query a newer release."""
    UpdatePlan.canonical_id(plan_id)
    with installation_access():
        installation = await asyncio.to_thread(_inspect_with_lease)
        plan = UpdatePlans(installation_state_dir(installation.environment) / "plans").load(plan_id)
        return review_plan(plan, installation, profiles=inspect_profiles(installation.environment))


def describe_plan(plan: UpdatePlan) -> str:
    """No promised install command until the owner/lifecycle executor is available."""
    return json.dumps({
        **plan.model_dump(mode="json"), "action": plan.action,
        "installation_id": plan.installation.installation_id,
        "owner_restriction": plan.owner_restriction,
        "profile_notice": "Known profiles only; this does not prove all runtimes or supervisors are accounted for.",
        "notice": "Plan only. No packages were changed or processes restarted.",
    }, indent=2)
