#!/usr/bin/env bash
set -euo pipefail

cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
python3 -m pymarkdown --config .pymarkdown.json scan --recurse --respect-gitignore .
python3 -m unittest discover --start-directory scripts/tests --pattern 'test_*.py'
python3 scripts/validate-worker.py
python3 scripts/validate-terraform.py
