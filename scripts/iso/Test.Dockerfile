FROM debian:trixie-slim@sha256:a72b4433de1cf7d6bae0c4cee74d0547f58601a643b3375f070c51600d63b6b2
ADD --checksum=sha256:427c064216761a1e72a8b30f0d954d08cb6e4435a82091fb865afd20c8497285 https://curl.se/ca/cacert-2026-11-05.pem /etc/ssl/certs/ca-certificates.crt
RUN set -eu; \
    chmod 0644 /etc/ssl/certs/ca-certificates.crt; \
    cp /etc/ssl/certs/ca-certificates.crt /tmp/build-ca.pem; \
    rm /etc/apt/sources.list.d/debian.sources; \
    apt_get() { apt-get -o Acquire::https::CaInfo=/tmp/build-ca.pem -o Acquire::Retries=3 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 "$@"; }; \
    indexed=0; \
    for mirror in snapshot.debian.org snapshot-cloudflare.debian.org; do \
      echo "deb [check-valid-until=no] https://$mirror/archive/debian/20261101T000000Z trixie main" > /etc/apt/sources.list; \
      if apt_get -o APT::Update::Error-Mode=any update; then \
        indexed=1; break; \
      fi; \
    done; \
    test "$indexed" = 1; \
    apt_get -o Acquire::ForceHash=SHA256 --print-uris --yes --download-only --no-install-recommends install ca-certificates iproute2 iptables kmod procps python3 conntrack > /tmp/bootstrap-packages; \
    while read -r quoted_uri filename size hash; do \
      case "$quoted_uri" in \'*) ;; *) continue ;; esac; \
      uri=${quoted_uri#\'}; uri=${uri%\'}; \
      case "$uri" in https://snapshot.debian.org/archive/debian/*/pool/*|https://snapshot-cloudflare.debian.org/archive/debian/*/pool/*) ;; *) exit 1 ;; esac; \
      case "$filename" in */*|.*|'') exit 1 ;; esac; \
      case "$size" in *[!0-9]*|'') exit 1 ;; esac; \
      case "$hash" in SHA256:*) digest=${hash#SHA256:} ;; *) exit 1 ;; esac; \
      test "${#digest}" = 64; \
      case "$digest" in *[!0-9a-f]*) exit 1 ;; esac; \
      pool_path=${uri#*/pool/}; \
      destination=/var/cache/apt/archives/$filename; \
      if ! /usr/lib/apt/apt-helper -o Acquire::https::CaInfo=/tmp/build-ca.pem -o Acquire::Retries=3 -o Acquire::https::Timeout=30 download-file "https://deb.debian.org/debian/pool/$pool_path" "$destination" "$hash"; then \
        /usr/lib/apt/apt-helper -o Acquire::https::CaInfo=/tmp/build-ca.pem -o Acquire::Retries=3 -o Acquire::https::Timeout=30 download-file "$uri" "$destination" "$hash"; \
      fi; \
    done < /tmp/bootstrap-packages; \
    apt_get --no-download install -y --no-install-recommends ca-certificates iproute2 iptables kmod procps python3 conntrack; \
    cp /tmp/build-ca.pem /etc/ssl/certs/ca-certificates.crt && \
    rm -rf /var/lib/apt/lists/*
ENTRYPOINT ["/bin/bash", "/fixture/test-coordinator.sh"]
