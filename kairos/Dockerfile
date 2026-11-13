# Only public inputs enter this image and its build cache.
FROM alpine@sha256:469c9ea410b9215eaee7976ba6c0639039b444be8e5d638f7abbada189971eb2 AS wifi
RUN apk add --no-cache wpa_supplicant=2.11-r3 nftables=1.1.5-r2 b43-fwcutter=019-r3 && \
    mkdir -p /out/usr/lib/guaranine-network && \
    for program in wpa_supplicant nft; do \
      install -m 0755 "$(command -v "$program")" "/out/usr/lib/guaranine-network/$program"; \
      ldd "$(command -v "$program")" | awk '$2 == "=>" && $3 ~ /^\// {print $3} $1 ~ /^\// {print $1}' | \
        sort -u | while read -r library; do cp -L "$library" /out/usr/lib/guaranine-network/; done; \
    done

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
ARG TAILSCALE_VERSION
ARG TAILSCALE_SHA256
COPY --from=wifi /out/ /
COPY --from=ghcr.io/kairos-io/hadron-firmware/linux-firmware-intel-iwlwifi@sha256:38ee9f52f6e66f28d3c09e711934804eeda37ad0cb5f0caaf08c079543f9c4e1 / /
COPY --from=ghcr.io/kairos-io/hadron-firmware/linux-firmware-uncategorized@sha256:5d57d181c3de2b53a68e89d9413358ac9e6c4b7352194aebe2bbcfa9e3a5341f / /
COPY --from=ghcr.io/kairos-io/hadron-firmware/linux-firmware-ath6k@sha256:8853eddb20c8d872264a893667467343c99e2af4395f28fd45e9459bafe4fdec / /
COPY --from=ghcr.io/kairos-io/hadron-firmware/linux-firmware-ath9k_htc@sha256:44430ab326328d4cee7a3c4a8d349ffcee53ae16af33fbafddc7b7b8a6b80e08 / /
COPY --from=ghcr.io/kairos-io/hadron-firmware/linux-firmware-ath10k@sha256:ef84b959a16d3fcba57528faeb1fcc1558e80307c67ad68264cf352f0920ae72 / /
COPY --from=ghcr.io/kairos-io/hadron-firmware/linux-firmware-ath11k@sha256:83f3ed39e11083658a3e347644deaa70266a09ae694c861d4bc123bcfa0220a8 / /
COPY --from=ghcr.io/kairos-io/hadron-firmware/linux-firmware-ath12k@sha256:b65c330aecd1898a15c4425ad2ff6e1d6663721fba84b8899f6ba4a45810f039 / /
COPY --from=ghcr.io/kairos-io/hadron-firmware/linux-firmware-rtlwifi@sha256:35abc3d26c40002b4a5b0b1a07c5e9342d89e47b5fe432083f2cca48669d9de4 / /
COPY --from=ghcr.io/kairos-io/hadron-firmware/linux-firmware-rtw88@sha256:2a8968ac8c269b975a1d156fc10409d279d1c5258ab20efbc6a88f93475012f5 / /
COPY --from=ghcr.io/kairos-io/hadron-firmware/linux-firmware-rtw89@sha256:46b1df9fd2a9dbb341ff79e5f00f4ce659090708e2af295b46126644f2f7b0b1 / /
COPY --from=ghcr.io/kairos-io/hadron-firmware/linux-firmware-mediatek@sha256:88492be8cfeca131781bf4a3b489516c218992736e10ce4faadd0ed9f26054a9 / /
COPY --from=ghcr.io/kairos-io/hadron-firmware/linux-firmware-brcm@sha256:3849bc5ee85ea652a583e684f8644a6106848cade1c707bfd50acc6462cec3b2 / /

# Keep image-owned firmware outside Kairos's persistent /usr/local mount.
RUN cp -a /usr/local/lib/firmware/. /usr/lib/firmware/ && \
    rm -rf /usr/local/lib/firmware && \
    mv /usr/lib/firmware/intel/iwlwifi/* /usr/lib/firmware/ && \
    rmdir /usr/lib/firmware/intel/iwlwifi && \
    curl --fail --location --output /run/k3s \
      "https://github.com/k3s-io/k3s/releases/download/${K3S_VERSION}/k3s" && \
    echo "${K3S_SHA256}  /run/k3s" | sha256sum --check && \
    install -m 0755 /run/k3s /usr/bin/k3s && \
    curl --fail --location --output /run/tailscale.tgz \
      "https://pkgs.tailscale.com/stable/tailscale_${TAILSCALE_VERSION}_amd64.tgz" && \
    echo "${TAILSCALE_SHA256}  /run/tailscale.tgz" | sha256sum --check && \
    mkdir /run/tailscale && tar -xzf /run/tailscale.tgz --strip-components=1 -C /run/tailscale && \
    install -m 0755 /run/tailscale/tailscale /usr/bin/tailscale && \
    install -m 0755 /run/tailscale/tailscaled /usr/sbin/tailscaled && \
    install -m 0644 /run/tailscale/systemd/tailscaled.service /usr/lib/systemd/system/tailscaled.service && \
    mkdir -p /etc/default && \
    install -m 0644 /run/tailscale/systemd/tailscaled.defaults /etc/default/tailscaled && \
    rm -rf /run/k3s /run/tailscale /run/tailscale.tgz && \
    /usr/lib/guaranine-network/ld-musl-x86_64.so.1 --library-path /usr/lib/guaranine-network \
      /usr/lib/guaranine-network/wpa_supplicant -v && \
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
