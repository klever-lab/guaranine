#!/usr/bin/env bash
set -euo pipefail
[[ ${RELEASE_REF:?} =~ ^refs/tags/worker-iso-v[0-9]+\.[0-9]+\.[0-9]+$ ]]
commit=$(git rev-parse HEAD)
git merge-base --is-ancestor "$commit" origin/main
# An ancestry check alone would admit direct pushes. Require a merged main PR
# and successful CI on its accepted head; autonomous work needs no review handoff.
prs=$(gh api --paginate --slurp "repos/$GH_REPO/commits/$commit/pulls")
pull=$(jq -ec --arg commit "$commit" --arg repo "$GH_REPO" \
  '[.[][] | select(.merged_at != null and .base.ref == "main" and .base.repo.full_name == $repo and
    (.merge_commit_sha == $commit or .head.sha == $commit))][0] // error("No merged main PR")' <<< "$prs")
accepted_head=$(jq -er '.head.sha' <<< "$pull")
gh api --paginate --slurp "repos/$GH_REPO/commits/$accepted_head/check-runs?filter=all&per_page=100" | \
  jq --exit-status --arg head "$accepted_head" \
    'def passed($name): map(select(.name == $name)) | max_by(.id) |
       .status == "completed" and .conclusion == "success";
     [.[] | .check_runs[] | select(.app.slug == "github-actions" and .head_sha == $head)] |
     passed("Clusterfork validation") and passed("trust") and passed("build-test")' >/dev/null
