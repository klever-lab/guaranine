# Talos operations

Use the official Talos tools for machines and Kubernetes for applications. There is one planned Azure control plane and laptop workers, all connected privately through Tailscale. No additional management server is required.

## One-time settings

On the operator computer, install `uv` and Docker with permission to run privileged Linux AMD64 containers. The launcher installs only the pinned Python runtime dependency; `talosctl` is downloaded automatically and checked against [versions.json](versions.json).

```bash
./guaranine init
```

This creates `~/.local/state/guaranine` with mode 0700 and private files with mode 0600. Use encrypted storage and an off-machine backup. To use another location, set `GUARANINE_OPERATOR_DIR` before every command. Never put this directory inside the repository. `init` refuses to overwrite an existing directory or regenerate cluster secrets.

Edit the generated `operator.yaml` once:

```yaml
tailnet: your-tailnet-name
tailscale_api_token: YOUR_PRIVATE_TAILSCALE_API_TOKEN
endpoint: null
```

The token belongs only in that private file, never in an image, git or command-line argument. In the existing Tailscale policy, authorize `tag:guaranine-controlplane` and `tag:guaranine-worker` and allow the cluster ports listed below. Set `endpoint` to `https://CONTROL_PLANE_TAILSCALE_IP:6443` once the control plane is running.

## Prepare a machine

For each laptop, verify its target disk and use a unique name:

```bash
./guaranine worker laptop-01 --disk /dev/nvme0n1 --confirm-install-disk /dev/nvme0n1
```

This checks Docker, creates a 24-hour single-use Tailscale key, generates and schema-validates the worker configuration, then builds its private ISO with the pinned upstream imager. The resulting path is printed. Write that ISO to installation media and boot the intended laptop; the image authorizes unattended installation to the selected disk. No extra key/profile/build commands are needed.

Outputs live under `nodes/laptop-01/` in the operator directory. Keep the whole directory private. Existing node outputs are never overwritten. If a build fails, inspect its protected diagnostics; revoke its Tailscale key before removing that node directory and preparing it again. Installed workers normally keep their identity across reboots and do not need a new installer.

For the one Azure control plane, prepare a VHD instead:

```bash
./guaranine controlplane --disk /dev/sda --confirm-install-disk /dev/sda --etcd-disk-selector 'disk.dev_path == "/dev/sdb"'
```

These disk names are examples. Verify the actual Azure SKU's device mapping: the OS disk is disposable and the retained data disk at LUN 0 belongs to Talos's `ETCD` volume. Follow the [Azure guide](../infra/azure/README.md) for upload and Terraform. No preparation command provisions a VM or changes a laptop.

## Bootstrap the control plane once

The initial control-plane profile uses a temporary loopback Kubernetes endpoint. After the VM joins Tailscale, find its actual IPv4 address with `tailscale status`. Set `GUARANINE_OPERATOR_DIR` to your private directory and `GUARANINE_CONTROL_IP` to that verified address. The launcher provides the checksum-pinned native CLI as `./guaranine talosctl`; no separate install or PATH changes are needed. Configure its private context and update the endpoint before enrolling workers:

```bash
export TALOSCONFIG="$GUARANINE_OPERATOR_DIR/nodes/guaranine-controlplane/config/talosconfig"
./guaranine talosctl config endpoint "$GUARANINE_CONTROL_IP"
./guaranine talosctl config node "$GUARANINE_CONTROL_IP"
cat > "$GUARANINE_OPERATOR_DIR/endpoint.patch.yaml" <<EOF
apiVersion: v1alpha1
kind: KubeClusterConfig
clusterName: guaranine
endpoint: https://$GUARANINE_CONTROL_IP:6443
EOF
./guaranine talosctl patch machineconfig --patch @"$GUARANINE_OPERATOR_DIR/endpoint.patch.yaml"
./guaranine talosctl bootstrap
./guaranine talosctl kubeconfig "$GUARANINE_OPERATOR_DIR"
```

Bootstrap etcd once. Check Talos health, etcd and `kubectl get nodes -o wide`; node addresses must be Tailscale addresses. Save the same endpoint in `operator.yaml`. Azure and hosted-tailnet bootstrap have not yet been live-qualified.

## Networking and maintenance

The committed patches restrict node addresses to `100.64.0.0/10`. Flannel binds to `tailscale0`, using VXLAN UDP 4789 and tunnel MTU 1280 before encapsulation overhead. Talos API TCP 50000, trust API 50001, Kubernetes API 6443 and kubelet TCP 10250 are tailnet-only. The host firewall blocks other ingress. Workloads still need appropriate Kubernetes NetworkPolicies.

The official Tailscale extension retains its identity, uses kernel networking, and changes neither host DNS nor advertised subnet routes. Alternate API endpoint selection and external cluster discovery are disabled. Test direct paths, relay paths and temporary disconnections on the real network.

Use `kubectl cordon` and `kubectl drain --ignore-daemonsets` before removing a worker. Wait for replacement workloads to become ready; resolve capacity, disruption-budget and storage blockers. Kubernetes chooses the destination. Fence a retired worker before deleting its Node and Tailscale identity. WAN disconnection is not fencing.

Use native `talosctl` upgrades with the matching Tailscale-enabled installer. Back up etcd with `talosctl etcd snapshot`, plus cluster secrets, operator files and Terraform state, outside Azure. For recovery, fence the old control plane and follow [native etcd recovery](https://docs.siderolabs.com/talos/v1.14/learn-more/control-plane). A retained data disk is not a backup.
