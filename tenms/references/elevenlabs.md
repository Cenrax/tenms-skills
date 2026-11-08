# Tenms with ElevenLabs Agents (Edge bridge)

ElevenLabs Agents (Conversational AI) run a hosted pipeline, so Tenms cannot run in process. Tenms Edge
proxies the conversation instead. The client connects to Edge, and Edge connects to ElevenLabs:

```
browser / device ──(ElevenLabs Agents WebSocket protocol)──► Tenms Edge ──(signed URL)──► ElevenLabs
                 ◄──────────────── relayed verbatim ─────────            ◄────────────
```

On the way through, Edge runs Focus on the caller's audio. It uses the agent's audio as the paced echo
reference, mutes noise and other speakers while the agent talks (Turn), and posts a `CallSummary` with
`source: "elevenlabs"` and `call_id` = the ElevenLabs `conversation_id`. The client changes only its URL.

## Contents

- [Prerequisites](#prerequisites)
- [Endpoint](#endpoint)
- [Backend: mint a session token](#backend-mint-a-session-token)
- [Browser client](#browser-client)
- [Python or other clients](#python-or-other-clients)
- [Errors](#errors)
- [Limitations](#limitations)

## Prerequisites

The bridge must be allowed to reach your agent. Either:

* **Your own ElevenLabs key** (self-serve). In https://app.tenms.cloud open Projects → Settings, save your
  ElevenLabs API key and the agent ids it may reach (or `*`), then create the API key in that project. Edge
  calls ElevenLabs with your key. Without a saved key the bridge answers `403 elevenlabs_key_missing`.
* **Agents granted on the license.** The license lists the agent ids in its `elevenlabs_agents` claim. Ask
  Tenms for this, with explicit agent ids.

A token that reaches neither gets `403 agent_not_allowed`.

## Endpoint

```
wss://edge.tenms.cloud/v1/elevenlabs/stream?agent_id=<agent_id>&token=<tenms session token>
```

| Query parameter | Default | Meaning |
|---|---|---|
| `agent_id` (required) | | ElevenLabs agent id, `[A-Za-z0-9_-]{1,128}` |
| `token` | | Tenms session token (browsers cannot set WebSocket headers). Servers may send `Authorization: Bearer` instead |
| `focus_profile` | the key's project default if it is `stt` / `listening`, else `stt` | **`stt` or `listening` only**. This audio goes to ElevenLabs' recogniser. Anything else, including `listening_mini` / `listening_pro`, is `400 bad_request` |
| `modules` | all licensed | e.g. `focus,turn`; intersected with the license |
| `gate` | `on` | `off` forwards the Focus output without interruption gating |
| `tenms_events` | off | `1` also sends Tenms events to the client (below) |
| `playout_ms` | 40 | client speaker + mic latency used for echo-reference pacing (0-2000) |
| `branch_id`, `environment`, `version_id` | | passed through to ElevenLabs |

Edge does the `get-signed-url` call itself, so private agents (`enable_auth`) work and no ElevenLabs key
reaches the client. Clients that call `get-signed-url` themselves must skip that step.

Caller audio formats processed: `pcm_8000`, `pcm_16000`, `pcm_24000`, `pcm_48000` and `ulaw_8000`.
`pcm_22050` and `pcm_44100` are relayed **untouched**, with no Focus, so pick 16 kHz in the agent's settings.

## Backend: mint a session token

The license stays on the server. Mint one token per conversation, just before connecting, after you
authenticate your own user:

```python
# pip install tenms fastapi uvicorn ; TENMS_LICENSE is set in the server environment
import os

from fastapi import FastAPI, HTTPException
from tenms.control import ControlError, amint_session_token

app = FastAPI()


@app.post("/api/voice-session")
async def voice_session(agent_id: str):
    # authenticate *your* user here before minting
    try:
        token, expires_at = await amint_session_token(os.environ["TENMS_LICENSE"], ttl_secs=300)
    except ControlError as e:   # fatal: bad, revoked or expired license -> do not retry
        raise HTTPException(503 if e.retryable else 403, e.code)
    return {"signed_url": f"wss://edge.tenms.cloud/v1/elevenlabs/stream?agent_id={agent_id}&token={token}",
            "expires_at": expires_at}   # unix seconds
```

In production, check `agent_id` against your own list before you build the URL. `mint_session_token(...)` is
the blocking version for Flask / Django. Both return `(token, expires_at)` and retry network errors, 429 and
5xx. Without the SDK, use plain HTTP:

```
POST https://app.tenms.cloud/v1/session-tokens
Authorization: Bearer <license>
Content-Type: application/json

{"ttl_secs": 300}          (optional; unknown fields → 400)

200 {"token": "tenms1....", "expires_at": <unix s>, "session_id": "...", "parent": "lic_...", "customer_id": "...", "project_id": "..."}
```

## Browser client

Keep `@elevenlabs/client` and use the **WebSocket** transport. WebRTC goes straight to ElevenLabs and
bypasses Tenms.

```js
import { Conversation } from "@elevenlabs/client";

const res = await fetch("/api/voice-session?agent_id=" + agentId, { method: "POST" });
const { signed_url } = await res.json();
const conversation = await Conversation.startSession({
  signedUrl: signed_url,          // the Edge URL, not ElevenLabs' get-signed-url result
  connectionType: "websocket",
});
```

Other ElevenLabs SDKs work the same way: pass the Edge URL as the signed URL and choose the WebSocket
transport. Mint a fresh token for every new conversation. A token must be used to connect within 5 minutes.
A conversation that started in time may run up to 4 h.

## Python or other clients

Any client that speaks the ElevenLabs Agents WebSocket protocol works unchanged. Point its WebSocket URL at
the Edge URL above. Every message is relayed verbatim and in order. Only `user_audio_chunk` is rewritten
(cleaned, same format). Send `conversation_initiation_client_data` first, as with ElevenLabs directly, and
send caller audio in real time.

With `?tenms_events=1` the client also gets Tenms events. ElevenLabs clients ignore unknown types:

```json
{"type":"tenms_event","tenms_event":{"type":"interruption","at_ms":4620,"kind":"noise"}}
{"type":"tenms_event","tenms_event":{"type":"summary","summary":{"call_id":"...","quality":{}}}}
```

## Errors

Refusals before the upgrade are HTTP errors with `{"error": "<code>", "message": "..."}`:

| Status | Codes | Fix |
|---|---|---|
| 400 | `missing_agent_id`, `invalid_agent_id`, `bad_request` (for example `focus_profile=listening_pro`) | fix the URL |
| 401 | `missing_license`, `invalid_license`, `license_expired` | mint a new session token (it lasts 5 minutes) |
| 403 | `agent_not_allowed`, `elevenlabs_key_missing`, `project_not_allowed`, `no_modules`, `license_revoked` | fix the project settings / key; never retry revoked |
| 429 | `rate_limited`, `license_session_limit` | back off |
| 503 | `draining`, `not_ready`, `at_capacity`, `bridge_config_unavailable`, `elevenlabs_not_configured` | back off and retry |

In-call errors arrive as `{"type":"tenms_error","tenms_error_event":{"code","message"}}`. Examples:
`elevenlabs_auth_failed` (bad ElevenLabs key), `elevenlabs_agent_not_found`, `elevenlabs_metadata_timeout`
(send `conversation_initiation_client_data` first), `pending_audio_overflow`, `idle_timeout`,
`license_revoked`, `license_expired`, `max_session_duration` (4 h) and `server_shutdown`. A normal ElevenLabs
close (1000) is relayed as is.

## Limitations

* WebSocket transport only.
* Pacing assumes the client plays agent audio as soon as it arrives (as the SDKs do). A client that buffers
  much longer should pass its latency as `playout_ms`.
* Gating mutes noise or other speakers only after Turn decides. The first 90-160 ms reach ElevenLabs at -6 dB.
* Barge-in decisions take about 1.0-1.3 s from onset, because ElevenLabs transcripts arrive too late to
  confirm sooner. Until the decision, the caller's audio is forwarded at -6 dB, so ElevenLabs hears it.
