#!/bin/bash
set -euo pipefail

PROJECT_ID="${GCP_PROJECT_ID:?Set GCP_PROJECT_ID environment variable}"
REGION="${GCP_REGION:-us-central1}"
SERVICE_NAME="weather-bot-sim"
IMAGE="gcr.io/${PROJECT_ID}/${SERVICE_NAME}"

# Check for Met Office API key
if [ -z "${METOFFICE_API_KEY:-}" ]; then
    echo "Warning: METOFFICE_API_KEY not set. London will have 3 sources instead of 4."
    read -p "Continue anyway? (y/N) " -n 1 -r
    echo
    [[ $REPLY =~ ^[Yy]$ ]] || exit 1
fi

echo "Building image..."
gcloud builds submit --tag "${IMAGE}"

echo "Deploying simulation to Cloud Run..."

# Write env vars to a temp YAML file (handles long values with special chars)
ENV_FILE=$(mktemp -t weather-sim-env-XXXXXX.yaml)
trap "rm -f ${ENV_FILE}" EXIT

cat > "${ENV_FILE}" <<EOF
DRY_RUN: "true"
SIM_INTERVAL: "300"
SIM_DATA_DIR: "/tmp/sim_data"
SIM_GCS_BUCKET: "weather-bot-sim-data"
EOF

if [ -n "${METOFFICE_API_KEY:-}" ]; then
    # Use Python to safely YAML-escape the long key
    python3 -c "
import yaml, sys
key = '''${METOFFICE_API_KEY}'''
with open('${ENV_FILE}', 'a') as f:
    yaml.safe_dump({'METOFFICE_API_KEY': key}, f, default_style='|')
" 2>/dev/null || echo "METOFFICE_API_KEY: \"${METOFFICE_API_KEY}\"" >> "${ENV_FILE}"
fi

gcloud run deploy "${SERVICE_NAME}" \
    --image "${IMAGE}" \
    --region "${REGION}" \
    --platform managed \
    --min-instances 1 \
    --max-instances 1 \
    --memory 1Gi \
    --cpu 1 \
    --no-cpu-throttling \
    --port 8080 \
    --timeout 3600 \
    --allow-unauthenticated \
    --env-vars-file "${ENV_FILE}" \
    --execution-environment gen2 \
    --command python \
    --args sim_server.py

echo ""
echo "✅ Deployment complete!"
URL=$(gcloud run services describe "${SERVICE_NAME}" --region "${REGION}" --format='value(status.url)')
echo "🌡️  Dashboard: ${URL}"
echo "📄 Report:    ${URL}/report"
echo "📊 Trades:    ${URL}/trades"
echo ""
echo "Simulation runs automatically. First cycle starts in ~1 minute."
echo "To stop: gcloud run services delete ${SERVICE_NAME} --region ${REGION}"
