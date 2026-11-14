# Only public inputs enter this image and its build cache.
FROM public.ecr.aws/docker/library/alpine@sha256:469c9ea410b9215eaee7976ba6c0639039b444be8e5d638f7abbada189971eb2 AS wifi
RUN apk add --no-cache wpa_supplicant=2.11-r3 nftables=1.1.5-r2 b43-fwcutter=019-r3 efibootmgr=18-r2 && \
    mkdir -p /out/usr/lib/guaranine-network /out/usr/bin && \
    for program in wpa_supplicant nft efibootmgr; do \
      install -m 0755 "$(command -v "$program")" "/out/usr/lib/guaranine-network/$program"; \
      ldd "$(command -v "$program")" | awk '$2 == "=>" && $3 ~ /^\// {print $3} $1 ~ /^\// {print $1}' | \
        sort -u | while read -r library; do cp -L "$library" /out/usr/lib/guaranine-network/; done; \
    done && \
    printf '%s\n' '#!/bin/sh' \
      'exec /usr/lib/guaranine-network/ld-musl-x86_64.so.1 --library-path /usr/lib/guaranine-network /usr/lib/guaranine-network/efibootmgr "$@"' \
      > /out/usr/bin/efibootmgr && chmod 0755 /out/usr/bin/efibootmgr

# Older Broadcom cards need b43 firmware, which linux-firmware-brcm does not contain.
RUN wget -q -O /tmp/broadcom-wl.tar.bz2 \
      https://sources.openwrt.org/broadcom-wl-5.100.138.tar.bz2 && \
    echo 'f1e7067aac5b62b67b8b6e4c517990277804339ac16065eb13c731ff909ae46f  /tmp/broadcom-wl.tar.bz2' | sha256sum -c && \
    tar -xjf /tmp/broadcom-wl.tar.bz2 -C /tmp && \
    mkdir -p /out/usr/lib/firmware && \
    b43-fwcutter -w /out/usr/lib/firmware /tmp/broadcom-wl-5.100.138/linux/wl_apsta.o

FROM quay.io/kairos/hadron@sha256:9051a82fb7d6df33598dbccb0c55417629334e384976ed6f535f40809b3977d2
ARG K3S_VERSION
ARG K3S_SHA256
COPY --from=wifi /out/ /
COPY --from=ghcr.io/kairos-io/hadron-layers/tailscale:1.102.3@sha256:d8cbe24be041718d7edca33cbeb93d5cc560fd9899f61561acc7483c8b29da2d / /
COPY --from=ghcr.io/kairos-io/hadron-firmware/linux-firmware-intel-iwlwifi@sha256:38ee9f52f6e66f28d3c09e711934804eeda37ad0cb5f0caaf08c079543f9c4e1 / /
COPY --from=ghcr.io/kairos-io/hadron-firmware/linux-firmware-uncategorized@sha256:5d57d181c3de2b53a68e89d9413358ac9e6c4b7352194aebe2bbcfa9e3a5341f / /
COPY --from=ghcr.io/kairos-io/hadron-firmware/linux-firmware-ath10k@sha256:ef84b959a16d3fcba57528faeb1fcc1558e80307c67ad68264cf352f0920ae72 / /
COPY --from=ghcr.io/kairos-io/hadron-firmware/linux-firmware-rtlwifi@sha256:35abc3d26c40002b4a5b0b1a07c5e9342d89e47b5fe432083f2cca48669d9de4 / /
COPY --from=ghcr.io/kairos-io/hadron-firmware/linux-firmware-rtw88@sha256:2a8968ac8c269b975a1d156fc10409d279d1c5258ab20efbc6a88f93475012f5 / /

