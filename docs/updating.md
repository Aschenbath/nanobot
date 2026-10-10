# Inspecting an update

The update work on this branch is a draft. Published stable installations continue
to use their existing package manager. Preview publication and the complete
stop/install/restart handoff are not enabled by these commands.

## Review an exact release

```bash
nanobot update --plan
nanobot update --plan --channel preview
```

Planning checks the running installation and fresh interpreter metadata, selects
a compatible prebuilt wheel, and saves an exact plan outside the package directory.
The report names the installation owner, installed and running versions, target
version, download size and hash, and whether this would be an upgrade, downgrade,
restart or no-op. It does not install anything, stop a task or restart a gateway.
It can write a private plan and release-metadata cache; it is not a zero-write command.

Plans expire after fifteen minutes. Revalidation rejects a changed interpreter
environment, version or ownership receipt, an expired plan, a withdrawn candidate,
or changed artifact bytes. It checks the version already in the plan rather than
selecting a new latest version. These checks are prerequisites for execution, not
evidence that execution or recovery has completed.

Stable metadata comes from PyPI. The complete five-platform wheel set and sdist must
be present and not yanked; unsupported platforms do not silently build from source.
Preview metadata uses the protected `release-index` catalogue of `HKUDS/nanobot`.
Until that catalogue and its approved releases are published, preview planning fails
with a network/not-available result. It does not install a Git checkout as a fallback.

The persisted catalogue rejects older generations and changed data under the same
generation. A cached catalogue cannot turn an unsuccessful network check into a fresh
result. Downloads only follow approved HTTPS hosts, use the existing DNS-pinning
transport, and check size, checksum and embedded wheel identity before staging.
An existing staged artifact is verified, never silently overwritten.

`--plan` is separate from the draft's older `--check`, release update and `--dev`
paths. Those execution paths do not consume these plans yet. Do not describe them
as implementing exact-plan consent, durable jobs, coordinated runtime maintenance,
backup or post-restart verification until the execution handoff is integrated and
tested. A plan also does not authorize bypassing uv/pipx, Docker, Desktop or an
external supervisor; the installation owner remains responsible for its files.
