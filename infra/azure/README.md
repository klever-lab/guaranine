# Azure infrastructure for Talos

Terraform describes the one planned Talos control-plane VM, private image artifacts and Entra-authenticated Blob state. No Azure resource has been provisioned or live-qualified by this repository. This is not AKS, an additional controller or an enrollment server. Laptop workers use Tailscale exclusively and never join an etcd quorum across the WAN.

## Ownership and boundaries

| Root | Resources |
| --- | --- |
| `state/` | Isolated state RG, restricted Blob account/container, scoped Entra data role, lease locking/versioning/soft deletion |
| `image/` | Private managed-disk upload staging and a Gen2 Talos managed image; no VM/server |
| `coordinator/` | Isolated RG/VNet/subnet/NSG, outbound-only public IP, NIC, exactly one Talos VM, retained SSD at LUN 0 |

Terraform and AzureRM remain pinned in `.terraform-version`, each `versions.tf` and provider locks. Existing Azure services and the Clusterfork Symphony controller have no network, state, resource or lifecycle dependency on these roots. Do not import unrelated resources or add another server to work around failures.

The control plane defaults to the exact `Standard_B2ls_v2` SKU (2 vCPUs/4 GiB), a 32-GiB disposable OS SSD and a 64-GiB retained data SSD. Region/SKU/capacities are inputs, without automatic fallback. Compute is burstable; measure sustained load. Talos's ETCD volume uses the retained disk; this is not portable workload storage or a backup. Never shrink existing disks.

The VM boots an explicit private Talos managed-image/gallery-version ID. There is no marketplace Ubuntu image, cloud-init, `custom_data`, VM agent, extension operation or SSH administration. Azure's Linux VM API still requires a username/public SSH key as provisioning metadata; Talos does not use them or run an SSH daemon. The pinned ordinary Gen2 VHD path has Secure Boot and vTPM disabled; a separately qualified trusted-boot/TPM image path has not been implemented.

The NSG denies every physical inbound port and direct VNet egress. Its public IP is for explicit internet egress and diagnostics, never Talos/Kubernetes ingress. Tailnet transport can use outbound connections and DERP. The machine's private configuration already contains its short-lived Tailscale enrollment key; it is embedded into a private VHD locally and never stored in Terraform state, CLI variables, a public schematic or a public ISO.

## Student-subscription preflight

Before any resource creation, record the explicitly selected subscription, owner, region, remaining Azure for Students credit, credit expiry, spending-limit status, existing subscription consumption, and an itemized monthly/daily estimate. Include compute, both VM SSDs, private image/upload disk storage, public IPv4 hours, outbound Tailscale/DERP traffic, Blob capacity/versions/transactions and off-subscription backups. Preserve the student spending limit. Stop when credit, expiry, pricing or quota is unknown; do not assume a free allowance or select a larger SKU/paid image automatically.

Use `az account show`, read-only billing/offer evidence, regional/family vCPU quota and `az vm list-skus` to verify availability and requirements. Azure CLI account selection is not proof of remaining credit. Verify `Microsoft.Compute`, `Microsoft.Network` and `Microsoft.Storage` registration and required resource/RBAC permissions; Terraform does not automatically register providers. Check the fixed Guaranine RG namespace and globally unique state-account name before applying. The operator needs the management permissions plus scoped Blob data access; workers need neither.

The state backend requires a narrow operator public IPv4 CIDR and a connection outside the storage account's Azure region, because same-region Storage traffic does not obey public-IP rules in the expected way. Portal access is limited to read-only credit/pricing evidence; creation and changes use the versioned Terraform. See [Azure for Students](https://azure.microsoft.com/free/students/) and [credit exhaustion/expiry](https://learn.microsoft.com/azure/cost-management-billing/manage/azurestudents-subscription-disabled).

## Operator records and remote state

Use an absolute directory outside the checkout on encrypted trusted storage, with `umask 077`. Keep tfvars, backend coordinates, plans, state and image outputs there; none belong in CI artifacts. The root `.gitignore` is defense in depth. Set `GUARANINE_OPERATOR_DIR` to that directory and explicitly set `ARM_SUBSCRIPTION_ID` to the preflight-selected subscription.

Copy the examples from `state/`, `image/` and `coordinator/` into external tfvars. Keep the existing operator's stable Entra object ID, real narrow egress CIDR and exact source image ID; documentation placeholders are not working credentials or resource coordinates. Bootstrap the state root once with its documented local backend override:

