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

Read README.md for the design and upstream references. The platform is not yet implemented or deployed. This architecture replaces the earlier standalone-Incus plan while preserving the Tailscale-only network requirement.

Repository validation uses Node.js 22 or newer: run `npm ci` and `npm test`. The current documentation-only baseline uses pinned Markdown lint. When introducing runnable code or infrastructure, add meaningful validation for that component and include it in `npm test` and CI; documentation lint alone is insufficient for such changes. Keep credentials out of git. Remote development uses the shared Clusterfork controller and central task workflow; do not add a per-repository worker workflow or runtime pin.
