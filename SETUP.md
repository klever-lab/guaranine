# Guaranine setup

Run these commands from the repository root on your Linux operator computer. See the [documentation index](docs/README.md) for background and ongoing operations.

Use the arrow keys and Enter in selection menus. Cancel or Skip is selected initially.

## Prerequisites

Install Git, GitHub CLI (`gh`), Python 3.12+, `uv`, Docker, `sudo`, OpenSSH client tools, Tailscale, `nmcli`, `qemu-img` and the Terraform version in [.terraform-version](.terraform-version). Authenticate `gh` with Actions and environment-secret management access to this repository. Controller deployment runs on GitHub Actions; local image builds need privileged Docker access and space for uncompressed disk images. USB flashing uses sudo. Use a `kubectl` version matching K3s in [kairos/versions.json](kairos/versions.json).

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

Before provisioning, check the OVH Public Cloud project's region, d2-2 availability, quota and CAD hourly cost. Monthly billing is a later, deliberate change.

Check the [controller resources and disk limits](docs/architecture.md#controller-resources). Include compute, private image storage, public IPv4 and outbound traffic in the estimate. Stopped resources can still incur charges.

Create a project-scoped OpenStack user with Compute Operator, Image Operator, Network Operator, Network Security Operator and ObjectStorage Operator roles. Save its credentials in a mode-0600 `ovh-clouds.yaml` under the operator directory, following the [standard OpenStack clouds.yaml format](https://docs.openstack.org/openstacksdk/latest/user/config/configuration.html). Name the cloud `guaranine`, use password authentication and select the actual catalog region (`BHS5` for this deployment), which can differ from an older OpenRC region label. Keep passwords out of Terraform variables and the checkout.

```bash
export OS_CLIENT_CONFIG_FILE="$GUARANINE_OPERATOR_DIR/ovh-clouds.yaml"
export OS_CLOUD=guaranine
```

## Controller deployment

The manual [deployment workflow](.github/workflows/deploy.yml) builds the OS base into GHCR, privately builds the QCOW2 and applies the OVH Terraform root. Read the [CI credential boundary](docs/security.md#private-material-and-enrollment) before configuring secrets.

### One-time state setup

Generate S3 credentials for the same OVH user. Save them as a mode-0600 [standard AWS credentials file](https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-files.html) at `$GUARANINE_OPERATOR_DIR/ovh-s3-credentials`, using the `default` profile. These credentials call OVH's S3 service.

Choose a unique private state bucket name. Copy [the OVH example](infra/ovh/terraform.tfvars.example) to `$GUARANINE_OPERATOR_DIR/ovh.tfvars`, set `state_bucket`, and temporarily set `image_sha256` to 64 zeroes. Keep the absolute future image path; the bucket-only bootstrap does not build or upload that image.

Create a mode-0600 `$GUARANINE_OPERATOR_DIR/ovh.backend.hcl`:

```hcl
bucket = "YOUR_PRIVATE_OVH_STATE_BUCKET"
region = "bhs"
endpoints = { s3 = "https://s3.bhs.io.cloud.ovh.net" }
```

Bootstrap the bucket with a protected copy of the **same** Terraform root and temporary local state. This is the only targeted apply: the remote backend cannot store state until its bucket exists. Do this once, before dispatching deployments. Inspect the plan to confirm it creates only the bucket, versioning and encryption configuration.

```bash
export AWS_SHARED_CREDENTIALS_FILE="$GUARANINE_OPERATOR_DIR/ovh-s3-credentials"
export AWS_REQUEST_CHECKSUM_CALCULATION=WHEN_REQUIRED
export AWS_RESPONSE_CHECKSUM_VALIDATION=WHEN_REQUIRED
mkdir -p "$GUARANINE_OPERATOR_DIR/terraform"
bootstrap_root="$GUARANINE_OPERATOR_DIR/terraform/ovh-bootstrap"
mkdir -m700 "$bootstrap_root"
cp infra/ovh/main.tf infra/ovh/.terraform.lock.hcl "$bootstrap_root/"
printf 'terraform {\n  backend "local" {}\n}\n' > "$bootstrap_root/backend_override.tf"
export TF_DATA_DIR="$GUARANINE_OPERATOR_DIR/terraform/ovh-bootstrap-cache"
terraform -chdir="$bootstrap_root" init -lockfile=readonly \
  -backend-config="path=$GUARANINE_OPERATOR_DIR/terraform/ovh-bootstrap.tfstate"
terraform -chdir="$bootstrap_root" plan -var-file="$GUARANINE_OPERATOR_DIR/ovh.tfvars" \
  -target=aws_s3_bucket_versioning.state \
  -target=aws_s3_bucket_server_side_encryption_configuration.state \
  -out="$GUARANINE_OPERATOR_DIR/ovh-bucket.tfplan"
terraform -chdir="$bootstrap_root" show "$GUARANINE_OPERATOR_DIR/ovh-bucket.tfplan"
terraform -chdir="$bootstrap_root" apply "$GUARANINE_OPERATOR_DIR/ovh-bucket.tfplan"
rm "$bootstrap_root/backend_override.tf"
terraform -chdir="$bootstrap_root" init -migrate-state -lockfile=readonly \
  -backend-config="$GUARANINE_OPERATOR_DIR/ovh.backend.hcl"
unset TF_DATA_DIR
```

Keep the bootstrap state backup outside OVH. The bucket remains Terraform-managed, versioned and protected from Terraform deletion. Use one operator at a time.

### GitHub environment

Create a GitHub environment named `ovh`, with deployment branches restricted to the **selected branch `main`**, without tag rules. An unrestricted environment or the generic protected-branches setting is insufficient. Configure these environment values through GitHub Settings or `gh secret set --env ovh` / `gh variable set --env ovh`:

| Kind | Name | Value |
| --- | --- | --- |
| Secret | `OS_USERNAME`, `OS_PASSWORD`, `OS_PROJECT_ID` | Project-scoped OpenStack credentials. |
| Secret | `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | OVH S3 credentials from the protected credentials file. |
| Variable | `OVH_REGION` | OpenStack compute region. |
| Variable | `OVH_STORAGE_REGION` | S3 region from the backend configuration. |
| Variable | `OVH_STATE_BUCKET` | Bucket name from the backend configuration. |

The command supplies `SSH_PUBLIC_KEY` and a fresh, single-use `TAILSCALE_AUTH_KEY`. It requires a GitHub CLI version that returns the dispatched workflow's run URL. The operator's Tailscale API token and cluster credentials stay local.

```bash
./guaranine deploy
```

The command dispatches committed `main`, follows the run and saves a private infrastructure-state backup under its operator `deploy-*` directory. Check the printed `handoff` resource IDs. The workflow reuses the original private seed image on subsequent runs; publishing a new OS base does not upgrade the installed controller. Plans that delete resources or modify/recreate an existing controller stop for local review. For a later manual plan, download the managed private image and replace the bootstrap-only image path/hash in `ovh.tfvars` with its actual path and SHA-256.

The new VM joins Tailscale with K3s stopped. Verify the console SSH fingerprint and persistent storage. If replacing the running controller, stop here and follow [migration and recovery](docs/operations.md#restore-on-another-cloud-vm) after approval for cutover. Do not initialize a new empty cluster.

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

Choose a USB, checking its path, model, size, serial and label, or select **Skip flashing** to keep the ISO without writing a USB. Type `ERASE` to confirm. The CLI rechecks and unmounts the drive, writes with progress, flushes it and verifies every written image byte. Do not remove the USB until `Flashed` appears. System mounts, active encrypted/LVM volumes and the ISO's own disk are excluded. Redirected runs do not offer flashing.

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

## Raspberry Pi dashboard display

The dedicated Pi 2 Model B uses the stock Raspberry Pi OS image pinned in [kiosk/versions.json](kiosk/versions.json), with [kiosk/config.yaml](kiosk/config.yaml) and private configuration added during SD-card preparation. Its prepared card boots directly into the Grafana display after applying first-boot updates. Connect Ethernet, HDMI and power; leave it connected while setup completes. See [display operation and qualification](docs/monitoring.md#raspberry-pi-dashboard-display) for private operator files, recovery and the remaining physical checks.

To recreate the card for this Pi, put the SD card in a USB reader attached to the operator computer and run:

```bash
./guaranine kiosk
```

The command verifies the saved private recovery image. Select the reader's whole drive, check its path, model, size and serial, then type `ERASE`. It unmounts, writes and verifies the card before reporting success. With the Pi powered off, insert the card and connect Ethernet, HDMI and power. The installed setup resumes automatically after first-boot updates.

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
