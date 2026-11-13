# Guaranine setup

Run these commands from the repository root on your Linux operator computer. See the [documentation index](docs/README.md) for background and ongoing operations.

Use the arrow keys and Enter in selection menus. Cancel or Skip is selected initially.

## Prerequisites

Install Git, Python 3.12+, `uv`, Docker, `sudo`, OpenSSH client tools, Tailscale and `nmcli`. Building images needs privileged Docker access and space for uncompressed disk images. USB flashing uses sudo. Azure needs the Terraform version in [.terraform-version](.terraform-version), Azure CLI, AzCopy and `qemu-img`. Use a `kubectl` version matching K3s in [kairos/versions.json](kairos/versions.json).

Before building laptop media, read the [hardware inventory, network requirements and qualification limits](docs/hardware.md). Before replacing an existing deployment, complete the [migration prerequisites](docs/operations.md#migrate-an-older-deployment).

## Operator files

Use [protected storage](docs/security.md#private-material-and-enrollment). The default is `~/.local/state/guaranine-kairos`; set `GUARANINE_OPERATOR_DIR` to use another directory outside the checkout. Initialization refuses to overwrite an existing directory.

```bash
umask 077
export GUARANINE_OPERATOR_DIR="${GUARANINE_OPERATOR_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/guaranine-kairos}"
./guaranine init
```

Initialization creates an SSH key pair, separate K3s server/agent tokens and `operator.yaml`. Fill in `operator.yaml`:

```yaml
tailnet: YOUR_TAILNET
tailscale_api_token: YOUR_PRIVATE_TAILSCALE_API_TOKEN
endpoint: null
```

Leave `endpoint` null; bootstrap records the verified controller address.

## Tailnet access

Apply the [tailnet policy](docs/security.md#tailnet-policy) before starting nodes. Connect the operator to that tailnet; preserve its routes if another VPN is active.

## Cloud preflight

Before provisioning, check your Azure for Students subscription, remaining credit, expiry, spending limit, region/SKU availability, quota and total cost. Do not apply with unknown credit/cost/quota or use an automatic paid fallback. Keep existing Azure services separate.

Check the [controller resources](docs/architecture.md#controller-resources) and your Terraform variables. Include compute, both disks, image/upload storage, public IPv4 and outbound traffic. Stopped VMs can still incur disk/image/IP costs.

## Controller image and Terraform

```bash
./guaranine controlplane
```

The VHD is in `nodes/guaranine-controlplane/image/` under the operator directory. Check its fixed-VHD format and record its byte length and SHA-256:

```bash
controller_vhd="$GUARANINE_OPERATOR_DIR/nodes/guaranine-controlplane/image/guaranine-kairos.vhd"
qemu-img info -f vpc "$controller_vhd"
stat --format=%s "$controller_vhd"
sha256sum "$controller_vhd"
```

For incomplete or expired builds, follow [image batches and retries](docs/operations.md#image-batches-and-retries).

Copy the [image example](infra/azure/image/terraform.tfvars.example) to `$GUARANINE_OPERATOR_DIR/image.tfvars` and the [coordinator example](infra/azure/coordinator/terraform.tfvars.example) to `$GUARANINE_OPERATOR_DIR/coordinator.tfvars`. Fill in your values. For upload, use `image_ready = false`, the VHD's actual size/hash and a unique `guaranine-kairos-...` name. Changed image contents need their own state file.

Keep local Terraform state on protected operator storage with [off-machine backups](docs/operations.md#backup). Use one operator at a time. Review every saved plan before applying it.

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

The first apply creates upload staging. Get `upload_disk_id` and follow the [Azure direct-upload procedure](https://learn.microsoft.com/azure/virtual-machines/linux/disks-upload-vhd-to-managed-disk-cli): grant a short-lived write SAS, upload the exact VHD with AzCopy, then revoke disk access in an `EXIT` trap, including on failure. Keep shell tracing off and AzCopy logs/job plans private. Keep SAS values and image contents out of Terraform state.

Set `image_ready = true` and repeat plan/show/apply to close export access. Copy the `source_disk_id` output to `coordinator.tfvars`; use `name_prefix = "guaranine-kairos"` for the new deployment.

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

Confirm the final plan shows no changes. Keep the `handoff` output with the VM and disk IDs.

## Bootstrap the controller

After the VM registers with Tailscale, verify its tailnet IPv4 address and run:

```bash
./guaranine bootstrap
```

Compare the displayed SSH fingerprint with the protected VM console and enter `yes` if they match. For a new deployment, choose the NEW EMPTY retained data disk and type `INITIALIZE /dev/DISK` using its device path. Disks with filesystem signatures are refused. For an existing cluster, follow the [restore procedure](docs/operations.md#restore-on-another-cloud-vm) to reuse the retained/restored `GUARANINE_DATA` volume without formatting it.

Bootstrap waits for Ready, saves the kubeconfig and pinned server host key, and records the Tailscale endpoint in `operator.yaml`.

```bash
export KUBECONFIG="$GUARANINE_OPERATOR_DIR/kubeconfig"
kubectl get nodes -o wide
```

## Shared laptop ISO

Connect the operator to the laptops' Wi-Fi network, with exactly one active NetworkManager Wi-Fi connection. Run:

```bash
./guaranine iso
```

Open, WPA personal and WPA3 personal networks are supported. Enterprise, missing or ambiguous networks fail before media creation. Only the SSID/authentication is copied. Each laptop uses DHCP, without the operator's MAC, interface name, IP address or access-point binding.

The CLI prints the ISO path and enrollment deadline in local time. Enroll before that deadline and follow the [media handling limits](docs/security.md#private-material-and-enrollment). To change networks or reuse a batch, follow [image batches and retries](docs/operations.md#image-batches-and-retries).

Choose a USB, checking its path, model, size, serial and label, or select **Skip flashing** to keep the ISO without writing a USB. Type `ERASE` to confirm. The CLI rechecks and unmounts the drive, writes with progress, then flushes it. Do not remove the USB until `Flashed` appears. System mounts, active encrypted/LVM volumes and the ISO's own disk are excluded. Redirected runs do not offer flashing.

## Install a worker

Boot the USB. Wi-Fi connects automatically; the laptop stays live until you install it. Run on the operator:

```bash
./guaranine worker
```

Choose **Add worker**. The command finds the laptop. If several are online, select the laptop to install. Compare the displayed SSH fingerprint with the one on the laptop screen and enter `yes` if they match. Remote commands use that pinned host key and the operator's SSH key.

Press Enter to accept the suggested name, or type your own. Select an installation disk, checking its model, size and serial. It must be unmounted, writable and at least 16 GiB. Type `ERASE` to erase that disk and install. Cancelling before confirmation sends no installation configuration.

The CLI rechecks the disk and batch expiry, then sends the configuration to `kairos-agent --strict-validation manual-install`. The name, join credentials and Wi-Fi settings are saved privately under `nodes/NAME/`.

When the laptop powers off, remove the USB and turn it on. Leave the command running; it checks the controller and reports Ready with the laptop's persistent Tailscale address. If installation reports a connection error, disk changes may have started. Check the laptop before retrying.

If the Ready check stops or times out, resume without reinstalling:

```bash
./guaranine worker --wait YOUR_LAPTOP_NAME
```

See [operations](docs/operations.md) for maintenance, backups and recovery, or [development](docs/development.md) for validation.
