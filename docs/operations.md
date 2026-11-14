# Operations

Use the existing operator files created in [SETUP.md](../SETUP.md#operator-files); set `GUARANINE_OPERATOR_DIR` to that directory in a new shell. Do not rerun initialization over an existing deployment.

Run `./guaranine` or `./guaranine --help` for command help.

## Check the cluster

```bash
export KUBECONFIG="$GUARANINE_OPERATOR_DIR/kubeconfig"
kubectl get nodes -o wide
kubectl get pods --all-namespaces -o wide
```

Use the [networking guide](security.md) when checking addresses, access policy or connectivity. The [availability limits](architecture.md#workloads-and-availability) matter when a worker disappears or workloads need to move.

## Image batches and retries

`./guaranine iso` records the Git commit and a hash of the public build inputs in `installer/image/source.json`, beside the ISO. It reuses the image only when both match the current checkout and its registration key is valid. A new commit, local changes to the CLI, launcher, runtime requirements or Kairos files, or missing/invalid source metadata automatically triggers a rebuild. Existing images without this record are rebuilt on their next use. The command prints the source commit and input hash before offering USB flashing.

Rebuilds keep the saved Wi-Fi profile and unexpired registration key. Changing the operator's active Wi-Fi does not change that saved profile. To change networks or start a new batch, revoke the old key in Tailscale, archive the private `installer/` directory and build again. A key-only retry reuses the same key. `./guaranine iso --rebuild` forces a rebuild even when the source matches.

Replacement images are completed before the previous image directory is archived. A failed replacement leaves the previous ISO available and its protected build logs in `installer/image-build-*`; rerunning the command retries the update. Source changes during a build stop the command before flashing. An existing USB changes only after a successful flash. Builds, installation and the final Ready check show elapsed status while keeping subprocess diagnostics private.

The USB confirmation identifies the selected drive, asks for `ERASE` and explains a mismatch. Skipping or cancelling flashing leaves the ISO saved and prints the retry command; laptop boot instructions appear only after a successful USB write. Flashing uses `dd` with synchronized block writes and periodic byte/rate statistics, followed by a final flush before reporting success; updates can pause while USB I/O is pending. Each write saves its progress and errors in a mode-0600 `installer/image/flash-*.log` while displaying progress in the terminal. Write failures print the log path and leave the ISO available for retry.

Incomplete builds preserve outputs and protected logs. Inspect them before revoking the batch key, archiving outputs and retrying. Failed subprocesses can also leave protected `error-*.log` files. Do not put diagnostic contents in public issues or logs. The CLI does not automatically repeat key-creation POSTs after ambiguous transport failures; inspect the live Tailscale state before retrying.

`./guaranine controlplane` reuses a completed VHD under `nodes/guaranine-controlplane/` while its registration key is valid. An incomplete controller image needs inspection before another build. `bootstrap` refuses to overwrite an existing kubeconfig or `controlplane-known-hosts`; inspect those files instead of treating it as a repeatable repair command.

For worker retries, follow the [installation boundaries](../SETUP.md#install-a-worker), including the check after an interrupted remote install. [Enrollment limits](security.md#private-material-and-enrollment) explain why ending a batch does not retire its devices.

To free a name reserved by a failed installation, run `./guaranine worker`, choose **Forget worker**, then select the saved worker. The controller is excluded from that list. It checks the controller and refuses any name still registered in Kubernetes, including an offline worker. On success, it archives the private `nodes/NAME/` directory under `backups/` in the operator directory. It needs no valid installer key and does not erase a laptop or remove its Tailscale identity. Check the failed laptop before reusing its name. The name prompt accepts the name again after it is freed in another terminal.

## Laptop discovery

The worker command reads the operator's local `tailscale status --json` and waits for an online peer with the `guaranine-live-` hostname prefix and `tag:guaranine-worker` tag. It checks every two seconds until a candidate appears; Ctrl+C cancels the wait. A single candidate is selected automatically; several candidates produce a selection menu. It then waits up to two minutes for SSH startup. The operator must be connected to Tailscale. No LAN discovery or manual IP lookup is needed.

These names and tags identify candidates; they do not replace the SSH fingerprint verification or live-media check in the [worker installation procedure](../SETUP.md#install-a-worker).

Before erasing a worker disk, the CLI checks UEFI boot tools and firmware access. Old USBs missing the required tool stop with an update instruction. It also refuses installation if another attached device has partition names or filesystem labels used by Kairos: the native installer resolves those labels globally and could otherwise format the wrong device. The selected disk may already contain Kairos labels.

Laptop names are suggested from DMI model information, with a suffix when a name already exists in Kubernetes or private operator outputs. Installation is issued once after disk confirmation. A definite installer error stops with a private diagnostic log; it does not show reboot instructions or wait for Ready. SSH transport loss or timeout leaves the outcome uncertain; the command can still confirm success if the installed worker subsequently joins. It waits up to ten minutes for Ready and requires a Tailscale internal address before reporting success. The [resume command](../SETUP.md#install-a-worker) performs only that verification, using saved operator outputs and pinned controller SSH; it does not require an unexpired installer key or contact the laptop for installation.

## Retire a worker

Use `kubectl cordon NAME` and `kubectl drain NAME --ignore-daemonsets` before removal. Wait for replacement workloads, and resolve spare-capacity, disruption-budget and storage blockers. Fence the machine before deleting its Kubernetes and Tailscale identities or reusing its name.

A disconnected machine may still be running. WAN disconnection is not fencing.

## Backup

Use [native K3s SQLite backup/restore](https://docs.k3s.io/datastore/backup-restore), including the server token. For a consistent full server-directory copy, briefly stop K3s, archive `server/` from the mounted `/var/lib/rancher/k3s`, and restart K3s in an `EXIT` trap. Stream the archive over pinned Tailscale SSH into a mode-0600 operator file.

Back up operator configuration/keys, Terraform state and application volumes separately, outside the student subscription. Back up Terraform state after every apply. A retained disk is not an off-machine backup.

## Restore on another cloud VM

Fence the old server first. Boot the same prepared Kairos base on a replacement cloud VM, with fresh Tailscale registration if required. Keep K3s stopped while attaching and mounting the retained/restored `GUARANINE_DATA` volume. If the disk was lost, prepare an empty ext4 replacement with that label before restoring. Never initialize or format a volume containing recovery data.

Restore the server directory with K3s stopped and preserve the server token from the backup, then start K3s. Verify API health, worker connectivity and application data. Update both the kubeconfig endpoint and private `operator.yaml` endpoint for the replacement's tailnet address, and verify its SSH fingerprint through the protected console before replacing the pinned controller host key. Keep the old server fenced throughout recovery.

## Migrate an older deployment

Replacing an earlier OS/cluster deployment is a fresh-cluster migration. Create a new `guaranine-kairos` resource prefix and fresh Terraform state. Preserve the existing deployment, state, cluster secrets, boot/data disks and an off-machine datastore backup for rollback. Do not point the new configuration at the old state or format its data disk. Kubernetes manifests and application data need explicit migration; replacing an OS image does not migrate a cluster.

Fence the previous controller before restoring its cluster elsewhere. Keep its recovery copy until the replacement and backups are verified. Retained-disk cleanup requires deliberate scope and verified backups; never bypass retention by deleting Terraform state or targeting resources. [History](history.md) identifies superseded implementations so their commands are not mistaken for current procedures.

## Configuration rollouts

The [Kairos Operator](https://kairos.io/operator-docs/) runs node operations through Kubernetes. Install it once using [Node management](../SETUP.md#node-management). The repository's [Kustomize overlay](../kubernetes/kairos-operator/kustomization.yaml) pins the upstream release and container digests; it does not build a custom controller.

After editing and validating a public, native Kairos rollout profile, run from the operator computer:

```bash
./guaranine rollout kairos/lid.yaml --node demure
```

Omit `--node` to update all managed workers. The command requires every selected worker to be Ready, excludes control-plane/master nodes, snapshots the selected names, and refuses to submit while another NodeOp in `kairos-operator` is active. Use one operator at a time. It creates a fresh NodeOp for each run, processes one node at a time, stops on failure and never reboots, cordons or drains nodes. Git pushes do not trigger deployment.

Rollout profiles live under `kairos/` and contain only `name` and `initramfs`/`boot` stages. Their files and commands run as host root; review them as infrastructure code. Keep credentials and role/installation profiles outside this workflow. The CLI embeds the public profile in the NodeOp, validates a temporary candidate with the host's native Kairos agent, then atomically replaces `/oem/91_guaranine-NAME.yaml` and syncs it. It activates the profile's `initramfs` and `boot` steps sequentially under temporary, content-derived stage names. Kairos v4.4.0 re-adds default configuration paths despite `--override-cloud-init-paths`; these dedicated names prevent unrelated boot stages from running. Unrelated private OEM files remain in place. Failed activation is reported; it does not automatically roll the configuration back.

The CLI prints the operation name and waits up to ten minutes for all selected nodes. Closing the terminal, Ctrl+C or a timeout leaves a submitted operation running in the cluster. Inspect it before creating another:

```bash
kubectl -n kairos-operator get nodeops
kubectl -n kairos-operator get nodeop OPERATION_NAME -o yaml
kubectl -n kairos-operator get jobs -l kairos.io/nodeop=OPERATION_NAME
kubectl -n kairos-operator logs job/JOB_NAME
```

Completed and failed operations retain status and Job logs. After reviewing a finished operation, delete its NodeOp to remove its owned Jobs. Correct a failed profile and submit a new operation; applying changes to an existing NodeOp does not start another run.

### Operator permissions and traffic

The operator runs on the cloud controller as a non-root container with a read-only root filesystem, dropped capabilities and the runtime seccomp profile. Upstream node metadata helpers run on each Kairos node with read-only host `/etc` access and permission to patch node metadata. Initial discovery also creates one completed Job per node. These helpers explain the extra node-management pods.

Only the `kairos-operator` namespace permits privileged maintenance Pods. A NodeOp mounts host `/` read-write and has host-administrator access; access to creating NodeOps and to this namespace is therefore administrator access. The controller also manages nodes, Pods, Jobs, DaemonSets, service accounts and RBAC for upstream discovery/reboot helpers. Its RBAC omits upstream build-only Secret, PVC and ConfigMap permissions, so in-cluster OSArtifact builds are unsupported. Application namespaces keep their restricted security policy.

Namespace network policies deny ingress and allow only Kubernetes API egress over the cluster/tailnet. The default service account has no mounted token or role binding; ordinary configuration Jobs do not need API credentials. The operator and metadata helpers use their dedicated service accounts. Container pulls use the node's normal registry access. Host activation uses system D-Bus (`SYSTEMCTL_FORCE_BUS=1`), because systemd's private socket cannot resolve clients in a Pod PID namespace. No new host firewall ports or public endpoints are needed. These controls do not sandbox a privileged host operation after compromise.

## OS updates

The installed operator also provides [NodeOpUpgrade](https://kairos.io/operator-docs/nodeop-upgrade/), which wraps native Kairos image upgrades and tracks node reboot completion. Use a tested **Guaranine public OS base**, built from this repository, with a pinned registry digest. The current image workflow builds private installation media and does not publish an upgrade image; never upload a private ISO, VHD or node configuration to a registry.

Schedule downtime, back up persistent data and use `kubectl drain NAME --ignore-daemonsets` before an OS upgrade. The pinned operator's built-in drain deletes Pods directly without the Eviction API, so keep `drainOptions.enabled: false` in the NodeOpUpgrade and perform the Kubernetes drain yourself. A single worker's local volumes cannot move to the controller; its applications will be unavailable during reboot. Resolve disruption-budget and capacity blockers before proceeding.

Create a fresh NodeOpUpgrade in `kairos-operator` with an explicit hostname selector, `concurrency: 1`, `stopOnFailure: true`, `cordon: true`, `rebootOnSuccess: true`, and the tested image. For a rebuilt custom image with unchanged Kairos/K3s version metadata, set `force: true`: upstream's preflight compares versions, not image digests. The node pulls the OS image; the upgrade Job then uses its local root filesystem (`dir:/`), so the namespace needs no registry egress exception. Upgrade publishing and a production OS upgrade are not yet qualified by this setup.

Keep secrets in OEM configuration, outside the public base. Verify active/passive rollback, node Ready, network isolation and persistent data after upgrades; then uncordon a node you manually cordoned. Terraform seed-image changes do not upgrade an installed OS; see [controller disk ownership](architecture.md#controller-resources).

Check the [hardware qualification](hardware.md#qualification-and-limits) whenever the base image, kernel, firmware or network configuration changes.
