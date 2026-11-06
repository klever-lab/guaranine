# guaranine

Talos Linux and upstream Kubernetes for laptop workers in different locations. One stable control-plane VM uses local etcd; laptops are workers. Every private inter-host connection uses Tailscale. No shared LAN or sub-10 ms latency is assumed.

Talos replaces the former Debian/K3s/Ansible installation path. Native Kubernetes controllers schedule and manage workloads; Talos owns machine configuration and OS/Kubernetes upgrades. There is no custom fleet API, extra enrollment server, Omni deployment, SideroLink or KubeSpan.

## Current implementation

- Version-pinned Talos, Kubernetes, Tailscale extension, imager and checksum-verified `talosctl`.
- Public Talos ISO generation/release with no machine configuration, keys or node identities. BIOS/UEFI guests must boot, expose the maintenance API, and preserve an unconfigured disk before publication.
- Private machine configuration and local ISO/Azure VHD generation using upstream Talos tooling. Both node roles are validated by the actual pinned Talos schema. Public Image Factory never receives private configuration.
- Tailscale node addressing, Flannel on `tailscale0`, tunnel-aware MTU, restricted host ingress and a dedicated retained etcd volume. Laptop workers do not run etcd. Cluster discovery services and automatic alternate API endpoint selection are disabled.
- Terraform for Entra-authenticated Blob state, private image upload/publication, isolated Azure networking, the one planned Talos control-plane VM and retained data disk. No Azure resources or live cluster have been deployed.
- Existing Guaranine Linear/Symphony integration: Todo issues use shared autonomous implementation, validation and merge. This migration adds no development controller or worker workflow.

Private images support unattended Talos installation after an operator supplies configuration and authorizes the target disk. The stricter requested 24-hour, single-use, YubiKey-authorized enrollment policy is still incomplete; see the [image guide](images/laptop/README.md#enrollment-security-status). A Tailscale key alone is not a hardware-bound, single-use Talos authorization. Hosted tailnet operation and physical laptop hardware remain unqualified.

## Guides

| Topic | Guide |
| --- | --- |
| Cluster secrets, node profiles, bootstrap, networking, maintenance, backups | [Talos operations](talos/README.md) |
| Public ISO, local private images, release evidence, enrollment limitations | [Laptop images](images/laptop/README.md) |
| Student credits, remote state, private Azure images, VM and disk recovery | [Azure infrastructure](infra/azure/README.md) |

## Platform scope

| Need | Implementation |
| --- | --- |
| Container placement and lifecycle | Kubernetes Deployments, resource requests/limits, scheduler and controllers |
| Host configuration and updates | Talos machine configuration and `talosctl`; no SSH or package manager |
| Add/remove workers | Private node configuration; Kubernetes cordon/drain and replacement readiness |
| Private inter-host traffic | Tailscale system extension; Flannel node traffic uses tailnet addresses |
| API keys and secrets | Kubernetes access controls; external secret management remains later work |
| Public domains/TLS | Later ingress/tunnel/DNS integration |
| Monitoring | Later metrics, dashboards and alerting deployment |
| Movable persistent workloads | Later shared/portable storage and fencing; local disks are not portable storage |
| QEMU/KVM VMs | Later KubeVirt integration and hardware qualification |
| LXC/custom workload API | Excluded |

A single control plane is an availability tradeoff: existing workloads may continue through a control-plane outage, while scheduling and recovery wait. Draining does not guarantee eviction without spare capacity and satisfiable disruption/storage constraints. Network loss does not prove a node is fenced. Container recreation and VM live migration have different guarantees.

## Validation

Use Python 3.12+ and the pinned dependencies. For local work, use `uv`:

```bash
env -u NO_COLOR uv run --no-project --with-requirements requirements-dev.txt bash scripts/validate.sh
```

This runs documentation lint, behavioral tests, real Talos schema validation for both roles, workflow/shell checks, and credential-free Terraform schema/mock/lifecycle checks for all three Azure roots. The separate ISO workflow builds both private image formats using disposable credentials, then qualifies the public ISO in BIOS/UEFI guests. Only public artifacts may leave CI. Source checks and image builds do not certify a live cluster or the full enrollment policy.

## Original design question

The following is preserved verbatim from the earlier design discussion. The current platform choice is Talos-managed Kubernetes; K3s and LXC are not current implementations.

```text
okay be honest, should i go with a purpose built controller or k3s?
my needs:
- many machines, only connected over tailscale
- automatic management of services and machines
- good support for visibility/monitering
- easily expose services online via domains, subdomains, etc.
- secret management for being able to use APIs, etc.
- docker, lxc, qemu, etc. all sorts of support
and potentially more i havent thought of yet


paste this exact message in the readme and the succint short answer beneath it.

give me the proper version here
```

Use Talos-managed Kubernetes for the fleet. Kubernetes handles applications and placement; Talos handles machines. The existing Tailscale-only and no-extra-server authorization constraints still apply.

References: [Talos overview](https://docs.siderolabs.com/talos/v1.14/overview/what-is-talos), [Image Factory and local imager](https://docs.siderolabs.com/talos/v1.14/platform-specific-installations/boot-assets), [Tailscale extension](https://github.com/siderolabs/extensions/tree/main/network/tailscale), [hardware requirements](https://docs.siderolabs.com/talos/v1.14/getting-started/system-requirements).
