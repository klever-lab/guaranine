# guaranine

One Talos control-plane VM on Azure, plus laptop workers anywhere. Kubernetes places applications automatically. Every private connection between machines uses Tailscale; no shared LAN or sub-10 ms connection is required.

**Keep it simple:** prebuilt Talos images, the official Tailscale extension, native Kubernetes and Terraform. No custom platform API, enrollment server or extra management stack.

## Setup

1. Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and Docker on your operator computer. Run `./guaranine init` once and fill in its private `operator.yaml`.
2. Set up the one Azure control plane using the [Azure guide](infra/azure/README.md). Nothing is provisioned yet; this is still a one-time operator task.
3. Save its Tailscale Kubernetes endpoint in `operator.yaml`. Prepare each laptop with one command:

   ```bash
   ./guaranine worker laptop-01 --disk /dev/nvme0n1 --confirm-install-disk /dev/nvme0n1
   ```

4. Install and boot that laptop's **private** ISO. Its embedded configuration handles installation and enrollment. Use Kubernetes manifests with CPU/RAM requests to run applications; Kubernetes chooses the worker.

The preparation command creates a short-lived Tailscale key, generates and validates the node configuration, and runs the pinned upstream Talos imager. Its files stay in `~/.local/state/guaranine` by default. [Talos operations](talos/README.md) covers settings, bootstrap and worker removal.

## What is ready

Public boot media is [prebuilt and released](https://github.com/klever-lab/guaranine/releases/latest). It contains no credentials and boots into maintenance. Each unattended installer must be generated privately for its intended laptop; it cannot safely be a shared public download. See [images and enrollment limits](images/laptop/README.md).

The repository has configuration, Terraform and tested image tooling. **There is no deployed cluster yet.** The full 24-hour, single-use, hardware-bound enrollment policy and YubiKey boot authorization remain unfinished. Physical hardware and live Azure/Tailscale operation still need verification.

| Need | Current choice |
| --- | --- |
| Schedule and manage containers | Kubernetes Deployments, resource requests and standard lifecycle commands |
| Configure and update machines | Talos configuration and `talosctl` |
| Connect scattered machines | Official Tailscale system extension |
| Add/remove workers | One private image command; Kubernetes cordon/drain before removal |
| Provision the control plane | Terraform; local state on encrypted operator storage with off-machine backups |
| Monitoring, public domains/TLS, external secrets | Later upstream integrations, only when needed |
| Movable persistent storage and QEMU/KVM VMs | Later work; storage/fencing and KubeVirt need qualification |
| LXC and a custom workload API | Excluded |

One control plane is an availability tradeoff. A drain needs spare capacity and usable storage; local disks do not follow workloads. A disconnected laptop may still be running, so stateful failover needs fencing.

## Development

Use Python 3.12+. Run the same checks as CI:

```bash
env -u NO_COLOR uv run --no-project --with-requirements requirements-dev.txt bash scripts/validate.sh
```

This checks both Talos roles, machine preparation, image publication boundaries, workflows, shell and credential-free Terraform plans/lifecycle. The image workflow separately builds disposable private ISO/VHD fixtures and boots the public ISO in BIOS/UEFI. No private image leaves CI. Todo issues in the existing [Guaranine Linear project](https://linear.app/kian-finishing-zombie-granola/project/guaranine-5e147e0efbea) use the shared Clusterfork Symphony controller.

## Original design question

Preserved verbatim. The current choice is Talos-managed Kubernetes; K3s and LXC are historical references.

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

Use Talos-managed Kubernetes. Kubernetes manages applications; Talos manages machines. Reuse upstream components and keep the repository small.
