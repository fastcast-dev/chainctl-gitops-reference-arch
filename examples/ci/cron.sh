#!/usr/bin/env bash
# Cron / workstation adapter: nightly drift detection + optional self-heal.
#
#   17 6 * * * /opt/chainguard-gitops/examples/ci/cron.sh >> /var/log/cg-sync.log 2>&1
#
# Auth options on a host:
#   - service account: chainctl auth login --identity "$ID" --identity-token "$TOKEN_FILE"
#     with a token your IdP mints for the host (short-lived, rotate via your IdP)
#   - human workstation: `chainctl auth login` once; the refresh token keeps
#     subsequent runs non-interactive.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

# Always reconcile from the reviewed source of truth, never local edits.
git fetch --quiet origin main
git checkout --quiet origin/main

SELF_HEAL="${SELF_HEAL:-false}" # true => re-apply on drift (e.g. console edits)

set +e
./bin/cg-sync plan --output text
rc=$?
set -e

case "$rc" in
  0) echo "$(date -u +%FT%TZ) in sync" ;;
  2)
    echo "$(date -u +%FT%TZ) DRIFT DETECTED"
    if [ "$SELF_HEAL" = "true" ]; then
      ./bin/cg-sync apply --output text
    else
      # Hook your alerting here (Slack webhook, PagerDuty, email...)
      exit 2
    fi
    ;;
  *) echo "$(date -u +%FT%TZ) plan failed (rc=$rc)"; exit "$rc" ;;
esac
