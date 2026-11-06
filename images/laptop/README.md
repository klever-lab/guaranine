# Laptop installer and automatic worker enrollment

This Debian 13 amd64 **installer**, owned by KIAN-975, installs a minimal host and automatically runs the reusable [Ansible worker profile](../../provision/worker/playbook.yml). Installation is complete only when the node is Ready, schedulable, reports allocatable CPU, memory and ephemeral storage, and runs the resource-requesting enrollment probe through Kubernetes' default scheduler. A successful Debian installation alone is not enrollment success.

The coordinator must already exist. [Azure Terraform](../../infra/azure/README.md) remains infrastructure only; it has not deployed or configured a K3s server. This worker profile never installs a server. Workload storage and KubeVirt are future work. Local disk capacity reported here is ephemeral storage, not a portable persistent volume.

## Requirements and coordinator contract

- An amd64 laptop with legacy BIOS or UEFI, at least 2 GiB RAM, one CPU core and a 20 GiB installation disk; 4 GiB RAM is more comfortable. The profile reserves 200m CPU, 512 MiB RAM and 2 GiB ephemeral storage for the host and Kubernetes. Workload requests must fit the remaining allocatable resources.
- **Disable Secure Boot for this installer.** Upstream signed kernel/boot images are retained, but this remastered image is not certified for Secure Boot. QEMU tests use SeaBIOS and OVMF without Secure Boot. Test physical firmware, storage controllers, suspend/lid behavior and thermals on your hardware.
- Working internet for enrollment: DHCP/DNS, Tailscale control/DERP, and container registries. Worker packages and their complete dependencies are bundled from the pinned signed Debian snapshot, verified locally and installed without mirror downloads. There is no LAN cluster fallback. Captive portals must be completed beforehand. Ethernet or USB Ethernet is recommended; the Debian installer can configure supported Wi-Fi. Intel, Realtek, Atheros and Broadcom firmware packages are included in the installed system. Unsupported firmware/drivers, proprietary Wi-Fi and unusual storage controllers may require different hardware or a reviewed image update. Enrollment still needs internet even though the OS/profile package installation uses local media.
- One stable K3s server with SQLite, **the exact K3s version in [inputs.json](inputs.json)**, and a stable literal Tailscale IPv4 API endpoint, `https://100.x.y.z:6443`. Configure its node/advertise address, TLS SAN and Flannel interface on Tailscale. Do not hand out an Azure public or private VNet address. MagicDNS and IPv6-only API endpoints are not currently supported by this profile.
- Default K3s self-signed server CA, supplied through a trusted channel. The secure bootstrap token must contain the SHA-256 of the **PEM bytes** in `server-ca.crt`, including its final newline. Custom CA hierarchies require a reviewed extension for K3s's DER-root hashing convention; do not disable verification to accommodate one.
- Tailnet policy permitting tagged workers to the coordinator TCP 6443, the coordinator to worker TCP 10250, and worker/worker Flannel VXLAN UDP 8472, exclusively through Tailscale. Restrict administration SSH TCP 22 to operators. Do not permit public or LAN cluster ports, subnet routes, exit nodes, or Tailscale SSH as an enrollment shortcut. The worker firewall blocks physical cluster traffic, and kubelet binds its tailnet address. The coordinator must enforce its corresponding firewall and ACLs too.

Tailscale's underlay necessarily uses internet/DNS and UDP traversal or HTTPS DERP. Kubernetes, Flannel and administration traffic use the encrypted tailnet. A direct path and a relay are both valid; no shared LAN or sub-10 ms latency is required. Flannel derives MTU from `tailscale0`; verify effective MTU and packet paths on actual WAN links before production use. Keep the default K3s pod/service CIDRs (`10.42.0.0/16`, `10.43.0.0/16`) with this firewall profile.

## Download, verify and write USB

