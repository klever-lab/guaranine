# Laptop hardware

## Network and boot requirements

Use a 2.4-GHz WPA2-Personal network with 802.11g clients enabled for this mixed fleet; its older single-band cards cannot join a 5-GHz-only SSID. Disable Secure Boot for this image. Built-in Wi-Fi must work from live boot through installation and normal operation, without Ethernet, adapters or external bridges.

New laptop ISOs and installed workers enable `efi=debug` by default so EFI startup diagnostics can be photographed without editing GRUB. The native worker profile owns the value; AuroraBoot appends the same value to live boot entries. Updating an existing USB follows the [image rebuild procedure](operations.md#image-batches-and-retries).

On UEFI workers, a native `after-install` hook registers **Kairos** for the installed disk's `\EFI\boot\bootx64.efi`, places it first in the firmware boot order and clears an overriding one-time boot choice. Existing entries are preserved. The hook verifies the partition GUID and loader path before allowing poweroff; `strict: true` makes hook errors fatal. BIOS workers keep the native BIOS bootloader path.

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

The [boot checks](../scripts/test-private-images.py) exercise BIOS/UEFI USB boot, an older CPU baseline, installation and reboot, plus WPA2 association, DHCP and AP outage recovery with `mac80211_hwsim`. The UEFI worker case seeds old Ubuntu/Linux/Windows entries and a stale one-time boot choice, retains the same firmware state across installation and reboot, and checks that the registered Kairos entry actually booted. Installation needs an explicit success marker before a VM shutdown counts as success. The checks also cover distinct live SSH identities, the controller's missing-data-volume guard, persistent worker configuration and unchanged unrelated guest disks. [Development](development.md#disposable-image-checks) owns the command and CI workflow.

Recorded result on 2026-11-13 UTC: the [full four-suite run](https://github.com/klever-lab/guaranine/actions/runs/43349091458) passed all eight guests, including Wi-Fi after BIOS and UEFI worker installation, default EFI diagnostics, temporary live Tailscale state and its absence after installation. This is a software qualification record. The installed physical cards and their RF behavior have not been measured; lid/suspend behavior and real WAN recovery also need hardware/live-network checks. Keep those pending checks distinct from the simulated-radio and firmware-path results.

A physical Inspiron 5558 initially failed during UEFI startup with GRUB reporting `start_image() returned 0x8000000000000001`, with Secure Boot disabled. The operator subsequently reported reaching Kairos after adding `efi=debug`, and the live laptop appeared online in Tailscale. This records a successful physical live boot; it does not establish that the flag caused the recovery or verify installation and post-install Wi-Fi. The ISO also reached Linux in isolated UEFI checks using the host CPU with 2 GiB RAM and an emulated Broadwell CPU with 4 GiB RAM; its kernel checksum matches the running controller's kernel. The initial failure's cause remains unconfirmed.

The inventory above owns model names. There is no second model list or manual qualification flag in operator configuration. Before adding a model or changing the kernel, firmware or installer, verify its actual radio variants against the recipe's driver/alias/firmware checks, then rerun the relevant build and boot checks. Confirm physical boot, installation and post-install Wi-Fi on the target unit before extending a hardware compatibility claim.

Secure Boot, TPM-bound enrollment and YubiKey boot authorization require separate qualification and are not implemented here. [Enrollment security](security.md#private-material-and-enrollment) describes the media's trust limits.
