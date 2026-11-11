# Guaranine setup

One Azure control plane runs Talos and local etcd; laptops join as workers over Tailscale. The commands below run from the repository root.

## Initialize the operator computer

On the Linux operator computer, install `uv`, `sudo` and Docker. Run the CLI as your normal user; it requests sudo only for Docker's privileged Linux AMD64 build. The launcher installs the pinned Python runtime dependency and downloads checksum-verified `talosctl` automatically. Azure provisioning also uses Terraform, Azure CLI, AzCopy, `zstd` and `qemu-img`; cluster checks use `kubectl` matching the pinned Kubernetes version.

Use encrypted operator storage with an off-machine backup. The default directory is `~/.local/state/guaranine`; set `GUARANINE_OPERATOR_DIR` before initialization to choose another location outside the checkout.

```bash
umask 077
export GUARANINE_OPERATOR_DIR="${GUARANINE_OPERATOR_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/guaranine}"
./guaranine init
```

This creates a mode-0700 directory, cluster secrets and one mode-0600 `operator.yaml`. It refuses to overwrite an existing directory or regenerate cluster secrets. Preserve `secrets.yaml` for every node in this cluster. Fill in the private settings:

```yaml
tailnet: your-tailnet-name
tailscale_api_token: YOUR_PRIVATE_TAILSCALE_API_TOKEN
endpoint: null
```

The API credential stays only in this private file. Apply the versioned tailnet policy below before preparing nodes. Set `endpoint` after the control plane is bootstrapped.

Other operator VPNs must allow the Tailscale range and preserve its routes. On this laptop, Windscribe uses OpenVPN UDP on port 443 with `100.64.0.0/10` excluded in its native split-tunneling settings. Check that `ip route get CONTROL_PLANE_TAILSCALE_IP` selects `tailscale0` and authenticated Talos access works while the VPN and its firewall are enabled.

## Tailscale policy

[tailscale/policy.json](tailscale/policy.json) declares this account's complete tailnet policy, including existing personal-device access, SSH rules and Funnel settings. Edit this file and save its complete contents through the standard [Tailscale Access controls editor](https://console.tailscale.com/admin/acls). Tailscale validates the policy and runs its native allow/deny tests before accepting an update. The repository checks JSON syntax without credentials; it does not apply live policy from CI.

Only tailnet admins own the Guaranine role tags. Workers can initiate Kubernetes API and Talos trust traffic to the control plane; the control plane can initiate Talos API and kubelet traffic to workers. Both roles can carry Flannel VXLAN between nodes. Tests reject node SSH, cross-host etcd access, worker administration of peers, access to the existing personal server and protocol mixups. The operator retains the pre-existing broad personal grant; Kubernetes workload NetworkPolicies remain separate.

Before applying a change, back up the live policy on protected operator storage with its off-machine backup. If using the native Tailscale API, validate the proposed file, use the live policy's ETag in `If-Match`, and read back the result. Keep API credentials in private `operator.yaml`; no policy deployment credential belongs in git or CI.

## Check Azure credit and quota

Before provisioning, verify the selected Azure for Students subscription, region/SKU availability, vCPU quota, remaining credit, expiry and complete cost. Keep the spending limit enabled. Do not apply while credit, quota or cost is unknown or choose an automatic paid fallback. Confirm Compute/Network provider registration and resource permissions, using an existing Azure CLI login for that subscription.

