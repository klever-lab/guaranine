#!/usr/bin/env bash
set -euo pipefail
[[ ${RELEASE_REF:?} =~ ^refs/tags/worker-iso-v[0-9]+\.[0-9]+\.[0-9]+$ ]]
commit=$(git rev-parse HEAD)
git merge-base --is-ancestor "$commit" origin/main
# An ancestry check alone would also admit direct pushes. Require an associated
# merged main PR, and approval evidence from that PR's reviews.
prs=$(gh api --paginate --slurp "repos/$GH_REPO/commits/$commit/pulls")
pull=$(jq -ec --arg commit "$commit" '[.[][] | select(.merged_at != null and .base.ref == "main" and (.merge_commit_sha == $commit or .head.sha == $commit))][0] // error("No merged main PR")' <<< "$prs")
pr=$(jq -er '.number' <<< "$pull")
reviewed_head=$(jq -er '.head.sha' <<< "$pull")
gh api --paginate --slurp "repos/$GH_REPO/pulls/$pr/reviews" | \
  jq --exit-status --arg head "$reviewed_head" \
    '[.[][]] | group_by(.user.id) | map(sort_by(.submitted_at) | last) |
     any(.[]; .state == "APPROVED" and .commit_id == $head) and
     all(.[]; .state != "CHANGES_REQUESTED")' >/dev/null