```bash
export TF_DATA_DIR="$GUARANINE_OPERATOR_DIR/state-init"
cp infra/azure/state/local-backend.tf.example infra/azure/state/backend_override.tf
terraform -chdir=infra/azure/state init -input=false -lockfile=readonly \
  -backend-config="path=$GUARANINE_OPERATOR_DIR/bootstrap.tfstate"
terraform -chdir=infra/azure/state plan -lock-timeout=5m \
  -var-file="$GUARANINE_OPERATOR_DIR/state.tfvars" \
  -out="$GUARANINE_OPERATOR_DIR/state.tfplan"
terraform -chdir=infra/azure/state show "$GUARANINE_OPERATOR_DIR/state.tfplan"
terraform -chdir=infra/azure/state apply "$GUARANINE_OPERATOR_DIR/state.tfplan"
terraform -chdir=infra/azure/state output -json backend > "$GUARANINE_OPERATOR_DIR/backend.json"
```

Inspect saved plans against authorized scope/preflight before live apply. The state account requires Entra ID, HTTPS/TLS, no shared-key/public-blob access, narrow firewall ranges, versioning and soft deletion. No worker receives backend access. Allow time for scoped RBAC propagation; do not enable shared keys, fabricate SAS credentials or widen access as a workaround.

Copy `backend.hcl.example` into separate external backend files. Use distinct keys: `state/bootstrap.tfstate`, `images/CONTROL_IMAGE_ID.tfstate`, and `coordinator/terraform.tfstate`. Keep Terraform's default workspace. After backing up the local bootstrap state, move the override outside the checkout and migrate the state root:

```bash
cp "$GUARANINE_OPERATOR_DIR/bootstrap.tfstate" "$GUARANINE_OPERATOR_DIR/bootstrap-before-migration.tfstate"
mv infra/azure/state/backend_override.tf "$GUARANINE_OPERATOR_DIR/bootstrap-local-backend.used"
terraform -chdir=infra/azure/state init -migrate-state -lockfile=readonly \
  -backend-config="$GUARANINE_OPERATOR_DIR/state.backend.hcl"
terraform -chdir=infra/azure/state state list
terraform -chdir=infra/azure/state plan -var-file="$GUARANINE_OPERATOR_DIR/state.tfvars" -detailed-exitcode
```

Require a no-change plan after migration. Preserve lineage/serial and compare any interrupted local/remote copies before choosing the authoritative state. Never overwrite an existing backend with empty state. Keep Blob lease locking enabled and serialize writers; `force-unlock` is only for a proven abandoned lock. State/plan files are sensitive; Blob history in the same student subscription is not an off-site backup.

## Private Talos image upload

Generate the private control-plane configuration and Azure image locally as described in the [Talos guide](../../talos/README.md) and [image guide](../../images/laptop/README.md). Decompress the resulting `.vhd.zst` privately using `zstd --decompress`; validate the fixed VHD with `qemu-img info`. Record its actual uncompressed byte length and SHA-256 in external image tfvars. Use a unique `guaranine-talos-...` base name; Terraform appends the content hash so new bytes have a new artifact identity.

Initialize the image root with its own `TF_DATA_DIR` and backend key. Plan/apply with `image_ready = false` to create only upload staging. Neither the VHD contents nor a SAS enter Terraform:

```bash
export TF_DATA_DIR="$GUARANINE_OPERATOR_DIR/image-init"
terraform -chdir=infra/azure/image init -input=false -lockfile=readonly \
  -backend-config="$GUARANINE_OPERATOR_DIR/image.backend.hcl"
terraform -chdir=infra/azure/image plan -var-file="$GUARANINE_OPERATOR_DIR/image.tfvars" \
  -out="$GUARANINE_OPERATOR_DIR/image-upload.tfplan"
terraform -chdir=infra/azure/image show "$GUARANINE_OPERATOR_DIR/image-upload.tfplan"
terraform -chdir=infra/azure/image apply "$GUARANINE_OPERATOR_DIR/image-upload.tfplan"
GUARANINE_UPLOAD_DISK_ID=$(terraform -chdir=infra/azure/image output -raw upload_disk_id)
```

