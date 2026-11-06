# Azure control plane

Terraform owns one planned Talos VM and its private image artifacts. Nothing is provisioned. Existing Azure services and the shared Clusterfork Symphony controller are independent. No additional server is part of this setup.

The default VM is `Standard_B2ls_v2` (2 vCPUs/4 GiB), with a 32-GiB disposable OS SSD and a separate retained 64-GiB etcd SSD at LUN 0. All private machine traffic uses Tailscale. The NSG denies physical inbound connections and direct VNet egress; the public IP supplies explicit internet egress for Tailscale/DERP and image downloads.

## Before provisioning

Verify the selected Azure for Students subscription, region/SKU availability, vCPU quota, remaining credit, expiry and complete cost. Include the VM, both SSDs, image/upload storage, public IPv4 and outbound traffic. Keep the spending limit enabled. Do not apply when credit, quota or cost is unknown; choose no automatic paid fallback. Confirm Compute/Network provider registration and resource permissions.

Use an existing Azure CLI login for that subscription. Terraform uses the pinned CLI/provider and creates resources only from a reviewed saved plan. See [Azure for Students](https://azure.microsoft.com/free/students/) and [credit expiry/exhaustion](https://learn.microsoft.com/azure/cost-management-billing/manage/azurestudents-subscription-disabled).

## Operator files and state

First run `./guaranine init` as described in the [Talos guide](../../talos/README.md). Keep its directory on encrypted operator storage with an off-machine backup. For the commands below, use that same directory:

```bash
umask 077
export GUARANINE_OPERATOR_DIR="${GUARANINE_OPERATOR_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/guaranine}"
mkdir -p "$GUARANINE_OPERATOR_DIR/terraform"
```

There are only two Terraform roots: `image/` for an image upload, and `coordinator/` for the VM. Each uses native local state with an explicit path outside git. No Blob account, backend service, RBAC bootstrap or state migration is needed. Run one operator at a time and keep native state locking enabled. Back up state after every apply; encrypted local storage and off-machine backups are prerequisites. CI only runs credential-free mocks.

Copy each root's `terraform.tfvars.example` into this private directory and fill in the real subscription/region/image identifiers. Keep plans, state, caches, keys and build outputs there. Never create resources in another service's resource group.

## Upload the private control-plane image

Prepare the VHD with `./guaranine controlplane` from the Talos guide. Decompress its `.vhd.zst` privately with `zstd --decompress`. Check the fixed VHD with `qemu-img info`; record its uncompressed file size and SHA-256 in `image.tfvars`. The image root appends the hash to its name. Use a separate state path for each new image's contents.

For this first image:

```bash
export TF_DATA_DIR="$GUARANINE_OPERATOR_DIR/terraform/image-cache"
terraform -chdir=infra/azure/image init -lockfile=readonly \
  -backend-config="path=$GUARANINE_OPERATOR_DIR/terraform/image.tfstate"
terraform -chdir=infra/azure/image plan -var-file="$GUARANINE_OPERATOR_DIR/image.tfvars" \
  -out="$GUARANINE_OPERATOR_DIR/image.tfplan"
terraform -chdir=infra/azure/image show "$GUARANINE_OPERATOR_DIR/image.tfplan"
terraform -chdir=infra/azure/image apply "$GUARANINE_OPERATOR_DIR/image.tfplan"
```

Start with `image_ready = false`, which creates only the Gen2 upload disk. Obtain its ID with `terraform -chdir=infra/azure/image output -raw upload_disk_id`. Follow the [official Azure direct-upload procedure](https://learn.microsoft.com/azure/virtual-machines/linux/disks-upload-vhd-to-managed-disk-cli): grant a short-lived write SAS, copy the exact VHD with AzCopy, and revoke disk access in an `EXIT` trap even if copying fails. Keep tracing off and logs/job plans private. Neither the SAS nor image contents enter Terraform inputs/state.

After successful upload and revocation, set `image_ready = true` and repeat plan/show/apply. This closes disk export access and publishes the private managed image. Put its `talos_image_id` output into `coordinator.tfvars`. This one-time Azure upload remains operator work; it has not been live-qualified.

## Create the control plane

Azure requires a public SSH key as VM provisioning metadata even though Talos has no SSH daemon or users. Supply only that public key. The VM already has its private machine configuration embedded; there is no cloud-init, custom data, VM agent or extension service. This image format uses ordinary Gen2 boot with Secure Boot/vTPM disabled.

```bash
export TF_DATA_DIR="$GUARANINE_OPERATOR_DIR/terraform/coordinator-cache"
export TF_VAR_admin_ssh_public_key="$(cat "$GUARANINE_OPERATOR_DIR/azure-api-metadata.pub")"
terraform -chdir=infra/azure/coordinator init -lockfile=readonly \
  -backend-config="path=$GUARANINE_OPERATOR_DIR/terraform/coordinator.tfstate"
terraform -chdir=infra/azure/coordinator plan -var-file="$GUARANINE_OPERATOR_DIR/coordinator.tfvars" \
  -out="$GUARANINE_OPERATOR_DIR/coordinator.tfplan"
terraform -chdir=infra/azure/coordinator show "$GUARANINE_OPERATOR_DIR/coordinator.tfplan"
terraform -chdir=infra/azure/coordinator apply "$GUARANINE_OPERATOR_DIR/coordinator.tfplan"
terraform -chdir=infra/azure/coordinator plan -var-file="$GUARANINE_OPERATOR_DIR/coordinator.tfvars" -detailed-exitcode
```

Require a no-change rerun. Find the VM's actual Tailscale address and follow [Talos bootstrap](../../talos/README.md#bootstrap-the-control-plane-once). Azure public/private IPs are never cluster endpoints. The `talos_handoff` output records VM/image/disk identifiers and LUN, not a fabricated tailnet address.

## Recovery and removal

Back up etcd, cluster secrets, operator files and Terraform records outside the student subscription before replacement or credit expiry. Fence the old control plane before restoring another writer. Native Talos recovery and fresh Tailscale credentials may be required after VM replacement; retaining a disk alone does not restore a cluster.

The etcd disk has `prevent_destroy`. Validation proves unchanged plans, VM replacement with the data disk retained, and refusal of disk replacement. Never bypass this by deleting state or targeting resources. Deliberate disk removal requires explicit scope, verified backups and fencing. Disks, images and IPs keep costing money after a VM is stopped.

If local state is lost, stop Terraform writers and restore its off-machine backup before planning. Import only verified Guaranine resources if no backup exists. Do not recreate resources from empty state. Backend/multi-operator infrastructure should be considered only if a real future requirement justifies it.