# Keep image-owned firmware outside Kairos's persistent /usr/local mount.
RUN cp -a /usr/local/lib/firmware/. /usr/lib/firmware/ && \
    rm -rf /usr/local/lib/firmware && \
    mv /usr/lib/firmware/intel/iwlwifi/* /usr/lib/firmware/ && \
    rmdir /usr/lib/firmware/intel/iwlwifi && \
    curl --fail --location --output /run/k3s \
      "https://github.com/k3s-io/k3s/releases/download/${K3S_VERSION}/k3s" && \
    echo "${K3S_SHA256}  /run/k3s" | sha256sum --check && \
    install -m 0755 /run/k3s /usr/bin/k3s && \
    rm /run/k3s && \
    /usr/lib/guaranine-network/ld-musl-x86_64.so.1 --library-path /usr/lib/guaranine-network \
      /usr/lib/guaranine-network/wpa_supplicant -v && \
    efibootmgr --version && \
    rm /system/oem/00_datasource.yaml && \
    rm -f /etc/ssh/ssh_host_* /etc/machine-id /var/lib/dbus/machine-id && \
    touch /etc/machine-id && passwd --lock root

COPY config.yaml /system/oem/90_guaranine.yaml

# Require the documented laptop radios and the paths requested by their kernel drivers.
RUN set -eu; \
    set -- /usr/lib/modules/*; test "$#" -eq 1; kernel=${1##*/}; \
    for driver in ath9k ath10k_pci iwlwifi iwldvm iwlmvm rtl8192ce rtl8192se rtl8187 b43 rtw88_8821ce; do \
      modinfo -k "$kernel" "$driver" >/dev/null; \
    done; \
    for expected in \
      ath9k:pci:v0000168Cd0000002B ath9k:pci:v0000168Cd0000002A ath9k:pci:v0000168Cd00000036 \
      ath10k_pci:pci:v0000168Cd0000003E ath10k_pci:pci:v0000168Cd00000042 \
      rtl8192ce:pci:v000010ECd00008176 rtw88_8821ce:pci:v000010ECd0000C821 \
      rtl8192se:pci:v000010ECd00008171 rtl8192se:pci:v000010ECd00008172 \
      iwlwifi:pci:v00008086d000008B3 iwlwifi:pci:v00008086d000008B4 \
      iwlwifi:pci:v00008086d0000095A iwlwifi:pci:v00008086d0000095B \
      iwlwifi:pci:v00008086d00004232 iwlwifi:pci:v00008086d00004235 \
      iwlwifi:pci:v00008086d00009DF0 iwlwifi:pci:v00008086d0000A370 \
      iwlwifi:pci:v00008086d000034F0 \
      ssb:pci:v000014E4d00004311 ssb:pci:v000014E4d00004312 \
      ssb:pci:v000014E4d00004315 ssb:pci:v000014E4d0000432B \
      b43:ssb:v4243id0812; do \
      modinfo -k "$kernel" -F alias "${expected%%:*}" | grep -F "${expected#*:}" >/dev/null; \
    done; \
    for firmware in \
      iwlwifi-3160-17.ucode.zst iwlwifi-7265-17.ucode.zst iwlwifi-7265D-29.ucode.zst \
      iwlwifi-5000-5.ucode.zst iwlwifi-9000-pu-b0-jf-b0-46.ucode.zst \
      iwlwifi-Qu-b0-jf-b0-77.ucode.zst iwlwifi-Qu-c0-jf-b0-77.ucode.zst iwlwifi-QuZ-a0-jf-b0-77.ucode.zst \
      rtlwifi/rtl8192cfw.bin.zst rtlwifi/rtl8192cfwU.bin.zst rtlwifi/rtl8192cfwU_B.bin.zst \
      rtlwifi/rtl8192sefw.bin.zst \
      rtw88/rtw8821c_fw.bin.zst \
      ath10k/QCA9377/hw1.0/firmware-6.bin.zst ath10k/QCA9377/hw1.0/board.bin.zst \
      ath10k/QCA9377/hw1.0/board-2.bin.zst \
      ath10k/QCA6174/hw2.1/firmware-5.bin.zst ath10k/QCA6174/hw2.1/board.bin.zst \
      ath10k/QCA6174/hw3.0/firmware-6.bin.zst ath10k/QCA6174/hw3.0/board.bin.zst \
      ath10k/QCA6174/hw3.0/board-2.bin.zst \
      b43/ucode5.fw b43/b0g0initvals5.fw b43/b0g0bsinitvals5.fw \
      b43/ucode15.fw b43/lp0initvals15.fw b43/lp0bsinitvals15.fw \
      b43/ucode16_mimo.fw b43/n0initvals16.fw b43/n0bsinitvals16.fw; do \
      test -s "/usr/lib/firmware/$firmware" || { echo "Missing Wi-Fi firmware: $firmware" >&2; exit 1; }; \
    done