Use the existing [Azure direct-upload procedure](https://learn.microsoft.com/azure/virtual-machines/linux/disks-upload-vhd-to-managed-disk-cli) with Azure CLI and AzCopy: grant a short-lived **write** SAS for this disk, upload the exact private fixed VHD as a page blob, and revoke disk access in a shell `EXIT` trap even if upload fails. Run on the trusted operator host without tracing. Keep AzCopy logs/job plans under the protected operator directory and avoid printing the credential URL. The SAS is runtime-only, not a Terraform variable/output or CI secret. The upload disk is temporarily reachable by its signed URL; it is not an anonymous/public image.

Only after successful upload and revocation, set `image_ready = true`, regenerate/inspect a plan and apply. This closes disk export access and publishes a private managed image. Obtain `talos_image_id` from that root and place the non-secret resource ID in external coordinator tfvars. Use a new state key/name for each artifact; do not mutate an active machine image in place. There is no running image-generation service.

## Control-plane apply and handoff

The operator supplies only Azure-required public SSH metadata; its private counterpart is never used by Talos or Terraform. The private image already contains the node's machine configuration. Initialize the coordinator root with a separate `TF_DATA_DIR` and backend file, then plan/apply the exact authorized scope:

```bash
export TF_DATA_DIR="$GUARANINE_OPERATOR_DIR/coordinator-init"
export TF_VAR_admin_ssh_public_key="$(cat "$GUARANINE_OPERATOR_DIR/azure-api-metadata.pub")"
terraform -chdir=infra/azure/coordinator init -input=false -lockfile=readonly \
  -backend-config="$GUARANINE_OPERATOR_DIR/coordinator.backend.hcl"
terraform -chdir=infra/azure/coordinator plan -lock-timeout=5m \
  -var-file="$GUARANINE_OPERATOR_DIR/coordinator.tfvars" -out="$GUARANINE_OPERATOR_DIR/coordinator.tfplan"
terraform -chdir=infra/azure/coordinator show "$GUARANINE_OPERATOR_DIR/coordinator.tfplan"
terraform -chdir=infra/azure/coordinator apply "$GUARANINE_OPERATOR_DIR/coordinator.tfplan"
terraform -chdir=infra/azure/coordinator output -json talos_handoff > "$GUARANINE_OPERATOR_DIR/talos-handoff.json"
terraform -chdir=infra/azure/coordinator plan -var-file="$GUARANINE_OPERATOR_DIR/coordinator.tfvars" -detailed-exitcode
```

Require a no-change rerun. `talos_handoff` contains resource/disk/image IDs, LUN and `transport = tailscale`; it does not invent a tailnet endpoint. Discover the actual address after Tailscale registration, then follow native Talos endpoint configuration/bootstrap and health verification. Neither Azure private nor public IP is a Kubernetes/Talos API endpoint. No live Azure image upload, Talos bootstrap, billing or hosted tailnet was qualified by offline tests.

## Recovery, retention and teardown

Back up etcd, cluster secrets, operator configuration and Terraform records outside the student subscription before replacing anything. Stop/fence the old control plane before restoring or allowing another writer. A new VM/image may need fresh Tailscale credentials, identity cleanup and native Talos etcd recovery even though Terraform retained the data disk. The OS disk is disposable; no automatic host/data recovery is implied.

The independent data disk has `prevent_destroy`. Mock tests verify unchanged plans, VM replacement with retained data, and refusal of disk replacement. Do not remove state or target destruction to bypass that guard. Deliberate disk deletion requires backups, fencing, an explicit reviewed scope and deliberately changing the lifecycle guard. Retained disks/images/IPs continue costing money. State storage is removed last, only after active backends have migrated and both metadata and data backups have been verified.

For lost state, stop writers, recover a known-good Blob version/off-site copy while preserving lineage/serial, compare resource IDs, and regenerate plans. Import only verified Guaranine resources when no state backup exists. If the operator's egress IP changes, restore access with a narrow management-plane firewall rule, update declared CIDRs and converge; never permanently widen the backend or give workers access. Retain break-glass permissions outside the replaceable VM.

## Offline validation

`bash scripts/validate.sh` bootstraps the exact Terraform CLI after hash verification, uses the real locked provider schema, removes ambient Azure/Terraform credentials and runs mocked tests for all roots. It checks lifecycle plans and disk deletion refusal, plus the documented state bootstrap override. It creates no Azure resources and does not prove live SKU availability, image compatibility, credit runway, convergence or recovery. Host/Kubernetes configuration is validated separately with the real pinned Talos tool, not cloud-init or Ansible.
