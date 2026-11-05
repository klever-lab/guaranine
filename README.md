# guaranine

K3s for a fleet of old laptops (2–8 GB RAM each) in different locations. Use `kubectl`, standard Kubernetes manifests, and the native Kubernetes API to declare workloads; existing schedulers and controllers choose a host and manage them. **Automatic placement, lifecycle management, and easy worker enrollment/removal are requirements.** A new computer should add capacity after enrollment; removing a computer should evacuate its workloads without choosing their destination hosts.

**Every inter-host connection uses Tailscale. No shared LAN or sub-10 ms latency between laptops is assumed.**

The initial implementation is the basic **K3s** installation, Tailscale networking, worker enrollment, verification, and maintenance. A custom API and LXC are excluded. KubeVirt, portable persistent storage, public domains, and full monitoring are later additions. The infrastructure is still a design and has not been implemented or deployed.

## Remote development

The existing Linear project [guaranine](https://linear.app/kian-finishing-zombie-granola/project/guaranine-5e147e0efbea) is the intended Clusterfork binding for `klever-lab/guaranine` on `main`. Once enrollment is verified and enabled, issues in this project entering **Todo** go through the shared readiness review and development workflow. No repository-specific worker or custom platform API is needed.

Run repository validation with Python 3.12 or newer:

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install --requirement requirements-dev.txt
bash scripts/validate.sh
```

The current check lints the repository's Markdown, which is its only implementation content today. When adding Ansible, scripts, Kubernetes manifests, or an ISO build, extend validation to check and exercise those components. Passing documentation lint does not validate an undeployed cluster or an installer image.

## Infrastructure as code

Terraform owns the Azure resources and state backend. Ansible owns repeatable host and K3s configuration. Kubernetes manifests own deployed workloads. Version configuration and pin dependencies in this repository so environments can be recreated without portal-only changes. Live provisioning uses the documented operator credentials and preflight; repository CI validates configuration without creating Azure resources.

The first implementation issue is [KIAN-972: Manage the Azure K3s coordinator VM with Terraform](https://linear.app/kian-finishing-zombie-granola/issue/KIAN-972/manage-the-azure-k3s-coordinator-vm-with-terraform).

## Controller choice

Original design question, verbatim; the current scope above incorporates the later decision to drop the custom API and LXC:

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

**Use K3s as the foundation.** Its scheduler and existing monitoring, ingress, and secrets integrations fit this growing platform better than maintaining our own orchestration engine. Run one stable server with local SQLite and Tailscale-connected workers; no control-plane quorum crosses the laptops. Use `kubectl` and standard Kubernetes manifests directly.

K3s runs Docker/OCI application images; KubeVirt can add QEMU/KVM VMs later. **LXC is excluded from the current scope.** Stateful movement still needs suitable storage and fencing. Budget above the 512 MB VPS: K3s documents a 2-core / 2-GB server minimum before additional workloads.

| Your need | What I’d use |
| --- | --- |
| Machines connected only through Tailscale | One stable K3s server with SQLite; laptop workers communicate over Tailscale. Distributed workers are supported. [K3s architecture](https://docs.k3s.io/architecture), [distributed deployments](https://docs.k3s.io/networking/distributed-multicloud) |
| Automatic workload management | Kubernetes scheduling, controllers, health checks, and drain operations; KubeVirt extends this to VMs. Host installation and maintenance still need bootstrap automation. |
| Monitoring and visibility | Prometheus, Grafana, and Alertmanager, using existing Kubernetes integrations and dashboards. These are additional components to deploy and size. [Monitoring stack](https://github.com/prometheus-operator/kube-prometheus) |
| Domains and subdomains | Bundled Traefik for routing, plus Cloudflare Tunnel for public web access through the laptops’ NAT connections. [K3s components](https://docs.k3s.io/), [Cloudflare’s Kubernetes guide](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/deployment-guides/kubernetes/) |
| API keys and other secrets | Kubernetes Secrets with restricted access and explicitly enabled encryption at rest; External Secrets can synchronize credentials from an external secret manager. [Encryption](https://docs.k3s.io/security/secrets-encryption), [External Secrets](https://external-secrets.io/latest/) |
| Docker applications | Docker/OCI images through containerd. Existing Compose definitions need translating into Kubernetes workloads. [Container runtime support](https://docs.k3s.io/advanced#using-docker-as-the-container-runtime) |
| QEMU/KVM VMs | KubeVirt, on workers with suitable hardware and capacity. [KubeVirt architecture](https://kubevirt.io/user-guide/architecture/architecture/) |
| LXC system containers | Excluded from scope at the user's request. |

References: [K3s architecture](https://docs.k3s.io/architecture), [distributed workers](https://docs.k3s.io/networking/distributed-multicloud), [included components](https://docs.k3s.io/), [KubeVirt](https://kubevirt.io/), and [hardware requirements](https://docs.k3s.io/installation/requirements#hardware).

## Topology

```text
Operator --> Kubernetes API over Tailscale
                  |
        Stable hub outside the worker pool
        One K3s control-plane node
        Local SQLite database
        Shared storage endpoint
                  |
          Tailscale connections
          /       |       \
     laptop-a  laptop-b  laptop-c
      agents    agents    agents
       |          |          |
   containers and KubeVirt VMs, placed by the scheduler
```

Run exactly one K3s server/control-plane node initially, on a reliable dedicated machine or VM with sufficient RAM and an SSD. Keep it outside the replaceable laptop worker pool, together with the stable storage endpoint. The laptops join as K3s agents/workers. This lets any worker laptop leave without taking the API, scheduler, or disk service with it. A physical machine being a "server" does not mean it must run the `k3s server` role.

The initial coordinator will use the user's **Azure for Students credits**, on a separate Linux VM from existing Azure services. Target 2 vCPUs and 4 GiB RAM for initial headroom; choose the exact size and region after checking subscription quota, availability, remaining credit, and the complete VM/disk/network cost. Provisioning is still pending. Keep the student subscription's spending limit enabled. [Azure for Students](https://azure.microsoft.com/en-us/free/students/) provides credit subject to the offer's eligibility and expiry; do not assume an advertised free VM size meets K3s's memory requirements. [Credit exhaustion or expiry can disable the subscription and its services](https://learn.microsoft.com/en-us/azure/cost-management-billing/manage/azurestudents-subscription-disabled), so retain the backup and restore path for moving the coordinator later.

The worker network is a Tailscale mesh; scheduling and persistent storage have a stable home. This is not a fully leaderless system in which every node has the same role. The hub itself needs maintenance and recovery procedures distinct from worker removal.

K3s documents both a [single-server setup with local SQLite](https://docs.k3s.io/architecture#single-server-setup-with-an-embedded-db) and [agents distributed across different networks](https://docs.k3s.io/networking/distributed-multicloud). The local database has no peer whose acknowledgment must cross the WAN. The scheduler and controllers remain automatic; workers run workloads and report their state over Tailscale.

The single control plane is a deliberate availability tradeoff. If it is unavailable, existing worker workloads can keep running, but new placements, cross-node recovery, API changes, and migrations wait for its recovery. Back up its database and server token. Do not create an embedded-etcd quorum across the scattered laptops to make the control plane HA.

## What the API owns

| Request | Existing implementation |
| --- | --- |
| Run a container with specified CPU and RAM | Deployment, Pod resource requests/limits, Kubernetes scheduler |
| Start or stop a container service | Change the Deployment replica count |
| Replace a failed container | Kubelet and Deployment controllers, subject to capacity and storage availability |
| Move a container workload off a host | Drain/eviction and controller-created replacement on another eligible node |
| Create a full VM with specified CPU, RAM, image, and disk | KubeVirt `VirtualMachine`, instance types, and persistent volumes |
| Start, stop, or restart a VM | KubeVirt lifecycle API / `virtctl` |
| Move a running VM | KubeVirt migration API, when the VM, storage, hardware, and network meet its migration requirements |

KubeVirt supplies [VM lifecycle management](https://kubevirt.io/user-guide/user_workloads/lifecycle/), [CPU/memory instance types](https://kubevirt.io/user-guide/user_workloads/instancetypes/), and [live migration](https://kubevirt.io/user-guide/compute/live_migration/). There is no custom fleet scheduler or VM lifecycle daemon to write.

Workloads specify resource requirements, labels, and availability constraints, not a mandatory hostname. The scheduler selects a node with sufficient allocatable resources. It does not continuously rebalance every running workload simply because another node becomes less busy. A container move normally restarts the process on another node; preserving VM execution state requires KubeVirt migration.

## Bootstrap with existing tooling

1. Install minimal Debian and Tailscale on each laptop. Use unique hostnames, short-lived enrollment credentials, unattended updates, and lid settings suitable for an always-on host.
2. Reuse upstream [k3s-io/k3s-ansible](https://github.com/k3s-io/k3s-ansible), with one inventory entry in `server` and the rest in `agent`. Inventory SSH endpoints and the API endpoint use Tailscale addresses. Pin the Ansible revision and a compatible K3s release when implementing.
3. Keep the control-plane database local to the stable hub. K3s's documented minimums are [2 CPU cores / 2 GB RAM for a server and 1 core / 512 MB for an agent](https://docs.k3s.io/installation/requirements#hardware), before application resources. Reserve additional capacity for the OS, networking, controllers, and workload overhead.
4. Install KubeVirt on a Kubernetes release it supports, and enable VM workloads only on hosts with working hardware virtualization and sufficient RAM. Follow the upstream [installation and hardware checks](https://kubevirt.io/user-guide/cluster_admin/installation/). Small nodes can still run containers.
5. Install storage and networking policy before relying on cross-host recovery. Validate the setup on the actual hardware and WAN links before claiming it is operational.

## Adding and removing computers

The target workflow uses standard agent registration and maintenance APIs. The repeatable installer/enrollment profile is still to be implemented; it must not require hand-writing application placement or network routes for each new laptop.

**Add a worker:** boot the common installer or run the common bootstrap on Debian, supply enrollment credentials, and let it join Tailscale and the existing K3s server. The profile supplies the hub's stable tailnet API address and configures the new node's own Tailscale IP and Flannel interface. Give it a unique node name. Once it is `Ready`, its allocatable resources become available to the scheduler automatically. Existing workloads do not have to be rewritten. K3s already provides [agent registration using a server URL and join token](https://docs.k3s.io/quick-start).

**Remove a worker:** request maintenance through the Kubernetes API/tooling. Mark it unschedulable, drain its container workloads, and evacuate eligible VMs with KubeVirt's `LiveMigrate` eviction strategy. Controllers choose replacement hosts; the operator chooses only which computer to retire. Wait for evacuation and replacement readiness before powering it off. Use upstream [node draining](https://kubernetes.io/docs/tasks/administer-cluster/safely-drain-node/) and [KubeVirt node maintenance](https://kubevirt.io/user-guide/cluster_admin/node_maintenance/), with disruption budgets and VM migration constraints respected.

Reserve enough spare resources to evacuate a worker. If destination capacity, portable storage, or VM compatibility is missing, maintenance must report the blocker rather than claim it is safe to power down. Stopping/recreating a container can interrupt its connections; a successful drain is not a guarantee of zero application downtime.

**Retire permanently:** after evacuation, stop the node agent, delete its Kubernetes Node registration, and revoke its Tailscale identity and any dedicated enrollment credentials. Deleting the Node object while its agent remains running is not decommissioning. Keep shared workload volumes intact.

**Bring it back:** a temporarily drained worker can be uncordoned after it is healthy; a rebuilt worker goes through the same enrollment flow. Neither operation needs manual placement of its former workloads.

An unexpected shutdown or WAN outage follows failure detection and recovery instead of graceful drain. Stateful recovery still needs the fencing rules described below. The stable hub and storage service are not removable through the worker-retirement flow.

## Tailscale transport

Use the already-installed host Tailscale clients as the transport. Set each node's Kubernetes internal address to its Tailscale IP and bind Flannel to `tailscale0`. The initial network backend is VXLAN over Tailscale; encapsulated inter-node traffic must be addressed only to tailnet IPs. Choose a tested MTU that accounts for both encapsulation layers.

K3s documents [`--flannel-iface` and the VXLAN backend](https://docs.k3s.io/networking/basic-network-options). Verify node addresses, Flannel peer endpoints, and packet paths during bootstrap. The separate built-in K3s VPN-provider integration is currently [marked experimental](https://docs.k3s.io/networking/distributed-multicloud#integration-with-the-tailscale-vpn-provider-experimental); it is not required for this design.

The tailnet policy and host firewall permit the necessary cluster traffic only over Tailscale: API TCP 6443, Flannel VXLAN UDP 8472, kubelet/metrics access where needed, and storage traffic to the designated storage endpoint. See the [K3s networking requirements](https://docs.k3s.io/installation/requirements#networking). Keep these ports inaccessible on physical LAN/public interfaces. Internet access is still needed to establish Tailscale connections and fetch packages/images.

Both direct and relayed Tailscale paths must work. WAN latency affects API response time, startup, storage, and migrations; there is no requirement for every worker pair to maintain sub-10 ms latency. Configure and test node-health, eviction, and application timeouts for the actual links. Adding a second WAN control-plane replica is not the solution to a failed controller.

## Persistent disks that follow machines

Movable compute needs storage that the destination can access. The initial storage design is an NFSv4 server on a reliable designated host outside the replaceable worker pool, reachable only over its Tailscale address, with the upstream [NFS CSI driver](https://github.com/kubernetes-csi/csi-driver-nfs). It may share the stable control-plane hub initially, with separate capacity reserved for workload data and off-site backups. This is a central storage dependency, not a distributed block-storage quorum. A worker laptop's local disk must not become the only copy of another worker's persistent data.

The driver provisions volumes from an existing NFS export. Use RWX volumes for migratable VM disks. KubeVirt explicitly requires shared RWX access for live migration of VMs using PVCs. Native K3s [local-path storage binds a workload to its storage node](https://docs.k3s.io/add-ons/storage#setting-up-the-local-storage-provider), so it cannot be the storage class for machines promised automatic movement across hosts.

A requested PVC size is not automatically an enforced directory quota on NFS. VM virtual-disk capacity and storage-backend quotas must be configured and verified separately; do not advertise hard per-service disk limits just because a PVC requests `10Gi`.

Shared NFS avoids copying the entire disk each time compute moves, but WAN filesystem I/O can be slow. Benchmark the intended workloads. The NFS server and the single control-plane node are failure points; back them up and test recovery. A storage outage can stall every workload using it. Shared storage is also not a substitute for fencing during a network partition.

## Network isolation and ingress

Each host has a Tailscale identity. Inside the cluster, Kubernetes namespaces, labels, and NetworkPolicies define which workloads may communicate. Start with default-deny policies and explicitly allow required DNS, application dependencies, and egress. Keep the bundled policy controller enabled and test both same-node and cross-node isolation. Restrict ordinary application privileges and keep infrastructure privileges limited to the components that need them, including KubeVirt.

Kubernetes Services provide stable application endpoints as Pods move. Optional public HTTP endpoints use Cloudflare Tunnel to those Services. Private access can use Kubernetes port forwarding through the Tailscale-only API initially. A separate Tailscale daemon and identity in every Pod is not required to transport cross-host traffic over Tailscale.

## Failures and moves

- Container controllers can create replacements after node failure, with usable storage, an available control plane, and spare capacity. A bare Pod by itself is not a controller-managed service.
- A drain is a controlled maintenance operation: the platform handles replacement placement; the operator does not have to pick the destination host.
- Live VM migration needs compatible hardware/CPU configuration, destination memory, supported network binding, accessible shared storage, and sufficient bandwidth to converge. Default networking carries migration traffic through the inter-node Tailscale transport. Test it before enabling automatic migration policies.
- Losing a Tailscale connection does not prove a VM has stopped. Follow KubeVirt's [unresponsive-node guidance](https://kubevirt.io/user-guide/cluster_admin/unresponsive_nodes/) and implement a fencing/recovery policy before promising unattended failover of single-writer VMs or databases. A link outage must not start an unsafe second writer.

## Implementation sequence

1. Bootstrap the stable control-plane/storage hub and two agents in different networks, all private traffic over Tailscale.
2. Deploy a stateless container with CPU/memory requests and limits, without a selected host. Prove automatic placement, restart, scale-to-zero, and replacement after draining a worker.
3. Prove cluster packet paths stay on Tailscale, including relay fallback, and validate default-deny workload policies.
4. Provision shared storage with NFS CSI, then test container replacement on a different worker with the same data. Verify backup/restore and disk-capacity behavior.
5. Add KubeVirt, create a small VM through the API, and prove start/stop and a controlled cross-host migration with its persistent disk.
6. Prove that a fresh worker joins through the common enrollment profile and can be drained, removed, and re-enrolled without manually assigning its workloads. Exercise WAN delay, worker disconnection, control-plane outage, storage outage, and recovery. Document measured limits and safe stateful failure handling.

Store join tokens, kubeconfigs, tunnel credentials, and backup keys outside git. Pin component versions and maintain off-site backups of the control-plane database, server token, VM disks, and application data. Infrastructure provisioning can use Ansible/Terraform; workload placement and lifecycle belong to Kubernetes/KubeVirt.
