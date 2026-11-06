#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.."
output=$(realpath -m -- "${1:?Usage: build.sh OUTPUT_DIRECTORY}")
mkdir -p -- "$output"
docker build --platform linux/amd64 --tag guaranine-iso-builder --file images/laptop/Dockerfile images/laptop
# Mount source read-only. The builder copies an explicit public-file allowlist.
docker run --rm --platform linux/amd64 --user "$(id -u):$(id -g)" \
  --mount "type=bind,source=$PWD,target=/source,readonly" \
  --mount "type=bind,source=$output,target=/output" \
  --env "SOURCE_COMMIT=$(git rev-parse HEAD)" \
  --env "SOURCE_DIRTY=$([[ -z $(git status --porcelain) ]] && echo false || echo true)" \
  guaranine-iso-builder /output
