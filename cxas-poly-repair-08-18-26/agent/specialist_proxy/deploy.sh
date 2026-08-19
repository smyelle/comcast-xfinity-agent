#!/usr/bin/env bash
# Deploy the specialist proxy to Cloud Run.
#
# Two IAM facts this depends on, both already in place and both easy to lose:
#
#   * the service runs as `comcast-spec-proxy@`, which holds `roles/ces.client` (to open
#     a CES session at each specialist) and `roles/datastore.user` (the job store). It
#     holds NO Comcast credential -- CES resolves the Apigee key server-side.
#   * only `service-<project number>@gcp-sa-ces.iam.gserviceaccount.com` may invoke it,
#     which is what `flows.service_agent_auth()` on the toolset authenticates as.
#
# A revision started BEFORE a Firestore grant caches the denial, so a permissions fix is
# not live until the next rollout. The service now write-probes Firestore at startup and
# says so in its first log line -- check it after every deploy:
#
#     ./deploy.sh && python probe.py
set -euo pipefail

PROJECT="${PROJECT:-ces-deployment-dev}"
REGION="${REGION:-us-central1}"
SERVICE="${SERVICE:-comcast-specialist-proxy}"
SA="${SA:-comcast-spec-proxy@${PROJECT}.iam.gserviceaccount.com}"

cd "$(dirname "$0")"
gcloud run deploy "$SERVICE" \
  --project "$PROJECT" \
  --region "$REGION" \
  --source . \
  --service-account "$SA" \
  --no-allow-unauthenticated \
  --timeout 900 \
  --concurrency 20 \
  --min-instances 0 \
  "$@"
