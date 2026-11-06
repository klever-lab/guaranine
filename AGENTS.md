# Repository instructions

Use the native Kubernetes API, `kubectl`, and standard manifests to automatically place and manage workloads based on requested CPU, RAM, and storage. The user explicitly rejected a custom API and dropped LXC. Do not add either, or replace automatic scheduling with manual host selection. New computers must be easy to enroll, and worker computers must be easy to evacuate and remove without assigning their workloads to replacement hosts by hand.

All laptops are in separate locations. Every private inter-host connection must use Tailscale. They do not share a LAN or have guaranteed sub-10 ms latency. Direct connections, relays, and temporary disconnections are expected.

Current architecture:

- K3s with one control-plane/server node and local SQLite on a stable hub outside the replaceable worker pool; the laptops are agents/workers connected over Tailscale. Keep persistent storage outside the replaceable worker pool as well. The hub is infrastructure, not another disposable worker.
- Use Azure for Students credits for the initial coordinator, on a separate Linux VM from existing Azure services. Target 2 vCPUs and 4 GiB RAM initially; verify quota, availability, remaining credit, and total cost before provisioning. Preserve the subscription spending limit and document credit expiry and coordinator recovery.
- Kubernetes schedules containers automatically. The initial delivery is basic K3s installation, Tailscale networking, worker enrollment, verification, and maintenance. KubeVirt, portable persistent storage, public domains, and full monitoring are future additions.
- Use host Tailscale addresses for cluster transport, with Flannel bound to the Tailscale interface. Do not assume LAN connectivity or form an embedded-etcd/control-plane quorum across the laptops.
- Use storage accessible from destination hosts for movable workloads; local-path storage does not provide portable disks. Record storage, control-plane availability, and partition/fencing limitations honestly.
- Preserve automatic scheduling. Distinguish container recreation, VM live migration, and stateful failure recovery; do not promise they have identical guarantees.
- Use a repeatable worker enrollment profile and standard drain/maintenance APIs. Wait for workload evacuation before declaring a worker removable; document spare-capacity, migration, and fencing blockers. Distinguish planned removal from an abrupt failure.

Read README.md for the design and upstream references, and infra/azure/README.md for the Terraform operator guide. Coordinator base infrastructure is implemented as IaC but has not been deployed; host/K3s configuration is pending. This architecture replaces the earlier standalone-Incus plan while preserving the Tailscale-only network requirement.

Implement the platform primarily as infrastructure as code: Terraform for Azure resources/state, Ansible for host and K3s configuration, and Kubernetes manifests for workloads. Do not substitute manual portal setup for versioned infrastructure. The first implementation task is KIAN-972, the Terraform-managed Azure coordinator VM.

Repository validation uses Python 3.12 or newer: install `requirements-dev.txt` in a virtual environment and run `bash scripts/validate.sh`. The shared entry point reuses a matching Terraform executable or downloads the exact `.terraform-version` release into temporary storage after checking its committed SHA-256 checksum. It runs pinned PyMarkdown, tool-bootstrap regression tests, Terraform format/init/validate and credential-free mock plan/lifecycle tests. No system Terraform installation is required; release/registry network access is needed for downloads. When introducing more runnable code or infrastructure, add meaningful validation for that component to this entry point and CI; documentation lint alone is insufficient for such changes. This repository does not use Node.js or npm. Keep credentials out of git. Remote development uses the shared Clusterfork controller and central task workflow; do not add a per-repository worker workflow or runtime pin.
