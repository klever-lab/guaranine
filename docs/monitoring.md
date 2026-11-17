# Monitoring

[kubernetes/monitoring.yaml](../kubernetes/monitoring.yaml) deploys Grafana, Prometheus, kube-state-metrics, one Tailscale Serve proxy and the worker battery readers. Apply it explicitly; node bootstrap only installs the default namespace policy. The manifest owns image versions and digests, resource limits, retention, provisioning, Serve configuration and workload permissions.

## Terminal status

Run from the operator computer for a snapshot that works without Grafana or Prometheus:

```bash
./guaranine status
```

The command shows Guaranine-tagged Tailscale devices, Kubernetes node readiness and pressure, Deployment/StatefulSet/DaemonSet readiness, unhealthy or terminating Pods with scheduling errors, and PVC bindings with their storage nodes. A bound local volume on an unavailable node is flagged explicitly. Completed Pods and workloads intentionally set to zero replicas are shown without being treated as failures. Peer presence is informational for sleeping readers and stopped application gateways; an offline display kiosk needs attention.

The Nodes table shows model, role, readiness and each worker's battery percentage and charging state, for example `BAT0: 82% Charging`. The model comes from host DMI; Lenovo's descriptive product version is used instead of its short product code. Missing DMI reports `Unknown`; an unreachable reader reports `Unavailable`. Node condition messages appear below the table instead of in a Detail column. Multiple batteries are listed separately; a removed battery, missing capacity value and no battery are distinguished. Peripheral batteries are excluded. Offline nodes, absent reader Pods, access errors and read timeouts show battery `Unavailable`, which makes the overall result need attention. The controller has no laptop model or battery check. Readings are current snapshots, not cached values or battery-health estimates.

The `battery-status` DaemonSet runs a pinned upstream BusyBox `sleep` on each Linux worker. `status` uses `kubectl exec` to read the kernel's DMI and power-supply sysfs through those existing Pods, with eight parallel reads at most and ten seconds per read. The reader has no service-account token, network port, host mount or privileged namespace, runs as non-root with a read-only root filesystem and dropped capabilities, and inherits the monitoring namespace's deny policies. No SSH key or battery daemon is installed on the host. The caller needs access to `pods/exec` in `monitoring`; the fixed command only reads model and battery files.

It also checks Grafana's HTTPS health endpoint using the URL in the monitoring manifest. Each network check has a timeout, and unavailable checks leave the independent checks running. It returns exit code `0` when the checks pass or `1` for unhealthy or unknown status. It reads Kubernetes, battery files and local Tailscale state without changing workload configuration or requiring a Tailscale API token, SSH access, or Grafana credentials. Normal kubectl context selection applies; an explicit `GUARANINE_OPERATOR_DIR` selects that directory's kubeconfig.

This is a current health snapshot, without the dashboard's historical CPU, memory or disk-usage charts. If the Kubernetes API is down, node, workload and storage health is reported as unknown while Tailscale presence and Grafana availability are still checked. Tailscale presence does not prove an application or the Pi's browser is working.

## Open Grafana

Connect to Tailscale and open <https://monitoring.tail3a78b9.ts.net>. Log in as `admin`. Read the generated password locally:

```bash
cat "${GUARANINE_OPERATOR_DIR:-$HOME/.local/state/guaranine-kairos}/monitoring/admin-password"
```

The **Services** panel in the cluster summary row links to the cluster's web applications. Update its links in the `overview.json` section of the monitoring manifest when adding or retiring an application.

Charts show host/pod CPU and memory. Percentage charts use automatic precision, showing whole percentages on their axes while retaining fractional readings in tooltips. The host charts include Kubernetes nodes and the standalone `grafana-pi` display. The Pi remains outside Kubernetes and is excluded from cluster node counts and disk totals.

The running-pods table groups pods by service using their namespace, with friendly names for Jellyfin, Argument Arena, Monitoring, Kairos, DNS and Storage. Each service row shows its pod count and the total CPU and memory of its running pods. Expanding a service lists each pod, its node, and that pod's CPU and memory; new namespaces appear automatically under their own names. Its footer counts services and sums pods, CPU and memory. Summary cards immediately above the table show those same totals plus node readiness, disk usage and capacity. The Ready nodes card shows `ready/total` across all registered Kubernetes nodes, counting nodes with a true Ready condition as ready. Services means namespaces with running pods, rather than Kubernetes Service objects. Disk totals count each mounted block filesystem once per node using cAdvisor's root filesystem metrics for SATA/SCSI, virtio, NVMe and eMMC devices. They include OS and workload data across reporting nodes, exclude RAM, overlays, loop-mounted OS images and unmounted disks, and do not measure disk usage per service or pod.

