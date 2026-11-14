# Monitoring

[kubernetes/monitoring.yaml](../kubernetes/monitoring.yaml) deploys Grafana, Prometheus, kube-state-metrics and one Tailscale Serve proxy. Apply it explicitly; node bootstrap only installs the default namespace policy. The manifest owns image versions and digests, resource limits, retention, provisioning, Serve configuration and workload permissions.

## Open Grafana

Connect to Tailscale and open <https://monitoring.tail3a78b9.ts.net>. Log in as `admin`. Read the generated password locally:

```bash
cat "${GUARANINE_OPERATOR_DIR:-$HOME/.local/state/guaranine-kairos}/monitoring/admin-password"
```

The Guaranine home dashboard shows node readiness, running/unready pods, restarts, and node/pod CPU and memory. CPU rates need at least two scrapes. Grafana's Explore page can query the provisioned Prometheus source directly. This setup does not collect logs or configure alert delivery.

Serve provides tailnet-only HTTPS with an automatically managed certificate. MagicDNS and HTTPS certificates must be enabled for the tailnet. Grafana verifies this hostname and sets Secure, SameSite strict cookies. The proxy is named `monitoring`; changing the tailnet suffix also requires updating Grafana's domain and root URL in the manifest. The [tailnet policy](security.md#tailnet-policy) controls who can connect.

The proxy reads its native Serve JSON from a whole-directory ConfigMap mount using `TS_SERVE_CONFIG`; Tailscale watches and reapplies changes. It runs in userspace without a TUN device, added capabilities, host networking or an operator. No NodePort, public ingress or custom DNS server is installed. The physical LAN firewall stays unchanged.

## Permissions and traffic

The namespace enforces restricted Pod Security. All four containers run as non-root, drop capabilities, refuse privilege escalation and use read-only root filesystems. Grafana has no mounted Kubernetes token. Its admin password and encryption key come from a Kubernetes Secret, generated outside Git in protected operator storage.

The proxy's ServiceAccount can only get, update and patch the precreated `monitoring-tailscale` Secret. It cannot create or list Secrets or read Grafana credentials. A one-use tagged auth key seeds that Secret during enrollment; Tailscale removes it after login and stores its persistent identity there. Recreating the proxy pod preserves its tailnet address and name. Retain this Secret with the controller's encrypted K3s backup. Losing it requires retiring the old tailnet device and enrolling a replacement. Local proxy volumes are disposable caches and sockets.

NetworkPolicy denies ingress and egress by default. The exceptions are:

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

The Services are ClusterIP only. Grafana accepts pod traffic only from the Serve proxy. Grafana, Prometheus and kube-state-metrics have no general internet access. The proxy's internet exception allows any public endpoint on the listed protocols; standard NetworkPolicy does not restrict this to Tailscale domain names. Its other private destinations are limited to DNS, Grafana and the Kubernetes API. Grafana disables signup, anonymous access, telemetry, update checks, plugin installation and its unauthenticated metrics endpoint; it enables a Content Security Policy.

Prometheus discovers nodes and reads `nodes/metrics`. It does not receive `nodes/proxy`, which can authorize container execution. Kubelet certificates are verified against the cluster CA, including their Tailscale IP SANs. Node discovery keeps only Tailscale addresses. Kube-state-metrics can list/watch the workload and volume types it exports, including pods, but cannot read Secrets, ConfigMaps or RBAC objects or change resources. Network permissions do not replace these API permissions. The [networking guide](security.md) owns host firewall and tailnet policy changes.

## Storage and reapplication

Both retained local volumes use `demure`'s persistent Kairos filesystem:

| Directory on demure | Owner UID:GID | Volume |
| --- | --- | --- |
| `/usr/local/guaranine-monitoring/grafana` | `472:472` | Grafana database, users and saved dashboards |
| `/usr/local/guaranine-monitoring/prometheus` | `65534:65534` | Prometheus metrics |

The directories must exist before applying the manifest. Create them on the worker over pinned Tailscale SSH with `install -d -m 0700 -o 472 -g 472 /usr/local/guaranine-monitoring/grafana` and the corresponding command using `65534` for `prometheus`. Do not use `/var`'s temporary overlay. These existing static local PVs stay explicitly bound and satisfy the restricted workload policy. New applications can use [automatic storage provisioning](storage.md); installing it does not migrate monitoring data.

Follow [Optional monitoring](../SETUP.md#optional-monitoring) for a fresh deployment. Reapply public configuration with `kubectl apply -f kubernetes/monitoring.yaml`; the state Secret declaration preserves Tailscale-managed data. Grafana's file mounts and Prometheus do not automatically reload ConfigMaps: restart the affected deployment after changing them. Serve watches its own configuration. Keep the proxy at one replica with the `Recreate` strategy so two pods never share its identity concurrently. Its readiness probe checks for a tailnet address; liveness checks the local listener so a WAN outage does not trigger restarts. The admin-password file seeds a new database only; changing it does not reset an existing Grafana account. Preserve `secret-key` with the database.

Local volumes stay on `demure`; monitoring is unavailable while that laptop is offline. Volume capacities are scheduling declarations, not filesystem quotas. Prometheus prunes by its configured age or size, whichever comes first, and needs free disk space for its WAL and temporary blocks. Back up the private files and Grafana data separately from the controller's K3s backup; stop Grafana briefly for a consistent database copy. Metrics are local history and are not an off-machine backup.

To move the volumes, stop their deployments and fence an unreachable old worker first. Copy their directories with ownership preserved to the replacement's persistent filesystem, then replace the static PV definitions with that worker's hostname and rebind the PVCs. Their `Retain` policy preserves data when claims are deleted. Do not delete the monitoring namespace as an update procedure. Controller replacement uses the normal [cloud restore procedure](operations.md#restore-on-another-cloud-vm); this monitoring stack has no Azure dependency.

## Check collection

```bash
kubectl -n monitoring get pods,pvc
kubectl -n monitoring logs deployment/monitoring-proxy --tail=30
kubectl -n monitoring logs deployment/prometheus --tail=30
kubectl -n monitoring logs deployment/kube-state-metrics --tail=30
```

For scrape errors, temporarily forward `service/prometheus` to operator loopback on port 9090 and inspect <http://127.0.0.1:9090/targets>. Both nodes' cAdvisor targets and kube-state-metrics should be up. Do not expose this unauthenticated Prometheus endpoint beyond loopback.

Verified on 2026-11-13: all four deployments became Ready, all four Prometheus targets were up, and all eight dashboard queries returned data. Trusted HTTPS, the existing Grafana login and Secure/HttpOnly/SameSite cookies worked; anonymous dashboard access returned 401. Reapplication and proxy replacement preserved its tailnet identity, with the enrollment key removed and Funnel unavailable. Grafana reused its database and Prometheus retained earlier samples after pod replacement. Live probes confirmed permitted backend/API/HTTPS access for the proxy and denied Prometheus, exporter, kubelet and public HTTP access. A probe on the controller could not reach monitoring services or proxy health directly from another namespace. Earlier Grafana probes also confirmed its permitted Prometheus/DNS access and denied API, kubelet, exporter and public-internet access. Full OS reboot persistence was not tested.

The components and permissions follow [Grafana provisioning](https://grafana.com/docs/grafana/latest/administration/provisioning/), [Prometheus Kubernetes discovery](https://prometheus.io/docs/prometheus/latest/configuration/configuration/#kubernetes_sd_config), [Kubernetes kubelet authorization](https://kubernetes.io/docs/reference/access-authn-authz/kubelet-authn-authz/), and [Tailscale container configuration](https://tailscale.com/docs/features/containers/docker/docker-params).
