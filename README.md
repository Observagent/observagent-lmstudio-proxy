# LM Studio Bionic telemetry integration

This guide sends logs, metrics, and traces for LM Studio API requests to Observagent through a local proxy. It covers the LM Studio local API used by Bionic and other OpenAI-compatible clients. LM Studio documents Bionic projects and a local model API, but does not document an app-wide OTLP exporter for every Bionic chat, tool action, browser action, or cloud session. The proxy therefore observes requests that are routed through it; it cannot capture activity that stays inside Bionic or uses LM Studio Secure Cloud.

## What this integration captures

The proxy listens on `127.0.0.1:1235`, forwards to the LM Studio API on `127.0.0.1:1234`, and exports three OTLP signals:

- Logs at `/v1/logs`: one structured completion, failure, or incomplete event per observed request.
- Metrics at `/v1/metrics`: request count, request duration, and input/output token counters when usage is present.
- Traces at `/v1/traces`: one span per observed chat, completion, or embeddings request.

It observes `/v1/chat/completions`, `/v1/completions`, and `/v1/embeddings`, plus `/v1/models` for health checks. It preserves streaming bytes and does not include prompts or generated text in telemetry attributes. Token counts are recorded only when the upstream response reports usage. LM Studio model logs can expose exact input and output strings; do not enable or upload those logs as a substitute for this proxy unless your privacy policy permits it.

LM Studio Bionic can use local, remote LM Link, or Secure Cloud models. Route only local API traffic through this proxy if you want local inference telemetry. Cloud sessions and Bionic's internal agent events remain outside this integration.

## 1. Prepare LM Studio and the API server

Install LM Studio Bionic and download a local model. In Bionic, choose a local model for the session. LM Studio's Bionic documentation describes local and remote model choices, while the local API documentation describes the server at port 1234.

Start the local API server from LM Studio's Developer / Local Model API controls, or in a terminal:

```powershell
lms server start
```

Verify it before starting the proxy:

```powershell
Invoke-RestMethod "http://127.0.0.1:1234/v1/models"
```

If you enabled LM Studio API-token authentication, keep the token private. The proxy forwards it only to LM Studio; it sends the separate Observagent project key only to Observagent.

```powershell
$env:LMSTUDIO_API_TOKEN = Read-Host "LM Studio API token (leave blank if authentication is disabled)"
```

The local API server defaults to no authentication in LM Studio, but authentication can be enabled in Developer → Server Settings. Test the upstream server directly before debugging the proxy.

## 2. Get the proxy files and create its environment

Use the `lmstudio-integration` folder in the guides repository, or create a copy of the downloadable files from this page. The required files are `lmstudio_proxy.py`, `requirements.txt`, and `test_lmstudio_proxy.py`.

```powershell
New-Item -ItemType Directory -Force "$HOME\observagent-guides\lmstudio-integration"
Set-Location "$HOME\observagent-guides\lmstudio-integration"
python -m venv .venv
```

Install dependencies from the provided file:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

If you prefer separate commands, keep the final package name on one line:

```powershell
.\.venv\Scripts\python.exe -m pip install fastapi uvicorn httpx
.\.venv\Scripts\python.exe -m pip install opentelemetry-sdk
.\.venv\Scripts\python.exe -m pip install opentelemetry-exporter-otlp-proto-http
```

Activation is optional. If Python or `py` is not found, install Python and reopen PowerShell before creating `.venv`. The integration environment belongs in this folder; it is separate from the Ollama proxy environment.

## 3. Create or select an Observagent project

In Observagent, open Settings / Projects. Create or select a project, enable Logs, Metrics, and Traces, enable monitoring, and choose Connect source → Generate / replace project key. Copy the full key; it must have `telemetry:write` permission.

Generating a key replaces the previous key for that project only. It does not change keys for Ollama, Codex, or other projects. Existing records stay in their original project. If project key creation is unavailable, contact your Observagent administrator or support channel; this is not an LM Studio client setting.

## 4. Configure the proxy

Run these commands in the PowerShell window that will start the proxy. Use the backend base URL without `/v1/logs`, `/v1/metrics`, or `/v1/traces`:

