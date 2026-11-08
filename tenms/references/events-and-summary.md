# Events and the CallSummary

## Events (Turn and Pulse)

Every integration delivers the same events. In Python they are `tenms.Event` objects with the attributes
`type`, `at_ms`, `probability`, `silence_ms`, `kind`, `snr_db`, `mos` and `asr_risk`. Attributes that do not
apply to an event are `None`. `event.to_dict()` gives the Edge JSON shape.

| `type` | Set fields | Meaning | What to do |
|---|---|---|---|
| `speech_start` | `at_ms` | the caller started speaking | UI, metrics |
| `turn_prediction` | `at_ms`, `probability` | end of turn is likely ("prepare"), ~150-300 ms after the caller stops | optionally start the LLM speculatively; drop the result if the caller continues |
| `turn_end` | `at_ms`, `probability`, `silence_ms` | the turn ended (prosodic), ~350-900 ms after the caller stops, at most `turn.max_silence_ms` | commit the user turn |
| `interruption` | `at_ms`, `kind` | the caller made a sound while the agent spoke | **only `kind == "barge_in"`**: stop TTS and flush playback |
| `quality` | `at_ms`, `snr_db`, `mos`, `asr_risk` | live call quality, about once per second | monitoring, alerts |

Interruption kinds: `barge_in`, `backchannel` ("mm-hmm", "yeah"), `noise`, `other_speaker`. Interruptions are
classified only while the engine knows the agent is speaking. A provisional `backchannel` / `noise` can be
followed by a `barge_in` for the same utterance. There is at most one barge-in per agent utterance.

`at_ms` is stream time: the milliseconds of caller audio processed when the event fired.

### Where events arrive

| Integration | How |
|---|---|
| Pipecat | `TenmsTurnPredictionFrame`, `TenmsInterruptionFrame` and `TenmsEventFrame` pushed downstream by `tenms.processor()`; `@proc.event_handler("on_tenms_event")` gets `(processor, event)` |
| LiveKit | `call.session.add_listener(fn)`, where `fn(session, event)` runs on the audio path (keep it fast) |
| Edge raw | JSON text messages `{"type": "turn_end", "at_ms": ..., ...}` |
| `EdgeClient` | `on_event=fn(event)`, `async for ev in edge.events()` or `edge.events_nowait()` |
| `tenms.Engine` | `res.events` from each `engine.process(...)` |
| ElevenLabs bridge | only with `?tenms_events=1`: `{"type":"tenms_event","tenms_event":{...}}` |

The Pipecat and LiveKit integrations already act on these events: gated VAD, turn strategies and transcript
gating. Handle them yourself only for extra behaviour, such as speculative LLM calls, UI or alerts.

## CallSummary

One JSON object per call, built when the call ends. It holds no audio and no transcripts. The SDKs return a
`dict`.

```json
{
  "call_id": "c1", "started_at_ms": 1793847800519, "duration_ms": 30120,
  "source": "edge", "region": "asia-southeast1-eqsg3a", "modules": ["focus", "turn", "pulse"],
  "focus_profile": "stt",
  "quality": {"mos": 4.16, "snr_db": 30.1, "clipping_ratio": 0, "dropout_ratio": 0,
              "narrowband_ratio": 0, "overlap_ratio": 0.06, "echo_ratio": 0.29,
              "speech_ratio": 0.34, "asr_risk": 0.004},
  "turns": {"user_turns": 4, "barge_ins": 1, "backchannels": 1, "ignored_noise": 0,
            "ignored_other_speaker": 0, "eot_delay_ms_p50": 360, "eot_delay_ms_p95": 610},
  "failure": {"cause": "none", "confidence": 0.6, "reasons": []},
  "processing": {"frames": 3012, "compute_us_p50": 20.9, "compute_us_p99": 102.9,
                 "compute_us_max": 347.6, "algorithmic_delay_ms": 5.0}
}
```

| Field | Notes |
|---|---|
| `call_id` | yours (`call_id=` / `start.call_id`), else a UUID; for the bridge, the ElevenLabs `conversation_id` |
| `started_at_ms`, `duration_ms` | unix ms; processed audio duration |
| `source` | `edge`, `elevenlabs`, `pipecat`, `livekit` or `sdk` |
| `region` | Edge region; `null` in process unless you set `region=` |
| `modules` | modules that ran (requested ∩ licensed) |
| `focus_profile` | the profile in effect; omitted when Focus is off or the settings are custom |
| `quality` | Pulse: `mos`, `snr_db`, `asr_risk` and ratios (`clipping`, `dropout`, `narrowband`, `overlap`, `echo`, `speech`); `null` without Pulse |
| `turns` | Turn: `user_turns`, `barge_ins`, `backchannels`, `ignored_noise`, `ignored_other_speaker`, `eot_delay_ms_p50/p95`; final counts |
| `failure` | `cause` is `none`, `audio`, `transcription` or `llm`, with `confidence` and `reasons` (Pulse failure attribution, which uses final transcripts) |
| `processing` | `frames`, `compute_us_p50/p99/max` per 10 ms frame, `algorithmic_delay_ms` |

`quality`, `turns`, `failure` and `region` are optional (null when the module was off). The Pulse scores are
heuristic and uncalibrated. Use them to compare calls and find bad ones, not as absolute MOS.

### Where the summary arrives

| Integration | How |
|---|---|
| Pipecat | `@proc.event_handler("on_call_summary")` → `(processor, summary)` on `EndFrame` / `CancelFrame` |
| LiveKit | `@call.on_summary` → `summary` when the session closes; `call.finish()` on demand |
| Edge raw | `{"type": "summary", "summary": {...}}` after you send `end` |
| `EdgeClient` | `summary = await edge.end()` |
| `tenms.Engine` | `engine.finish(call_id, started_at_ms=None, source="sdk", region=None)` |
| ElevenLabs bridge | posted to the control plane; with `?tenms_events=1` also sent to the client before the close |

### Dashboard, API and webhooks

* Edge and the bridge post summaries to the control plane themselves. In-process SDKs post only when
  `TENMS_CONTROL_URL=https://app.tenms.cloud` is set. Pass `reporter=False` to `TenmsCall` / `TenmsLiveKit`
  to turn that off.
* Read calls back with `GET https://app.tenms.cloud/v1/calls?limit=5` and `Authorization: Bearer $TENMS_LICENSE`.
  Also available: `GET /v1/calls/{call_id}`, `GET /v1/stats?hours=24` and `GET /v1/usage`. A project key
  reads only its project.
* **Webhook** (project setting): an `https://` URL that receives each `CallSummary` as JSON right after
  ingest. The `X-Tenms-Signature: t=<unix>,v1=<hex>` header is the HMAC-SHA256 of `"<t>.<raw body>"`, keyed
  with the project's `whsec_…` secret. Deliveries are retried (up to 4 attempts) and may repeat, so
  deduplicate on `X-Tenms-Call-Id`.

```python
import hashlib
import hmac
import time


def verify(secret: str, header: str, body: bytes, tolerance_s: int = 300) -> bool:
    parts = dict(p.split("=", 1) for p in header.split(","))
    t, sig = int(parts["t"]), parts["v1"]
    expected = hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    return abs(time.time() - t) <= tolerance_s and hmac.compare_digest(expected, sig)
```
