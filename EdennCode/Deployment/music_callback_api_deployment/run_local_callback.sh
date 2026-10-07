#!/usr/bin/env bash
set -euo pipefail

export PROVIDER_C_WEBHOOK_SECRET="${PROVIDER_C_WEBHOOK_SECRET:-dev-secret}"

echo "Starting ProviderC callback API on http://0.0.0.0:8787"
echo ""
echo "Expose locally with:"
echo "  Option A: ngrok http 8787"
echo "  Option B: cloudflared tunnel --url http://localhost:8787"
echo ""
echo "Callback URL format:"
echo "  https://<public-host>/provider_c/callback?secret=${PROVIDER_C_WEBHOOK_SECRET}"
echo ""

uvicorn EdennCode.Deployment.music_callback_api_deployment.callback_api:app --host 0.0.0.0 --port 8787
