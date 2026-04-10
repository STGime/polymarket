#!/bin/bash
set -euo pipefail

PROJECT_ID="${GCP_PROJECT_ID:?Set GCP_PROJECT_ID environment variable}"
REGION="${GCP_REGION:-us-central1}"
SERVICE_NAME="polymarket-weather-bot"
IMAGE="gcr.io/${PROJECT_ID}/${SERVICE_NAME}"

echo "Building and submitting image..."
gcloud builds submit --tag "${IMAGE}"

echo "Deploying to Cloud Run..."
gcloud run deploy "${SERVICE_NAME}" \
    --image "${IMAGE}" \
    --region "${REGION}" \
    --platform managed \
    --min-instances 1 \
    --max-instances 1 \
    --memory 512Mi \
    --cpu 1 \
    --no-cpu-throttling \
    --port 8080 \
    --timeout 3600 \
    --no-allow-unauthenticated \
    --set-env-vars "DRY_RUN=true,BANKROLL_USD=1000" \
    --execution-environment gen2

echo "Deployment complete!"
gcloud run services describe "${SERVICE_NAME}" --region "${REGION}" --format='value(status.url)'
