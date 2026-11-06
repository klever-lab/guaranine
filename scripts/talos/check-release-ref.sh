#!/usr/bin/env bash
set -euo pipefail
[[ ${RELEASE_REF:?} =~ ^refs/tags/worker-iso-v[0-9]+\.[0-9]+\.[0-9]+$ ]]
commit=$(git rev-parse HEAD)
git merge-base --is-ancestor "$commit" origin/main
# Direct pushes to main are supported. Require successful repository CI on the
# exact tagged commit; no PR or human approval is part of release authorization.
gh api --paginate --slurp "repos/$GH_REPO/commits/$commit/check-runs?filter=all&per_page=100" |
  jq --exit-status --arg head "$commit" \
    '[.[] | .check_runs[] | select(.app.slug == "github-actions" and .head_sha == $head and .name == "Clusterfork validation")] |
     max_by(.id) | .status == "completed" and .conclusion == "success"' >/dev/null
