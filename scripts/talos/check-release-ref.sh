#!/usr/bin/env bash
set -euo pipefail
[[ ${RELEASE_REF:?} =~ ^refs/tags/worker-iso-v[0-9]+\.[0-9]+\.[0-9]+$ ]]
commit=$(git rev-parse HEAD)
git merge-base --is-ancestor "$commit" origin/main
# Direct pushes to main are supported. Require successful repository CI on the
# exact tagged commit; no PR or human approval is part of release authorization.
# Pushing a tag also starts source validation. Wait for its newest result rather
# than racing it or accepting an older success while a rerun is still pending.
for attempt in {1..25}; do
  result=$(gh api --paginate --slurp "repos/$GH_REPO/commits/$commit/check-runs?filter=all&per_page=100" |
    jq --raw-output --arg head "$commit" \
      '[.[] | .check_runs[] | select(.app.slug == "github-actions" and .head_sha == $head and .name == "Clusterfork validation")] |
       max_by(.id) | if .status == "completed" then .conclusion else "pending" end')
  case "$result" in
    success) exit 0 ;;
    pending)
      if (( attempt < 25 )); then
        echo "Waiting for exact-commit repository validation ($attempt/25)."
        sleep 10
      fi
      ;;
    *) echo "Repository validation did not pass: $result" >&2; exit 1 ;;
  esac
done
echo "Timed out waiting for exact-commit repository validation." >&2
exit 1