The cluster summary cards stack centered labels above their values within one compact row. Labels use the card body rather than panel headers so they remain readable on the Pi's 1280×1024 display.

The table refreshes with the dashboard and shows pod CPU in cores and memory in bytes, as do the summary cards. Host CPU is the five-minute average usage as a percentage of each machine's total CPU capacity. Kubernetes node memory is the working set as a percentage of total RAM; Pi memory is total minus available RAM as a percentage of total RAM. The pod charts show each pod's five-minute average CPU usage and memory working set as percentages of its node's total CPU capacity and RAM, using the same 0–100% scales as the host charts. CPU rates need at least two scrapes. The companion `OVH Cloud` dashboard (`/d/guaranine-ovh`, linked in the top dashboard bar) tracks the `BHS5` controller status, uptime, `d2-2` CPU/RAM/disk capacity bars, static catalog CAD billing table with a sum footer row, and live `d2-2` CPU/PSI contention, memory breakdown, disk write I/O, and `ens3` versus `tailscale0` network throughput without storing cloud credentials in the cluster. Grafana's Explore page can query the provisioned Prometheus source directly. This setup does not collect logs or configure alert delivery.

Serve provides tailnet-only HTTPS with an automatically managed certificate. MagicDNS and HTTPS certificates must be enabled for the tailnet. Grafana verifies this hostname and sets Secure, SameSite strict cookies. The proxy is named `monitoring`; changing the tailnet suffix also requires updating Grafana's domain and root URL in the manifest. The [tailnet policy](security.md#tailnet-policy) controls who can connect.

The proxy reads its native Serve JSON from a whole-directory ConfigMap mount using `TS_SERVE_CONFIG`; Tailscale watches and reapplies changes. It runs in userspace without a TUN device, added capabilities, host networking or an operator. No NodePort, public ingress or custom DNS server is installed. The physical LAN firewall stays unchanged.

## Permissions and traffic

The namespace enforces restricted Pod Security. All containers run as non-root, drop capabilities, refuse privilege escalation and use read-only root filesystems. Grafana has no mounted Kubernetes token. Its admin password and encryption key come from a Kubernetes Secret, generated outside Git in protected operator storage.

The proxy's ServiceAccount can only get, update and patch the precreated `monitoring-tailscale` Secret. It cannot create or list Secrets or read Grafana credentials. A one-use tagged auth key seeds that Secret during enrollment; Tailscale removes it after login and stores its persistent identity there. Recreating the proxy pod preserves its tailnet address and name. Retain this Secret with the controller's encrypted K3s backup. Losing it requires retiring the old tailnet device and enrolling a replacement. Local proxy volumes are disposable caches and sockets.

NetworkPolicy denies ingress and egress by default. Battery readers have no network exceptions apart from the namespace's shared DNS rule. The other exceptions are:

| Source | Destination | Access |
| --- | --- | --- |
| Monitoring pods | Cluster DNS | TCP/UDP 53 |
| Serve proxy | Grafana | TCP 3000 |
| Serve proxy | Kubernetes API | HTTPS through the cluster service or Tailscale TCP 6443 |
| Serve proxy | Public internet | TCP 443 and UDP for Tailscale control, relays, peer connections and certificate issuance |
| Grafana | Prometheus | TCP 9090 |
| Prometheus | kube-state-metrics | TCP 8080 |
| Prometheus and kube-state-metrics | Kubernetes API | HTTPS through the cluster service or Tailscale TCP 6443 |
| Prometheus | Tailscale node addresses | HTTPS TCP 10250 |
| Prometheus | Standalone Grafana Pi Tailscale address | TCP 9100 |

The Services are ClusterIP only. Grafana accepts pod traffic only from the Serve proxy. Grafana, Prometheus and kube-state-metrics have no general internet access. The proxy's internet exception allows any public endpoint on the listed protocols; standard NetworkPolicy does not restrict this to Tailscale domain names. Its other private destinations are limited to DNS, Grafana and the Kubernetes API. Grafana disables signup, anonymous access, telemetry, update checks, plugin installation and its unauthenticated metrics endpoint; it enables a Content Security Policy.

Prometheus discovers nodes and reads `nodes/metrics`. It does not receive `nodes/proxy`, which can authorize container execution. Kubelet certificates are verified against the cluster CA, including their Tailscale IP SANs. Node discovery keeps only Tailscale addresses. Kube-state-metrics can list/watch the workload and volume types it exports, including pods, but cannot read Secrets, ConfigMaps or RBAC objects or change resources. Network permissions do not replace these API permissions. The [networking guide](security.md) owns host firewall and tailnet policy changes.

