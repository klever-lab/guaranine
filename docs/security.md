# Networking and security

## Tailnet policy

[tailscale/policy.json](../tailscale/policy.json) owns the complete policy for this tailnet, including existing personal access. Only admins own the Guaranine node tags. Node grants permit:

| Source | Destination | Port | Purpose |
| --- | --- | --- | --- |
| Worker | Controller | TCP 6443 | Kubernetes API |
| Controller | Worker | TCP 10250 | Kubelet |
| Worker | Both node roles | TCP 10250 | Authenticated kubelet metrics |
| Both node roles | Both node roles | UDP 8472 | Flannel VXLAN |
| Operator | Nodes | TCP 22 | Ordinary key-based SSH |
| Operator | Monitoring proxy | TCP 443 | Private Grafana HTTPS |
| Kiosk | Monitoring proxy | TCP 443 | Private dashboard display |
| Worker | Kiosk | TCP 9100 | Prometheus host metrics |

Nodes cannot administer peers. The policy retains the operator's broader personal access; the table describes cluster traffic, not a replacement for the complete policy.

The monitoring proxy has its own admin-owned tag, with no grants to reach other tailnet devices. Cluster node tags and unrelated users cannot connect to it. Its Serve configuration disables Funnel. The [monitoring guide](monitoring.md) owns its Kubernetes permissions and traffic.

The display has the admin-owned `tag:guaranine-kiosk`. It can reach Grafana but has no grant to administer cluster nodes or personal devices. Operators retain ordinary key-based SSH access. Its [OS profile](../kiosk/config.yaml) installs the firewall before NetworkManager starts.

