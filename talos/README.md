# Talos configuration and operations

Talos owns the OS and upstream Kubernetes. One planned Azure control-plane node runs local etcd on its retained data disk; laptops are workers. The former K3s, SQLite, Ansible and SSH path is removed. There is no additional management or enrollment server. Nothing is deployed yet.

## Pinned tools and private operator files

[versions.json](versions.json) pins Talos/Kubernetes, the Tailscale extension, upstream OCI digests, public ISO digest and `talosctl` checksums. [schematic.yaml](schematic.yaml) is public and contains only the Tailscale extension name. Tool downloads check the committed hash; private operations never submit their input to public Image Factory.

Use Python 3.12+ with `requirements-dev.txt`, and Docker for private image generation. The scripts below run from the repository root. Put operator files outside the checkout on trusted encrypted storage; directories are mode 0700 and files mode 0600. Generated configurations contain secrets even when their filenames look ordinary. Do not put them in Terraform inputs, shell arguments, GitHub issues, build contexts, CI artifacts or public releases.

```bash
umask 077
export GUARANINE_OPERATOR_DIR="$HOME/.local/state/guaranine"
mkdir -p "$GUARANINE_OPERATOR_DIR"
python3 scripts/talos/config.py init --output "$GUARANINE_OPERATOR_DIR/cluster"
```

Run `init` once for this cluster. Preserve `cluster/secrets.yaml`; all node profiles must use that same bundle. The command downloads checksum-pinned `talosctl` into the operator's cache. `GUARANINE_TALOSCTL_BINARY` may select an existing executable only if its bytes match the pin. No system package installation is performed.

## Enrollment keys and node profiles

Set the existing Tailscale policy to recognize `tag:guaranine-controlplane` and `tag:guaranine-worker` and narrowly allow the required cluster ports. Keep the operator's Tailscale API credential in a mode-0600 file, never an image. The optional local helper calls the existing Tailscale API, creates a preauthorized, non-ephemeral, single-use key with a 24-hour lifetime, and writes its response privately:

```bash
python3 scripts/talos/config.py mint-key \
  --api-token "$GUARANINE_OPERATOR_DIR/tailscale-api-token" \
  --tailnet YOUR_TAILNET --role worker \
  --output "$GUARANINE_OPERATOR_DIR/laptop-01-key.json"
```

