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

## Runtime coordination in this draft

The `nanobot` CLI entrypoint now holds a shared installation lease while running. Profiles using
the same real Python environment and OS account share it, including when launched
with different `--home` values or through a symlink. The updater requires an exclusive
lease and refuses to change package files while these runtimes are still using them.
Startup refuses while that lease is held or an interrupted-install marker remains.
An OS lock, not a PID file, owns the lease; a process crash releases it without
deleting or replacing the lock file.

This is a prerequisite, not the complete maintenance handoff. In particular, the
older in-process WebUI updater cannot replace its own live gateway: it now reports
that the installation is in use instead of performing that unsafe operation. The
external worker, scoped backup and supervised restart still need to be connected
before enabling the finished in-app install flow. Updates do not kill active tasks.

Leases protect cooperating entrypoints in the same account. They do not discover
older runtimes without this mechanism, arbitrary Python imports, another OS user's
processes or unrecognized supervisors. Such installations must not be advertised
as eligible for unattended/coordinated updates merely because a lease is available.

## Exact offline executor (internal, not enabled in the UI)

`update_jobs.prepare_job` now prepares an immutable plain-venv transaction. It requires
explicitly confirmed extras (including an explicit empty list for the base package),
refuses unsupported installation owners, and compares fresh interpreter and installed
metadata with the approved plan. It asks pip for a dry-run report, fetches the exact
required dependency wheels with hashes, and stages those alongside the verified
nanobot wheel. There is no source build or global package-manager fallback. Pip's
configured package sources remain its trust policy during preparation; sensitive
resolver URLs stay in a private temporary directory and are not put in the journal.

Staging alone does not authorize execution. Starting a job revalidates the same release,
checks the current environment and writes a short-lived approval bound to the exact
request. The immutable worker zipapp uses only Python's standard library and lives
outside the files being replaced. Its coordinator runs with the base interpreter;
its dedicated pip CLI child holds the installation lease itself. Killing the coordinator
therefore cannot unlock a still-running installer. Installation is offline, from only
the staged wheels, with pip's destination/configuration overrides disabled.

The journal records the attempt, phase, whether mutation began, and a bounded message.
After pip exits, a fresh interpreter must import the target from the correct environment;
application files, including bundled WebUI/TUI assets, must match the approved wheel,
and `pip check` must pass. The resulting state is **installed**, not “gateway updated”:
no restarted gateway has been verified by this executor.

A stopped job can be cancelled only before mutation. Failed/interrupted mutation keeps
its startup fence and exact inputs. Explicit repair rechecks eligibility and cannot
overwrite a different version installed by another updater. Within a valid approval,
the copied worker can retry or show `--status <job-directory>` without importing a
broken nanobot installation. After expiry it refuses execution: a control-plane approval
must be renewed for the same reviewed job. There is not yet a finished public recovery
CLI for the case where that control plane is itself unavailable.

This internal executor is verified with actual pip processes and disposable application
wheels, including dependency upgrades, coordinator loss and repair. It is not full
product E2E or native verification on every OS. The caller still needs installation
receipts, supported process-owner discovery, task shutdown, scoped data backup,
restart/reconnection verification and authenticated UI/CLI integration. Until those
are connected, no public button or CLI flag starts this executor.
