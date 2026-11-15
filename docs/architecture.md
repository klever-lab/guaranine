# Architecture

Guaranine runs one K3s server in a cloud VM and K3s agents on laptops. All nodes use the same immutable Kairos/Hadron base. The server uses SQLite; applications run on workers. OVH is the prepared migration target; the Azure controller remains live until cutover.

## Component ownership

| Source | Owns |
| --- | --- |
| [guaranine](../guaranine) and [scripts/guaranine.py](../scripts/guaranine.py) | Thin launcher and operator CLI; private inputs, media preparation, SSH trust and explicit installation. |
| [kairos/Dockerfile](../kairos/Dockerfile) | Public OS base, upstream binaries, Wi-Fi drivers/firmware checks and image defaults. |
| [kairos/config.yaml](../kairos/config.yaml) | Shared native Kairos stages, host services, networking and firewall. |
| [kairos/controlplane.yaml](../kairos/controlplane.yaml) | K3s server settings, persistence check and activation guard. |
| [kairos/worker.yaml](../kairos/worker.yaml) | K3s agent settings and power-off after installation. |
| [kairos/auroraboot.yaml](../kairos/auroraboot.yaml) | Native UEFI disk build recipe; the CLI converts its raw output to QCOW2. |
| [infra/ovh/main.tf](../infra/ovh/main.tf) | Private image, d2-2 VM, public network port and security group. |
| [infra/azure/](../infra/azure/) | Existing Azure resources retained for rollback and eventual cleanup. |
| [kubernetes/](../kubernetes/) | Application network policy, [storage provisioner](storage.md), [monitoring stack](monitoring.md) and [Kairos Operator](operations.md#configuration-rollouts). |

Terraform owns infrastructure, Kairos owns the OS and K3s configuration, and Kubernetes manifests own workloads. There is no custom management API or enrollment service. See [networking and security](security.md) for transport and trust boundaries.

## Images and node roles

The CLI builds a local public base tagged from a hash of the version manifest, Dockerfile and shared Kairos configuration. Pinned AuroraBoot turns that base into private laptop ISO or controller QCOW2 media. [Dependency pins](development.md#dependency-pins) and [private material handling](security.md#private-material-and-enrollment) have separate owners.

The shared profile disables automatic installation. Live media loads its private configuration into RAM, gives each boot its own machine/SSH identity and starts networking without enabling K3s. An installed worker receives the native worker profile through an explicit manual installation. Its root filesystem comes from the live media, so installation does not need a public image registry or LAN provisioning service.

The installed system carries its Wi-Fi profile forward and keeps its own persistent Tailscale identity. Shared host services use systemd-networkd, systemd-resolved and `wpa_supplicant`; NetworkManager remains an operator-side input source. The image has no Wi-Fi management CLI or supplicant control socket. Udev and boot-time detection start the supplicant for wireless interfaces. Ordinary discovery and reconnection still require scanning. The controller uses its cloud NIC without a Wi-Fi profile.

K3s starts after the firewall and Tailscale registration service. The new server checks that `/var/lib/rancher` uses the same persistent filesystem as `/usr/local` and requires `/var/lib/rancher/k3s/.guaranine-ready`. A freshly booted controller stays stopped until explicit initialization or restore, so a second VM can join the tailnet without starting a competing server. Worker and server settings live in native role profiles.

AuroraBoot creates the controller's persistent partition on first boot. Its native profile refreshes block-device events and explicitly waits for the `COS_PERSISTENT` device label before Kairos grows or mounts that filesystem. Native Kairos persistence holds K3s data on the included root disk; the OVH configuration adds no data volume.

## Controller resources

The OVH target is Public Cloud `d2-2` in `BHS5`, with 1 vCPU, 2,000 MB RAM and a 25-GB local root disk. The [Terraform root](../infra/ovh/main.tf) fixes the flavor and checks the exact private image hash. This is the user's chosen economical trial; its single CPU is below K3s's documented two-core server minimum. Boot, API health and resource headroom must be measured before relying on it for the live cluster. Perform the [cloud preflight](../SETUP.md#cloud-preflight) before applying.

The controller boots a private UEFI Glance image without cloud-init user data or a guest agent. The instance ignores later `image_id` changes because the OpenStack provider otherwise rebuilds the disk in place. [Kairos upgrades](operations.md#os-updates) own installed OS updates.

The VM has Terraform `prevent_destroy`, protecting its included disk against Terraform replacement or deletion while the resource remains configured. The local root disk cannot be detached and reattached to a replacement VM: deleting or rebuilding the instance loses its current contents. Before deliberate teardown, export the server state using [backup and restore](operations.md#backup). Terraform protection does not prevent deletion through the OVH console or API.

The existing Azure controller uses 2 vCPUs, 4 GiB RAM, a 32-GiB OS disk and a separate retained 64-GiB `GUARANINE_DATA` disk. Its existing roots and state remain untouched during OVH staging. Neither deployment depends on an external management controller; [recovery](operations.md#restore-on-another-cloud-vm) supports another cloud VM.

## Workloads and availability

The server has a `NoSchedule` control-plane taint. Bundled Traefik, ServiceLB, local-path storage, metrics-server and Gateway API CRDs are disabled. [Application storage](storage.md) and [monitoring](monitoring.md) are explicitly deployed through repository Kubernetes manifests; there is no preinstalled application or ingress bundle.

One server is an availability tradeoff: losing it interrupts control-plane operations. Disconnected workers can keep running existing workloads. Stateful failover still needs fencing, spare capacity and suitable storage; a lost connection does not prove a machine has stopped. Local disks do not follow rescheduled workloads. See [node retirement](operations.md#retire-a-worker) before removing a machine.
