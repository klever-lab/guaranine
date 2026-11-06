# Talos laptop images

The supported OS is Talos Linux with upstream Kubernetes. The legacy Debian/K3s installer is retired. Public boot images contain no machine configuration, enrollment credentials or identities. Private images are generated locally and remain outside the checkout; they are never release assets.

## Public boot media

The release contains `guaranine-talos-amd64.iso`, `build.json`, `qemu-results.json` and `SHA256SUMS`. Verify the release's source/tag over authenticated GitHub HTTPS, then run `sha256sum --check SHA256SUMS`. Checksums detect corruption and are not independent signatures of a compromised source.

The public image is the checksum-pinned upstream Talos Image Factory artifact for the committed public schematic, with the Tailscale system extension. It boots into maintenance and does not install, join a tailnet or join a cluster by itself. A public ISO must never carry a reusable login, cluster secrets, an operator kubeconfig or `talosconfig`.

Generate the same public media locally:

```bash
python3 scripts/talos/images.py public --output /absolute/new/output-directory
```

The public image workflow also builds both private formats with disposable CI-only credentials. Required QEMU tests boot the exact public ISO in BIOS and UEFI, query the real Talos maintenance API/version and check that an unconfigured target disk is unchanged. A failed or skipped case blocks publication. The report explicitly covers boot safety, not successful private enrollment, Kubernetes scheduling, hosted Tailscale or physical hardware. Private build outputs and logs are never uploaded.

## Private images for unattended installation

Use the [Talos operator guide](../../talos/README.md) to generate `machine.yaml` for one node, using the cluster's existing private secrets and a fresh single-use Tailscale key. The operator selects the target disk before image creation. Embedded configuration automatically applies on boot and can authorize installation to that disk; it is not safe public boot media.

```bash
python3 scripts/talos/images.py private \
  --config /absolute/private/operator/laptop-01/machine.yaml \
  --format iso --output /absolute/private/operator/laptop-01-image
```

The command runs the pinned upstream Talos `imager` container locally. It sends private configuration through stdin, streams image output back privately, and embeds only the supplied machine configuration. No secret enters a shell argument, Docker build context, public Image Factory request or repository file. Docker must support privileged Linux AMD64 containers; no running image-generation server is added. Operator administration files remain on the operator machine.

For the one planned Azure control plane, use `--format azure` with a control-plane profile. It produces a private compressed fixed VHD for the [Terraform image upload/publication root](../../infra/azure/README.md). Workers cannot be packaged as Azure control planes through this command. Build outputs are mode 0600 under a mode-0700 directory, contain `private.json`, and must never be published. The public publisher accepts only the exact pinned public ISO and rejects private markers, unexpected files, changed bytes or mismatched test evidence.

## Enrollment security status

The requested final behavior is: install, boot without further input, enroll one authorized laptop, and make unused enrollment authority expire after 24 hours. The user owns a YubiKey 5 NFC and requires explicit authorization before any extra server or enrollment service is introduced.

This migration supplies native Talos unattended machine configuration and local private image generation. The local key helper requests a single-use, non-ephemeral, preauthorized Tailscale key with a 24-hour lifetime. Tailscale enforces that key's expiry and one new tailnet registration; an enrolled device can retain its own identity after the key expires.

**This is not the complete requested security policy.** Talos cluster/PKI join authority is separate from the Tailscale key, and these private configurations are not bound to a specific laptop's hardware. A copied unused private image can race the intended laptop. YubiKey decryption/authorization during Talos boot is not implemented or claimed. No extra broker, authentication endpoint, Omni service or custom coordinator has been introduced to work around these limits. Keep private images private, and do not present the current generator as satisfying the full policy.

## Hardware and qualification

The pinned AMD64 release requires x86-64-v2. Check every older laptop's CPU and storage/NIC compatibility before installation. The upstream minimum is 1 GiB RAM for workers and 2 GiB for a control plane, before application capacity; the initial Azure target remains 2 vCPUs/4 GiB. Ethernet or USB Ethernet is the initial qualification path. Wi-Fi, firmware, lid/power behavior and physical WAN operation have not been certified by CI.

Boot media remains copyable and bootable after 24 hours; authoritative services enforce enrollment expiry. A local clock check cannot safely invalidate a copied ISO. Installed nodes keep their identity across ordinary reboots; resetting or reinstalling requires deliberately replacing/revoking the previous identity and credentials.

## Releases and pin changes

Run `bash scripts/validate.sh`, then the image workflow. Once the exact source commit has successful repository CI on main, push a new `worker-iso-vMAJOR.MINOR.PATCH` tag. Direct main pushes are supported; a PR or human review is not a release prerequisite. Tags also start repository CI, so the release guard waits up to four minutes for the newest exact-commit check; failed checks or a timeout block publication. The tag workflow independently builds private fixtures and tests the exact public image, binds evidence, creates a draft, uploads immutable assets and publishes last. Reruns refuse different bytes under an existing version; use the original tested artifact or a new tag.

Refresh `talos/versions.json` and the schematic deliberately, using official Talos release checksums, the version's official extension catalog and OCI digests. Re-run native schema validation and both actual-image boot modes. Public image integrity is input-pinned; private imager outputs are not promised to be bit-for-bit reproducible. Legacy releases remain historical artifacts and are not the current Guaranine installation path.
