FROM debian:trixie-slim@sha256:a72b4433de1cf7d6bae0c4cee74d0547f58601a643b3375f070c51600d63b6b2
ADD --checksum=sha256:427c064216761a1e72a8b30f0d954d08cb6e4435a82091fb865afd20c8497285 https://curl.se/ca/cacert-2026-11-05.pem /etc/ssl/certs/ca-certificates.crt
RUN chmod 0644 /etc/ssl/certs/ca-certificates.crt && \
    cp /etc/ssl/certs/ca-certificates.crt /tmp/build-ca.pem && \
    rm /etc/apt/sources.list.d/debian.sources && \
    echo 'deb [check-valid-until=no] https://snapshot-cloudflare.debian.org/archive/debian/20261101T000000Z trixie main' > /etc/apt/sources.list && \
    apt-get -o Acquire::https::CaInfo=/tmp/build-ca.pem -o Acquire::Retries=5 -o APT::Update::Error-Mode=any update && \
    apt-get -o Acquire::https::CaInfo=/tmp/build-ca.pem install -y --no-install-recommends ca-certificates iproute2 iptables kmod procps python3 conntrack && \
    cp /tmp/build-ca.pem /etc/ssl/certs/ca-certificates.crt && \
    rm -rf /var/lib/apt/lists/*
ENTRYPOINT ["/bin/bash", "/fixture/test-coordinator.sh"]
