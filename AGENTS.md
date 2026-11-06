# Repository instructions

## KISS is a hard requirement

**Keep this repo KISS as fuck: stupid easy to use and boring to maintain. Complexity alone is enough to veto an idea.**

- Use prebuilt upstream components and their standard interfaces. Prefer deleting code, options and steps to adding another abstraction.
- Keep one supported setup path with sensible defaults. A laptop should need one image-preparation command, then installation and boot. The user must not assemble profiles, credentials and build steps by hand for every laptop.
- Generate private node installers locally. Do not reintroduce public ISO builds/releases or publishing machinery. CI may build disposable private fixtures and discard them; it must not upload machine images.
- Do not build a framework, custom controller/API, enrollment service, plugin system, fallback matrix or speculative integration. Add only the minimum glue needed for a current requirement.
- Do not add a service, dependency, Terraform root or operator procedure merely because it might be useful later. A complicated proposal is rejected until there is a simpler design.
- Keep the main README short and actionable. Put each necessary operational detail in one owning guide; avoid duplicated explanations and alternative workflows.
- Simplicity must preserve credentials, backups, Tailscale-only transport and honest limits. Never call an unfinished security guarantee complete or hide essential manual work behind an "automatic" claim.

## Platform boundaries

Guaranine runs upstream Kubernetes on Talos Linux. Use the native Kubernetes API, `kubectl` and standard manifests for workload placement and lifecycle; use `talosctl` and versioned Talos configuration for machines. Do not reintroduce a custom workload API, LXC, K3s, a general-purpose Debian/Ubuntu worker OS, Ansible host provisioning, cloud-init, or SSH administration.

All laptops are in separate locations. Every private inter-host connection must use Tailscale. They do not share a LAN or have guaranteed sub-10 ms latency. Direct Tailscale connections, relays and temporary disconnections are expected. KubeSpan and SideroLink are not substitutes for this requirement.

**Server authorization:** Do not add any extra server without the user's explicit authorization. The current architecture is one planned Talos control-plane VM and laptop workers. A separate enrollment broker, authentication server, custom coordinator, Omni installation or other management service requires explicit authorization, even when hosted on the control-plane VM or inside the cluster. A design discussion, existing Terraform or a request for automatic enrollment does not grant that authorization.

Read [README.md](README.md) first, then the owning guide:

- [Talos configuration and operations](talos/README.md): private ISO/VHD generation, enrollment limitations, Tailscale/CNI policy, control-plane bootstrap, maintenance and etcd recovery.
- [Azure infrastructure](infra/azure/README.md): student-credit preflight, private image upload, protected local Terraform state, one control-plane VM, retained data disk and recovery.

The control plane uses local etcd on its retained disk. Laptop workers never join an etcd quorum. One control plane is a deliberate availability tradeoff. Persistent workload storage, KubeVirt, public domains, external secret management and full monitoring remain later platform work. Preserve automatic scheduling and drain/eviction; never choose replacement workload hosts by hand. WAN disconnection is not proof of fencing.

Terraform owns Azure resources; Talos machine configuration owns the OS and Kubernetes; Kubernetes manifests own workloads. Use native local Terraform state on encrypted operator storage, outside git, with off-machine backups and one operator at a time. Do not provision a state service or restore the unused backend bootstrap/migration machinery. Nothing has been provisioned. Live provisioning still requires explicit scope and subscription credit/quota/cost preflight; CI must not create Azure resources. Keep the student spending limit intact and retain an exit/restore path.

Keep enrollment credentials, cluster secrets and machine identities out of the public repository and Image Factory schematics. Private profiles/images belong outside the checkout on protected operator storage and must never be published or uploaded as CI artifacts. CI uses disposable credentials only. The desired 24-hour, single-use, YubiKey-authorized unattended enrollment policy is not fully implemented by stock Talos; do not weaken it or claim that a Tailscale key alone provides hardware-bound, single-use cluster authorization.

Run validation using Python 3.12+ and pinned `requirements-dev.txt`: `bash scripts/validate.sh`. It validates both Talos roles with checksum-pinned `talosctl`, exercises the one-command machine preparation and credential boundaries, lints workflows and shell, and runs credential-free Terraform schema/mock/lifecycle checks. The private-image workflow separately builds both formats with disposable credentials and discards the outputs. These are build tests, not proof of real enrollment. No Node.js/npm or repository-specific Symphony worker workflow is used. Remote development remains bound to the existing Guaranine Linear project and shared Clusterfork controller.

Keep each operational fact in its owning guide. Update relevant validation with material executable changes. Small authorized changes can be pushed directly to main when repository rules permit; a PR is not required.
