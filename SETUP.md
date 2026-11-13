# Guaranine setup

Run these commands from the repository root on a Linux operator computer. Use the [documentation index](docs/README.md) for explanations and ongoing operations.

## Prerequisites

Install Python 3.12+, `uv`, Docker, `sudo`, OpenSSH client tools, Tailscale and `nmcli`. Image building needs privileged Docker access and scratch space for uncompressed disk images; USB flashing also uses sudo. Azure operations need the Terraform version in [.terraform-version](.terraform-version), Azure CLI, AzCopy and `qemu-img`. Cluster checks use a `kubectl` version matching the pinned K3s release in [kairos/versions.json](kairos/versions.json).

Before building laptop media, read the [hardware inventory, network requirements and qualification limits](docs/hardware.md). For an existing deployment, complete the [migration prerequisites](docs/operations.md#migrate-an-older-deployment) before provisioning a replacement.

## Operator files

Choose protected storage as described in [private material handling](docs/security.md#private-material-and-enrollment). The default operator directory is `~/.local/state/guaranine-kairos`; `GUARANINE_OPERATOR_DIR` may select another directory outside the checkout. Initialization creates a new directory and refuses to overwrite an existing one.

```bash
umask 077
export GUARANINE_OPERATOR_DIR="${GUARANINE_OPERATOR_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/guaranine-kairos}"
./guaranine init
```

Initialization creates an SSH key pair, separate K3s server/agent tokens and `operator.yaml`. Fill in the private `operator.yaml`:

```yaml
tailnet: YOUR_TAILNET
tailscale_api_token: YOUR_PRIVATE_TAILSCALE_API_TOKEN
endpoint: null
```

Leave `endpoint` null until bootstrap records the verified controller address.

## Tailnet access

Apply the [tailnet policy procedure](docs/security.md#tailnet-policy) before starting nodes. Connect the operator to that tailnet and preserve its routes if another VPN is active.

## Cloud preflight

Verify the selected Azure for Students subscription, remaining credit, expiry, spending limit, region/SKU availability, quota and complete cost before provisioning. Do not apply with unknown credit/cost/quota or select an automatic paid fallback. Keep existing Azure services separate.

Check the [controller resources](docs/architecture.md#controller-resources) and your actual Terraform variables. Account for compute, both disks, image/upload storage, public IPv4 and outbound traffic. Stopped VMs can still incur disk/image/IP costs.

## Controller image and Terraform

```bash
./guaranine controlplane
```

The VHD appears under `nodes/guaranine-controlplane/image/` in the operator directory. Check its fixed-VHD format and record the exact byte length and SHA-256:

```bash
controller_vhd="$GUARANINE_OPERATOR_DIR/nodes/guaranine-controlplane/image/guaranine-kairos.vhd"
qemu-img info -f vpc "$controller_vhd"
stat --format=%s "$controller_vhd"
sha256sum "$controller_vhd"
```

For incomplete or expired outputs, follow [image batches and retries](docs/operations.md#image-batches-and-retries) before building again.

Copy the [image example](infra/azure/image/terraform.tfvars.example) to `$GUARANINE_OPERATOR_DIR/image.tfvars` and the [coordinator example](infra/azure/coordinator/terraform.tfvars.example) to `$GUARANINE_OPERATOR_DIR/coordinator.tfvars`; fill in real values. For image upload, use `image_ready = false`, the actual VHD size/hash and a unique `guaranine-kairos-...` name. New image contents require their own state file.

Use native local state on protected operator storage, one operator at a time, with [off-machine backups](docs/operations.md#backup). Review every saved plan before applying it.

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

This first apply creates only upload staging. Obtain `upload_disk_id` and follow the [Azure direct-upload procedure](https://learn.microsoft.com/azure/virtual-machines/linux/disks-upload-vhd-to-managed-disk-cli): grant a short-lived write SAS, upload the exact VHD with AzCopy, then revoke disk access in an `EXIT` trap, including on failure. Keep shell tracing off and AzCopy logs/job plans private. SAS values and image contents never enter Terraform state.

Set `image_ready = true` and repeat plan/show/apply to close export access. Put the `source_disk_id` output into `coordinator.tfvars`; use `name_prefix = "guaranine-kairos"` for the new deployment.

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

Require a no-change rerun. Keep the `handoff` output for the VM and disk IDs.

## Bootstrap the controller

After the VM registers with Tailscale, find its verified tailnet IPv4 address. Run:

```bash
./guaranine bootstrap
```

Compare the displayed SSH fingerprint with protected VM console output and enter `yes` if they match. For a new deployment, explicitly choose the NEW EMPTY retained data disk and type `INITIALIZE /dev/DISK` for the selected device. Existing filesystem signatures are refused. Recovery reuses the retained/restored `GUARANINE_DATA` volume without formatting it; follow the [restore procedure](docs/operations.md#restore-on-another-cloud-vm) for an existing cluster.

Bootstrap waits for the node to become Ready, saves a private kubeconfig and pinned server host key, and records the Tailscale endpoint in `operator.yaml`.

```bash
export KUBECONFIG="$GUARANINE_OPERATOR_DIR/kubeconfig"
kubectl get nodes -o wide
```

## Shared laptop ISO

Connect the operator to exactly one active NetworkManager Wi-Fi connection on the network the laptops should use, then run:

```bash
./guaranine iso
```

Capture supports open, WPA personal or WPA3 personal authentication. Enterprise credentials and ambiguous/missing networks fail before media creation. Only the SSID/authentication is copied; each laptop uses DHCP, without the operator's MAC, interface name, IP address or access-point binding.

The CLI prints the ISO path and registration expiry in local time. Complete enrollment before that deadline; read the [enrollment limits](docs/security.md#private-material-and-enrollment) when handling the media. Changing a network or reusing a batch follows the [image retry procedure](docs/operations.md#image-batches-and-retries).

Interactive terminals offer a numbered USB menu. Enter skips it. Choose by path/model/size/serial/label and type `ERASE` to confirm that drive. The CLI rechecks the drive, unmounts it, writes the image with progress and flushes it. System mounts, active encrypted/LVM volumes and the ISO's own disk are excluded. Redirected runs never prompt for flashing.

## Install a worker

Boot the shared USB. Wi-Fi connects automatically and the laptop stays live until explicitly installed. Run on the operator:

```bash
./guaranine worker
```

The command finds the laptop automatically. If several are online, choose its number. Compare the displayed SSH fingerprint with the one on the laptop screen and enter `yes` if they match; no fingerprint transcription is required. Every remote command uses that pinned host key and the operator's SSH key.

Press Enter to accept the suggested laptop name, or type your own. Choose its installation disk by number, checking the displayed model, size and serial. The disk must be unmounted, writable and at least 16 GiB. There is no default disk. Type `ERASE` to authorize erasing the selected disk; cancellation before it sends no installation configuration.

The CLI rechecks the disk and batch expiry, then streams the approved configuration to native `kairos-agent --strict-validation manual-install`. Name, join credentials and captured Wi-Fi configuration are saved privately under `nodes/NAME/`.

When the laptop powers off, remove the USB and turn it on again. Leave the command running: it checks the controller and reports when the worker is Ready, including its persistent Tailscale address. No separate `kubectl` or SSH command is required for this check. A transport error during installation can occur after disk changes started; inspect the laptop before retrying.

If the final check was interrupted or timed out, resume verification without reinstalling:

```bash
./guaranine worker --wait YOUR_LAPTOP_NAME
```

Continue with [operations](docs/operations.md) for maintenance, backups and recovery, or [development](docs/development.md) for validation.
