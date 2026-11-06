# Azure coordinator operator guide

This is the base infrastructure for KIAN-972. It has been validated with mocks, **not deployed**. Live bootstrap/apply commands below are for the operator after preflight; CI and Symphony must never provision Azure as a test. Terraform owns only Azure infrastructure and the secret-free first-boot baseline. Ansible will own packages, Tailscale enrollment, disk filesystems/mounts, host firewall, and K3s. Kubernetes will own workloads and their automatic placement.

The coordinator is infrastructure outside the replaceable laptop pool: one future K3s server with local SQLite, not AKS or a WAN etcd quorum. Existing Azure services and the Clusterfork Symphony controller have no resource, network, state, or lifecycle dependency on these roots.

## Layout and defaults

| Root | Ownership |
| --- | --- |
| `state/` | Separate `guaranine-tfstate-rg`, operator-restricted Blob account/container and the existing operator user's scoped Blob Data Contributor role. Bootstrap locally once, then migrate its state into its own account. |
| `coordinator/` | Dedicated resource group, VNet/subnet, NSG, NIC, outbound public IP, Linux VM, independent SSD data disk and attachment. |

Terraform is pinned to **1.17.1** in the root `.terraform-version` and each root's `required_version`. AzureRM is pinned to **5.13.0**; each root commits its `.terraform.lock.hcl`. For live operator commands, install the exact Terraform release from [HashiCorp](https://releases.hashicorp.com/terraform/1.17.1/) and verify its published checksum before use. Repository validation bootstraps its own temporary verified CLI when needed; it requires Python 3.12 or newer and the pinned `requirements-dev.txt`. Azure CLI is needed only for operator preflight and authenticated operations.

