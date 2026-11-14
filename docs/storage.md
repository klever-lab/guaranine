# Application storage

[kubernetes/storage.yaml](../kubernetes/storage.yaml) installs only OpenEBS Local PV Hostpath. Its `openebs-hostpath` StorageClass is the cluster default. The manifest owns both upstream image pins, permissions, directory configuration and network policies. K3s's bundled `local-storage` component stays disabled to avoid a second provisioner.

The controller runs as one non-root pod on the cloud control plane. For each new claim, it launches a temporary helper on the selected worker to create `/usr/local/openebs/local/pvc-<claim-uid>`. Kairos persists `/usr/local`; the worker's `/var` overlay is unsuitable for application data. No per-application SSH or directory preparation is required. See [setup](../SETUP.md#application-storage) for installation.

## Request storage

Create the application's namespace and explicit network policies alongside its deployment. Application namespaces retain restricted Pod Security. A claim can omit `storageClassName` to use the default:

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: app-data
spec:
  accessModes: [ReadWriteOnce]
  resources:
    requests:
      storage: 5Gi
```

Mount it in the application's pod and set a filesystem group appropriate for that application:

```yaml
spec:
  securityContext:
    runAsNonRoot: true
    runAsUser: 1000
    runAsGroup: 1000
    fsGroup: 1000
    fsGroupChangePolicy: OnRootMismatch
    supplementalGroupsPolicy: Strict
    seccompProfile: {type: RuntimeDefault}
  containers:
    - name: app
      # Set the application's pinned image, command and resource limits here.
      securityContext:
        allowPrivilegeEscalation: false
        readOnlyRootFilesystem: true
        capabilities: {drop: [ALL]}
      volumeMounts:
        - {name: data, mountPath: /data}
  volumes:
    - name: data
      persistentVolumeClaim: {claimName: app-data}
```

Directories start with mode `0770`. OpenEBS creates Kubernetes `local` PVs; kubelet applies the pod's `fsGroup` so non-root applications can write without world-writable directories or root init containers. Choose the application's actual IDs rather than changing existing data ownership blindly.

`WaitForFirstConsumer` leaves a claim Pending until a pod uses it, allowing the scheduler to choose a suitable worker first. Use a node selector or affinity if placement matters; do not set `nodeName`, which bypasses that scheduling step. Once provisioned, the volume's node affinity keeps its consumers on that worker. The control-plane taint keeps ordinary applications off the server.

## Permissions and network access

The `openebs` namespace is trusted infrastructure with privileged Pod Security enforcement and restricted audit/warnings. Only storage components belong there. Upstream helpers run as privileged root with the storage base directory mounted at `/data`; they use a separate ServiceAccount with no RoleBindings and no automatically mounted API token. They use pod networking, with no host network, PID or IPC namespace. Namespace NetworkPolicy denies their ingress and egress.

The controller itself has a read-only root filesystem, dropped capabilities, RuntimeDefault seccomp and no host mounts. Its API permissions cover reading nodes/StorageClasses, managing PVs and updating claims, writing events, and creating/getting/deleting helper pods only in `openebs`. It cannot read Secrets, modify RBAC, execute into pods, access `nodes/proxy` or create pods in application namespaces. Its only permitted network destination is the Kubernetes API; analytics is disabled. There is no storage Service or host port.

Creating privileged helpers makes this controller part of the host trust boundary. Token removal, RBAC and NetworkPolicy reduce exposure; they do not sandbox a privileged helper after host compromise. Application namespace restrictions remain unchanged. Keep the default protection against PVC-supplied base-path overrides enabled; application claims must not choose arbitrary host directories.

## Capacity, outages and backups

This is local storage on existing worker disks. It does not replicate data between laptops. If the owning worker is offline, its stateful application remains unavailable until it returns or its data is deliberately restored elsewhere. A bound claim cannot transparently follow a pod to another worker. Fence an unreachable old worker before moving its data or reusing its identity.

Requested PVC sizes are declarations, not enforced filesystem quotas or reserved disk capacity. All volumes share the worker's free space. Monitor disk usage and keep headroom; a full persistent filesystem can affect other workloads and node services. This configuration does not enable quotas, online expansion or volume snapshots.

`Retain` preserves the volume directory after claim deletion; it does not make a backup or automatically rebind that volume to another claim. Back up application data separately from the controller's K3s database, outside the worker and the cloud account. Use an application-consistent export or stop its writer before copying files with ownership preserved. Keep the corresponding PV metadata so its path and worker are known during recovery.

## Existing volumes and cleanup

Existing static PVs and claims continue to work. In particular, monitoring explicitly sets `storageClassName: ""` and binds named volumes; adding this default does not change those claims or move their data. [Monitoring](monitoring.md#storage-and-reapplication) owns their paths and recovery procedure. Reapplying the storage manifest does not recreate or migrate application volumes.

Adopt automatic provisioning when deploying a new application. For an existing application that needs migration, back up its data, stop all writers, create a new claim and restricted copy pod on the existing worker, and copy into the new volume with ownership preserved. Verify the application's data before retiring the old claim and retained volume. Do not delete a claim or edit its bound `volumeName`/storage class as an in-place migration technique.

Inspect volume ownership before cleanup:

```bash
kubectl get pvc -A
kubectl get pv -o wide
kubectl get pv VOLUME_NAME -o yaml
```

After deliberate claim deletion, a retained PV becomes Released and its directory remains. To reuse the data, stop old writers, explicitly prebind a replacement claim and clear the old PV claim reference only after checking its identity. To discard it, verify backups and the exact PV path/worker, remove only that volume directory over pinned Tailscale SSH, then delete the Released PV object. Never remove the storage base directory or delete the `openebs` namespace as an update procedure.

## Verification

Check the controller and claims with:

```bash
kubectl -n openebs rollout status deployment/openebs-localpv
kubectl -n openebs logs deployment/openebs-localpv --tail=40
kubectl get storageclass,pv
kubectl get pvc -A
```

Verified on 2026-11-13 against demure: a fresh claim selected the default class and waited for its consumer. Without manual directory preparation, a restricted pod running as UID 1000 and primary GID 2000 wrote using `fsGroup: 3000`; its volume directory became mode `2770`, group 3000. A PVC-supplied base-path override did not change the configured storage location. The data survived pod replacement and claim deletion, then remained readable through an explicitly prebound replacement claim. Only the disposable test volume was switched to Delete and reclaimed through the upstream cleanup helper.

Actual helper pod specifications contained the pinned image and storage-base host mount, with no API-token volume or host namespaces. Thirteen RBAC checks passed. Live network probes with successful connectivity baselines confirmed controller API access, denied its public HTTPS/DNS access, and denied helper API/public HTTPS/DNS access. Application admission still rejected privileged hostPath pods. Existing monitoring PV/PVC identities and specifications stayed unchanged, all original pods kept zero restarts, and Grafana's database health remained OK. Repository validation passed. All test resources were removed; a worker reboot was not performed or verified.

The upstream [Hostpath guide](https://openebs.io/docs/main/user-guides/local-storage-user-guide/local-pv-hostpath/hostpath-overview), [file permissions](https://github.com/openebs/dynamic-localpv-provisioner/blob/v4.6.0/docs/tutorials/hostpath/filepermissions.md) and [helper implementation](https://github.com/openebs/dynamic-localpv-provisioner/blob/v4.6.0/cmd/provisioner-localpv/app/helper_hostpath.go) describe this configuration. Coordinate a worker reboot check before claiming reboot persistence.