NetworkManager runs [ordinary dispatcher scripts serially](https://manpages.debian.org/trixie/network-manager/NetworkManager-dispatcher.8.en.html). The Pi's up/down and DHCP hooks refresh its attached IPv4 subnet and active-interface sets from current kernel state; physical egress stays blocked until those sets are populated. The Pi permits tailnet SSH and metrics, network setup and DNS, and encrypted Tailscale transport. Ordinary physical internet egress uses IPv4; physical IPv6 is restricted to network setup, DNS and marked Tailscale transport. Direct LAN traffic remains blocked, including attached public subnets and later SLAAC prefixes.

Application gateways can use the admin-owned `tag:guaranine-app-gateway`, with no grants to reach other tailnet devices. Each application repository owns its private Serve deployment and client access restrictions. The tag has no Funnel permission, and application gateways must keep `AllowFunnel` disabled.

Jellyfin is the sole public-access exception. Its existing gateway uses the separate admin-owned `tag:guaranine-jellyfin`, which alone has Funnel permission and has no grants to administer other tailnet devices. Jellyfin's repository owns its HTTPS proxy and exact public source-IP allowlist. General application gateways, cluster nodes, monitoring and ordinary tailnet members have no Funnel permission.

The reader uses the admin-owned `tag:guaranine-kobo`, with one grant to the Books gateway on TCP 443. The `guaranine-books` host alias pins that gateway's enrolled Tailscale address; update it deliberately if the identity is replaced. The reader cannot administer nodes or reach qBittorrent, monitoring or Jellyfin, and has no Funnel permission. Its installer and credentials belong to the application repositories and protected operator storage.

Before applying it, back up the live policy outside the checkout. Use the standard [Access controls editor](https://console.tailscale.com/admin/acls); for API updates, validate first, use the live ETag with `If-Match` and read back the result. CI checks JSON syntax without credentials and never applies the live policy.

The cluster nodes use Tailscale kernel networking and persistent identity, with subnet-route acceptance, Tailscale SSH and host DNS changes disabled. K3s uses Tailscale node addresses and binds Flannel to `tailscale0`; Flannel derives its MTU from that interface. Administration, API, kubelet and cross-node pod traffic use the tailnet. Cloud/LAN addresses are never cluster endpoints. Other operator VPNs must preserve the `100.64.0.0/10` routes.

This BHS5 project has zero quota for custom security groups and rules, so the VM uses OVH's default group. The native host firewall restricts physical ingress to encrypted Tailscale transport on UDP 41641 and rejects direct private/LAN traffic. SSH and the Kubernetes API are reachable only over the tailnet. The VM's public interface supplies internet access for Tailscale/DERP and downloads. Real direct/relayed paths, reconnects and temporary WAN outages still need live-network verification.

## LAN isolation

Wi-Fi client isolation is useful but never assumed. The host nftables rules in [kairos/config.yaml](../kairos/config.yaml) block direct LAN traffic from hosts and pods, including attached subnets using public addresses. Systemd-networkd updates those subnet sets as addresses change. Ordinary physical egress stays blocked until the network service registers the interface.

Exceptions allow network-service DHCP, system-resolver DNS, required IPv6 link control and encrypted Tailscale transport. Cluster pod interfaces permit TCP 6443 and 10250 to the host for the authenticated API and kubelet; application network policies and Kubernetes authorization still apply. Unsolicited physical traffic cannot reach host services or pods; internet replies remain allowed. SSH and networking start after the firewall. [Packet tests](development.md#firewall-packet-checks) exercise the actual rules without relying on access-point isolation.

## Workload restrictions

The server enables K3s secrets encryption and the standard restricted Pod Security policy: no privileged pods or host namespaces, no privilege escalation and dropped capabilities. `kube-system` is exempt and reserved for trusted system components. Dedicated namespaces permit [OpenEBS storage helpers](storage.md#permissions-and-network-access) and [Kairos host maintenance](operations.md#operator-permissions-and-traffic); those guides own their permissions and traffic.

[kubernetes/network-policy.yaml](../kubernetes/network-policy.yaml) denies application ingress and egress in the default namespace, with a cluster-DNS exception. The controller installs this manifest through K3s's native server manifest directory. Add explicit ingress/egress rules alongside applications. New application namespaces need their own policies.

The current K3s network-policy controller installs Pod rules asynchronously: a newly started Pod can send traffic before those rules arrive. Verify connectivity after policy convergence; NetworkPolicy is not a startup isolation barrier. The host's LAN filtering still applies during this window.

Allowed destinations remain reachable by compromised applications. Firewall rules and removal of utilities cannot guarantee protection after host-root or kernel compromise.

## Private material and enrollment

Use the [operator directory](../SETUP.md#operator-files) on encrypted storage with an off-machine backup. The CLI creates directories with mode 0700 and private files with mode 0600, refuses private paths inside the checkout, and rejects private inputs with symlinks, incorrect ownership or group/other permissions. Gitignore is a second line of defense, not the storage boundary.

Only public inputs enter Docker layers and the OS base's build cache. Private machine configuration reaches pinned AuroraBoot through stdin, using protected scratch storage; private builds disable Docker's log driver. Errors go to protected diagnostic files. Private images, configurations and logs must never enter a public registry or GitHub artifacts. The controller QCOW2 goes only to private OVH Glance. Wi-Fi authentication is captured privately; passwords are neither printed nor placed in command arguments. Media carrying protected Wi-Fi authentication also carries access to that network.

Controller deployment runs manually from `main` on disposable GitHub-hosted runners. The public-image job has package-write permission and no OVH environment secrets. The private job uses the `ovh` environment restricted to the selected `main` branch, with project-scoped OpenStack/S3 credentials. Actions are pinned to full commits. The CLI sends a single-use enrollment key and SSH public key; existing K3s tokens, datastore backups, SSH private keys and the Tailscale API token never enter Actions. Bootstrap or approved restore installs cluster tokens into a private persistent OEM profile before activation. Repository administrators and anyone able to change trusted deployment code can access its environment credentials.

Terraform state is private, encrypted with SSE-S3, versioned and locked through the OVH S3 backend. Keep its separate operator backup outside the cloud project. State contains infrastructure identifiers and attributes, and providers can store sensitive values there; do not commit it or expose state/plan JSON in CI logs. The `aws` provider is an S3 client directed at OVH, with no AWS account involved.

The Tailscale API token stays on the operator. The controller gets a single-use registration key; a laptop ISO gets a reusable key. Both are preauthorized, non-ephemeral, role-tagged and valid for at most 24 hours. The ISO contains an operator public SSH key and network access, with no K3s token or installation target. After disk confirmation, workers receive the secure CA-pinned agent token over pinned controller SSH, never the server token. [SETUP.md](../SETUP.md#install-a-worker) owns identity verification and erasure confirmations.

Live USB sessions run `tailscaled --state=mem:` through a conditional runtime environment override. This registers a temporary, ephemeral device even with the reusable non-ephemeral key; Tailscale removes disconnected ephemeral devices. The override exists only under `/run` and is absent after installation, so installed workers retain persistent identities. No device cleanup service or second registration key is needed.

The registration window limits new registrations, not device lifetime. Expiry or revocation does not remove registered nodes, prevent copied media from booting, or revoke copied join credentials. The ISO is not hardware-bound. Retire identities deliberately using the [operations guide](operations.md#retire-a-worker).

The kiosk SD image contains an individually enrolled, persistent Tailscale identity and private first-boot configuration. Keep it and its operator copy private and use that identity on one Pi at a time. The enrollment key is consumed during preparation; the Pi retains its tagged identity across reboots. Cloud-init clears the boot partition's `user-data` after provisioning, before the display session starts. Its installed Viewer configuration and SSH host key retain their private filesystem permissions.
