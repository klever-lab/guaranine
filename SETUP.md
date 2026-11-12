# Guaranine setup

The cloud controller and laptop workers use the same Kairos/Hadron base. One K3s server uses SQLite on a retained data disk; laptop agents connect over Tailscale. Commands run from the repository root on the Linux operator computer.

## Operator files

Install `uv`, Docker and `sudo`; image building needs a privileged Docker container. USB flashing also uses sudo. Install `nmcli` on the operator to capture its active Wi-Fi connection. Azure operations use the pinned Terraform version, Azure CLI, AzCopy and `qemu-img`; cluster checks use a matching `kubectl`.

Keep private files on encrypted storage with an off-machine backup. The default is `~/.local/state/guaranine-kairos`; `GUARANINE_OPERATOR_DIR` may select another directory outside the checkout. Existing operator files are never overwritten.

```bash
umask 077
export GUARANINE_OPERATOR_DIR="${GUARANINE_OPERATOR_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/guaranine-kairos}"
./guaranine init
```

Initialization creates an SSH key pair, separate K3s server/agent tokens and `operator.yaml`. Directories are mode 0700 and private files mode 0600. Fill in:

```yaml
tailnet: YOUR_TAILNET
tailscale_api_token: YOUR_PRIVATE_TAILSCALE_API_TOKEN
endpoint: null
laptop_models: []
```

The API token stays on the operator. Set laptop models after checking their driver/firmware coverage; the physical ISO command refuses an empty list. Model variants still need a boot test. The image carries upstream firmware for common Intel, Atheros/Qualcomm, Realtek, MediaTek and Broadcom families; adding firmware cannot supply a missing kernel driver.

## Migrating the existing controller

Create a fresh K3s cluster with a new `guaranine-kairos` resource prefix and fresh Terraform state. Preserve the existing deployment, state, cluster secrets, boot/data disks and an off-machine datastore backup for rollback. Do not point the new configuration at the old state or format its data disk. Kubernetes manifests and application data need explicit migration; an OS image replacement does not migrate a cluster.

Fence the previous controller before restoring its cluster elsewhere. Keep its recovery copy until the replacement and backups are verified. Cleanup of retained disks requires deliberate scope and verified backups; never bypass retention by deleting Terraform state or targeting resources.

## Tailscale policy

