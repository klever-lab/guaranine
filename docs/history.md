# Superseded approaches

These are historical references, not installation instructions or supported compatibility paths. [Architecture](architecture.md) describes the current system; [SETUP.md](../SETUP.md) contains current deployment steps.

| Earlier approach | Git reference and disposition |
| --- | --- |
| Incus cluster plan | `1ac4539` recorded the original laptop/Tailscale plan. Subsequent implementations changed the platform; it is not a second runtime maintained in this checkout. |
| Debian/K3s provisioning and published automatic-enrollment ISO | `740bb3e` added the laptop workflow; `27e3924` replaced Debian provisioning with standalone Talos. `bdbe6aa` later removed public ISO releases in favor of private builds. Old publication/enrollment commands are obsolete. |
| Talos machine management | `27e3924` introduced it; `bd47a56` removed its profiles, API-based CLI path and tests when migrating to Kairos/Hadron and K3s. The built-in Wi-Fi requirement was recorded in `5bc9ba0`. Talos recovery or installation commands do not apply to the current images. |
| Shared remote-development enrollment | `4671f93` removed Clusterfork integration and established direct work on `main`. The repository no longer requires that controller or its enrollment workflow. |
| Extra Azure image resource | `3993bef` replaced the managed-image indirection with a private source disk copied directly into the controller boot disk. Earlier image-ID handoff instructions are obsolete. |

Use `git show <commit>` for the original change. Recovering old code from history does not make it a supported fallback. Keep any future experiment's outcome here or in its own focused topic rather than appending abandoned instructions to SETUP.md.
