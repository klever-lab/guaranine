# Laptop images

Use the [latest public release](https://github.com/klever-lab/guaranine/releases/latest) for credential-free Talos boot media. Verify its `SHA256SUMS` with `sha256sum --check SHA256SUMS`. It is the pinned upstream Image Factory ISO with the official Tailscale extension; it boots into maintenance and cannot install or enroll a machine by itself.

For unattended installation, use the single `./guaranine worker` command in the [Talos guide](../../talos/README.md#prepare-a-machine). It builds one private ISO using the official local Talos imager. Only that machine's configuration is embedded; operator credentials and cluster signing material are not included in a worker image. Private configuration never goes to public Image Factory.

Private images stay outside the checkout, with files mode 0600 and directories mode 0700. Keep them on encrypted operator storage. They authorize installation to the selected disk and contain enrollment credentials; never publish or upload them as CI artifacts.

## Enrollment security status

The requested behavior is unattended enrollment for one laptop, with unused authority expiring after 24 hours, potentially authorized by the user's YubiKey 5 NFC. The current command requests a preauthorized, non-ephemeral, single-use Tailscale key with a 24-hour lifetime. Enrolled devices keep their own identity after that key expires.

**The full policy is unfinished.** Talos cluster join authority is separate from Tailscale registration. A copied unused private image can race the intended laptop; it is not hardware-bound. YubiKey authorization during boot is not implemented. An ISO remains copyable and bootable after 24 hours; a local clock check cannot securely expire it. No extra enrollment/authentication server is authorized.

## Hardware

The pinned AMD64 image needs x86-64-v2, compatible storage/NIC hardware and at least 1 GiB RAM for a worker before application capacity. Start with Ethernet or USB Ethernet. Physical laptops, Wi-Fi/firmware, lid/power behavior and live WAN operation still need qualification.

## Releases

The image workflow fetches the prebuilt, checksum-pinned upstream ISO. It also tests the one-command private ISO/VHD paths with disposable keys and the real upstream imager; private outputs are discarded. Actual BIOS/UEFI tests require a responsive Talos maintenance API and an unchanged unconfigured target disk. This verifies boot safety, not live enrollment.

For a release, run repository validation, push to main with passing CI, then push a new `worker-iso-vMAJOR.MINOR.PATCH` tag. The release guard waits up to four minutes for the newest exact-commit repository check. Failed checks or timeout block publication. Only the tested public ISO, `build.json`, `qemu-results.json` and `SHA256SUMS` are published, using a draft and immutable uploads. Reruns accept only identical existing bytes; changed bytes require a new tag. A PR is not required.

Update [versions.json](../../talos/versions.json) deliberately using official Talos checksums, extension metadata and OCI digests. Schema checks, both private builds and both public boot modes must pass. Legacy Debian/K3s releases are historical artifacts.

Upstream: [Talos boot assets and imager](https://docs.siderolabs.com/talos/v1.14/platform-specific-installations/boot-assets), [Tailscale extension](https://github.com/siderolabs/extensions/tree/main/network/tailscale).