Versioned assets are published by the [ISO workflow](../../.github/workflows/laptop-iso.yml) under `worker-iso-vMAJOR.MINOR.PATCH` in [GitHub Releases](https://github.com/klever-lab/guaranine/releases). The workflow must finish its actual-image tests before publishing. No release, live coordinator deployment or hosted tailnet enrollment is implied by source changes alone.

On a trusted operator machine, download the ISO, `SHA256SUMS`, `build.json` and `qemu-results.json` from the same release. Verify the release/tag/source provenance over authenticated GitHub HTTPS, then verify the files:

```bash
gh release download worker-iso-v1.0.0 --repo klever-lab/guaranine \
  --dir "$HOME/Downloads/guaranine-worker"
cd "$HOME/Downloads/guaranine-worker"
sha256sum --check SHA256SUMS
```

Checksums detect corruption; a checksum fetched from a compromised source is not an independent signature. `build.json` records the source commit, public input hashes, builder packages, installed guest packages, image hash and bound QEMU evidence. Confirm `source_dirty` is false and both firmware test results passed.

Use a USB writer that copies an ISO as a disk image (for example GNOME Disks' Restore Disk Image). On Linux, identify the **whole USB device** by model, size and serial with `lsblk`; unmount its partitions, then write it. The example deliberately requires an explicit device variable:

```bash
lsblk -o NAME,SIZE,MODEL,SERIAL,TRAN,MOUNTPOINTS
export GUARANINE_USB_DEVICE='/dev/disk/by-id/usb-YOUR_SELECTED_DEVICE'
# This erases the selected USB device. Check the path before running it.
sudo dd if=guaranine-worker-amd64.iso of="$GUARANINE_USB_DEVICE" \
  bs=4M status=progress conv=fsync
```

Do not write to a numbered partition or the laptop's system disk from the operator machine. Firmware can boot the hybrid USB in BIOS or UEFI mode.

## Install and supply enrollment inputs once

1. Boot the USB. Select the screen/keyboard installer, or `serial` at the BIOS prompt / the UEFI serial menu entry for a 115200-baud serial console. Connect internet using Debian's network dialog.
2. Set a new local `fleetadmin` password when asked. There is **no default password**. Root login is disabled. The password is for local console/sudo recovery; the profile disables password and root SSH access. Install your own public SSH key locally if remote administration is needed, then use the node's tailnet endpoint.
3. Select the target **whole disk**, type its exact path when asked, inspect Debian's partition summary, and explicitly confirm writing changes. Everything on that target is erased. The installer excludes its own labeled USB device from the custom selection. Disconnect unrelated disks if uncertain. No target disk or destructive acceptance is preseeded.
4. Remove the installer USB at reboot. First-boot setup displays a generated `worker-…` identity. Keep it when issuing per-node credentials below. Machine ID and SSH host keys are generated by Debian on the target; the public ISO contains none of them.
5. Supply a protected runtime JSON file, or enter each field at the hidden console prompts. Setup automatically saves the protected input and starts enrollment; **do not run Ansible or a separate worker-bootstrap command**. Setup can proceed while internet/coordinator connectivity is interrupted; enrollment retries afterward. On another local console, `sudo nmtui` can set or repair the NetworkManager connection.
6. Log in locally and run `sudo guaranine-worker status`. Only `"success": true` with `"stage": "enrolled"` is success. The report includes the tailnet address, allocatable resources and scheduler proof. Check the same node with operator `kubectl get node NODE -o wide` over Tailscale.

The input fields are:

| JSON field | Value and handling |
| --- | --- |
| `server` | Literal coordinator Tailscale IPv4 HTTPS endpoint on port 6443. |
| `server_ca` | Trusted coordinator `server-ca.crt` PEM, with original bytes/newline. Console entry uses one-line base64 of the PEM. |
| `bootstrap_token` | Expiring **secure K3s bootstrap token**, generated with `k3s token create`. Server tokens and shared static agent tokens are rejected. |
| `tailscale_auth_key` | Short-lived, single-use, pre-approved key with the worker tag and narrow ACLs. Not ephemeral: the installed worker must retain its node identity across shutdowns. Blank preserves an already authenticated identity. |
| `verification_namespace` | Dedicated namespace created for this worker by the credential issuer. |
| `verification_token` | One-hour service account token for that namespace and read access to exactly this node. No administrator kubeconfig. |
| `control_url` | Optional HTTPS coordination URL; omit for hosted Tailscale. Used by disposable Headscale tests or an explicitly trusted private control server. |
| `control_ca` | Optional private coordination CA PEM; console entry uses base64. Omit for hosted Tailscale. This affects coordination trust, never Kubernetes CA verification. |

Runtime JSON may only be imported from a **root-owned mode-0600 regular file under `/run`**; it is consumed after validation. To transfer from a separate operator USB, mount it read-only with root-only permissions on the worker's second console, copy the JSON to `/run/enrollment.json` using `sudo install -m 0600`, then unmount/remove that USB. Enter `/run/enrollment.json` in setup. Do not put credentials on the public installer, use kernel arguments, ordinary environment variables, shell arguments/history, preseed answers, Terraform, build configuration or CI artifacts. Keep the source file on encrypted operator storage and remove the temporary USB copy afterward; deleting files on flash is not secure erasure.

## Issue and replace scoped credentials

On the existing coordinator, use its existing root/operator authorization. Keep output outside the checkout and never use shell tracing or CI logs. The standard commands underlying the helper are `k3s token create --ttl 1h` and `k3s kubectl create token enrollment-verifier --duration=1h` with the versioned [RBAC manifests](../../provision/worker/verification-rbac.yaml). The helper applies those manifests for **the identity displayed by setup**, creates both expiring credentials, reads the public CA and tailnet endpoint, and writes a new mode-0600 JSON file:

```bash
sudo python3 scripts/iso/issue-credentials.py \
  worker-0123456789abcdef0123 /run/laptop-enrollment.json
```

This is an operator credential issuance action on the existing server, not a server deployment. The namespace enforces restricted Pod Security, a one-pod resource quota and deny-all probe networking. The service account can only read the named Node and get/create/delete pods in its own namespace. It cannot list nodes, access Secrets, create RBAC, mutate Node state or administer workloads. The probe requires its own hostname affinity to prove eligibility of the new worker; it never pre-binds `spec.nodeName`. Application workloads continue to use normal automatic scheduling.

Generate a single-use tagged Tailscale key using the [Tailscale key controls](https://tailscale.com/docs/features/access-control/auth-keys) or your existing authorized automation. Set an expiry appropriate for this one installation, pre-approval if required, and the worker tag whose ACL permits only the necessary cluster traffic. Do not distribute an OAuth client secret or account credential to the worker. Add `tailscale_auth_key` privately to the operator JSON; omit the optional control fields for hosted Tailscale. There is no Tailscale account authentication flow in this repository.

Credential rotation follows the same path: issue a fresh per-worker input with the **same node name**, transfer it root-only under `/run`, then run:

```bash
sudo guaranine-worker configure --input-file /run/enrollment.json
```

Alternatively run `sudo guaranine-worker configure` for hidden prompts. No reinstall or new hostname is required. Credential replacement briefly stops only the provisioning controller and its timer so a pending attempt cannot overwrite the new input; the agent and Tailscale identity remain intact. An already authenticated Tailscale state is reused. A replacement bootstrap token restarts the agent if necessary. Configuration cannot silently change cluster endpoint/CA; a move to another cluster needs deliberate retirement and re-enrollment.

After success, the worker removes Tailscale enrollment input, the temporary key under `/run`, the bootstrap input copy and verification bearer token. It preserves `/var/lib/tailscale`, `/etc/rancher/node`, the K3s agent state/certificates, the immutable node name, and a root-only bootstrap-token file needed by the upstream agent configuration. Reboot tests revoke the bootstrap token and verification RBAC before restarting; normal restarts use the already issued node client identity. Never clone these directories or an installed worker disk as an enrollment image.

After verifying success from the operator API, remove the temporary verification namespace, ClusterRole and ClusterRoleBinding, and delete the issued bootstrap token using **its ID** (`k3s token delete ID`; do not print full tokens). Revoke an unused Tailscale auth key, or expire a rejected one. Node-key expiry/rotation is separate from auth-key expiry; preserve the installed machine's normal identity and handle an expired node authorization with replacement enrollment input. If initial enrollment took longer than credential TTL, issue fresh credentials rather than extending server-token access.

## Reboots, outages and troubleshooting

`guaranine-enroll.timer` runs on boot and retries after each attempt (60 seconds plus jitter). A bounded attempt can take up to 12 minutes while waiting for registration, images or the coordinator. The same node name, Tailscale state and node password survive failed attempts and reboots. Enrollment uses an explicit localhost inventory with one Ansible fork; it never scans the bundled binaries as inventory. Transport/enrollment attempts do not depend on a network-online boot job; they retry actual connectivity instead. Every new attempt clears current success and verifies again; a retained scheduler proof is reused only for the same Kubernetes Node UID. After reboot, read-only readiness verification uses that node's own kubelet client certificate; the temporary service account token is unnecessary.

Use local `sudo guaranine-worker status`, `systemctl status guaranine-enroll.timer guaranine-enroll.service`, `ip route get COORDINATOR_TAILNET_IP`, and `sudo tailscale status`. Status contains only fixed redacted stages and resource evidence. Tailscale/K3s service output and Ansible module output are suppressed to keep credentials out of journals. Do not turn on shell tracing or upload full state, guest disks, terminal transcripts, kubeconfigs or debug logs.

| Blocker | Resolution |
| --- | --- |
| Internet/coordination unavailable | Repair the uplink/DNS/captive portal; retry is automatic. Never substitute LAN/public API addresses. |
| Tailscale key rejected/expired, node authorization expired or device approval pending | Approve the correctly tagged device or supply a fresh scoped key. Existing authenticated state is preserved. |
| Coordinator route is not `tailscale0` | Fix Tailscale/ACL/routing. The profile refuses fallback; physical API/Flannel traffic is blocked. |
| Invalid CA or secure token | Reissue through the trusted coordinator channel; preserve PEM bytes. Do not use `--insecure`, short tokens or server tokens. |
| Verification token rejected | Reissue the per-worker service account token/RBAC for this exact node name. Registration alone is not success. |
| Verification timeout | Check server version/availability, join token expiry, node Ready conditions/taints/cordon, image egress and spare CPU/RAM/disk. A blocked probe must not be reported as enrolled. |
| Node password rejected | Confirm the saved name/password belong together. Do not erase identity directories to work around a duplicate; fence/retire the old machine first. |
| Node UID changed or switching clusters | Drain/fence the old identity, remove its API/tailnet registrations, then perform deliberate re-enrollment. |

A successful report is evidence at `checked_at`, not a continuous availability guarantee. During a coordinator outage, existing containers may continue, while new placements and enrollment wait. Laptop connectivity loss is not proof of fencing. Stateful recovery, portable disks, VM live migration and fencing remain future work.

For a fresh rebuild, use standard `kubectl cordon` and `kubectl drain --ignore-daemonsets` over Tailscale; resolve disruption budgets, local data and spare-capacity blockers. Wait for evacuation and replacements before removal. Stop the old agent, delete its Node and revoke the old tailnet machine after it is fenced. Install with this same ISO to create a new identity and issue new per-worker credentials. A temporarily drained machine keeps its identity and can be uncordoned after health verification; no workload destinations are assigned by hand.

## Existing Debian hosts

On an existing **Debian 13 amd64** host without a K3s server, mount a verified ISO read-only and run its public profile installer:

```bash
sudo mount -o loop,ro guaranine-worker-amd64.iso /mnt
sudo bash /mnt/guaranine/worker/install.sh
```

The wrapper installs dependencies from that existing host’s signed Debian sources, the same pinned binaries and service units, then invokes protected setup and automatic Ansible provisioning. Existing hosts require package-repository access; their OS package versions may be newer than the ISO snapshot and are not downgraded by the wrapper. It preserves existing host SSH keys and machine ID while generating a persistent worker name. No separate Ansible invocation is necessary. Treat changes to an existing host's swap, hostname, lid policy, SSH and firewall as host administration; back up its configuration first. The ISO installation path is the tested baseline.

## Local builds, validation and release gates

From a checkout, with Docker and internet access:

```bash
bash images/laptop/build.sh /tmp/guaranine-iso
```

The builder remasters the checksum-pinned upstream Debian netinst using `xorriso` boot-image replay, retaining BIOS, UEFI and hybrid USB support. It copies an explicit public-file allowlist, never a host filesystem or arbitrary checkout contents. [inputs.json](inputs.json) pins Debian, its APT snapshot, builder digest, K3s binary, upstream K3s install script, Tailscale tarball and probe image digest. Dockerfiles pin the public Mozilla trust bundle by dated URL/checksum. The builder resolves and downloads the complete worker dependency closure from a signed Debian snapshot, starting from an empty package-status database. Its package names/versions and every .deb hash are in the build manifest; late installation verifies the bundle and uses APT with downloads disabled. Both guests also record actual installed versions. Installed machines follow signed Debian security updates without automatic reboot; K3s and Tailscale remain pinned until a deliberate profile update. Releases require a clean source commit. The controlled Docker command is the supported local build. Native diagnostic builds additionally require the complete verified package bundle via `GUARANINE_PACKAGE_BUNDLE`; their recorded host package set differs from the controlled builder.

Validate source using the shared entry point with Python 3.12+:

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install --requirement requirements-dev.txt
bash scripts/validate.sh
```

This includes worker behavioral tests, ShellCheck, Ansible syntax, pinned Actionlint, installer/release contracts and the existing Terraform suite. It requires public release/registry access for pinned tools and no production credentials.

On a Linux amd64 machine with KVM, Docker, QEMU, OVMF and xorriso, test the **actual built ISO**:

```bash
docker build --tag guaranine-iso-test --file scripts/iso/Test.Dockerfile scripts/iso
python3 scripts/iso/qemu-test.py /tmp/guaranine-iso/guaranine-worker-amd64.iso \
  --output /tmp/guaranine-iso/qemu-results.json
python3 scripts/iso/finalize.py /tmp/guaranine-iso
```

KVM must be accessible to the test user. The harness creates temporary disks and a separate privileged disposable Docker coordinator with SQLite, pinned K3s, actual Tailscale clients, a private test CA, Headscale and embedded DERP. It exercises BIOS/UEFI media boot, interactive disk selection, installation/reboot, distinct node/machine/SSH identities, rejected credentials, no false success during coordinator outage, automatic retry/recovery, resource-requesting scheduler placement, credential consumption and enrolled reboot after bootstrap/verification revocation without duplicate Nodes. It forces WireGuard UDP off in the fixture to verify a real embedded DERP path. Test identities and private keys exist only in a mode-0700 temporary directory and disposable container, removed afterward. Guest streams/disks/logs are never artifacts; only redacted results and package names/versions are exported.

The ISO workflow runs on standard GitHub-hosted Ubuntu runners for relevant PRs, main, version tags and manual **Run workflow** previews. Every required test must pass before artifact upload/publication; `finalize.py` binds evidence to the exact image SHA/source commit and refuses missing/skipped firmware checks. Build/test permissions are read-only. Only the dependent release job receives `contents: write`; all third-party actions are commit-pinned, jobs have timeouts, and no `pull_request_target` is used.

A release tag must match `worker-iso-vMAJOR.MINOR.PATCH`, be in main history and correspond to a merged main PR. The accepted PR head must have successful GitHub Actions results for `Clusterfork validation`, `trust` and `build-test`; the latest result for each check governs reruns. Human approval is not required. Protect main and release tags using repository rulesets; the workflow checks merged PR provenance, exact-head CI and ancestry but cannot create repository governance. Publication is performed by the normal worker/release process, not by repository validation. The release publisher creates a draft, uploads tested assets, and publishes last. Reruns compare existing versioned assets and refuse differing bytes; never clobber them. Rerun failed release jobs using the original tested artifact, or use a new version for a rebuilt image. Manual previews never publish releases.

Reproducibility is **input-pinned, not bit-for-bit**. ISO filesystem/build timestamps, boot metadata, package maintainer scripts, installation time, DHCP and random target identities vary; registries and upstream archives can also become unavailable. The guest package manifest is evidence of the installed snapshot, not a claim that future security updates preserve those bytes. Refresh pins together, verify upstream checksums, rerun all source and actual-image tests and issue a new version. Do not replace an existing version's assets.

## Hosted Tailscale and physical WAN qualification

Credential-free CI proves the actual clients and complete enrollment path against **disposable Headscale identities**, including its embedded relay. It does not prove hosted Tailscale account policy, device approval, public DERP regions, actual laptop firmware/Wi-Fi or separated-location behavior. No production tailnet credential is needed by default CI.

Before calling a hosted deployment operational, enroll disposable QEMU workers using the same ISO against a disposable hosted tailnet/coordinator and scoped short-lived test keys. Check `tailscale ping`/`tailscale status` for a direct path on a permissive network, then block physical WireGuard UDP on the **test endpoints only** to force DERP and repeat enrollment/probe/reboot/outage checks. Confirm `tailscale0` routes, Node InternalIPs, Flannel peer annotations, MTU and packet captures; no physical API TCP 6443, kubelet TCP 10250 or VXLAN UDP 8472 traffic may leave the guests. Revoke test identities and credentials afterward. Record measured results separately from the credential-free CI report. Neither a source test nor a Done issue proves a hosted deployment; report unperformed hosted tests explicitly.

Upstream contracts: [Debian installer preseeding](https://www.debian.org/releases/trixie/amd64/apb.en.html), [Debian Live Manual installer approaches](https://live-team.pages.debian.net/live-manual/html/live-manual.en.html), [K3s agent registration](https://docs.k3s.io/quick-start), [agent options](https://docs.k3s.io/cli/agent), [tokens and cluster trust](https://docs.k3s.io/cli/token), [Tailscale auth keys](https://tailscale.com/docs/features/access-control/auth-keys), [Headscale embedded DERP](https://headscale.net/stable/ref/derp/), [workflow permissions/triggers](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax), and [GitHub release creation](https://cli.github.com/manual/gh_release_create).
