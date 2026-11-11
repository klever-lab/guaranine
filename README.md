# guaranine

One Talos control-plane VM on Azure, plus laptop workers anywhere. Kubernetes places applications automatically. Every private connection between machines uses Tailscale; no shared LAN or sub-10 ms connection is required.

**Keep it simple:** the official Talos imager and Tailscale extension, native Kubernetes and Terraform. No custom platform API, enrollment server or extra management stack.

## Setup

1. Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and Docker on your operator computer. Run `./guaranine init` once and fill in its private `operator.yaml`.
2. Follow [SETUP.md](SETUP.md) for the one-time Azure control-plane setup.
3. Save its Tailscale Kubernetes endpoint in `operator.yaml`. Build one private ISO for the laptop batch:

   ```bash
   ./guaranine iso
   ```

4. Boot each laptop from that ISO. On the operator computer, run `./guaranine worker` to choose its name, select from its detected disks and explicitly confirm erasure. See [SETUP.md](SETUP.md#add-a-laptop) for the prompts and verification. Use Kubernetes manifests with CPU/RAM requests to run applications; Kubernetes chooses the worker.

The ISO command runs the pinned upstream Talos imager locally. Private files stay in `~/.local/state/guaranine` by default. [SETUP.md](SETUP.md) owns batch renewal, enrollment limits, bootstrap and worker removal.

## What is ready

The shared private ISO boots into maintenance mode. Each laptop receives its name, disk choice and cluster configuration only after the operator confirms installation. See [enrollment limits](SETUP.md#enrollment-security-status).

**The Azure control plane is deployed.** See [SETUP.md](SETUP.md) for operation and recovery. Hardware-bound enrollment, enforceable expiry of all cluster join authority and YubiKey boot authorization remain unfinished. Physical laptop installation and operation across worker locations still need verification.

| Need | Current choice |
| --- | --- |
| Schedule and manage containers | Kubernetes Deployments, resource requests and standard lifecycle commands |
| Configure and update machines | Talos configuration and `talosctl` |
| Connect scattered machines | Official Tailscale system extension |
| Add/remove workers | Shared private ISO, then one name/disk confirmation command per laptop; Kubernetes cordon/drain before removal |
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

This checks both Talos roles, machine preparation, credential boundaries, workflows, shell and credential-free Terraform plans/lifecycle. The [private-image workflow](.github/workflows/private-images.yml) separately builds disposable private ISO/VHD fixtures and discards them. It publishes no images. Todo issues in the existing [Guaranine Linear project](https://linear.app/kian-finishing-zombie-granola/project/guaranine-5e147e0efbea) use the shared Clusterfork Symphony controller.

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
