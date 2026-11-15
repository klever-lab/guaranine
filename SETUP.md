# Guaranine setup

Run these commands from the repository root on your Linux operator computer. See the [documentation index](docs/README.md) for background and ongoing operations.

Use the arrow keys and Enter in selection menus. Cancel or Skip is selected initially.

## Prerequisites

Install Git, Python 3.12+, `uv`, Docker, `sudo`, OpenSSH client tools, Tailscale, `nmcli`, `qemu-img` and the Terraform version in [.terraform-version](.terraform-version). Building images needs privileged Docker access and space for uncompressed disk images. USB flashing uses sudo. Use a `kubectl` version matching K3s in [kairos/versions.json](kairos/versions.json).

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

Before provisioning, check the OVH Public Cloud project's region, d2-2 availability, quota and CAD hourly cost. Monthly billing is a later, deliberate change. Keep the running Azure controller and its Terraform state separate until migration is approved.

Check the [controller resources and disk limits](docs/architecture.md#controller-resources). Include compute, private image storage, public IPv4 and outbound traffic in the estimate. Stopped resources can still incur charges.

Create a project-scoped OpenStack user with Compute Operator, Image Operator, Network Operator and Network Security Operator roles. Save its credentials in a mode-0600 `ovh-clouds.yaml` under the operator directory, following the [standard OpenStack clouds.yaml format](https://docs.openstack.org/openstacksdk/latest/user/config/configuration.html). Name the cloud `guaranine`, use password authentication and select the actual catalog region (`BHS5` for this deployment), which can differ from an older OpenRC region label. Keep passwords out of Terraform variables and the checkout.

```bash
export OS_CLIENT_CONFIG_FILE="$GUARANINE_OPERATOR_DIR/ovh-clouds.yaml"
export OS_CLOUD=guaranine
```

## Controller image and Terraform

```bash
./guaranine controlplane
```

The private QCOW2 is in `controller/image/` under the operator directory. Check its format and record its SHA-256:

```bash
controller_image="$GUARANINE_OPERATOR_DIR/controller/image/guaranine-kairos.qcow2"
qemu-img info -f qcow2 "$controller_image"
sha256sum "$controller_image"
```

For incomplete or expired builds, follow [image batches and retries](docs/operations.md#image-batches-and-retries).

Copy the [OVH example](infra/ovh/terraform.tfvars.example) to `$GUARANINE_OPERATOR_DIR/ovh.tfvars`. Set the region, absolute image path and its actual SHA-256. Terraform uploads the image privately and boots it directly; no temporary Ubuntu VM or public provisioning ports are required.

Keep local Terraform state on protected operator storage with [off-machine backups](docs/operations.md#backup). Use one operator at a time. Review every saved plan before applying it.

```bash
mkdir -p "$GUARANINE_OPERATOR_DIR/terraform"
export TF_DATA_DIR="$GUARANINE_OPERATOR_DIR/terraform/ovh-cache"
terraform -chdir=infra/ovh init -lockfile=readonly \
  -backend-config="path=$GUARANINE_OPERATOR_DIR/terraform/ovh.tfstate"
terraform -chdir=infra/ovh plan -var-file="$GUARANINE_OPERATOR_DIR/ovh.tfvars" \
  -out="$GUARANINE_OPERATOR_DIR/ovh.tfplan"
terraform -chdir=infra/ovh show "$GUARANINE_OPERATOR_DIR/ovh.tfplan"
terraform -chdir=infra/ovh apply "$GUARANINE_OPERATOR_DIR/ovh.tfplan"
terraform -chdir=infra/ovh plan -var-file="$GUARANINE_OPERATOR_DIR/ovh.tfvars" -detailed-exitcode
```

Confirm the final plan shows no changes. Keep the `handoff` output with the cloud resource IDs. The new VM joins Tailscale with K3s stopped. If replacing the running controller, stop here and follow [migration and recovery](docs/operations.md#restore-on-another-cloud-vm) after approval for cutover.

## Bootstrap the controller

After the VM registers with Tailscale, verify its tailnet IPv4 address and run:

```bash
./guaranine bootstrap
```

Compare the displayed SSH fingerprint with the protected VM console and enter `yes` if they match. For a new deployment, type `INITIALIZE` to create a NEW EMPTY cluster on native Kairos persistent storage. For an existing cluster, follow the [restore procedure](docs/operations.md#restore-on-another-cloud-vm) first. Bootstrap refuses to overwrite an existing kubeconfig or pinned controller key; it is not the migration command.

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

## Node management

Install the pinned upstream Kairos Operator and repository configuration:

```bash
kubectl apply --server-side -k kubernetes/kairos-operator
kubectl -n kairos-operator rollout status deployment/operator-kairos-operator
kubectl wait --for=jsonpath='{.metadata.labels.kairos\.io/managed}'=true node --all --timeout=120s
```

See [configuration rollouts](docs/operations.md#configuration-rollouts) for updates from the operator computer.

## Application storage

After a worker is Ready, install the repository's storage provisioner:

```bash
kubectl apply -f kubernetes/storage.yaml
kubectl -n openebs rollout status deployment/openebs-localpv
kubectl get storageclass
```

See [application storage](docs/storage.md) for the PVC workflow, permissions, local disk limits and existing-volume handling.

## Optional monitoring

For private Grafana and cluster metrics, prepare the worker directories described in [monitoring storage](docs/monitoring.md#storage-and-reapplication). Apply the current [tailnet policy](docs/security.md#tailnet-policy) and host firewall from this repository before collecting kubelet metrics; updating files in the checkout does not update an installed OS.

Enable MagicDNS and HTTPS certificates in the tailnet's DNS settings. Create a single-use, preauthorized, non-ephemeral auth key for `tag:guaranine-monitoring`, expiring within 24 hours. This key enrolls only the monitoring proxy; the operator API token stays outside the cluster.

Generate private credentials once and deploy a new installation:

```bash
umask 077
monitoring_dir="${GUARANINE_OPERATOR_DIR:-$HOME/.local/state/guaranine-kairos}/monitoring"
mkdir -p "$monitoring_dir"
test -f "$monitoring_dir/admin-password" || openssl rand -base64 36 > "$monitoring_dir/admin-password"
test -f "$monitoring_dir/secret-key" || openssl rand -base64 48 > "$monitoring_dir/secret-key"
read -rsp 'Tailscale monitoring auth key: ' monitoring_auth_key
printf '\n'
printf '%s' "$monitoring_auth_key" > "$monitoring_dir/tailscale-auth-key"
unset monitoring_auth_key
kubectl create namespace monitoring --dry-run=client -o yaml | kubectl apply -f -
kubectl -n monitoring create secret generic grafana \
  --from-file="admin-password=$monitoring_dir/admin-password" \
  --from-file="secret-key=$monitoring_dir/secret-key" \
  --dry-run=client -o yaml | kubectl apply --server-side -f -
kubectl -n monitoring create secret generic monitoring-tailscale \
  --from-file="authkey=$monitoring_dir/tailscale-auth-key"
rm -- "$monitoring_dir/tailscale-auth-key"
kubectl apply -f kubernetes/monitoring.yaml
kubectl -n monitoring rollout status deployment/monitoring-proxy
kubectl -n monitoring rollout status deployment/grafana
kubectl -n monitoring rollout status deployment/prometheus
kubectl -n monitoring rollout status deployment/kube-state-metrics
```

See [Open Grafana](docs/monitoring.md#open-grafana) for private access and the generated password.
