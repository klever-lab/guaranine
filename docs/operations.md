# Operations

Use the existing operator files created in [SETUP.md](../SETUP.md#operator-files); set `GUARANINE_OPERATOR_DIR` to that directory in a new shell. Do not rerun initialization over an existing deployment.

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

The USB confirmation identifies the selected drive, asks for `ERASE` and explains a mismatch. Skipping or cancelling flashing leaves the ISO saved and prints the retry command; laptop boot instructions appear only after a successful USB write. Flashing uses `dd` with synchronized block writes and periodic byte/rate statistics, followed by a final flush before reporting success; updates can pause while USB I/O is pending.

Incomplete builds preserve outputs and protected logs. Inspect them before revoking the batch key, archiving outputs and retrying. Failed subprocesses can also leave protected `error-*.log` files. Do not put diagnostic contents in public issues or logs. The CLI does not automatically repeat key-creation POSTs after ambiguous transport failures; inspect the live Tailscale state before retrying.

`./guaranine controlplane` reuses a completed VHD under `nodes/guaranine-controlplane/` while its registration key is valid. An incomplete controller image needs inspection before another build. `bootstrap` refuses to overwrite an existing kubeconfig or `controlplane-known-hosts`; inspect those files instead of treating it as a repeatable repair command.

For worker retries, follow the [installation boundaries](../SETUP.md#install-a-worker), including the check after an interrupted remote install. [Enrollment limits](security.md#private-material-and-enrollment) explain why ending a batch does not retire its devices.

## Laptop discovery

The worker command reads the operator's local `tailscale status --json` and waits for an online peer with the `guaranine-live-` hostname prefix and `tag:guaranine-worker` tag. It checks every two seconds until a candidate appears; Ctrl+C cancels the wait. A single candidate is selected automatically; several candidates produce a numbered menu. It then waits up to two minutes for SSH startup. The operator must be connected to Tailscale. No LAN discovery or manual IP lookup is needed.

These names and tags identify candidates; they do not replace the SSH fingerprint verification or live-media check in the [worker installation procedure](../SETUP.md#install-a-worker).

Laptop names are suggested from DMI model information, with a suffix when a name already exists in Kubernetes or private operator outputs. Installation is issued once after disk confirmation. A lost installation connection leaves the outcome uncertain; the command can still confirm success if the installed worker subsequently joins. It waits up to ten minutes for Ready and requires a Tailscale internal address before reporting success. The [resume command](../SETUP.md#install-a-worker) performs only that verification, using saved operator outputs and pinned controller SSH; it does not require an unexpired installer key or contact the laptop for installation.

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

## OS updates

Use native `kairos-agent upgrade` with a tested image produced from this recipe and a pinned digest or private local image. Keep secrets in OEM configuration, outside the public base. Verify active/passive rollback and persistent data after upgrades. Terraform seed-image changes do not upgrade an installed OS; see [controller disk ownership](architecture.md#controller-resources).

Check the [hardware qualification](hardware.md#qualification-and-limits) whenever the base image, kernel, firmware or network configuration changes.
