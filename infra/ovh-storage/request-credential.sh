#!/usr/bin/env bash
set +ax
set -euo pipefail
umask 077

cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.."
ovh_dir=$(realpath -e -- "${GUARANINE_OPERATOR_DIR:-$HOME/.local/state/guaranine-kairos}")
ovh_out="$ovh_dir/ovh-storage.env"
if [[ $ovh_dir == "$PWD" || $ovh_dir == "$PWD/"* ]]; then
  echo 'Use an operator directory outside this checkout.' >&2
  exit 1
fi

read -r ovh_project ovh_bucket ovh_storage_region < <(
  OVH_STORAGE_DIRECTORY="$ovh_dir" uv run --no-project --with-requirements requirements.txt python - <<'PY'
import os
from pathlib import Path
import re

import yaml

directory = Path(os.environ["OVH_STORAGE_DIRECTORY"])
cloud = yaml.safe_load((directory / "ovh-clouds.yaml").read_text())["clouds"]["guaranine"]
backend = (directory / "ovh.backend.hcl").read_text()
values = dict(re.findall(r'(?m)^[ \t]*(bucket|region)[ \t]*=[ \t]*"([a-z0-9.-]+)"', backend))
print(cloud["auth"]["project_id"], values["bucket"], values["region"])
PY
)
if [[ ! $ovh_project =~ ^[a-f0-9]{32}$ || ! $ovh_storage_region =~ ^[a-z0-9-]+$ ]]; then
  echo 'Check the OVH project ID and S3 region in your operator configuration.' >&2
  exit 1
fi

IFS= read -r -p 'OVH account region [ca/eu, default ca]: ' ovh_realm </dev/tty
case "${ovh_realm:-ca}" in
  ca) ovh_endpoint=ovh-ca; ovh_url='https://ca.api.ovh.com/createToken/' ;;
  eu) ovh_endpoint=ovh-eu; ovh_url='https://api.eu.ovhcloud.com/createToken/' ;;
  *) echo 'Enter ca or eu for the region of your OVH account.' >&2; exit 1 ;;
esac

ovh_project_api="/cloud/project/$ovh_project"
ovh_storage_api="$ovh_project_api/region/${ovh_storage_region^^}/storage"
ovh_suffix=$(printf '%s' "$ovh_project" | sha256sum | cut -c1-12)
ovh_books_api="$ovh_storage_api/guaranine-books-$ovh_suffix"
ovh_rights=(
  "GET=$ovh_project_api/role"
  "GET=$ovh_project_api/user/*"
  "POST=$ovh_project_api/user"
  "POST=$ovh_project_api/user/*/policy"
  "POST=$ovh_project_api/user/*/s3Credentials"
  "POST=$ovh_project_api/user/*/s3Credentials/*/secret"
  "POST=$ovh_storage_api"
  "GET=$ovh_books_api"
  "PUT=$ovh_books_api"
)
ovh_query=$(IFS='&'; printf '%s' "${ovh_rights[*]}")
printf '\nOpen this OVH link and sign in to your account.\n%s?%s\n\n' "$ovh_url" "$ovh_query"
printf 'Enter Guaranine Storage as the application name.\n'
printf 'Enter Provision scoped S3 access for Guaranine Terraform state and the Books bucket as the description.\n'
printf 'Select a validity period of at least 24 hours and verify the Rights rows.\n'
printf '  %s\n' "${ovh_rights[@]}"
printf '\nClick Create keys and enter the three values below.\n'

unset ovh_ak ovh_as ovh_ck
IFS= read -rs -p 'Application key (AK): ' ovh_ak </dev/tty
printf '\n'
IFS= read -rs -p 'Application secret (AS): ' ovh_as </dev/tty
printf '\n'
IFS= read -rs -p 'Consumer key (CK): ' ovh_ck </dev/tty
printf '\n'
if [[ ! $ovh_ak =~ ^[A-Za-z0-9_-]+$ || ! $ovh_as =~ ^[A-Za-z0-9_-]+$ || ! $ovh_ck =~ ^[A-Za-z0-9_-]+$ ]]; then
  echo 'Each key must be nonempty and contain only letters, numbers, underscores or hyphens.' >&2
  exit 1
fi

ovh_tmp=$(mktemp "$ovh_dir/.ovh-storage.XXXXXX")
trap 'rm -f -- "$ovh_tmp"' EXIT
{
  printf 'export OVH_ENDPOINT=%q\n' "$ovh_endpoint"
  printf 'export OVH_APPLICATION_KEY=%q\n' "$ovh_ak"
  printf 'export OVH_APPLICATION_SECRET=%q\n' "$ovh_as"
  printf 'export OVH_CONSUMER_KEY=%q\n' "$ovh_ck"
  printf 'export TF_VAR_project_id=%q\n' "$ovh_project"
  printf 'export TF_VAR_state_bucket=%q\n' "$ovh_bucket"
  printf 'export TF_VAR_storage_region=%q\n' "$ovh_storage_region"
} > "$ovh_tmp"
mv -T -- "$ovh_tmp" "$ovh_out"
printf 'Saved %s with mode 0600\n' "$ovh_out"
