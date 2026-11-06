#!/usr/bin/env bash
# Install the SAME profile on a d-i target or an existing Debian 13 host.
set -euo pipefail
umask 077
test "$(id -u)" = 0
. /etc/os-release
test "$ID" = debian && test "$VERSION_ID" = 13
test "$(dpkg --print-architecture)" = amd64
test ! -e /etc/systemd/system/k3s.service
test ! -d /var/lib/rancher/k3s/server
source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
if [[ ${1:-} != --installer ]]; then
  # Existing hosts retain their own signed update sources and newer OS packages.
  apt-get -o Acquire::Retries=5 -o APT::Update::Error-Mode=any update -qq
  DEBIAN_FRONTEND=noninteractive xargs apt-get --yes --no-install-recommends install < "$source_dir/apt-packages.txt"
fi
if [[ $source_dir != /usr/lib/guaranine ]]; then
  install -d -m 0755 /usr/lib/guaranine
  cp -R "$source_dir"/. /usr/lib/guaranine/
fi
chmod -R go-w /usr/lib/guaranine
install -m 0755 /usr/lib/guaranine/vendor/k3s /usr/local/bin/k3s
install -m 0755 /usr/lib/guaranine/vendor/tailscale /usr/local/bin/tailscale
install -m 0755 /usr/lib/guaranine/vendor/tailscaled /usr/local/sbin/tailscaled
install -d -m 0700 /var/lib/guaranine /etc/rancher/k3s
install -m 0644 /usr/lib/guaranine/files/*.service /usr/lib/guaranine/files/*.timer /etc/systemd/system/
install -m 0644 /usr/lib/guaranine/files/network.nft /etc/guaranine-network.nft
install -d -m 0755 /etc/ssh/sshd_config.d /etc/systemd/system/ssh.service.d
install -m 0644 /usr/lib/guaranine/files/sshd.conf /etc/ssh/sshd_config.d/90-guaranine.conf
if [[ ${1:-} == --installer ]]; then
  # The installed host follows signed security updates after the pinned install.
  rm -f /etc/apt/sources.list
  install -m 0644 /usr/lib/guaranine/files/debian.sources /etc/apt/sources.list.d/debian.sources
  printf 'APT::Periodic::Update-Package-Lists "1";\nAPT::Periodic::Unattended-Upgrade "1";\nUnattended-Upgrade::Automatic-Reboot "false";\n' > /etc/apt/apt.conf.d/20auto-upgrades
fi
printf '[Unit]\nRequires=guaranine-network.service\nAfter=guaranine-network.service\n' > /etc/systemd/system/ssh.service.d/guaranine.conf
install -m 0755 /usr/lib/guaranine/files/guaranine-worker /usr/local/sbin/guaranine-worker
systemctl enable guaranine-network.service tailscaled.service guaranine-setup.service guaranine-enroll.timer
if [[ ${1:-} != --installer ]]; then
  systemctl daemon-reload
  systemctl start guaranine-network.service tailscaled.service guaranine-enroll.timer
  exec /usr/local/sbin/guaranine-worker configure
fi
