# OVH controller egress and IPv4 cost

This note records the 2026-11-15 evaluation of removing the public IPv4 address from the `BHS5` control-plane VM (`guaranine-controlplane`) to eliminate its ~CA$3.15/mo (`CA$0.00432/hr`) line item in [the OVH Cloud dashboard](../kubernetes/monitoring.yaml) while retaining outbound internet egress.

**Conclusion:** On OVH Public Cloud, putting the controller behind a managed NAT increases network cost, and switching to IPv6-only egress introduces registry breakages, DNS risks, DERP-only relaying for IPv4-only laptops, and unverified `BHS5` OpenStack/billing behavior. Switching `d2-2` compute from hourly to monthly billing saves almost the same amount (~CA$3.05/mo) with no networking changes.

## Why the controller only needs egress

Guaranine accepts no public application, Kubernetes, or SSH ingress on the controller's cloud interface (`ens3`):

* SSH (`22/tcp`), the Kubernetes API (`6443/tcp`), kubelet (`10250/tcp`), and Flannel VXLAN (`8472/udp`) are permitted only on `tailscale0` by host `nftables` in [kairos/config.yaml](../kairos/config.yaml).
* Exposed services and Grafana run through Tailscale Serve/Funnel or tunnels on workers; see [networking and security](security.md#tailnet-policy).
* The only unsolicited physical ingress permitted by the host firewall is DHCP, required ICMPv6 control traffic, and WireGuard UDP `41641` for direct Tailscale peer hole-punching. Even without inbound UDP `41641`, Tailscale operates over outbound connections to coordination servers and DERP relays.

## Why OVH NAT does not cut costs

Unlike providers with a shared low-cost NAT layer, OVH Public Cloud has no free OpenStack virtual SNAT router:

| Option on OVH Public Cloud | Estimated monthly CAD | Internet egress | Why it does not save money |
| --- | --- | --- | --- |
| Current `Ext-Net` port | ~CA$3.15 | IPv4 + IPv6 | Baseline Basic IPv4 rate (`CA$0.00432/hr`) |
| Private network + OVH Gateway `S` | ~CA$6.07 | IPv4 SNAT | Gateway `S` (~CA$0.004/hr, ~CA$2.92/mo) plus a separately billed SNAT IPv4 (~CA$0.00432/hr, ~CA$3.15/mo) |
| Self-managed NAT VM | >CA$12.00 extra | IPv4 SNAT | Requires a second VM (for example a second `d2-2` at CA$9.14/mo monthly) plus its own public IPv4 (CA$3.15/mo) |
| Private OpenStack L3 router (no Gateway) | CA$0.00 | None | Routes only between private vRack subnets |
| Private-only VM + laptop Tailscale exit node | CA$0.00 | Cannot bootstrap | No initial internet path to reach Tailscale control/DERP; [kairos/config.yaml](../kairos/config.yaml) also sets `--accept-routes=false` |

In addition, this `BHS5` project has `0/0` quota for custom security groups in [infra/ovh/main.tf](../infra/ovh/main.tf), so private-network and router quotas in `BHS5` would also have to be verified or requested before deploying an OVH Gateway.

## AWS comparison

The idea that NAT provides free outbound egress on AWS mixes two separate AWS features (as documented on 2026-11-15):

* **AWS Egress-Only Internet Gateway** has no hourly gateway charge, **but is IPv6-only** and still bills standard AWS per-GB egress data transfer after the free tier.
* **AWS NAT Gateway** (IPv4) is not free (~US$0.045/hr plus ~US$0.045/GB processed and US$0.005/hr for its Elastic IP), and AWS bills US$0.005/hr per public IPv4 address.
* **OVH Public Cloud (`BHS5`)** includes unmetered public egress bandwidth at no per-GB fee, but bills separately for every public IPv4 address and managed NAT Gateway.

## Limitations of IPv6-only egress on OVH

OVH's Basic IP documentation states that a Basic IP on `Ext-Net` pairs one IPv4 address and one IPv6 address that can be deleted separately, and IPv6 carries no separate IP fee. Actually running `guaranine-controlplane` IPv6-only has five limitations:

1. **Irreversible port deletion, Terraform drift, and unverified `BHS5` support.**
   * Deleting Basic IPv4 on the existing port is permanent on that port; getting IPv4 back requires attaching a replacement port or rebuilding the VM, while the cluster's single SQLite state lives on that VM's non-detachable local root disk protected by `prevent_destroy` in [infra/ovh/main.tf](../infra/ovh/main.tf).
   * Creating a fresh IPv6-only `Ext-Net` port in Terraform is untested against `BHS5` OpenStack policy (which already rejected `port_security_enabled` on `Ext-Net` ports), and OVH invoice behavior must be verified live to confirm whether deleting Basic IPv4 stops the `CA$0.00432/hr` charge.
2. **IPv4-only container registries and GitHub endpoints fail.**
   Live DNS checks on 2026-11-15 showed:

   | Host | IPv4 (`A`) | IPv6 (`AAAA`) | Controller dependency |
   | --- | --- | --- | --- |
   | `ghcr.io` | Yes | None | Public OS base pulls for [OS updates](operations.md#os-updates) (`NodeOpUpgrade`) |
   | `github.com` | Yes | None | GitHub release/API endpoints |
   | `public.ecr.aws` | Yes | None | Pinned `SENTINEL_IMAGE` / `NODEOP_DEFAULT_IMAGE` in [kairos-operator](../kubernetes/kairos-operator/kustomization.yaml) |
   | `ecr-public.aws.com` | Yes | Yes | AWS ECR Public dual-stack domain (not currently configured) |
   | `quay.io` | Yes | Yes | `operator-kairos-operator` image in [kairos-operator](../kubernetes/kairos-operator/kustomization.yaml) |
   | `registry-1.docker.io` | Yes | Yes | `openebs/provisioner-localpv` in [storage.yaml](../kubernetes/storage.yaml) |

   Although GHCR's blob CDN (`pkg-containers.githubusercontent.com`) publishes `AAAA` records, container pulls authenticate and fetch manifests from `ghcr.io` first, so pulls fail before reaching CDN blobs. Pods scheduled on laptop workers egress through the laptop's Wi-Fi rather than the cloud controller.
3. **Host DNS lacks explicit IPv6 resolvers.**
   [kairos/config.yaml](../kairos/config.yaml) enables `DHCP=yes` and `IPv6AcceptRA` without static `DNS=` servers, currently relying on DHCPv4 for DNS. Because OVH `Ext-Net` assigns IPv6 via SLAAC without guaranteed RDNSS, dropping IPv4 leaves `systemd-resolved` without DHCPv4 DNS and can trigger timeouts against compiled-in IPv4 fallback resolvers unless explicit IPv6 DNS servers are configured and rolled out first.
4. **IPv4-only laptops lose direct Tailscale peer connections.**
   Tailscale's control plane and DERP relays support IPv6, and `tailscale0` still transports IPv4 (`100.64.0.0/10`) inside the tunnel, so K3s API, kubelet, Flannel VXLAN, and SSH remain functional. However, any worker or operator machine on an IPv4-only ISP or Wi-Fi network cannot establish direct WireGuard UDP paths to an IPv6-only controller and must relay all cluster traffic through Tailscale DERP servers.
5. **NAT64/DNS64 workarounds add external trust or complexity.**
   Reaching `ghcr.io` or `public.ecr.aws` from an IPv6-only controller requires DNS64 + NAT64 (which OVH does not supply on `Ext-Net`) or a dual-stack proxy/exit node, trading Guaranine's minimal architecture for external gateway dependencies to save CA$3.15/mo.

## Practical cost-reduction alternatives

* **Switch `d2-2` to monthly billing:** Changing compute billing in the OVH Control Panel cuts compute from ~CA$12.19/mo (730h at `CA$0.0167/hr`) to CA$9.14/mo—saving ~CA$3.05/mo with zero config or Terraform changes, as noted in [SETUP.md](../SETUP.md#cloud-preflight) and [the OVH Cloud dashboard](../kubernetes/monitoring.yaml). Verify on the next OVH invoice whether the `Ext-Net` IPv4 line (`CA$3.15/mo`) stays separate.
* **Migrate to a cheaper cloud VM:** Reducing total monthly cost substantially below ~CA$12/mo CAD requires migrating via [the VM restore procedure](operations.md#restore-on-another-cloud-vm) to a provider whose base plans bundle or cheaply price IPv4 (for example Hetzner Cloud in Ashburn, Virginia, where shared-x86 `CX`/`CPX` plans price primary IPv4 around €0.50/mo and include free cloud firewalls, at the tradeoff of slightly higher distance from Montreal and no direct Glance QCOW2 upload).
