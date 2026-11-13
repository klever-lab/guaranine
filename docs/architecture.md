# Architecture

Guaranine runs one K3s server in a cloud VM and K3s agents on laptops. All nodes use the same immutable Kairos/Hadron base. The server uses SQLite on a separate retained disk; applications run on workers.

## Component ownership

| Source | Owns |
| --- | --- |
| [guaranine](../guaranine) and [scripts/guaranine.py](../scripts/guaranine.py) | Thin launcher and operator CLI; private inputs, media preparation, SSH trust and explicit installation. |
| [kairos/Dockerfile](../kairos/Dockerfile) | Public OS base, upstream binaries, Wi-Fi drivers/firmware checks and image defaults. |
| [kairos/config.yaml](../kairos/config.yaml) | Shared native Kairos stages, host services, networking and firewall. |
| [kairos/controlplane.yaml](../kairos/controlplane.yaml) | K3s server settings and retained data mount. |
| [kairos/worker.yaml](../kairos/worker.yaml) | K3s agent settings and power-off after installation. |
| [kairos/auroraboot.yaml](../kairos/auroraboot.yaml) | Native VHD build recipe. |
| [infra/azure/image/main.tf](../infra/azure/image/main.tf) | Artifact-specific upload group and private source disk. |
| [infra/azure/coordinator/main.tf](../infra/azure/coordinator/main.tf) | Cloud VM, network, boot disk and protected data disk. |
| [kubernetes/](../kubernetes/) | Workload manifests; currently the application network policy. |

Terraform owns infrastructure, Kairos owns the OS and K3s configuration, and Kubernetes manifests own workloads. There is no custom management API, enrollment service or additional Terraform root. See [networking and security](security.md) for transport and trust boundaries.

## Images and node roles

The CLI builds a local public base tagged from a hash of the version manifest, Dockerfile and shared Kairos configuration. Pinned AuroraBoot turns that base into private laptop ISO or controller VHD media. [Dependency pins](development.md#dependency-pins) and [private material handling](security.md#private-material-and-enrollment) have separate owners.

The shared profile disables automatic installation. Live media loads its private configuration into RAM, gives each boot its own machine/SSH identity and starts networking without enabling K3s. An installed worker receives the native worker profile through an explicit manual installation. Its root filesystem comes from the live media, so installation does not need a public image registry or LAN provisioning service.

The installed system carries its Wi-Fi profile forward and keeps its own persistent Tailscale identity. Shared host services use systemd-networkd, systemd-resolved and `wpa_supplicant`; NetworkManager remains an operator-side input source. The image has no Wi-Fi management CLI or supplicant control socket. Udev and boot-time detection start the supplicant for wireless interfaces. Ordinary discovery and reconnection still require scanning. The controller uses its cloud NIC without a Wi-Fi profile.

K3s starts after the firewall and Tailscale registration service. The server additionally binds its service to the ext4 volume labeled `GUARANINE_DATA` at `/var/lib/rancher/k3s`; an absent mount prevents the server from starting. Worker and server settings live in native role profiles, rather than generated copies of shared machine policy.

## Controller resources

The current coordinator defaults are `Standard_B2ls_v2` (2 vCPUs, 4 GiB RAM), a 32-GiB OS SSD and a separate 64-GiB data SSD at LUN 0. The [Terraform variables](../infra/azure/coordinator/main.tf) own their exact constraints. Perform the [cloud preflight](../SETUP.md#cloud-preflight) against the selected region, SKU and subscription.

The controller attaches a prepared Gen2 disk without Azure guest provisioning or a VM agent. Terraform copies the finalized private upload disk into the controller boot disk once. The boot disk ignores later changes to its seed; [Kairos upgrades](operations.md#os-updates) own installed OS updates.

VM replacement reuses both disks. The data disk has `prevent_destroy`; the boot disk is an independent resource retained across VM replacement. The `handoff` output records resource IDs and the data-disk LUN. Disk retention and [off-machine backup](operations.md#backup) serve different purposes.

Azure is temporary hosting. The deployment does not depend on another Azure service or a shared external controller, and [recovery](operations.md#restore-on-another-cloud-vm) supports a replacement cloud VM.

## Workloads and availability

The server has a `NoSchedule` control-plane taint. Traefik, ServiceLB, local-path storage, metrics-server and Gateway API CRDs are disabled. Add required applications and supporting components through repository Kubernetes manifests; there is no preinstalled ingress, monitoring or application bundle.

One server is an availability tradeoff: losing it interrupts control-plane operations. Disconnected workers can keep running existing workloads. Stateful failover still needs fencing, spare capacity and suitable storage; a lost connection does not prove a machine has stopped. Local disks do not follow rescheduled workloads. See [node retirement](operations.md#retire-a-worker) before removing a machine.
