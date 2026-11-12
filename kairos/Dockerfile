# Only public inputs enter this image and its build cache.
FROM alpine@sha256:469c9ea410b9215eaee7976ba6c0639039b444be8e5d638f7abbada189971eb2 AS wifi
RUN apk add --no-cache wpa_supplicant=2.11-r3 nftables=1.1.5-r2 && \
    mkdir -p /out/usr/lib/guaranine-network && \
    for program in wpa_supplicant nft; do \
      install -m 0755 "$(command -v "$program")" "/out/usr/lib/guaranine-network/$program"; \
      ldd "$(command -v "$program")" | awk '$2 == "=>" && $3 ~ /^\// {print $3} $1 ~ /^\// {print $1}' | \
        sort -u | while read -r library; do cp -L "$library" /out/usr/lib/guaranine-network/; done; \
    done

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
