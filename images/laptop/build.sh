#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.."
python3 scripts/talos/images.py public --output "${1:?Usage: build.sh NEW_EXTERNAL_OUTPUT_DIRECTORY}"
