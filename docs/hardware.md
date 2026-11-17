# Laptop hardware

## Network and boot requirements

Use a 2.4-GHz WPA2-Personal network with 802.11g clients enabled for this mixed fleet; its older single-band cards cannot join a 5-GHz-only SSID. Disable Secure Boot for this image. Built-in Wi-Fi must work from live boot through installation and normal operation, without Ethernet, adapters or external bridges.

New laptop ISOs and installed workers enable `efi=debug` by default so EFI startup diagnostics can be photographed without editing GRUB. The native worker profile owns the value; AuroraBoot appends the same value to live boot entries. Updating an existing USB follows the [image rebuild procedure](operations.md#image-batches-and-retries).

On UEFI workers, a native `after-install` hook registers **Kairos** for the installed disk's `\EFI\boot\bootx64.efi`, places it first in the firmware boot order and clears an overriding one-time boot choice. Existing entries are preserved. The hook verifies the partition GUID and loader path before allowing poweroff; `strict: true` makes hook errors fatal. BIOS workers keep the native BIOS bootloader path.

## Keep laptops awake

The native [lid profile](../kairos/lid.yaml) writes `/etc/systemd/logind.conf.d/10-guaranine.conf` during initramfs, before normal services start. Its three settings tell systemd-logind to ignore lid closure on battery, on external power and while docked. New images include the same profile; committing or pulling the repository does not update an installed worker.

For an existing worker, use the [configuration rollout](operations.md#configuration-rollouts) from the operator computer. Keep its lid open until the operation completes:

```bash
./guaranine rollout kairos/lid.yaml --node NAME
```

The profile's boot stage reloads an active logind and checks all three D-Bus settings before reporting success. No worker reboot is needed. `/oem` is persistent on installed Kairos nodes, so the profile also restores the file at boot. Reloading logind makes it read its application configuration; `systemctl daemon-reload` alone only reloads systemd unit definitions.

For an independent check on the worker, inspect the daemon's active policy:

```bash
for setting in HandleLidSwitch HandleLidSwitchExternalPower HandleLidSwitchDocked; do
  printf '%s: ' "$setting"
  busctl get-property org.freedesktop.login1 /org/freedesktop/login1 \
    org.freedesktop.login1.Manager "$setting"
done
```

Every line should report `s "ignore"`. Then watch `kubectl get nodes -w` from the operator while physically closing the lid; verify SSH or Grafana remains reachable and check `journalctl -b -u systemd-logind.service` after reopening it. Exercise both external power and battery, then verify the policy after a deliberately scheduled reboot before claiming reboot persistence on that worker.

The image harness checks all three active D-Bus properties on live media and installed BIOS/UEFI worker boots. Those checks verify policy loading, while the physical lid test verifies the actual laptop. The settings follow [systemd's logind configuration](https://www.freedesktop.org/software/systemd/man/latest/logind.conf.html).

## Laptop models

These model names are confirmed by the operator. Coverage targets their factory Wi-Fi options, including variants that cannot be identified while the laptops are powered off.

| Model | Wi-Fi options and image recipe coverage |
| --- | --- |
| Toshiba Satellite C650 | [Toshiba's guide](https://www.manualslib.com/manual/624333/Toshiba-Satellite-C650-Series.html) lists Atheros AR9285/AR9002WB-1NG and Realtek RTL8188CE options; its [earlier C640/C650 guide](https://device.report/m/27dd712a4520ab4e618483fed0e7fcbed1276d4b1a3762a8e78c56d4da21267b.pdf) also lists RTL8191SE/RTL8187B. `ath9k`, `rtl8192ce`, `rtl8192se`, `rtl8187` and the required Realtek firmware are present. |
| Dell Inspiron 5558 | Dell documents [Intel 3160/7265](https://www.dell.com/support/home/en-us/drivers/driversdetails?driverid=wrrmc) and [DW1707](https://www.dell.com/support/home/en-us/drivers/driversdetails?driverid=0wd27) options. `iwlwifi`, `ath9k` and the Intel firmware are present. |
| Dell Inspiron 1440 | Dell documents [DW1397/BCM4312 and DW1510/BCM4322](https://www.dell.com/support/home/en-us/drivers/driversdetails?driverid=k4h75), [Atheros DW1515](https://www.dell.com/support/home/en-us/drivers/driversdetails?driverid=phydt) and [Intel 5100/5300](https://www.dell.com/support/home/en-us/drivers/driversdetails?driverid=1t47j). `b43` and its full G/LP/N firmware, `ath9k`, `iwlwifi`/`iwldvm` and Intel 5000 firmware are included. |
| Lenovo IdeaPad L340-15API | Lenovo's [1x1 AC specification](https://psref.lenovo.com/syspool/Sys/PDF/IdeaPad/ideapad_L340_15API/ideapad_L340_15API_Spec.PDF) and [OEM driver package](https://pcsupport.lenovo.com/us/en/products/laptops-and-netbooks/ideapad-l-series-laptops/l340-15api/81lw/downloads/ds506548) identify RTL8821CE and QCA9377 as the matching Realtek/Qualcomm candidates. Their `rtw88_8821ce`/`ath10k_pci` drivers, firmware and Atheros board data are included. |
| HP Pavilion tx1000 | [HP's service guide](https://www.manualowl.com/m/Hewlett-Packard/Pavilion-tx1000/Manual/104969?page=24) lists 407159/407160 G/AG modules. The Broadcom BCM4311-class PCI IDs in [HP's tx1000 WLAN manifest](https://ftp.hp.com/pub/softpaq/sp37501-38000/sp37746.cva) have `ssb`/`b43` support and the full extracted Broadcom G/LP/N firmware. |
| Dell Inspiron 5493 | [Dell's specifications](https://www.dell.com/support/manuals/en-us/inspiron-14-5493-laptop/inspiron-14-5493-setup-and-specifications/communications?guid=guid-25120817-dddf-4aed-baf6-cd32f9074c86&lang=en-us) list Qualcomm QCA9565/QCA9377/QCA61x4A and Intel 9462/9560. Their `ath9k`, `ath10k_pci` and `iwlwifi` drivers and firmware families are included. |

The earlier Toshiba series guide also contains an RTL8192E regulatory entry. That does not identify the card in this C650. The pinned kernel has no RTL8192E driver, so an RTL8192E-equipped unit is outside this image's coverage; the C650 model name cannot support an unconditional compatibility claim.

## Qualification and limits

The [image recipe](../kairos/Dockerfile) rejects missing kernel modules, chipset PCI aliases or required firmware paths before a registration key is issued. It places Intel firmware at the names requested by the pinned kernel and extracts checksum-pinned Broadcom `b43` firmware with upstream `b43-fwcutter`. Atheros PCI `ath9k` radios need no external Wi-Fi firmware. Only firmware bundles needed by the documented inventory are copied; unrelated radio families are excluded. Adding firmware cannot supply a missing kernel driver.

The [boot checks](../scripts/test-private-images.py) exercise BIOS/UEFI USB boot, an older CPU baseline, installation and reboot, plus WPA2 association, DHCP and AP outage recovery with `mac80211_hwsim`. The UEFI worker case seeds old Ubuntu/Linux/Windows entries and a stale one-time boot choice, retains the same firmware state across installation and reboot, and checks that the registered Kairos entry actually booted. Installation needs an explicit success marker before a VM shutdown counts as success. The checks also cover distinct live SSH identities, the controller's persistence and activation guards, persistent worker configuration and unchanged unrelated guest disks. [Development](development.md#disposable-image-checks) owns the command and CI workflow.

Recorded result on 2026-11-13 UTC: the [full four-suite run](https://github.com/klever-lab/guaranine/actions/runs/43359077952) passed all eight guests, including Wi-Fi after BIOS and UEFI worker installation, registered boot priority over stale entries, default EFI diagnostics, temporary live Tailscale state and its absence after installation. A separate local KVM failure case confirmed that read-only EFI variables stop native installation without reporting success or powering off. That local case repacked the existing public rootfs with the pinned tool; CI verified the full Docker build. These are software qualification results. The installed physical cards and their RF behavior have not been measured; lid/suspend behavior and real WAN recovery also need hardware/live-network checks. Keep those pending checks distinct from the simulated-radio and firmware-path results.

On 2026-11-17 UTC, the [controller image check](https://github.com/klever-lab/guaranine/actions/runs/44007571963) failed during first-boot creation of `COS_PERSISTENT`: `mkfs.ext4` ran while `/dev/vda5` was absent, and Kairos stopped rather than starting without persistent storage. Live-media and both worker suites passed. This intermittent controller layout failure remains unresolved; later passing boots alone do not establish a fix. The harness now records the on-disk partition table and kernel messages to distinguish a partition-publication race from an invalid layout.

A physical Inspiron 5558 initially failed during UEFI startup with GRUB reporting `start_image() returned 0x8000000000000001`, with Secure Boot disabled. The operator subsequently reported reaching Kairos after adding `efi=debug`, and the live laptop appeared online in Tailscale. This does not establish that the flag caused the recovery. On a later installation, the operator selected the firmware's `ubuntu` entry and reported a black screen. After rebuilding the USB and reinstalling on 2026-11-13 UTC, the operator booted the installed laptop and the controller reported the worker Ready with a persistent Tailscale address. This confirms physical installed boot and cluster enrollment; the remaining radio, suspend and WAN checks above are still pending. The ISO also reached Linux in isolated UEFI checks using the host CPU with 2 GiB RAM and an emulated Broadwell CPU with 4 GiB RAM; its kernel checksum matches the running controller's kernel. The initial failure's cause remains unconfirmed.

The inventory above owns model names. There is no second model list or manual qualification flag in operator configuration. Before adding a model or changing the kernel, firmware or installer, verify its actual radio variants against the recipe's driver/alias/firmware checks, then rerun the relevant build and boot checks. Confirm physical boot, installation and post-install Wi-Fi on the target unit before extending a hardware compatibility claim.

Secure Boot, TPM-bound enrollment and YubiKey boot authorization require separate qualification and are not implemented here. [Enrollment security](security.md#private-material-and-enrollment) describes the media's trust limits.

## Lenovo Wi-Fi investigation

Status on 2026-11-17 UTC: manual WLAN unblocking restores Wi-Fi on the physical L340-15API (DMI product `81LW`), now installed as `kate`. A controlled test reproduced the block when `ideapad_laptop` registered, with Linux saved-state restoration stopped. Why its firmware reports Wi-Fi off even while Wi-Fi works remains under investigation. Automatic unblocking is not part of the ISO or installed-worker configuration. Keep subsequent findings here, distinguishing direct observations, hypotheses and confirmed causes.

### Observations and recovery

- The live USB reached a local shell and printed its SSH fingerprint, but did not join Tailscale. Ethernet provided temporary connectivity for diagnosis over pinned Tailscale SSH.
- The RTL8821CE PCI adapter loaded `rtw88_8821ce` and firmware version `24.11.0`. Its interface `wlp1s0` was `off (configuring)`, with no access point or address. The Wi-Fi service was active and reported `rfkill: WLAN soft blocked` without restarting.
- Both `ideapad_wlan` and the actual Wi-Fi radio `phy0` had `soft=1`, `hard=0`. The platform and adapter Bluetooth radios were also software blocked. The separate missing `rtl_bt/rtl8821c_fw.bin` message concerned Bluetooth; the Wi-Fi firmware loaded successfully.
- The kernel's `rfkill.default_state` was `1` (enabled), with no rfkill override in the boot command line. The read-only live root had no `/var/lib/systemd/rfkill` directory. The runtime state files were created during this boot, not loaded from an rfkill state directory baked into that root. `/var/lib/systemd/rfkill` was on the live RAM overlay.
- Lenovo's embedded-controller status, read through `/sys/kernel/debug/ideapad/status`, reported the master radio **on**, but individual Wi-Fi and Bluetooth **off**. The `ideapad_laptop` module's `hw_rfkill_switch` parameter was `N`; no hard block was reported.
- At 14:04:59 UTC, after explicit authorization, `rfkill unblock wlan` cleared both WLAN software blocks. The existing Wi-Fi service connected and obtained DHCP by 14:05:04 UTC, without a service restart or driver change. Bluetooth remained blocked. This establishes the immediate connection blocker and a manual recovery, not its original cause.
- A subsequent `curl --interface wlp1s0` HTTPS request to Tailscale's login endpoint returned HTTP 302, verifying internet connectivity through Wi-Fi while Ethernet remained attached.
- The embedded-controller status still reported Wi-Fi **off** after Wi-Fi was connected. That discrepancy is an important lead: the reported firmware flag may be stale or ineffective on this model. It does not yet establish whether a firmware or kernel-driver defect created the original block.
- The operator subsequently installed and restarted the laptop. It returned as Kubernetes worker `kate` and was Ready at 14:14 UTC. The operator confirmed the new target's identity without a visual fingerprint comparison; its new SSH key was pinned for diagnostics. Both WLAN radios were software blocked again on the installed boot, and Ethernet still had carrier. Ready therefore did not establish Wi-Fi connectivity.
- On the installed system, `/var/lib/systemd/rfkill` was in the temporary `/var` overlay; its state files were born during the installed boot. The actual `PERSISTENT_STATE_PATHS` omitted that directory. The earlier manual choice was therefore not retained through Kairos's native persistent state paths.

### Current interpretation and open checks

The [upstream IdeaPad driver](https://github.com/torvalds/linux/blob/master/drivers/platform/x86/lenovo/ideapad-laptop.c) initializes its platform software block from the embedded controller's per-radio value in `ideapad_register_rfkill()`. Read-only disassembly of the actual `7.3.0-hadron` module copied from this laptop confirmed the same sequence inside `ideapad_acpi_add()`: read the EC value, turn zero into a blocked boolean, call `rfkill_init_sw_state()`, then register the radio. This is a concrete Lenovo-specific initialization path; no blacklist or module reload was used to inspect it.

The copied rfkill core module also contains a registration path calling `__rfkill_switch_all()`, consistent with the [upstream legacy input handling](https://github.com/torvalds/linux/blob/master/net/rfkill/core.c). The controlled test below reproduced a platform registration blocking the existing adapter despite the enabled kernel default.

[systemd-rfkill](https://www.freedesktop.org/software/systemd/man/latest/systemd-rfkill.service.html) saves and restores Linux radio state. The controlled test identifies platform-driver registration as a sufficient trigger without saved-state restoration. The original boot's EC read was not traced, but the same driver/firmware path reproduces its failure. Whether the firmware's per-radio value is a retained setting, an ineffective legacy interface or a firmware/driver defect remains unresolved.

Still unresolved:

- Why the EC readback remains zero in both blocked and working states, and whether the legacy ACPI interface is meaningful on this model.
- Whether preserving the operator-selected rfkill state with Kairos's native persistence can restore that choice across installed boots. The native profile below has been staged, but physical state-restoration verification is pending.
- Whether the block returns after another installed reboot with saved state preserved, suspend or a cold power cycle. The initial installed boot already reproduced the block; those additional cases remain untested.
- Whether Tailscale remains reachable after physically disconnecting Ethernet; association and DHCP alone do not prove the remaining traffic uses Wi-Fi.

### Controlled registration test

At 14:18 UTC, with additional tests authorized and Ethernet confirmed connected, the prepared `registration-test.sh` was executed over pinned SSH. WLAN was first manually unblocked and both saved WLAN state files were confirmed to contain `0`. The test then stopped both `systemd-rfkill` units to exclude state restoration, removed and re-registered only `ideapad_laptop`, and captured the following states:

| Phase | Platform WLAN software block | Adapter `phy0` software block |
| --- | --- | --- |
| Before removal, state restoration stopped | Unblocked | Unblocked |
| Platform driver removed | Platform radio absent | Unblocked |
| Same platform driver registered again | Blocked | Blocked |
| Manual recovery and state-saving socket restored | Unblocked | Unblocked |

The EC continued to report master radio on and Wi-Fi off throughout. The real Wi-Fi driver was not removed. The exit trap manually unblocked WLAN and restored the state-saving socket; Wi-Fi re-associated, acquired DHCP and passed the interface-bound HTTPS check afterward. Bluetooth remained blocked. This reproduced the connection failure without a reboot, image change, stale blocked state file or Linux state-restoration writer. The causal trigger is platform registration; the reason for the firmware's inconsistent per-radio value is still open.

### Selected-state persistence trial

At 14:28 UTC, a native `rootfs` step was added to `kate`'s installed OEM profile and its private operator configuration. It writes `CUSTOM_BIND_MOUNTS: /var/lib/systemd/rfkill` to `/run/cos/custom-layout.env`, following [Kairos's documented post-install bind mounts](https://kairos.io/docs/advanced/customizing/#bind-mounts). The same proposed step is in `kairos/worker.yaml`. It preserves whichever radio state the operator chooses; it contains no unblock command and is excluded from live-media boots.

The existing OEM and operator profiles were backed up privately. Native Kairos validation passed. Both `systemd-rfkill` units were briefly stopped while the current selections were copied to `/usr/local/.state/var/lib/systemd/rfkill/`: both WLAN entries enabled (`0`), both Bluetooth entries blocked (`1`). The socket was restored, both filesystems were flushed, and a reboot was requested. Pinned SSH returned with a new boot ID at approximately 14:30 UTC, but radio and bind-mount verification was not captured before work paused.

On resumption at 15:08 UTC, Tailscale reported `kate` offline since 15:00 UTC and Kubernetes reported NotReady. Its present power/cable state has been requested from the operator. This is not yet evidence that the persistence trial failed: the laptop returned after the test reboot and went offline later. Further radio tests and Wi-Fi-only reachability verification require it to be reachable again. Local validation of the proposed profile passed its 116 unit tests and native Kairos checks; remaining results will be recorded when complete.

At 15:13 UTC, after the operator confirmed power and Ethernet, `kate` rejoined with another boot ID. Inspection confirmed the native bind was active on `/dev/sda5[/.state/var-lib-systemd-rfkill.bind]`, but WLAN was blocked and both saved WLAN files contained `1`. The initial seeding had used `/usr/local/.state/var/lib/systemd/rfkill/`, which is not the backing location Kairos uses for `CUSTOM_BIND_MOUNTS`. That first trial therefore did not test restoration of the intended enabled state. The current WLAN choice was manually enabled and allowed to save through the running service into the mounted directory, both saved WLAN values were verified as `0`, and another reboot was requested. No startup unblock hook was present.

The disposable image harness has been extended to verify both selected states with the real systemd rfkill service across three installed boots per worker suite; [development](development.md#disposable-image-checks) owns the test procedure. These test additions are pending a full image run. Persistence alone does not change the Lenovo's initial live-USB state or a first installed boot with no saved selection. That limit remains relevant to enrollment; no automatic unblock or firmware change has been substituted for it.

### Changes and test history

Commit `c36aa92` proposed automatic WLAN unblocking before Wi-Fi service startup, a build-time check for the existing `rfkill` tool and an image test starting with a software-blocked client. Local repository validation passed, including 116 unit tests and native Kairos/Terraform checks; CI repository validation and firewall packet checks also passed. The controller, BIOS worker and UEFI worker image suites passed. Those results demonstrate the proposed recovery in simulated radios, not the origin of the physical Lenovo's block.

In the [first image-test attempt](https://github.com/klever-lab/guaranine/actions/runs/44096359976/attempts/1), the live BIOS guest failed before Wi-Fi startup: the kernel received no initrd and panicked while mounting the live root. Its cause is unresolved. A read-only local BIOS USB boot of the unchanged saved ISO, with guest networking disabled, reached Guaranine userspace in 37 seconds. The [single live-suite rerun](https://github.com/klever-lab/guaranine/actions/runs/44096359976/attempts/2) passed all three live boots; the resulting workflow had all four suites passing. That rerun does not explain or fix the initial failure.

The automatic-unblock implementation and its dedicated regression additions were withdrawn in commit `e7cd8aa` at the user's request. Repository validation and all four image suites passed for that revision. No automatic workaround, driver blacklist, firmware update or kernel argument was applied. The operator performed installation and a restart; diagnostics subsequently performed the authorized temporary platform-driver reload and manual WLAN recovery. Private observations are saved under the operator directory's `diagnostics/lenovo-1794923077933573636/` (`initial.log`, `rfkill.log`, `block-origin.log`, live and installed recovery logs, copied driver modules, `driver-inspection.log`, `registration-test.log`, `registration-test-recovery.log` and CI/local boot logs); credentials and raw private logs stay outside Git.