```powershell
Set-Location "$HOME\observagent-guides\lmstudio-integration"
$env:OBSERVAGENT_ENDPOINT = Read-Host "Observagent base URL, such as https://dashboard.observagent.cloud"
$env:OBSERVAGENT_API_KEY = Read-Host "Paste the Observagent project API key"
$env:OBSERVAGENT_SESSION_ID = "lmstudio-bionic-test"
$env:LMSTUDIO_UPSTREAM = "http://127.0.0.1:1234"
```

The proxy constructs these endpoints automatically:

```text
<base>/v1/logs
<base>/v1/metrics
<base>/v1/traces
```

Check the non-secret settings:

```powershell
Write-Host "Observagent endpoint:" $env:OBSERVAGENT_ENDPOINT
Write-Host "LM Studio upstream:" $env:LMSTUDIO_UPSTREAM
Write-Host "Observagent key configured:" (-not [string]::IsNullOrWhiteSpace($env:OBSERVAGENT_API_KEY))
```

Run the offline tests, then start the proxy on port 1235:

```powershell
.\.venv\Scripts\python.exe test_lmstudio_proxy.py
.\.venv\Scripts\python.exe -m uvicorn lmstudio_proxy:app --host 127.0.0.1 --port 1235
```

Leave this window open. The proxy must remain on 1235 so the Bionic or client configuration can use it while LM Studio remains on 1234.

## 5. Route a client through the proxy

The Bionic app does not document a setting that rewrites every internal app request to an arbitrary local proxy. For a supported, verifiable path, use the LM Studio local API from a script, OpenAI-compatible client, or another tool and set its base URL to `http://127.0.0.1:1235/v1`. For example, a PowerShell smoke test is:

```powershell
$body = @{
    model = "YOUR_LM_STUDIO_MODEL_ID"
    messages = @(@{ role = "user"; content = "Reply with one short sentence." })
    stream = $false
} | ConvertTo-Json -Depth 6

Invoke-RestMethod "http://127.0.0.1:1235/v1/chat/completions" -Method Post `
    -ContentType "application/json" -Body $body
```

Replace the model ID with one returned by `http://127.0.0.1:1234/v1/models`. If LM Studio authentication is enabled, the proxy adds `LMSTUDIO_API_TOKEN` when forwarding. Do not send the Observagent key to LM Studio.

For Python clients, use a base URL ending in `/v1`:

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:1235/v1", api_key="local-proxy")
response = client.chat.completions.create(
    model="YOUR_LM_STUDIO_MODEL_ID",
    messages=[{"role": "user", "content": "Reply with one short sentence."}],
)
print(response.choices[0].message.content)
```

The `api_key` value is only required by the OpenAI client library; the local proxy does not use it to authenticate the caller. Do not place the Observagent key in that client configuration.

If a future Bionic release adds a configurable API base URL, point it to `http://127.0.0.1:1235/v1` and keep the same project key in the proxy. Until then, Bionic's built-in chat, tool calls, file operations, and cloud sessions are not guaranteed to pass through this proxy.

## 6. Verify all three signals

After a completed request, the proxy window should show a message such as:

```text
lmstudio.request.completed: input=... output=...; logs, metrics and trace queued
```

Queued means the OpenTelemetry SDK accepted the records for background export. It is not proof of backend storage.

Refresh Observagent and select the project used by the proxy. In Telemetry Data, choose All sources and All available, then inspect Logs and Metrics. Look for `lmstudio.request.completed` and `lmstudio.request.count`. Project history should show a recent `traces` record. Traces are not currently included in the main logs/metrics telemetry query.

For a direct endpoint check with no records created, run this in the proxy window after stopping it with Ctrl+C:

```powershell
$base = $env:OBSERVAGENT_ENDPOINT.TrimEnd('/')
$key = $env:OBSERVAGENT_API_KEY
$headers = @{ Authorization = "Bearer $key" }
foreach ($signal in @('logs', 'metrics', 'traces')) {
    try {
        $result = Invoke-WebRequest -Uri "$base/v1/$signal" -Method Post -Headers $headers `
            -ContentType "application/x-protobuf" -Body ([byte[]]@()) -UseBasicParsing
        Write-Host $signal "HTTP status:" $result.StatusCode
        Write-Host "Response:" $result.Content
    } catch {
        Write-Host $signal $_.Exception.Message
        Write-Host $_.ErrorDetails.Message
    }
}
Remove-Variable key,headers -ErrorAction SilentlyContinue
```

Each endpoint should return HTTP 200 with `status: ok` and `count: 0`. This proves endpoint access and key acceptance, not proxy export. Use the real request to verify storage. A 401 means the key is invalid, missing, expired, or revoked. A 403 means missing `telemetry:write`, paused monitoring, or a disabled signal. A 404 usually means a wrong path. HTML indicates the request reached a website route rather than ingestion.

The downloadable `verify_delivery.py` sends one synthetic log, metric, and trace or an empty batch with `--empty`. It is useful when you need to separate backend storage from LM Studio routing. Synthetic checks create records unless `--empty` is used.

## 7. Privacy and data boundaries

The proxy forwards request and response bytes so clients continue to work, but it records only model, operation, HTTP status, outcome, timing, and usage counts in telemetry. It does not place prompt or completion text in spans, logs, or metric attributes. The LM Studio `lms log stream --source model --filter input,output --json` command is a separate diagnostic stream that can contain exact model input and output; do not pipe it to Observagent unless you intentionally build a redacted exporter.

Local model traffic stays on the machine until the proxy exports metadata to Observagent. Remote LM Link inference is handled by another device. Secure Cloud sessions may process data in LM Studio's hosted service and are outside the local API proxy's capture boundary. Review your workspace, model, and network policies before enabling collection.

## 8. Troubleshooting and lifecycle

- `Set OBSERVAGENT_ENDPOINT ... first`: configure the endpoint and key in the same PowerShell window that starts Uvicorn.
- Endpoint has a duplicate `/v1`: set `OBSERVAGENT_ENDPOINT` to the base domain only.
- Cannot reach LM Studio: start the local API server and verify port 1234. Do not set `LMSTUDIO_UPSTREAM` to port 1235.
- 401 from LM Studio: set `LMSTUDIO_API_TOKEN` if LM Studio API authentication is enabled. That token is separate from the Observagent key.
- Bionic activity is missing: confirm the request actually uses the local API. Built-in Bionic sessions, cloud models, and internal tools do not automatically route through this proxy.
- Logs or traces arrive but metrics do not: confirm Metrics is enabled for the project and allow the periodic metrics reader time to flush. Check `/v1/metrics` independently.
- Token counts are absent: the upstream response did not include a `usage` object. The proxy does not infer token counts from text.
- 502 from the proxy: inspect the proxy window and test `http://127.0.0.1:1234/v1/models` directly.
- PowerShell stays at `>>`: press Ctrl+C. Use the saved scripts instead of pasting multiline Python blocks.

To rotate the key, stop the proxy, set `OBSERVAGENT_API_KEY` to the new key, and restart. To stop collection, press Ctrl+C in the proxy window and clear `OBSERVAGENT_API_KEY` before starting another session. Existing Observagent records are not deleted.

## References and scope

- [LM Studio Bionic](https://lmstudio.ai/docs/bionic): projects, sessions, and local/cloud/remote model choices.
- [LM Studio local API](https://lmstudio.ai/blog/muse-glimmer): local API server and default port 1234.
- [LM Studio API quickstart](https://lmstudio.ai/docs/developer/rest/quickstart): local server, authentication, and API requests.
- [LM Studio log stream](https://lmstudio.ai/docs/cli/serve/log-stream): diagnostic model/server logs and their JSON output.
- [LM Studio authentication](https://lmstudio.ai/docs/developer/core/authentication): optional LM Studio API tokens.

This guide adds an Observagent proxy for the LM Studio local API. It does not claim that Bionic itself emits native OTLP or that a local proxy can see app-internal and cloud-only events.

## License

MIT. See [LICENSE](LICENSE).