The default is `Standard_B2ls_v2` (2 vCPUs/4 GiB), a 32-GiB disposable OS SSD and a retained 64-GiB etcd SSD at LUN 0. Include compute, both SSDs, image/upload storage, public IPv4 and outbound traffic in the estimate. Existing Azure services and the shared Clusterfork controller remain independent. See [Azure for Students](https://azure.microsoft.com/free/students/) and [credit expiry/exhaustion](https://learn.microsoft.com/azure/cost-management-billing/manage/azurestudents-subscription-disabled).

## Prepare the control-plane image

Verify the actual Azure SKU's disk properties before selecting disks. For the supported 32-GiB OS disk and unique non-system 64-GiB data disk at LUN 0:

```bash
./guaranine controlplane --disk /dev/sda --confirm-install-disk /dev/sda --etcd-disk-selector '!system_disk && disk.size == 68719476736u'
```

This creates a short-lived, single-use Tailscale key, validates the native machine configuration and runs the pinned upstream Talos imager locally. The private `.vhd.zst` is under `nodes/guaranine-controlplane/image/` in the operator directory. The Azure image contains an installed OS. Linux disk names can change after VM replacement, so select the retained etcd disk using verified stable properties. No VM is provisioned by this command.

To finish an already-prepared control-plane configuration, run `./guaranine build-controlplane`. It reuses the existing unexpired registration key and configuration and extracts the VHD with private permissions under your ownership. Existing image outputs are never overwritten.

The imager stages its output in a private temporary directory on operator storage. Leave room there for the uncompressed VHD and compressed output; Docker still needs space for its container images. Temporary build files are removed after the imager exits.

Decompress the VHD privately with `zstd --decompress` and check it with `qemu-img info -f vpc`. Record the actual uncompressed file size and SHA-256 for the next step. Configuration is embedded locally, never sent to public Image Factory or placed in Terraform inputs/state. The VM uses ordinary Gen2 boot with Secure Boot/vTPM disabled.

## Upload the image with Terraform

There are two independent Terraform roots, each with one `main.tf`: [image](infra/azure/image/main.tf) owns upload staging and the private managed image; [coordinator](infra/azure/coordinator/main.tf) owns the VM, network and retained disk. Terraform/AzureRM versions and provider locks remain pinned.

Both use native local state on protected operator storage. Run one operator at a time with state locking enabled and back up state after every apply. Keep tfvars, plans and caches outside git. CI only uses credential-free mocks.

Copy each root's `terraform.tfvars.example` into the operator directory and fill in real subscription, region and artifact identifiers. For this first image, use `image.tfvars` with `image_ready = false`, the VHD size/hash and a unique `guaranine-talos-...` name. New image contents need a separate state path; Terraform appends the content hash to the resource name.

```bash
mkdir -p "$GUARANINE_OPERATOR_DIR/terraform"
export TF_DATA_DIR="$GUARANINE_OPERATOR_DIR/terraform/image-cache"
terraform -chdir=infra/azure/image init -lockfile=readonly \
  -backend-config="path=$GUARANINE_OPERATOR_DIR/terraform/image.tfstate"
terraform -chdir=infra/azure/image plan -var-file="$GUARANINE_OPERATOR_DIR/image.tfvars" \
  -out="$GUARANINE_OPERATOR_DIR/image.tfplan"
terraform -chdir=infra/azure/image show "$GUARANINE_OPERATOR_DIR/image.tfplan"
terraform -chdir=infra/azure/image apply "$GUARANINE_OPERATOR_DIR/image.tfplan"
```

Inspect each saved plan against the authorized scope before applying. This first apply creates only a Gen2 upload disk. Obtain its ID with `terraform -chdir=infra/azure/image output -raw upload_disk_id`.

Follow the [official Azure direct-upload procedure](https://learn.microsoft.com/azure/virtual-machines/linux/disks-upload-vhd-to-managed-disk-cli): grant a short-lived write SAS, copy the exact VHD with AzCopy, and revoke disk access in an `EXIT` trap even if copying fails. Keep tracing off and AzCopy logs/job plans private. Neither the SAS nor VHD contents enter Terraform state.

After upload and revocation, set `image_ready = true` and repeat plan/show/apply. This closes disk export access and creates the private image. Put its `talos_image_id` output into `coordinator.tfvars`.

## Provision the control plane

Terraform first creates a managed boot disk from the private image, then attaches that prepared disk to the VM. This uses Azure's existing-OS path and avoids the generalized-guest provisioning handshake that Talos skips with a complete embedded configuration. No SSH metadata, cloud-init, custom data, VM agent or extension service is needed. Azure-managed boot diagnostics provide protected console logs for failed boots.

```bash
export TF_DATA_DIR="$GUARANINE_OPERATOR_DIR/terraform/coordinator-cache"
terraform -chdir=infra/azure/coordinator init -lockfile=readonly \
  -backend-config="path=$GUARANINE_OPERATOR_DIR/terraform/coordinator.tfstate"
terraform -chdir=infra/azure/coordinator plan -var-file="$GUARANINE_OPERATOR_DIR/coordinator.tfvars" \
  -out="$GUARANINE_OPERATOR_DIR/coordinator.tfplan"
terraform -chdir=infra/azure/coordinator show "$GUARANINE_OPERATOR_DIR/coordinator.tfplan"
terraform -chdir=infra/azure/coordinator apply "$GUARANINE_OPERATOR_DIR/coordinator.tfplan"
terraform -chdir=infra/azure/coordinator plan -var-file="$GUARANINE_OPERATOR_DIR/coordinator.tfvars" -detailed-exitcode
```

Require a no-change rerun. The NSG denies physical inbound traffic and direct VNet egress. Its public IP supplies explicit internet egress for Tailscale/DERP and downloads. Azure IPs are never cluster endpoints. The `talos_handoff` output records resource/image/disk identifiers and LUN. VM replacement reuses both disks, preserving the installed machine identity. The image seeds a boot disk once; use native Talos upgrades for an installed OS.

## Bootstrap the control plane once

The initial profile uses a temporary loopback Kubernetes endpoint. After the VM joins Tailscale, find its actual IPv4 address with `tailscale status`. Set `GUARANINE_CONTROL_IP` to that verified address and use the generated control-plane operator context:

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
./guaranine talosctl kubeconfig --merge=false "$GUARANINE_OPERATOR_DIR/kubeconfig"
```

Bootstrap etcd once. Check Talos health, etcd and `kubectl get nodes -o wide`; node addresses must be Tailscale addresses. Set `endpoint` in the private `operator.yaml` to `https://CONTROL_PLANE_TAILSCALE_IP:6443` before preparing workers. The deployed controller has passed native Talos health checks with its etcd volume on the retained disk and its Kubernetes API reached over Tailscale.

## Prepare a shared laptop ISO

With Docker running, build the private boot media once on the operator computer:

```bash
./guaranine iso
```

This checks Docker, requests one preauthorized, reusable, non-ephemeral Tailscale key with a 24-hour lifetime and builds one ISO with the pinned upstream Talos imager. Reuse that same ISO on multiple laptops during the registration window; the command prints its actual expiry. The boot configuration contains only a generated hostname, Tailscale registration and ingress rules: no disk-install instruction, cluster join credentials or machine identity.

The pinned Tailscale extension normally waits for CRI and stores state under `/var/lib`, which are unavailable in maintenance mode. For this boot ISO only, the build exports the pinned upstream extension with Docker, removes the CRI dependency and persistent-state mount, and uses its existing `/var/run/tailscale` mount for RAM state. Its binaries and other settings stay upstream. The imager consumes this local tar archive through its native `tarballPath` input. Installed workers use the ordinary upstream Tailscale-enabled installer with persistent state. Docker removes the stopped export container afterward.

Outputs are under `installer/` in the operator directory: `tailscale-key.json` records the batch key, `boot.yaml` contains the maintenance configuration, and `image/` holds the ISO and protected logs. Files use mode 0600 in mode-0700 directories. Keep the ISO private; it authorizes new Tailscale registrations during its window. It must never be published or uploaded as a CI artifact.

Existing installer outputs are never overwritten. If only `tailscale-key.json` was saved, rerun `./guaranine iso` to reuse that unexpired key with its original expiry. After a later build failure, inspect protected logs and revoke the issued key before removing `installer/` and retrying. To start another batch, revoke the old key and archive or remove its `installer/` directory before running `./guaranine iso` again. Revoking or expiring an auth key does not remove devices already registered with it.

## Add a laptop

Write the shared ISO to a USB drive and boot a laptop with Ethernet or USB Ethernet and internet access. The laptop stays in maintenance mode; booting the ISO does not authorize installation or erasure. Its host firewall permits the maintenance API only from the Tailscale range, and the tailnet policy limits who can reach it. An existing installed Talos system causes this ISO to halt rather than reuse its disks automatically.

Run this on the operator computer for each laptop:

```bash
./guaranine worker
```

The command asks for the laptop's Tailscale IPv4 address and the Talos certificate fingerprint shown on its screen. It pins that certificate for every native maintenance API request. Choose a unique lowercase worker name, then select a disk from the laptop's detected model, size, serial, transport and device path. No disk is preselected. The command displays the name, laptop address and disk, then requires the exact displayed `ERASE NAME /dev/DISK` phrase. Cancelling before that confirmation sends no installation configuration. The selected disk's contents will be destroyed when the confirmed configuration is applied.

After confirmation, the command checks the disk again, generates and validates the native worker configuration using the existing cluster secrets and batch key, and applies it with `talosctl`. The chosen name becomes both the Kubernetes node name and Tailscale hostname. Configuration is saved privately under `nodes/NAME/config/machine.yaml`. Worker profiles contain neither the operator API credential, an admin `talosconfig` nor cluster signing keys. Existing worker output is never overwritten.

The command reports that configuration was sent, not that enrollment succeeded. Remove the USB when the laptop reboots and find its installed identity in Tailscale; its address can change when the in-memory registration is replaced by persistent state. Verify `kubectl get node NAME -o wide` reports Ready and its address is in the Tailscale range. Remove a leftover offline installer identity only after identifying it separately from the installed worker. If applying configuration reports a connection failure, inspect the laptop before retrying: the installation might already have started.

Installed workers normally retain their identity across reboots and do not need another ISO. Physical installation, identity handoff and operation across laptop locations still need qualification.

## Maintain and recover

All private inter-host traffic uses Tailscale, including relayed paths. Node addresses are restricted to `100.64.0.0/10`. Flannel binds to `tailscale0`, using VXLAN UDP 4789 and tunnel MTU 1280 before encapsulation overhead. Talos API TCP 50000, trust API 50001, Kubernetes API 6443 and kubelet TCP 10250 accept tailnet traffic. The control plane also permits TCP 6443 from the default pod subnet `10.244.0.0/16` so CoreDNS and workloads can use the Kubernetes API through its ClusterIP. Remote pod traffic reaches the host through Flannel over Tailscale. Other host ingress is blocked; workloads still need appropriate NetworkPolicies.

The official Tailscale extension retains its identity, uses kernel networking, and changes neither host DNS nor accepted subnet routes. Alternate API endpoint selection and external cluster discovery are disabled. Test direct paths, relays, reboots and temporary disconnections on the real network.

Use `kubectl cordon` and `kubectl drain --ignore-daemonsets` before worker removal. Wait for replacements to become ready; resolve capacity, disruption-budget and storage blockers. Fence a retired worker before deleting its Node and Tailscale identity. WAN disconnection is not fencing. Use native `talosctl` upgrades with the matching Tailscale-enabled installer.

Back up etcd with `talosctl etcd snapshot`, cluster secrets, operator files and Terraform state outside the student subscription before replacement or credit expiry. The etcd disk has `prevent_destroy`; VM replacement must retain it. Never bypass retention by deleting state or targeting resources. Deliberate disk removal needs explicit scope, verified backups and fencing. Disks, images and IPs continue costing money after a VM is stopped.

For cluster recovery, fence the old control plane and follow [native etcd recovery](https://docs.siderolabs.com/talos/v1.14/learn-more/control-plane). Fresh Tailscale credentials may be needed. A retained disk is not a backup and does not automatically restore a cluster. If Terraform state is lost, stop writers and restore its off-machine backup before planning; import only verified Guaranine resources if no backup exists.

## Enrollment security status

Each laptop generates its own identity; registered devices remain authorized after the auth key expires. A maintenance-mode device already connected before expiry can also remain connected afterward.

**The full policy is unfinished.** Tailscale registration expiry is separate from Talos cluster join authority. The command checks the saved batch expiry before sending a new worker configuration, but a local clock check cannot securely expire copied worker configuration or already-issued cluster credentials. A copied ISO can register additional devices during the window; it is not hardware-bound. YubiKey boot authorization is not implemented. The ISO itself stays copyable and bootable after 24 hours.

## Hardware and validation

The pinned AMD64 release needs x86-64-v2, compatible storage/NIC hardware and at least 1 GiB RAM for a worker before application capacity. Physical laptops, Wi-Fi/firmware, lid/power behavior and live WAN operation still need qualification.

Run repository validation as described in [README.md](README.md#development). It validates the maintenance configuration and both native Talos roles, restricts admin output to the control plane and tests the name/disk prompts, cancellation, certificate pinning, credentials, workflows, shell and credential-free Terraform plans and lifecycle. The [private-image workflow](.github/workflows/private-images.yml) builds shared-ISO/VHD fixtures with disposable keys, boots the same ISO in two isolated QEMU guests, then discards the outputs. It checks maintenance mode, Tailscale kernel-network startup, distinct certificates and unchanged guest disks. Synthetic guest networking and invalid keys exercise startup without authorizing a real tailnet device. These are construction and maintenance-boot tests, not proof of successful installation or real enrollment.

Update [Talos pins](talos/versions.json) from official checksums, extension metadata and OCI digests, then run repository validation and both private builds. Keep [the public schematic](talos/schematic.yaml) for the Tailscale-enabled installer referenced by generated profiles; it contains only the extension name. Private configuration goes to the local imager through stdin, never to public Image Factory. There is no public ISO publishing workflow or published installer release.