[tailscale/policy.json](tailscale/policy.json) owns the complete policy for this tailnet, including existing personal access and Funnel rules. Back up the live policy outside the checkout before updating it through the standard [Access controls editor](https://console.tailscale.com/admin/acls). For API updates, validate first, use the live ETag with `If-Match`, and read back the result. CI checks syntax without credentials and never applies live policy.

Only admins own the node tags. Workers reach the server on TCP 6443; the server reaches kubelets on TCP 10250. Both roles carry Flannel VXLAN on UDP 8472. Operator SSH uses TCP 22 over Tailscale with ordinary SSH keys and physically verified host fingerprints. Nodes cannot administer peers. Private Azure/LAN addresses are never cluster endpoints. Other operator VPNs must preserve the `100.64.0.0/10` routes.

## Cloud preflight

Verify the selected Azure for Students subscription, remaining credit, expiry, spending limit, region/SKU availability, quota and complete cost before provisioning. Do not apply with unknown credit/cost/quota or select an automatic paid fallback. Keep existing Azure services separate.

The Terraform default is `Standard_B2ls_v2` (2 vCPUs/4 GiB), a 32-GiB OS SSD and a retained 64-GiB data SSD at LUN 0. Account for compute, both disks, image/upload storage, public IPv4 and outbound traffic. Stopped VMs can still incur disk/image/IP costs.

## Controller image and Terraform

```bash
./guaranine controlplane
```

This builds the shared public base before issuing a single-use Tailscale registration key valid for at most 24 hours. Private configuration is fed to pinned AuroraBoot through stdin, outside Docker layers, git and public image services. The VHD appears under `nodes/guaranine-controlplane/image/` in the operator directory. Rerun to reuse a completed image while its key is unexpired. Incomplete outputs are preserved with protected logs for inspection.

Check the fixed VHD with `qemu-img info -f vpc`, and record its exact byte length and SHA-256. Build scratch space must accommodate uncompressed disk images. The prepared Gen2 disk boots without Secure Boot/vTPM, Azure guest provisioning or a VM agent. Native Kairos upgrades own the installed OS.

The [image root](infra/azure/image/main.tf) owns an artifact-specific upload group/disk and private managed image. The [coordinator root](infra/azure/coordinator/main.tf) owns the VM, isolated network, boot disk and protected data disk. Both have one `main.tf` and pinned provider locks. Use native local state on protected operator storage, one operator at a time, with off-machine backups after every apply.

Copy each root's `terraform.tfvars.example` outside the checkout and fill in real values. For image upload, use `image_ready = false`, the VHD size/hash and a unique `guaranine-kairos-...` name. New image contents require their own state file.

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

Review the saved plan before applying. This first apply creates only upload staging. Obtain `upload_disk_id` and follow the [Azure direct-upload procedure](https://learn.microsoft.com/azure/virtual-machines/linux/disks-upload-vhd-to-managed-disk-cli): grant a short-lived write SAS, upload the exact VHD with AzCopy, then revoke disk access in an `EXIT` trap, including on failure. Keep shell tracing off and AzCopy logs/job plans private. SAS values and image contents never enter Terraform state.

Set `image_ready = true` and repeat plan/show/apply to close export access and create the image. Put the `image_id` output into `coordinator.tfvars`; use `name_prefix = "guaranine-kairos"` for the migration.

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

Require a no-change rerun. The NSG denies physical inbound access and direct VNet egress; its public IP provides explicit internet egress for Tailscale/DERP and downloads. VM replacement retains both disks. The `handoff` output records IDs and the data-disk LUN. Changing the image input does not reinstall an existing boot disk.

## Bootstrap the controller

After the VM registers with Tailscale, find its verified tailnet IPv4 address. Run:

```bash
./guaranine bootstrap
```

Compare the SSH fingerprint with protected VM console output. For a new deployment, explicitly choose the NEW EMPTY retained data disk and confirm initialization. Existing filesystem signatures are refused. Recovery uses the retained/restored `GUARANINE_DATA` volume without formatting it. The K3s service requires that mount and fails closed when it is absent.

Bootstrap waits for the API, saves a private kubeconfig and pinned server host key, and records the Tailscale endpoint in `operator.yaml`.

```bash
export KUBECONFIG="$GUARANINE_OPERATOR_DIR/kubeconfig"
kubectl get nodes -o wide
```

The server is tainted against ordinary application placement. Workers receive the secure CA-pinned agent token, never the server token. Traefik, ServiceLB, local-path storage, metrics-server and Gateway API CRDs are disabled; add workloads only through repository manifests when needed.

## Shared laptop ISO

Connect the operator to the Wi-Fi network the laptops should use, then run:

```bash
./guaranine iso
```

The CLI captures exactly one active NetworkManager Wi-Fi connection, with open, WPA personal or WPA3 personal authentication. It copies the SSID/authentication only, using DHCP on each laptop; it does not copy the operator's MAC, interface name, IP address or access-point binding. Enterprise credentials and ambiguous/missing networks fail before media creation. Passwords are captured privately, never printed or placed in command arguments.

The image uses only `wpa_supplicant` and the existing network service. It has no NetworkManager, Wi-Fi management CLI or supplicant control socket. Wi-Fi configuration is carried from the live profile into the installed configuration. Ordinary discovery/reconnection still requires Wi-Fi scanning. The controller uses the same OS base with its cloud NIC and no Wi-Fi profile.

A reusable, preauthorized, non-ephemeral Tailscale key allows registrations for at most 24 hours. The command prints the ISO path and expiry in local time. The ISO has no K3s token or installation target. Files under `installer/` are private; media containing a protected Wi-Fi profile also carries that network credential. Never publish images or upload them as CI artifacts.

Interactive terminals offer a numbered USB menu. Enter skips it. Choose by path/model/size/serial/label and type the exact `ERASE /dev/DEVICE` phrase. The CLI rechecks the drive, unmounts it, writes the image with progress and flushes it. System mounts, active encrypted/LVM volumes and the ISO's own disk are excluded. Redirected runs never prompt for flashing.

Completed images are reused with their original network/profile while the registration key remains valid. To change the network or start another batch, revoke the previous key and archive its installer directory. Key-only retries reuse the same key. After a partial build, inspect protected logs before archiving outputs and retrying. Expiring/revoking a key does not remove already registered devices.

## Install a worker

Boot the shared USB. Wi-Fi connects automatically and the laptop stays live until explicitly installed. Run on the operator:

```bash
./guaranine worker
```

Enter its Tailscale IPv4 and the SSH fingerprint displayed on its console. Every remote command uses that pinned host key and the operator's SSH key. Choose a unique worker name and an unmounted, writable disk of at least 16 GiB. There is no default disk. The exact `ERASE NAME /dev/DISK` confirmation authorizes erasure; cancellation before it sends no installation configuration.

The CLI rechecks the disk and batch expiry, fetches the secure K3s agent token over pinned server SSH and invokes native `kairos-agent manual-install`. The root filesystem comes from the live media, so installation does not need a public image registry or LAN provisioning service. Name, join credentials and the captured Wi-Fi configuration are saved privately under `nodes/NAME/`.

After installation powers off, remove the USB and boot the laptop. Its persistent Tailscale identity can have a different address from the live session. Require `kubectl get node NAME -o wide` to report Ready with a Tailscale address. A transport error during installation can occur after disk changes started; inspect the laptop before retrying.

## Networking and workload security

Tailscale uses kernel networking, persistent identity, no accepted subnet routes and no host DNS changes. K3s binds node addresses and Flannel to `tailscale0`; Flannel derives its MTU from that interface. Administration, API, kubelet and cross-node pod traffic use the tailnet. Test direct/relayed paths, reconnects and temporary outages on the real network.

The host firewall blocks physical administration and direct private-LAN/VNet traffic, with required DHCP/DNS and IPv6 control exceptions. Its forwarding rule also blocks pod traffic to private physical networks. Workloads use the standard restricted Pod Security policy: no privileged pods or host namespaces, no privilege escalation, and dropped capabilities. `kube-system` is reserved for trusted system components.

[kubernetes/network-policy.yaml](kubernetes/network-policy.yaml) isolates the default application namespace and permits cluster DNS. Add explicit ingress/egress rules alongside applications. New application namespaces need their own policies. Allowed destinations remain reachable by compromised applications; firewall rules and removing utilities cannot guarantee safety after host-root or kernel compromise.

Use `kubectl cordon` and `kubectl drain --ignore-daemonsets` before removal. Wait for replacements and resolve capacity, disruption-budget and storage blockers. Fence retired machines before deleting their Kubernetes/Tailscale identities. A disconnected machine may still be running; WAN disconnection is not fencing.

## Backup, restore and OS updates

Use [native K3s SQLite backup/restore](https://docs.k3s.io/datastore/backup-restore), including the server token. For a consistent full server-directory copy, briefly stop K3s, archive `server/` from the mounted `/var/lib/rancher/k3s`, and restart it in an `EXIT` trap. Stream the archive over pinned Tailscale SSH into a mode-0600 operator file. Back up operator configuration/keys, Terraform state and application volumes separately, outside the student subscription.

For recovery on another cloud VM: fence the old server, boot the same prepared Kairos base with fresh network registration if required, attach an empty retained disk, restore the server directory with K3s stopped, then start K3s. Preserve the server token from the backup. Verify API health, worker connectivity and application data; update the kubeconfig endpoint and private operator endpoint for the replacement address. A retained disk is not an off-machine backup.

Use native `kairos-agent upgrade` with the tested image produced from this recipe and a pinned digest or private local image. Keep secrets in OEM configuration, outside the public base. Verify active/passive rollback and persistent data after upgrades; do not use Terraform image changes as an OS upgrade mechanism. Secure Boot, TPM-bound enrollment and YubiKey boot authorization require separate qualification and are not implemented here.

## Validation and enrollment limits

Run the checks in [README.md](README.md#development). They validate all native Kairos profiles, private-file handling, role credentials, disk confirmations, workflow/shell syntax and credential-free Terraform lifecycle plans. The private-image workflow builds disposable ISO/VHD fixtures and boots isolated QEMU guests; no real enrollment credentials or images are published.

Laptop model coverage, physical Wi-Fi, lid/suspend behavior and real WAN recovery need hardware tests. The shared image remains copyable and bootable after the registration window. A local expiry check cannot revoke copied join credentials or already authorized nodes, and the ISO is not hardware-bound. Keep operational claims limited to checks actually completed.
