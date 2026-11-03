# guaranine

My AWS at home. A pile of old laptops (2–8 GB RAM each) becomes one pool I can provision on: "give me a box with 1 CPU, 1 GB RAM, 10 GB disk, reachable at `api.mydomain.com`, and only allowed to talk to `db`."

No custom code on the laptops. Just off-the-shelf pieces glued together with Terraform:

| Piece | Job |
|---|---|
| **Debian 13** | The OS on every laptop. Installed hands-free from one USB stick, then left alone (auto security updates) |
| **Incus** | Runs the environments (containers + VMs) with hard CPU/RAM/disk limits, and clusters the laptops into one pool |
| **Tailscale** | The only network. Every host and every service gets its own tailnet identity; ACLs decide who can talk to whom |
| **Cloudflare Tunnel** | Public access on my domains without a public IP |
| **Terraform** | Describes every service in code; `terraform apply` makes reality match |

## Hosts (the laptops)

Set up once, then never touched by hand.

1. Build one Debian USB stick with a preseed file (`hosts/preseed.cfg`) that answers every installer question
2. Its post-install step:
   - installs `incus`, `tailscale`, `unattended-upgrades`
   - joins the tailnet with a reusable auth key tagged `tag:host`
   - sets Incus `core.https_address` to the host's Tailscale IP (API never on LAN) and trusts my Incus client cert
   - sets `HandleLidSwitch=ignore` so closing the lid doesn't suspend
   - disables SSH on LAN (SSH only via Tailscale, as a break-glass)
3. Plug the USB into a laptop, power on, walk away. It shows up in the tailnet
4. Join the hosts into one Incus cluster over Tailscale (3+ hosts so it survives one dying)
5. Cluster groups by size: `small` (2–4 GB) runs containers only, `big` (8 GB) can run VMs

After that, hosts are only touched through the Incus API (via Terraform).

BIOS, once per laptop: boot from USB, "power on after AC loss" on, battery charge limit ~80% if available.

## A service

One module block per service:

```hcl
# services.tf
module "api" {
  source = "./modules/service"
  name   = "api"
  type   = "container"            # or "virtual-machine" (big hosts only)
  image  = "images:debian/12/cloud"
  cpu    = 1
  memory = "1GiB"
  disk   = "10GiB"
  domain = "api.mydomain.com"     # optional, public via Cloudflare Tunnel
  port   = 8000                   # what the domain points at
  allow  = ["db:5432"]            # who this service may reach over Tailscale
}
```

`terraform apply` creates or updates everything. Delete the block and apply again to tear it down.

## What the `service` module does

1. **Incus**: launches the instance with `limits.cpu`, `limits.memory`, and root disk `size`, in the right cluster group. Incus picks the least-loaded host.
2. **Tailscale**: mints a single-use, pre-authorized auth key tagged `tag:svc-api`; cloud-init runs `tailscale up` on first boot.
3. **Cloudflare** (if `domain` is set): creates a tunnel pointing at `localhost:8000` and a DNS record for the domain; cloud-init runs `cloudflared` with the tunnel token.
4. **ACLs**: exports its `allow` list; `acl.tf` merges every service's list into one Tailscale policy.

## Isolation

Default is **deny everything**. Access only exists if a service's `allow` says so. Generated policy looks like:

```jsonc
{
  "tagOwners": {
    "tag:host":    ["autogroup:admin"],
    "tag:svc-api": ["autogroup:admin"],
    "tag:svc-db":  ["autogroup:admin"]
  },
  "grants": [
    { "src": ["autogroup:admin"], "dst": ["tag:host", "tag:svc-api", "tag:svc-db"], "ip": ["*"] },
    { "src": ["tag:host"],        "dst": ["tag:host"],   "ip": ["8443"] },  // incus cluster
    { "src": ["tag:svc-api"],     "dst": ["tag:svc-db"], "ip": ["5432"] }   // from api's allow
  ]
}
```

> [!IMPORTANT]
> Instances on the same host could otherwise talk directly over the Incus bridge and skip Tailscale. To force all service-to-service traffic through Tailscale:
> - instances use an isolated bridge with NIC `security.port_isolation=true` (no instance-to-instance traffic)
> - an Incus network ACL rejects egress to `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16` (no LAN) and allows the internet

## Layout

```
hosts/preseed.cfg   hands-free Debian install for every laptop
modules/service/    the module above
services.tf         one block per service
acl.tf              builds the Tailscale policy
network.tf          isolated bridge + network ACL
```

## Secrets

Passed as env vars, never committed:

- Incus client cert + key
- Tailscale OAuth client (scopes: auth keys, devices, policy file)
- Cloudflare API token (Tunnel + DNS edit) and account ID

Terraform state holds auth keys and tunnel tokens in plain text, so keep it in an encrypted backend (e.g. HCP Terraform free tier, or an S3-compatible bucket with encryption), never in git.

## Roadmap

1. **Hosts**: preseed USB, 3 laptops installed, on the tailnet, clustered. Launch one instance by hand to prove it works.
2. **Core**: `service` module with limits + Tailscale join.
3. **Network**: Cloudflare domains, generated ACLs, bridge isolation.
4. **Durability**: scheduled snapshots + backups off-site (e.g. Backblaze B2).
5. **Later**: VMs on big hosts.

## Known gotchas

- **Why not IncusOS**: it needs TPM 2.0 + Secure Boot, which these laptops don't have. Debian + preseed gets the same "never touch it" result.
- **The reusable host auth key is on the USB stick.** Make it expire in a day or two and regenerate it per batch of installs.
- **Host upgrades** (e.g. Debian 13 → 14) are the one manual-ish job. Easiest path: re-image the laptop from a new USB and let the cluster move its instances.
- **Laptops die.** Anything stateful (databases) needs backups before it matters.
- **Stale Tailscale devices**: destroyed instances leave their device in the tailnet until removed. Clean up with a destroy-time step.
- **Relay fallback**: with LAN egress blocked, Tailscale between instances may go through DERP relays (slower). Fine to start; revisit if latency hurts.
