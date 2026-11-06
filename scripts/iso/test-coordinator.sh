#!/usr/bin/env bash
# DISPOSABLE TEST CONTAINER ONLY. Never used by the laptop profile or in Azure.
set -euo pipefail
umask 077
mkdir -p /var/lib/headscale /var/lib/tailscale /run/tailscale /etc/headscale
cp /fixture/config.yaml /etc/headscale/config.yaml
cp /fixture/ca.crt /usr/local/share/ca-certificates/test-control.crt
update-ca-certificates >/dev/null 2>&1
/fixture/headscale serve >/dev/null 2>&1 &
/fixture/tailscaled --state=/var/lib/tailscale/state --socket=/run/tailscale/tailscaled.sock >/dev/null 2>&1 &
sleep 3
/fixture/headscale users create iso-test >/dev/null
key=$(/fixture/headscale preauthkeys create --user 1 --expiration 2h --output json | python3 -c 'import json,sys; print(json.load(sys.stdin)["key"])')
printf '%s' "$key" > /run/coordinator-key
/fixture/tailscale up --login-server="https://$TEST_CONTROL_IP:8443" \
  --auth-key=file:/run/coordinator-key --hostname=test-hub --accept-dns=false --timeout=60s >/dev/null 2>&1
rm /run/coordinator-key
address=$(/fixture/tailscale ip -4)
exec /fixture/k3s server --node-ip="$address" --advertise-address="$address" \
  --bind-address="$address" --tls-san="$address" --flannel-iface=tailscale0 \
  --snapshotter=native \
  --disable=traefik,servicelb,metrics-server,local-storage \
  --node-taint=node-role.kubernetes.io/control-plane=true:NoSchedule \
  --write-kubeconfig-mode=0600 >/dev/null 2>&1
