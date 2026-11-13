# Repository instructions

Read [README.md](README.md) first, then [SETUP.md](SETUP.md).

## Rules

- Work directly on `main` unless the user explicitly asks for another branch.
- The controller (K3s control plane) must run in a cloud VM.
- Only update this file when the user instructs you to.
- Azure is temporary hosting; preserve an exit and restore path to another cloud VM.
- **Keep this repo KISS as fuck: stupid easy to use and boring to maintain. Complexity alone is enough to veto an idea.**
- Use prebuilt upstream components and standard interfaces; prefer deleting code, options and steps over adding abstractions.
- Accept breaking changes to keep code simple. Replace superseded implementations and remove their code and options; avoid compatibility layers and legacy fallbacks.
- Add only minimum glue for current requirements; exclude speculative services, dependencies, Terraform roots, frameworks, plugins, fallback matrices and operator procedures.
- Every private inter-host connection must use Tailscale; support separate sites, direct/relayed paths and outages without LAN or low-latency assumptions.
- Wi-Fi client isolation is desirable but may be absent; never rely on it. Host firewalls must block direct LAN communication to and from nodes and their workloads, with only minimal exceptions for network setup, DNS and encrypted Tailscale transport.
- Laptop workers must support built-in Wi-Fi from initial boot through installation and normal operation, without requiring Ethernet, adapters or external bridges. Verify OS, installer and driver support for the target laptops before recommending a platform or building installation media.
- Terraform owns infrastructure, Kairos configuration owns the OS and K3s setup, and Kubernetes manifests own workloads.
- Keep README.md short, setup instructions in SETUP.md and other documentation in docs/, the CLI in `scripts/guaranine.py`, and each Terraform root in one `main.tf`.
- Avoid helper-file proliferation, duplicate metadata and repeated operational facts; maintain each fact in its owning guide.

## Documentation (read/write)

- **Read first:** For how this repo works, start at `docs/README.md`, then
  open only the topic files you need. Do not re-derive behavior from
  filenames alone when a doc covers it.
- **Before non-trivial changes:** Check the relevant doc so you match
  existing patterns.
- **After behavior changes:** Update the **one** topic file under `docs/`
  that owns that fact. Do not copy the same detail into AGENTS.md,
  README.md, or multiple docs.
- **Regenerate vs patch:** `docs/README.md` records the commit it was
  generated from; `git diff <sha>..HEAD --stat` shows what changed since.
  Prefer the `generate_docs` skill when docs are broadly stale or many
  topics shifted; for small, targeted edits, patch the single topic file
  (and `docs/README.md` if you add/remove a topic).
- `CLAUDE.md` is a symlink to this file.