The default region is `eastus`, pending preflight. The default SKU is `Standard_B2ls_v2`: [2 vCPUs and 4 GiB](https://learn.microsoft.com/en-us/azure/virtual-machines/sizes/general-purpose/bsv2-series). It is burstable and can throttle after CPU credits are consumed; measure sustained control-plane load before relying on it. It is not a promise of a free student-eligible VM. There is no automatic SKU/region fallback. K3s's [server minimum](https://docs.k3s.io/installation/requirements#hardware) is 2 cores / 2 GB before workloads; the extra memory is initial headroom.

The OS disk is 32 GiB `StandardSSD_LRS`; it is replaced with the VM. A separate 64 GiB `StandardSSD_LRS` disk is attached at **LUN 0**, with caching `None` and public disk export disabled. Its `prevent_destroy` guard blocks deletion/replacement. Both capacities are configurable integers from 32 to 1024 GiB. An existing disk can only grow; Ansible must also expand the filesystem. This unformatted disk is reserved for the later coordinator database/configuration task. It does not yet provide portable workload storage, a backup, or cross-region replication.

`location`, `vm_size`, `image` (publisher/offer/SKU/exact numeric version), capacities, admin user/public key, and cost tags are inputs. No `latest` image is allowed. The example uses Ubuntu 24.04 LTS AMD64 Gen2, compatible with the SSH/cloud-init baseline and Trusted Launch. Alternative images must support cloud-init, the Azure Linux agent, Secure Boot/vTPM, and the `ssh` systemd unit, and must be tested before use. The exact image version is intentionally required rather than guessed from an unauthenticated image list. Resource names are stable, not timestamped or random.

## Network and enrollment boundary

The VNet `10.80.0.0/24` and subnet `10.80.0.0/27` are isolated, with no peering, LAN routes or dependency on an existing VNet. They do not overlap the default K3s pod/service ranges or Tailscale addresses. The subnet NSG denies **all unsolicited inbound traffic from every source**, overriding Azure's default VNet/load-balancer allows. It also denies direct VNet outbound traffic. There is no public SSH, Kubernetes API, kubelet, Flannel, storage service, or public application ingress rule.

A Standard static IPv4 on the NIC supplies [explicit outbound connectivity](https://learn.microsoft.com/en-us/azure/virtual-network/ip-services/default-outbound-access); implicit subnet outbound access is disabled. The public IP is a billed egress facility, not an administration endpoint. Internet egress remains available for Azure agent operations, packages, DNS and Tailscale coordination, DERP and UDP hole punching. Stateful return traffic is allowed. No inbound UDP exception is required initially: [Tailscale supports restrictive firewalls using relays](https://tailscale.com/docs/reference/faq/firewall-ports). Direct paths may work through outbound-created state, but relay fallback must be verified later. Do not open public cluster ports to improve connectivity.

`coordinator/cloud-init.yaml` disables password/root SSH access and writes the handoff marker. It installs neither Tailscale nor K3s, formats no disk, and contains no credentials. The VM agent and extension operations are enabled so the following task can bootstrap without inbound SSH:

1. The following Ansible task must provide a versioned, pinned Tailscale bootstrap payload delivered through [Azure managed Run Command](https://learn.microsoft.com/en-us/azure/virtual-machines/linux/run-command-managed), using the operator's existing Azure identity. Wait for `cloud-init status --wait` before changing host configuration. Confirm outbound agent connectivity and its supported version first.
2. Deliver a short-lived, single-use, appropriately tagged enrollment key as a **protected parameter** of that management operation. Never include it in the script text, ordinary parameters, shell history, Terraform variables, custom data, provisioners, outputs, state or logs. The later implementation must submit a mode-0600 request file outside the checkout, disable command tracing/debug and Ansible logging of secrets, keep any guest key file in root-only `/run`, suppress secret-bearing stdout/stderr, and clean temporary payloads on both success and failure. Remove the Run Command resource after execution and revoke the enrollment key on failure. Do not pass reusable account credentials to the VM or worker. Do not add a Terraform Run Command/extension resource for enrollment.
3. Verify the VM's tailnet identity, tag/ACL authorization and host key using the management channel, then construct the Ansible inventory with its **Tailscale IP or MagicDNS name**. SSH thereafter uses that endpoint and the operator's existing private key outside Terraform. Neither the Azure private address nor the public egress IP is an inventory endpoint. Record only non-secret endpoint metadata.
4. Ansible must discover the disk by Azure LUN/identity, preserve existing filesystems, mount persistent coordinator state before installing K3s, and fail if the expected mount is absent. Configure a single SQLite server, node/API addresses on Tailscale and Flannel on `tailscale0`; host firewall and tailnet ACLs must restrict cluster traffic to the tailnet. Test direct/relay paths and temporary disconnections. Coordinator K3s configuration, storage exports and workloads remain separate work. The [laptop installer and Ansible worker profile](../../images/laptop/README.md) now own worker enrollment and define the pinned server-version/CA/credential contract; they require an existing configured server and do not deploy one.

Run Command is an Azure management-plane bootstrap/recovery channel, not a private inter-host transport. All private host-to-host administration, cluster and future storage traffic must use Tailscale. No enrollment mechanism is claimed operational by this infrastructure-only delivery.

## Mandatory live preflight

Use the operator's already authenticated Azure CLI session on their own trusted machine. Do not copy Azure CLI caches, tokens, client secrets, SSH private keys or account credentials into this repository, CI, the controller or remote workers. No new authentication flow is configured here. If that session is unavailable, run offline validation and perform planning later on the authenticated operator machine; `-refresh=false` does not make a real Azure plan credential-free.

Record the following evidence, timestamp and deployment inputs in an operator record outside git. Repeat preflight before either root's first apply, a region/SKU/capacity change, and any later apply if the evidence is stale. These are read-only examples; set the identifiers to the existing student subscription:

```bash
export ARM_SUBSCRIPTION_ID='<existing-student-subscription-uuid>'
export GUARANINE_REGION='eastus'
export GUARANINE_VM_SIZE='Standard_B2ls_v2'
az account show --subscription "$ARM_SUBSCRIPTION_ID" \
  --query '{id:id,name:name,tenant:tenantId,state:state}'
export ARM_TENANT_ID="$(az account show --subscription "$ARM_SUBSCRIPTION_ID" --query tenantId --output tsv)"
az rest --method get \
  --url "https://management.azure.com/subscriptions/$ARM_SUBSCRIPTION_ID?api-version=2022-12-01" \
  --query '{state:state,policies:subscriptionPolicies}'
az vm list-usage --subscription "$ARM_SUBSCRIPTION_ID" \
  --location "$GUARANINE_REGION" --output table
az vm list-skus --subscription "$ARM_SUBSCRIPTION_ID" \
  --location "$GUARANINE_REGION" --size "$GUARANINE_VM_SIZE" \
  --resource-type virtualMachines --all --output json
az vm image list --subscription "$ARM_SUBSCRIPTION_ID" \
  --location "$GUARANINE_REGION" --publisher Canonical \
  --offer ubuntu-24_04-lts --sku server --all --query '[].version'
az provider show --subscription "$ARM_SUBSCRIPTION_ID" \
  --namespace Microsoft.Compute --query registrationState
az provider show --subscription "$ARM_SUBSCRIPTION_ID" \
  --namespace Microsoft.Network --query registrationState
az provider show --subscription "$ARM_SUBSCRIPTION_ID" \
  --namespace Microsoft.Storage --query registrationState
az ad signed-in-user show --query id --output tsv
```

Check all of these before proceeding:

- Confirm this is the existing **Azure for Students** offer, the subscription is enabled and `subscriptionPolicies.spendingLimit` is `On`. Preserve that limit. Never upgrade the subscription, remove the limit or change the offer to obtain capacity. Stop if offer/limit evidence is unavailable or contradicts the account view.
- Verify remaining credit and the **actual expiry date** in the [Azure Sponsorships balance view](https://www.microsoftazuresponsorships.com/Balance). Save both and a reminder before expiry in the operator record. [Exhaustion or expiry can disable the subscription](https://learn.microsoft.com/en-us/azure/cost-management-billing/manage/azurestudents-subscription-disabled); Terraform cannot measure that balance or prevent it. Do not record an invented balance or expiry.
- Verify regional total and VM-family vCPU quota, currently used capacity, NIC/public-IP/disk limits, and exact SKU restrictions for this subscription. Check returned SKU capabilities for CPU, RAM, architecture, data-disk attachment and Trusted Launch. Quota and SKU listing do not reserve capacity; an allocation failure must stop the apply, not select a larger/more expensive fallback.
- Select an exact listed image version, verify its regional availability and requirements with `az vm image show --subscription "$ARM_SUBSCRIPTION_ID" --location "$GUARANINE_REGION" --urn '<publisher:offer:sku:version>'`, and put that value into the external tfvars. [Canonical's image guide](https://ubuntu.com/azure/docs/azure-how-to/instances/find-ubuntu-images/) identifies the free non-Pro offers. Do not silently switch to a paid marketplace image.
- Prepare an itemized estimate using the [Azure pricing calculator](https://azure.microsoft.com/en-us/pricing/calculator/) or the subscription's rate sheet: 730 hours/month of Linux compute; both SSD capacity tiers and Standard SSD transaction charges; Standard IPv4 hours; outbound bandwidth including Tailscale traffic; Blob storage/versions/transactions; and intended snapshots/off-site backups. Include currency, region, any applicable taxes and a traffic/transaction allowance. Account for **existing subscription consumption** when estimating credit runway. Record the monthly total, daily burn, credit runway and margin to expiry. Do not assume free allowances or omit the public IP/network cost. Stop if the total is unknown or does not fit the remaining credit.
- Ensure `Microsoft.Compute`, `Microsoft.Network` and `Microsoft.Storage` are registered. Terraform disables automatic provider registration, including during planning. If a registration is missing, the operator must deliberately register that provider before applying; preflight itself must not do so. Check permission to create the isolated RGs/resources and assign the operator's Blob role. Keep the stable Entra user object ID in state-root inputs; a different person running a plan must not silently change the state owner.
- Choose a globally unique lowercase state-account name and check it with `az storage account check-name --name '<name>' --subscription "$ARM_SUBSCRIPTION_ID"`. Verify the fixed Guaranine RG names/prefix are unused or already owned by these exact states; never import unrelated services to resolve a collision.

Portal use here is limited to read-only billing/credit/pricing evidence, not infrastructure creation. Once evidence is recorded, bootstrap and provisioning use the versioned Terraform below. No preflight or live deployment has been performed as part of the offline checks.

## Operator files and backend bootstrap

Run commands from the repository root. Choose an absolute operator directory **outside the checkout** on an encrypted, trusted disk. Treat even non-secret tfvars as operator records and keep state/plans encrypted and access restricted. `.gitignore` is defense in depth, not the storage location. Never upload plans, state, `.terraform` caches or logs as CI artifacts.

```bash
umask 077
export GUARANINE_OPERATOR_DIR="$HOME/.local/state/guaranine"
mkdir -p "$GUARANINE_OPERATOR_DIR"
cp infra/azure/state/terraform.tfvars.example "$GUARANINE_OPERATOR_DIR/state.tfvars"
cp infra/azure/coordinator/terraform.tfvars.example "$GUARANINE_OPERATOR_DIR/coordinator.tfvars"
```

Edit both external files with the explicit subscription/region and selected values from preflight. Set `operator_object_id` to the stable existing user's Entra object ID. Set `storage_account_name` once and supply the operator's actual **public IPv4 egress CIDR**, normally `/32`; the sample documentation address will not grant access. The state account requires Entra ID, HTTPS/TLS 1.2, denies shared-key/public-blob access, and permits data-plane access only from the listed narrow operator ranges. No VNet peering or worker access to state is needed. Use an operator connection outside that Azure region: Azure Storage IP rules do not admit same-region Azure clients by their public IP. [Storage firewall limitations](https://learn.microsoft.com/en-us/azure/storage/common/storage-network-security-limitations) apply.

Bootstrap uses a temporary **local backend override**; both committed roots normally select Azure Blob. Keep this shell's `TF_DATA_DIR` unchanged through bootstrap and migration, so Terraform remembers where the old state resides:

```bash
export TF_DATA_DIR="$GUARANINE_OPERATOR_DIR/state-init"
cp infra/azure/state/local-backend.tf.example infra/azure/state/backend_override.tf
terraform -chdir=infra/azure/state init -input=false -lockfile=readonly \
  -backend-config="path=$GUARANINE_OPERATOR_DIR/bootstrap.tfstate"
terraform -chdir=infra/azure/state plan -lock-timeout=5m \
  -var-file="$GUARANINE_OPERATOR_DIR/state.tfvars" \
  -out="$GUARANINE_OPERATOR_DIR/bootstrap.tfplan"
terraform -chdir=infra/azure/state show "$GUARANINE_OPERATOR_DIR/bootstrap.tfplan"
terraform -chdir=infra/azure/state apply -lock-timeout=5m \
  "$GUARANINE_OPERATOR_DIR/bootstrap.tfplan"
terraform -chdir=infra/azure/state output -json backend \
  > "$GUARANINE_OPERATOR_DIR/backend-coordinates.json"
cp infra/azure/backend.hcl.example "$GUARANINE_OPERATOR_DIR/state.backend.hcl"
cp infra/azure/backend.hcl.example "$GUARANINE_OPERATOR_DIR/coordinator.backend.hcl"
```

Inspect the saved plan against preflight before live apply. Bootstrap requires resource-creation permission plus role-assignment permission (such as Owner or appropriate Contributor and RBAC administration permissions); account ownership alone is not Blob data-plane authorization. The root grants only `Storage Blob Data Contributor` at this account to the existing operator. RBAC propagation can briefly delay container/backend access: after checking the firewall and role, wait, regenerate a plan from current state and retry with the same inputs. Never enable shared keys, generate a SAS, copy credentials or widen access to `0.0.0.0/0` as a workaround.

Edit the two external backend files using the output coordinates. Use **`state/bootstrap.tfstate`** as the state root key and **`coordinator/terraform.tfstate`** as the coordinator key. Do not reuse a key for two roots; keep the default workspace. Backend auth settings `use_azuread_auth = true` and `use_cli = true` are committed in both roots. Identifiers may be in backend files; credentials must never be there.

```bash
cp "$GUARANINE_OPERATOR_DIR/bootstrap.tfstate" "$GUARANINE_OPERATOR_DIR/bootstrap-before-migration.tfstate"
mv infra/azure/state/backend_override.tf "$GUARANINE_OPERATOR_DIR/bootstrap-local-backend.used"
terraform -chdir=infra/azure/state init -migrate-state -lockfile=readonly \
  -backend-config="$GUARANINE_OPERATOR_DIR/state.backend.hcl"
terraform -chdir=infra/azure/state state list
terraform -chdir=infra/azure/state plan -lock-timeout=5m \
  -var-file="$GUARANINE_OPERATOR_DIR/state.tfvars" -detailed-exitcode
```

Confirm Terraform's migration prompt only for this local state and these Blob coordinates. Verify the remote blob and resource list, then require a no-change plan (exit 0; exit 2 means changes; exit 1 means error). Stop using the local copy as a writer. Retain the encrypted pre-migration copy until a remote-state restore is verified, then remove stale local states/plans according to the operator's retention policy. Back up the external tfvars and backend coordinates too. An interrupted migration requires comparing local/remote lineage, serial and resources before choosing the authoritative copy; never create an empty replacement state over existing resources.

The [Azure Blob backend](https://developer.hashicorp.com/terraform/language/backend/azurerm) uses Entra ID data-plane authorization and native **Blob lease locking**. Always keep locking enabled and use a lock timeout; serialize operations for each key. `force-unlock` is only for a confirmed abandoned lock after checking that no writer remains. A normal operator connection needs the assigned Blob data role and an allowed egress IP. The backend account/container and state RG have `prevent_destroy`; versioning and 30-day blob/container soft deletion aid recovery. LRS and same-subscription version history do not replace encrypted off-subscription backups or survive student-subscription disablement reliably.

## Coordinator planning, applying and rerunning

Use the same preflight-approved values and the existing operator identity. Only the **public** SSH key enters Terraform:

```bash
export TF_DATA_DIR="$GUARANINE_OPERATOR_DIR/coordinator-init"
export TF_VAR_admin_ssh_public_key="$(cat "$HOME/.ssh/id_ed25519.pub")"
terraform -chdir=infra/azure/coordinator init -input=false -lockfile=readonly \
  -backend-config="$GUARANINE_OPERATOR_DIR/coordinator.backend.hcl"
terraform -chdir=infra/azure/coordinator plan -lock-timeout=5m \
  -var-file="$GUARANINE_OPERATOR_DIR/coordinator.tfvars" \
  -out="$GUARANINE_OPERATOR_DIR/coordinator.tfplan"
terraform -chdir=infra/azure/coordinator show "$GUARANINE_OPERATOR_DIR/coordinator.tfplan"
terraform -chdir=infra/azure/coordinator apply -lock-timeout=5m \
  "$GUARANINE_OPERATOR_DIR/coordinator.tfplan"
terraform -chdir=infra/azure/coordinator output -json ansible_handoff \
  > "$GUARANINE_OPERATOR_DIR/ansible-handoff.json"
terraform -chdir=infra/azure/coordinator plan -lock-timeout=5m \
  -var-file="$GUARANINE_OPERATOR_DIR/coordinator.tfvars" -detailed-exitcode
```

`ansible_handoff` provides subscription, resource group, VM name/ID, admin user, disk ID/LUN and `transport = "tailscale"`. The later task must add the verified Tailscale endpoint to its inventory; no unassigned tailnet address or secret is fabricated. `egress_public_ip` is only for diagnostics. Validate the live disk attachment, effective NSG, cloud-init completion and Tailscale enrollment separately before claiming the coordinator is usable.

Repeat plans with the same tfvars, key, image and backend. Stable names, separate disk ownership and no timestamp/random bootstrap inputs allow convergence; offline tests verify a no-change rerun and retained disk during VM replacement. Live Azure convergence still needs the post-apply plan above. For a fresh checkout, initialize each remote root with its own `TF_DATA_DIR`/backend file; never recreate the local bootstrap override after migration. An unchanged state-root plan should also be empty.

Do not apply a stale saved plan after any preflight, input, source or state change; regenerate it. Changing image, custom data, admin identity or resource names can replace a VM. Schedule coordinator downtime first. Region/name changes that replace the persistent disk are deliberately blocked; design an explicit backup/restore migration instead. Cloud-init runs at first boot, not as an ongoing configuration manager. Do not use `ignore_changes` to hide host drift; Ansible owns subsequent host configuration.

For a deliberate Terraform/provider upgrade, update `.terraform-version` and both `versions.tf` files, and update the version/platform archive hashes in `scripts/terraform-checksums.json` from that release's official `SHA256SUMS`. Regenerate both provider locks with `terraform providers lock -platform=linux_amd64 -platform=linux_arm64 -platform=darwin_arm64`, run the shared validation and review authenticated plans before applying. Ordinary init uses `-lockfile=readonly`, never an unreviewed provider upgrade.

## Teardown and recovery

The coordinator is not a drainable laptop worker. After K3s exists, stopping it interrupts the API, scheduling and recovery; workers may keep running existing workloads. There is no HA control plane or automatic data recovery in this task. Before planned replacement, stop K3s and storage writers, fence the old VM against restarting, capture a consistent backup and verify its restore. Never treat a WAN outage as proof the old writer has stopped.

- **Replace compute, retain data:** after fencing/backups, plan with `-replace=azurerm_linux_virtual_machine.coordinator` and the normal external tfvars. Inspect that the separate data disk is unchanged, apply the saved plan, then rerun Ansible/Tailscale enrollment against the new VM. The OS disk is disposable. Mount LUN 0 without formatting existing data; restore configuration and the matching K3s server token/database as needed. Remove the old tailnet identity and confirm there is only one server/writer before enabling agents. Terraform does not perform these host/data recovery steps.
- **Recover deleted/lost Terraform state:** stop all writers; retrieve a known-good blob version or encrypted off-subscription state backup. Preserve lineage/serial and validate resources/IDs before restoring under the original key with exclusive access. Use `terraform import` for verified existing Guaranine resources only when no valid state backup exists. Plan and inspect drift before any apply. State backups contain metadata and may contain provider-sensitive values; they are not disk/database backups.
- **Recover coordinator data or leave Azure:** retain consistent off-site copies of SQLite, the server token, configuration and any later workload data. The current blank disk contains none of those yet. Recreate the hub with an explicit target region/subscription and backend, restore tested backups, enroll Tailscale and rerun Ansible. Do not start a second writer until the old hub is fenced. Make an exit plan before the recorded student-credit expiry; state storage in the same disabled subscription may also be unavailable. Portable workload storage/fencing automation remains future work.
- **Remove the whole coordinator deliberately:** a normal destroy plan is expected to fail because the data disk has `prevent_destroy`. Archive verified data/state backups, stop/fence workloads and revoke the tailnet identity. If truly deleting the disk, deliberately remove that disk's lifecycle guard in a reviewed local change, run a full `plan -destroy` with the normal tfvars and save/inspect its scope before applying. Do not use state removal or targeted destruction to bypass protection. Keep the state root intact until recovery/retention decisions are complete; disks and the public IP continue costing money while retained.
- **Remove state storage last:** first export both states and migrate their active backends to a different tested backend (a temporary encrypted local backend uses the same override pattern and `init -migrate-state`). Prove that neither root still writes to the old account. Then deliberately remove the state RG/account/container guards and plan their destruction using the migrated state. Deleting a backend while it is storing its own active state can lose both infrastructure records and locking. Confirm the destruction scope excludes all existing Azure services and the Symphony controller.

If the operator's public IP changes and the backend firewall locks them out, an authorized operator can add the new narrow address through the Azure management plane (`az storage account network-rule add`), then update `operator_ipv4_cidrs` and converge Terraform immediately. Preserve Entra-only access and record the recovery action. Keep that break-glass permission outside the VM/worker pool; do not permanently widen the firewall or try to repair backend access from CI.

## Offline validation

From the repository root on Linux/macOS AMD64/ARM64 (or Linux under WSL), without needing Terraform preinstalled:

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install --requirement requirements-dev.txt
bash scripts/validate.sh
```

The entry point keeps PyMarkdown and runs tool-bootstrap regression tests. A matching CLI on `PATH` is reused; otherwise the pinned archive is fetched from `releases.hashicorp.com`, verified against `scripts/terraform-checksums.json` and unpacked into the temporary validation directory. A different installed version is left intact. The temporary CLI is removed on exit; no system directory or persistent shell configuration is changed. Downloads require HTTPS access to HashiCorp releases and the provider registry. Bootstrap tests cover absent/wrong installed versions, matching-version reuse, checksum rejection, download failure and unsupported platforms.

The entry point also validates the OS bootstrap YAML/SSH policy. For both roots it copies configuration into temporary directories, excludes operator tfvars/backend overrides/state/plans, clears ambient Azure/Terraform inputs, checks formatting, runs `init -backend=false -lockfile=readonly`, validates against the real pinned provider schema, and runs [Terraform mocked tests](https://developer.hashicorp.com/terraform/language/tests/mocking). No Azure credentials or Azure resources are involved. Provider locks are read-only during validation. The GitHub validation workflow and shared Symphony worker call this same entry point, including CLI bootstrap.

Plan assertions cover default/configurable SKU/image/region/capacities, key-only OS bootstrap, attached NSG and explicit outbound IP, denial of inbound/direct-VNet traffic, independent SSD attachment/cache/export settings, state authorization/firewall/versioning, and invalid inputs. Mock-only lifecycle setup creates in-memory test state; the runner checks the JSON plans for fully unchanged reruns of both roots and a VM replacement with a no-op data disk. A separate forced-disk-replacement test must fail with exactly the disk's `prevent_destroy` diagnostic; other errors fail validation. The documented local backend override is also initialized and checked without Azure. No live apply, actual state migration, image/SKU availability, billing, Tailscale path or guest boot is verified by these tests.