## Storage and reapplication

Grafana's retained local volume uses the persistent Kairos filesystem on the worker pinned in [the monitoring manifest](../kubernetes/monitoring.yaml):

| Directory on pinned worker | Owner UID:GID | Volume |
| --- | --- | --- |
| `/usr/local/guaranine-monitoring/grafana` | `472:472` | Grafana database, users and saved dashboards |

The directory must exist before applying the manifest. Create it on the worker over pinned Tailscale SSH with `install -d -m 0700 -o 472 -g 472 /usr/local/guaranine-monitoring/grafana`. Do not use `/var`'s temporary overlay. Its static local PV stays explicitly bound and satisfies the restricted workload policy. New applications can use [automatic storage provisioning](storage.md); installing it does not migrate monitoring data.

Prometheus uses a disk-backed `emptyDir` limited to 10 GiB. It can restart on another worker without a PVC; replacing its pod starts a new metrics history. Its age/size retention limits still apply while the pod exists. The former `prometheus` PVC and `monitoring-prometheus` PV are no longer declared or consumed. Applying without pruning leaves existing copies and their old history intact for recovery; do not remove them as part of this update.

Follow [Optional monitoring](../SETUP.md#optional-monitoring) for a fresh deployment. Reapply public configuration with `kubectl apply -f kubernetes/monitoring.yaml`; the state Secret declaration preserves Tailscale-managed data. Grafana's file mounts and Prometheus do not automatically reload ConfigMaps: restart the affected deployment after changing them. Serve watches its own configuration. Keep the proxy at one replica with the `Recreate` strategy so two pods never share its identity concurrently. Its readiness probe checks for a tailnet address; liveness checks the local listener so a WAN outage does not trigger restarts. The admin-password file seeds a new database only; changing it does not reset an existing Grafana account. Preserve `secret-key` with the database.

Grafana enables SQLite write-ahead logging so dashboard reads can proceed during writes. Its health probes allow brief storage delays, with the upstream Grafana chart's 30-second liveness timeout and ten-failure threshold; readiness still removes an unhealthy instance from service. This avoids the former one-second liveness deadline turning database contention into repeated forced restarts.

Grafana stays pinned by its local volume and is unavailable while that laptop is offline. Its volume capacity is a scheduling declaration, not a filesystem quota. Prometheus prunes by its configured age or size, whichever comes first, and needs free disk space for its WAL and temporary blocks. Back up the private files and Grafana data separately from the controller's K3s backup; stop Grafana briefly for a consistent database copy. Temporary metrics are not a backup.

To move Grafana's volume, stop its deployment and fence an unreachable old worker first. Copy its directory with ownership preserved to the replacement's persistent filesystem, then replace the static PV definition with that worker's hostname and rebind the PVC. Its `Retain` policy preserves data when the claim is deleted. Do not delete the monitoring namespace as an update procedure. Controller replacement uses the normal [cloud restore procedure](operations.md#restore-on-another-cloud-vm).

## Check collection

The `grafana-pi` scrape job collects Node Exporter metrics from the standalone display. Its target address and egress exception live in [the monitoring manifest](../kubernetes/monitoring.yaml). These metrics are separate from Kubernetes node discovery. Query `up{job="grafana-pi"}` in Explore to check collection; it becomes available after the Pi boots.

```bash
kubectl -n monitoring get pods,pvc
kubectl -n monitoring logs deployment/monitoring-proxy --tail=30
kubectl -n monitoring logs deployment/prometheus --tail=30
kubectl -n monitoring logs deployment/kube-state-metrics --tail=30
```

For scrape errors, temporarily forward `service/prometheus` to operator loopback on port 9090 and inspect <http://127.0.0.1:9090/targets>. Both nodes' cAdvisor targets and kube-state-metrics should be up. Do not expose this unauthenticated Prometheus endpoint beyond loopback.

Verified on 2026-11-13: all four deployments became Ready, all four Prometheus targets were up, and all eight dashboard queries returned data. Trusted HTTPS, the existing Grafana login and Secure/HttpOnly/SameSite cookies worked; anonymous dashboard access returned 401. Reapplication and proxy replacement preserved its tailnet identity, with the enrollment key removed and Funnel unavailable. Grafana reused its database and Prometheus retained earlier samples after pod replacement. Live probes confirmed permitted backend/API/HTTPS access for the proxy and denied Prometheus, exporter, kubelet and public HTTP access. A probe on the controller could not reach monitoring services or proxy health directly from another namespace. Earlier Grafana probes also confirmed its permitted Prometheus/DNS access and denied API, kubelet, exporter and public-internet access. Full OS reboot persistence was not tested.

The components and permissions follow [Grafana provisioning](https://grafana.com/docs/grafana/latest/administration/provisioning/), [Prometheus Kubernetes discovery](https://prometheus.io/docs/prometheus/latest/configuration/configuration/#kubernetes_sd_config), [Kubernetes kubelet authorization](https://kubernetes.io/docs/reference/access-authn-authz/kubelet-authn-authz/), and [Tailscale container configuration](https://tailscale.com/docs/features/containers/docker/docker-params).

## Raspberry Pi dashboard display

The Ethernet-connected Pi 2 Model B runs a dedicated native display session, independently of Kubernetes. [The OS profile](../kiosk/config.yaml) owns its users, browser supervision, screen settings, first-boot updates and service configuration. [The asset pins](../kiosk/versions.json) own the Raspberry Pi OS 32-bit image and prebuilt ARM binaries. [Setup](../SETUP.md#raspberry-pi-dashboard-display) owns the boot instructions; [security](security.md) owns enrollment, private media and network access.

The session starts Chromium through the upstream Grafana Kiosk utility and opens the existing Guaranine dashboard using the `grafana-pi` Viewer account. Openbox keeps the desktop running across browser restarts; each new desktop session starts the browser service with its current display credentials. The browser restarts hourly to renew login and recover accumulated or stuck state. Grafana Kiosk has no page-health watchdog, so a failed login can remain until that restart. The display user has no sudo access; the separate `guaranine` account accepts the operator's SSH key.

The protected operator directory's `kiosk/` contains the prepared image, individual device record, Viewer password, pinned SSH configuration and preparation logs. It is outside the checkout. Use its native SSH configuration after boot:

```bash
operator_dir="${GUARANINE_OPERATOR_DIR:-$HOME/.local/state/guaranine-kairos}"
ssh -F "$operator_dir/kiosk/ssh_config" grafana-pi
```

Back up that protected directory. The [card recovery command](../SETUP.md#raspberry-pi-dashboard-display) requires `grafana-pi.img` and its `prepared-image.json` size/checksum record, both operator-owned with private permissions. It restores the saved image as-is, including the existing Tailscale identity, SSH host key and Viewer credentials; keep that identity registered and the Viewer account valid. It needs no fresh enrollment key or cluster API connection. Refresh the prepared image and its checksum together when changing the kiosk profile; the command does not rebuild it from source.

Restart the display session with `sudo systemctl restart lightdm`. Check the connection and metric services with `sudo systemctl status tailscaled guaranine-tailscale guaranine-node-exporter`. Inspect browser logs with `sudo journalctl _UID=1000 _SYSTEMD_USER_UNIT=guaranine-kiosk.service --no-pager -n 50`. If first boot has not reached the display, inspect `sudo systemctl status cloud-final` for update progress. Plymouth's wait service is masked so it cannot block that initialization stage. A complete OS hang still needs a local power cycle. The Pi automatically resumes the installed setup after power returns; the monitor must also be powered on and using the correct HDMI input.

OS and browser updates run during first boot. Later OS/browser updates and upgrades of the pinned standalone binaries require operator maintenance; no unattended updater is configured.

Software checks on 2026-11-15 verified upstream image/binary checksums, the recovery filesystem and public file permissions, boot/root partition readback, private cloud-init schema and ARM utilities under Cortex-A7 emulation. Packet tests cover IPv4/IPv6 LAN isolation, late SLAAC prefixes, DNS, tailnet SSH/metrics, marked encrypted transport and interface down/up. Tailnet policy validation passed.

Physical checks the same day verified first-boot updates, automatic Viewer login and dashboard rendering. A controlled reboot loaded the updated kernel, restarted the display and monitoring services, and preserved the Tailscale identity; Prometheus reported `up{job="grafana-pi"}=1` afterward. Chromium allowed only the Grafana HTTPS host, rejecting other sites, plain HTTP and subdomains using its [documented URL filter format](https://www.chromium.org/administrators/url-blocklist-filter-format/). No undervoltage flags were recorded during boot, updates or reboot.

Two accelerated 90-second browser-service runtime expiries preserved the desktop session, and killing Chromium recovered automatically to the dashboard. The normal one-hour interval was restored. Network/Grafana outage and hard-power-loss recovery, detailed monitor readability and the 48-hour stability test remain pending.