Do not use reusable keys. The generator checks the returned lifetime and capabilities before using the key. This governs Tailscale registration, not the full [enrollment security policy](../images/laptop/README.md#enrollment-security-status).

Generate a unique profile for each worker using the existing control plane's Tailscale IPv4 address. Select the exact installation disk before generating an unattended image; the repeated disk argument is an explicit destructive-install selection, not automatic disk discovery.

```bash
python3 scripts/talos/config.py node --role worker --name laptop-01 \
  --endpoint https://100.64.0.1:6443 \
  --disk /dev/nvme0n1 \
  --confirm-install-disk /dev/nvme0n1 \
  --secrets "$GUARANINE_OPERATOR_DIR/cluster/secrets.yaml" \
  --tailscale-key "$GUARANINE_OPERATOR_DIR/laptop-01-key.json" \
  --output "$GUARANINE_OPERATOR_DIR/laptop-01"
```

Replace the example IP/disk with verified values. `machine.yaml` is the complete private profile; `talosconfig` is operator administration material and is never embedded into worker images. The generated profile is checked by the actual pinned Talos validator. Workers receive neither a control-plane role nor etcd configuration/signing keys. See the [image guide](../images/laptop/README.md) for local packaging.

## First control plane

Create a key tagged `tag:guaranine-controlplane` using `mint-key --role controlplane`. Generate the control-plane profile with the same cluster secrets, a unique name, an exact OS disk, and an explicit selector for the retained Azure data disk. There is exactly one data disk at LUN 0 in Terraform; verify the planned attachment, capacity and the selector before booting. Never select an unknown or workload disk. Talos's `ETCD` volume owns `/var/lib/etcd` on that disk.

The initial profile may use `--endpoint https://127.0.0.1:6443 --bootstrap-controlplane` while its tailnet address is not known. This loopback exception is control-plane-only. Workers always require a tailnet endpoint. Package this private control-plane profile into an Azure VHD, publish it through the [Azure image workflow](../infra/azure/README.md), and provision only the planned VM after preflight and authorization.

Once the VM appears in Tailscale, get its actual address from the operator's `tailscale status`. Use the profile's operator `talosconfig` to reach the Talos API through that address. Set `GUARANINE_CONTROL_IP` to that verified IPv4 address. Replace the temporary local Kubernetes endpoint before enrolling workers:

```bash
talosctl --talosconfig "$GUARANINE_OPERATOR_DIR/controlplane/talosconfig" config endpoint "$GUARANINE_CONTROL_IP"
talosctl --talosconfig "$GUARANINE_OPERATOR_DIR/controlplane/talosconfig" config node "$GUARANINE_CONTROL_IP"
cat > "$GUARANINE_OPERATOR_DIR/endpoint.patch.yaml" <<EOF
apiVersion: v1alpha1
kind: KubeClusterConfig
clusterName: guaranine
endpoint: https://$GUARANINE_CONTROL_IP:6443
EOF
talosctl --talosconfig "$GUARANINE_OPERATOR_DIR/controlplane/talosconfig" \
  patch machineconfig --patch @"$GUARANINE_OPERATOR_DIR/endpoint.patch.yaml"
talosctl --talosconfig "$GUARANINE_OPERATOR_DIR/controlplane/talosconfig" bootstrap
talosctl --talosconfig "$GUARANINE_OPERATOR_DIR/controlplane/talosconfig" \
  kubeconfig "$GUARANINE_OPERATOR_DIR"
```

Bootstrap etcd once. Check Talos service health, etcd, Kubernetes readiness and the actual node addresses before declaring the control plane usable. Record the final endpoint in operator configuration and use it for every worker. This repository has not yet qualified this flow against a real Azure VM or hosted tailnet.

## Tailscale-only networking

The common patch restricts node addresses to `100.64.0.0/10`, reserves OS/Kubernetes resources and blocks host ingress except the declared tailnet ports. The control plane deploys Flannel to every node with `--iface=tailscale0`, VXLAN UDP 4789 and tunnel MTU 1280; Flannel subtracts encapsulation overhead. Talos API 50000, trust API 50001, Kubernetes API 6443 and kubelet 10250 are tailnet-only. External etcd ingress is not enabled for this single-control-plane topology.

The Tailscale extension uses kernel networking, persists its identity in `/var/lib/tailscale`, and has `TS_AUTH_ONCE=true`. It does not replace host DNS or accept advertised subnet routes. KubePrism's alternate endpoint selection and external cluster discovery are disabled. No CNI, Kubernetes or Talos address should point to a physical LAN/VNet/public IP. Test actual traffic, connectivity loss, relay operation and reboot identity on the intended network before production use. Host firewall rules do not replace workload NetworkPolicies.

## Workloads, maintenance and recovery

Use Kubernetes manifests with CPU/RAM requests and appropriate storage. The control plane retains its default NoSchedule taint. Check worker Ready status, allocatable resources, scheduler placement and cross-worker networking before hosting applications. Public domains, monitoring, external secrets, portable storage and KubeVirt are later work.

Use `kubectl cordon`, `kubectl drain --ignore-daemonsets`, and readiness checks before removal. Resolve spare-capacity, disruption-budget and local-storage blockers; do not choose replacement hosts manually. After fencing a retired worker, remove its Node and tailnet identity. A disconnected node is not necessarily powered off.

Apply machine changes with `talosctl`; use its native Talos and Kubernetes upgrade procedures with the version-matched Tailscale-enabled installer image. Do not install packages, SSH, or run an Ansible playbook on a Talos node. Preserve the extension and node identity across upgrades, and test these procedures on a disposable worker first.

Back up etcd with `talosctl etcd snapshot` to protected storage outside the student subscription, together with cluster secrets, operator configuration and IaC inputs/state backups. A retained disk is not a backup. For recovery, fence the old control plane, restore Talos configuration and a verified etcd snapshot using native Talos recovery, then confirm there is exactly one control plane before enabling workers. OS replacement does not guarantee automatic membership/data recovery. Plan an exit before credit expiry.

Upstream references: [multihoming](https://docs.siderolabs.com/talos/v1.14/networking/multihoming), [ingress firewall](https://docs.siderolabs.com/talos/v1.14/networking/ingress-firewall), [control plane and etcd](https://docs.siderolabs.com/talos/v1.14/learn-more/control-plane), [Tailscale extension](https://github.com/siderolabs/extensions/tree/main/network/tailscale).
