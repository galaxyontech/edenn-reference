# Local ProviderC Callback Test

## Step 1: Export secrets
Make sure these match how `provider_c.py` reads env vars.

```bash
export PROVIDER_C_WEBHOOK_SECRET="dev-secret"
export PROVIDER_C_API_KEY="your-provider_c-api-key"
export PROVIDER_C_BASE_URL="https://api.provider-c.example.invalid/api/v1"
```

## Step 2: Run the callback API

```bash
./EdennCode/Deployment/music_callback_api_deployment/run_local_callback.sh
```

## Step 3: Start the worker

```bash
python -m EdennCode.Deployment.music_callback_api_deployment.worker drain --loop
```

## Step 4: Trigger generation with callBackUrl
Pass the callback URL (note the exact JSON key `callBackUrl`).

```json
{
  "prompt": "your prompt",
  "callBackUrl": "https://<public-host>/provider_c/callback?secret=dev-secret"
}
```

## Step 5: Verify outputs
- `EdennCode/Deployment/music_callback_api_deployment/webhook_logs/` contains raw callback payloads
- `EdennCode/Deployment/music_callback_api_deployment/webhook_queue/` is drained by the worker
- `EdennCode/Deployment/music_callback_api_deployment/downloads/` contains downloaded MP3 files
- `EdennCode/Deployment/music_callback_api_deployment/lyrics_ts/` contains timestamped lyrics JSON (if available)
