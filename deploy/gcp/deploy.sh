#!/usr/bin/env bash
# Provision + deploy shortforge on GCP: Cloud Run services, Pub/Sub topics with push subscriptions
# (retry backoff + dead-letter), Firestore state, GCS artifacts, Secret Manager for API keys.
#
#   export PROJECT=my-project REGION=asia-south1
#   ./deploy/gcp/deploy.sh               # idempotent: safe to re-run after code changes
#
# API keys are read from the environment (or .env) and stored in Secret Manager:
#   GROQ_API_KEY OPENROUTER_API_KEY GEMINI_API_KEY HF_API_TOKEN PEXELS_API_KEY
#   YOUTUBE_CLIENT_ID YOUTUBE_CLIENT_SECRET YOUTUBE_REFRESH_TOKEN
set -euo pipefail
[ -f .env ] && set -a && . ./.env && set +a

: "${PROJECT:?set PROJECT}"
REGION="${REGION:-asia-south1}"
PREFIX="${PREFIX:-sf}"
BUCKET="${BUCKET:-${PROJECT}-shortforge}"
REPO="${REPO:-shortforge}"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${REPO}/shortforge:$(git rev-parse --short HEAD 2>/dev/null || date +%s)"
RUNNER_SA="sf-runner@${PROJECT}.iam.gserviceaccount.com"
INVOKER_SA="sf-invoker@${PROJECT}.iam.gserviceaccount.com"
# On GCP the final MP4 always stays in GCS; add YouTube upload when OAuth creds are present.
PUBLISHERS="${SF_PUBLISHERS:-$([ -n "${YOUTUBE_REFRESH_TOKEN:-}" ] && echo youtube || echo none)}"

gc() { gcloud --project "$PROJECT" --quiet "$@"; }
exists() { "$@" >/dev/null 2>&1; }

echo "==> APIs"
gc services enable run.googleapis.com pubsub.googleapis.com firestore.googleapis.com \
  cloudbuild.googleapis.com artifactregistry.googleapis.com storage.googleapis.com secretmanager.googleapis.com

echo "==> Firestore / GCS / Artifact Registry"
exists gc firestore databases describe --database="(default)" || \
  gc firestore databases create --location="$REGION" --type=firestore-native
exists gcloud storage buckets describe "gs://${BUCKET}" --project "$PROJECT" || \
  gcloud storage buckets create "gs://${BUCKET}" --project "$PROJECT" --location "$REGION" --uniform-bucket-level-access
# artifacts are re-derivable; expire them after 30 days to cap storage cost
echo '{"rule":[{"action":{"type":"Delete"},"condition":{"age":30}}]}' > /tmp/sf-lifecycle.json
gcloud storage buckets update "gs://${BUCKET}" --lifecycle-file=/tmp/sf-lifecycle.json --project "$PROJECT"
exists gc artifacts repositories describe "$REPO" --location "$REGION" || \
  gc artifacts repositories create "$REPO" --repository-format=docker --location "$REGION"

echo "==> Service accounts"
for sa in sf-runner sf-invoker; do
  exists gc iam service-accounts describe "${sa}@${PROJECT}.iam.gserviceaccount.com" || \
    gc iam service-accounts create "$sa" --display-name "shortforge ${sa}"
done
for role in roles/datastore.user roles/pubsub.publisher roles/storage.objectAdmin roles/secretmanager.secretAccessor roles/logging.logWriter; do
  gc projects add-iam-policy-binding "$PROJECT" --member "serviceAccount:${RUNNER_SA}" --role "$role" >/dev/null
done

echo "==> Secrets"
SECRET_FLAGS=()
for k in GROQ_API_KEY OPENROUTER_API_KEY GEMINI_API_KEY HF_API_TOKEN PEXELS_API_KEY \
         YOUTUBE_CLIENT_ID YOUTUBE_CLIENT_SECRET YOUTUBE_REFRESH_TOKEN; do
  v="${!k:-}"
  [ -z "$v" ] && continue
  name="sf-$(echo "$k" | tr '[:upper:]_' '[:lower:]-')"
  exists gc secrets describe "$name" || gc secrets create "$name" --replication-policy=automatic
  printf '%s' "$v" | gc secrets versions add "$name" --data-file=- >/dev/null
  SECRET_FLAGS+=("${k}=${name}:latest")
