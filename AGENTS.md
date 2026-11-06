# Repository instructions

Guaranine runs upstream Kubernetes on Talos Linux. Use the native Kubernetes API, `kubectl` and standard manifests for workload placement and lifecycle; use `talosctl` and versioned Talos configuration for machines. Do not reintroduce a custom workload API, LXC, K3s, a general-purpose Debian/Ubuntu worker OS, Ansible host provisioning, cloud-init, or SSH administration.

All laptops are in separate locations. Every private inter-host connection must use Tailscale. They do not share a LAN or have guaranteed sub-10 ms latency. Direct Tailscale connections, relays and temporary disconnections are expected. KubeSpan and SideroLink are not substitutes for this requirement.

**Server authorization:** Do not add any extra server without the user's explicit authorization. The current architecture is one planned Talos control-plane VM and laptop workers. A separate enrollment broker, authentication server, custom coordinator, Omni installation or other management service requires explicit authorization, even when hosted on the control-plane VM or inside the cluster. A design discussion, existing Terraform or a request for automatic enrollment does not grant that authorization.

Read [README.md](README.md) first, then the owning guide:

- [Talos configuration and operations](talos/README.md): pinned tools, private cluster secrets, machine generation, Tailscale/CNI policy, control-plane bootstrap, maintenance and etcd recovery.
- [Laptop images](images/laptop/README.md): public boot media, local private ISO/VHD generation, release qualification and enrollment limitations.
- [Azure infrastructure](infra/azure/README.md): student-credit preflight, private image upload, Entra Blob state, one control-plane VM, retained data disk and recovery.

The control plane uses local etcd on its retained disk. Laptop workers never join an etcd quorum. One control plane is a deliberate availability tradeoff. Persistent workload storage, KubeVirt, public domains, external secret management and full monitoring remain later platform work. Preserve automatic scheduling and drain/eviction; never choose replacement workload hosts by hand. WAN disconnection is not proof of fencing.

Terraform owns Azure resources and state; Talos machine configuration owns the OS and Kubernetes; Kubernetes manifests own workloads. Nothing has been provisioned. Live provisioning still requires explicit scope and subscription credit/quota/cost preflight; CI must not create Azure resources. Keep the student spending limit intact and retain an exit/restore path.

The public repository, Image Factory schematics, ISOs and releases must contain no enrollment credentials, cluster secrets or machine identities. Private profiles/images belong outside the checkout on protected operator storage and must never be published or uploaded as CI artifacts. CI uses disposable credentials only. The desired 24-hour, single-use, YubiKey-authorized unattended enrollment policy is not fully implemented by stock Talos; do not weaken it or claim that a Tailscale key alone provides hardware-bound, single-use cluster authorization.

Run validation using Python 3.12+ and pinned `requirements-dev.txt`: `bash scripts/validate.sh`. It validates both Talos roles with checksum-pinned `talosctl`, exercises security/publication boundaries, lints workflows and shell, and runs credential-free Terraform schema/mock/lifecycle checks for all Azure roots. Actual BIOS/UEFI boot and private-format image builds run separately in the image workflow. No Node.js/npm or repository-specific Symphony worker workflow is used. Remote development remains bound to the existing Guaranine Linear project and shared Clusterfork controller.

Keep each operational fact in its owning guide. Update relevant validation with material executable changes. Small authorized changes can be pushed directly to main when repository rules permit; a PR is not required for release admission.
