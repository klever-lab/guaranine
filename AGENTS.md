# Repository instructions

Read [README.md](README.md) first, then [SETUP.md](SETUP.md).

## Rules

- The controller (Talos control plane) must run in a cloud VM.
- Only update this file when the user instructs you to.
- Azure is temporary hosting; preserve an exit and restore path to another cloud VM.
- **Keep this repo KISS as fuck: stupid easy to use and boring to maintain. Complexity alone is enough to veto an idea.**
- Use prebuilt upstream components and standard interfaces; prefer deleting code, options and steps over adding abstractions.
- Add only minimum glue for current requirements; exclude speculative services, dependencies, Terraform roots, frameworks, plugins, fallback matrices and operator procedures.
- Every private inter-host connection must use Tailscale; support separate sites, direct/relayed paths and outages without LAN or low-latency assumptions.
- Terraform owns infrastructure, Talos configuration owns the OS and Kubernetes, and Kubernetes manifests own workloads.
- Keep README.md short, operational details solely in SETUP.md, the CLI in `scripts/guaranine.py`, and each Terraform root in one `main.tf`.
- Avoid helper-file proliferation, duplicate metadata and repeated operational facts; maintain each fact in its owning guide.
