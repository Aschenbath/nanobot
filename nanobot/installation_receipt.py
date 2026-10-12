"""The official installer's explicit package selection, not inferred dependencies.

Only newly created, dedicated environments get this receipt. An existing uv,
pipx, source checkout or unrecorded venv is never adopted implicitly.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

from nanobot.utils.atomic_file import atomic_write_lines

RECEIPT_NAME = "nanobot-install.json"


def managed_extras(environment: str, expected_sha256: str) -> list[str]:
    path = Path(environment) / RECEIPT_NAME
    if path.is_symlink() or path.stat().st_size > 4096:
        raise ValueError("Invalid installer receipt")
    payload = path.read_bytes()
    if hashlib.sha256(payload).hexdigest() != expected_sha256:
        raise ValueError("Installer receipt changed")
    record = json.loads(payload)
    if record != {"schema_version": 1, "owner": "nanobot-installer",
                  "environment": str(Path(environment).resolve()), "extras": []}:
        raise ValueError("This installation selection needs its owner's update workflow")
    return []


def register() -> None:
    """Called by the installer only after a fresh base-package installation."""
    from importlib.metadata import distribution

    import nanobot

    prefix = Path(sys.prefix).resolve()
    direct_url = json.loads(distribution("nanobot-ai").read_text("direct_url.json") or "{}")
    if (sys.prefix == sys.base_prefix or not (prefix / "pyvenv.cfg").is_file()
            or any((prefix / name).exists() for name in ("uv-receipt.toml", "pipx_metadata.json"))
            or not Path(nanobot.__file__).resolve().is_relative_to(prefix)
            or direct_url.get("dir_info") is not None):
        raise ValueError("Only a dedicated package installation can be registered")
    path = prefix / RECEIPT_NAME
    if path.exists():
        raise ValueError("An existing installation receipt cannot be replaced")
    atomic_write_lines(path, [json.dumps({"schema_version": 1, "owner": "nanobot-installer",
                                         "environment": str(prefix), "extras": []})])
    path.chmod(0o600)


if __name__ == "__main__":
    if sys.argv[1:] != ["--register-fresh"]:
        raise SystemExit("Use --register-fresh only for a newly created installer environment")
    register()
