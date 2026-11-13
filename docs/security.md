# Networking and security

## Tailnet policy

[tailscale/policy.json](../tailscale/policy.json) owns the complete policy for this tailnet, including existing personal access and Funnel rules. Only admins own the Guaranine node tags. Node grants permit:

| Source | Destination | Port | Purpose |
| --- | --- | --- | --- |
| Worker | Controller | TCP 6443 | Kubernetes API |
| Controller | Worker | TCP 10250 | Kubelet |
| Both node roles | Both node roles | UDP 8472 | Flannel VXLAN |
| Operator | Nodes | TCP 22 | Ordinary key-based SSH |

Nodes cannot administer peers. The policy retains the operator's broader personal access; the table describes cluster traffic, not a replacement for the complete policy.

Before applying it, back up the live policy outside the checkout. Use the standard [Access controls editor](https://console.tailscale.com/admin/acls); for API updates, validate first, use the live ETag with `If-Match` and read back the result. CI checks JSON syntax without credentials and never applies the live policy.

Tailscale uses kernel networking and persistent identity, with subnet-route acceptance, Tailscale SSH and host DNS changes disabled. K3s uses Tailscale node addresses and binds Flannel to `tailscale0`; Flannel derives its MTU from that interface. Administration, API, kubelet and cross-node pod traffic use the tailnet. Private Azure/LAN addresses are never cluster endpoints. Other operator VPNs must preserve the `100.64.0.0/10` routes.

The Azure NSG denies physical inbound access and direct VNet egress. Its public IPv4 provides explicit internet egress for Tailscale/DERP and downloads; it is not an SSH or Kubernetes endpoint. Real direct/relayed paths, reconnects and temporary WAN outages still need live-network verification.

## LAN isolation

Wi-Fi client isolation is useful but never assumed. The host nftables rules in [kairos/config.yaml](../kairos/config.yaml) block direct LAN traffic from hosts and pods, including attached subnets using public addresses. Systemd-networkd updates those subnet sets as addresses change. Ordinary physical egress stays blocked until the network service registers the interface.

Exceptions allow network-service DHCP, system-resolver DNS, required IPv6 link control and encrypted Tailscale transport. Unsolicited physical traffic cannot reach host services or pods; internet replies remain allowed. SSH and networking start after the firewall. [Packet tests](development.md#firewall-packet-checks) exercise the actual rules without relying on access-point isolation.

## Workload restrictions

The server enables K3s secrets encryption and the standard restricted Pod Security policy: no privileged pods or host namespaces, no privilege escalation and dropped capabilities. `kube-system` is exempt and reserved for trusted system components.

[kubernetes/network-policy.yaml](../kubernetes/network-policy.yaml) denies application ingress and egress in the default namespace, with a cluster-DNS exception. The controller installs this manifest through K3s's native server manifest directory. Add explicit ingress/egress rules alongside applications. New application namespaces need their own policies.

Allowed destinations remain reachable by compromised applications. Firewall rules and removal of utilities cannot guarantee protection after host-root or kernel compromise.

## Private material and enrollment

Use the [operator directory](../SETUP.md#operator-files) on encrypted storage with an off-machine backup. The CLI creates directories with mode 0700 and private files with mode 0600, refuses private paths inside the checkout, and rejects private inputs with symlinks, incorrect ownership or group/other permissions. Gitignore is a second line of defense, not the storage boundary.

Only public inputs enter Docker layers and the public base's build cache. Private machine configuration reaches pinned AuroraBoot through stdin, using protected scratch storage; private builds disable Docker's log driver. Errors go to protected local diagnostic files. Images, configurations and logs must never be published or uploaded as CI artifacts. Wi-Fi authentication is captured privately; passwords are neither printed nor placed in command arguments. Media carrying protected Wi-Fi authentication also carries access to that network.

The Tailscale API token stays on the operator. The controller gets a single-use registration key; a laptop ISO gets a reusable key. Both are preauthorized, non-ephemeral, role-tagged and valid for at most 24 hours. The ISO contains an operator public SSH key and network access, with no K3s token or installation target. After disk confirmation, workers receive the secure CA-pinned agent token over pinned controller SSH, never the server token. [SETUP.md](../SETUP.md#install-a-worker) owns identity verification and erasure confirmations.

Live USB sessions run `tailscaled --state=mem:` through a conditional runtime environment override. This registers a temporary, ephemeral device even with the reusable non-ephemeral key; Tailscale removes disconnected ephemeral devices. The override exists only under `/run` and is absent after installation, so installed workers retain persistent identities. No device cleanup service or second registration key is needed.

The registration window limits new registrations, not device lifetime. Expiry or revocation does not remove registered nodes, prevent copied media from booting, or revoke copied join credentials. The ISO is not hardware-bound. Retire identities deliberately using the [operations guide](operations.md#retire-a-worker).
