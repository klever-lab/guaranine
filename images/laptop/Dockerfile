FROM debian:trixie-slim@sha256:a72b4433de1cf7d6bae0c4cee74d0547f58601a643b3375f070c51600d63b6b2
ADD --checksum=sha256:427c064216761a1e72a8b30f0d954d08cb6e4435a82091fb865afd20c8497285 https://curl.se/ca/cacert-2026-11-05.pem /etc/ssl/certs/ca-certificates.crt
RUN set -eu; \
    chmod 0644 /etc/ssl/certs/ca-certificates.crt; \
    cp /etc/ssl/certs/ca-certificates.crt /tmp/build-ca.pem; \
    rm /etc/apt/sources.list.d/debian.sources; \
    installed=0; \
    for mirror in snapshot.debian.org snapshot-cloudflare.debian.org; do \
      echo "deb [check-valid-until=no] https://$mirror/archive/debian/20261101T000000Z trixie main non-free-firmware" > /etc/apt/sources.list; \
      if apt-get -o Acquire::https::CaInfo=/tmp/build-ca.pem -o Acquire::Retries=3 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 -o APT::Update::Error-Mode=any update && \
         apt-get -o Acquire::https::CaInfo=/tmp/build-ca.pem -o Acquire::Retries=3 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 install -y --no-install-recommends ca-certificates python3 xorriso; then \
        installed=1; break; \
      fi; \
    done; \
    test "$installed" = 1; \
    cp /tmp/build-ca.pem /etc/ssl/certs/ca-certificates.crt
COPY packages.txt /opt/worker-packages.txt
COPY download-packages.py /opt/download-packages.py
# Empty package status makes APT resolve the complete dependency closure, including
# dependencies already installed in the builder. Only download; do not install it.
RUN python3 /opt/download-packages.py && \
    rm -rf /var/lib/apt/lists/*
WORKDIR /source
ENTRYPOINT ["python3", "images/laptop/build.py"]
