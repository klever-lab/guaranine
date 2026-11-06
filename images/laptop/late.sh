#!/bin/sh
set -eu
umask 077
mkdir -p /target/var/cache/apt/archives
cp /cdrom/guaranine/packages/*.deb /target/var/cache/apt/archives/
cp /cdrom/guaranine/packages/SHA256SUMS /target/var/cache/apt/archives/guaranine-SHA256SUMS
# in-target may bind the installer's /run over the target /run. Public packages
# belong in APT's archive cache. APT 3.0 --no-download also requires that cache
# location even when absolute local .deb paths are given.
if ! in-target /usr/bin/env DEBIAN_FRONTEND=noninteractive /bin/sh -c 'cd /var/cache/apt/archives && sha256sum --check --quiet guaranine-SHA256SUMS && apt-get --yes --no-download --no-install-recommends install /var/cache/apt/archives/*.deb' > /target/var/log/guaranine-package-install.log 2>&1; then
    echo 'Worker package installation failed; inspect target /var/log/guaranine-package-install.log' >&2
    exit 1
fi
rm -f /target/var/cache/apt/archives/*.deb /target/var/cache/apt/archives/guaranine-SHA256SUMS /target/var/log/guaranine-package-install.log
mkdir -p /target/usr/lib/guaranine
cp -R /cdrom/guaranine/worker/. /target/usr/lib/guaranine/
in-target /bin/bash /usr/lib/guaranine/install.sh --installer
# Never retain installer transcripts: future versions of d-i may record answers.
rm -rf /target/var/log/installer
# d-i has already created a fresh machine-id and SSH keys on this target.
# No credentials, host identities, or user-provided config exist on the ISO.