done
SECRETS_ARG=""
[ ${#SECRET_FLAGS[@]} -gt 0 ] && SECRETS_ARG="--set-secrets=$(IFS=,; echo "${SECRET_FLAGS[*]}")"

echo "==> Build ${IMAGE}"
gc builds submit --tag "$IMAGE" .

COMMON_ENV="SF_BACKEND=gcp,SF_GCP_PROJECT=${PROJECT},SF_GCS_BUCKET=${BUCKET},SF_PUBSUB_TOPIC_PREFIX=${PREFIX},SF_PUBLISHERS=${PUBLISHERS}"

# service name | stages | cpu | memory | max instances | concurrency
SERVICES=(
  "sf-api|none|1|512Mi|3|40"
  "sf-research|research,script|1|512Mi|5|8"
  "sf-media|voiceover,visual|2|2Gi|5|2"
  "sf-editor|editing|4|8Gi|3|1"
  "sf-gate|qa,publishing|2|2Gi|3|2"
)

declare -A STAGE_URL
for spec in "${SERVICES[@]}"; do
  IFS='|' read -r svc stages cpu mem maxi conc <<<"$spec"
  echo "==> Deploy ${svc} (${stages})"
  gc run deploy "$svc" --image "$IMAGE" --region "$REGION" --service-account "$RUNNER_SA" \
    --no-allow-unauthenticated --cpu "$cpu" --memory "$mem" --max-instances "$maxi" --concurrency "$conc" \
    --timeout 900 --set-env-vars "${COMMON_ENV},SF_SERVICE_ROLE=${stages}" ${SECRETS_ARG:+"$SECRETS_ARG"} >/dev/null
  url=$(gc run services describe "$svc" --region "$REGION" --format 'value(status.url)')
  gc run services add-iam-policy-binding "$svc" --region "$REGION" \
    --member "serviceAccount:${INVOKER_SA}" --role roles/run.invoker >/dev/null
  for st in ${stages//,/ }; do [ "$st" != none ] && STAGE_URL[$st]="$url"; done
done

echo "==> Pub/Sub topics, push subscriptions, dead letter"
PROJECT_NUMBER=$(gc projects describe "$PROJECT" --format 'value(projectNumber)')
PUBSUB_SA="service-${PROJECT_NUMBER}@gcp-sa-pubsub.iam.gserviceaccount.com"
gc iam service-accounts add-iam-policy-binding "$INVOKER_SA" --member "serviceAccount:${PUBSUB_SA}" \
  --role roles/iam.serviceAccountTokenCreator >/dev/null
DLQ="${PREFIX}-dead-letter"
exists gc pubsub topics describe "$DLQ" || gc pubsub topics create "$DLQ"
exists gc pubsub subscriptions describe "${DLQ}-hold" || gc pubsub subscriptions create "${DLQ}-hold" --topic "$DLQ" --message-retention-duration=7d
gc pubsub topics add-iam-policy-binding "$DLQ" --member "serviceAccount:${PUBSUB_SA}" --role roles/pubsub.publisher >/dev/null

for st in research script voiceover visual editing qa publishing; do
  topic="${PREFIX}-${st}"; sub="${topic}-push"; endpoint="${STAGE_URL[$st]}/pubsub/${st}"
  exists gc pubsub topics describe "$topic" || gc pubsub topics create "$topic"
  flags=(--push-endpoint "$endpoint" --push-auth-service-account "$INVOKER_SA" --ack-deadline 600
         --min-retry-delay 10s --max-retry-delay 600s --dead-letter-topic "$DLQ" --max-delivery-attempts 6)
  if exists gc pubsub subscriptions describe "$sub"; then
    gc pubsub subscriptions update "$sub" "${flags[@]}" >/dev/null
  else
    gc pubsub subscriptions create "$sub" --topic "$topic" "${flags[@]}" >/dev/null
  fi
  gc pubsub subscriptions add-iam-policy-binding "$sub" --member "serviceAccount:${PUBSUB_SA}" --role roles/pubsub.subscriber >/dev/null
done

API_URL=$(gc run services describe sf-api --region "$REGION" --format 'value(status.url)')
cat <<EOF

Deployed. Submit a job (caller needs roles/run.invoker on sf-api):
  curl -X POST "${API_URL}/jobs" -H "Authorization: Bearer \$(gcloud auth print-identity-token)" \\
       -H 'Content-Type: application/json' -d '{"topic":"the lighthouse keeper who never left"}'
  curl "${API_URL}/jobs/<job_id>" -H "Authorization: Bearer \$(gcloud auth print-identity-token)"
Dead letters: gcloud pubsub subscriptions pull ${DLQ}-hold --project ${PROJECT} --limit 10
EOF
