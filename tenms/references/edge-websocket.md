# Tenms Edge WebSocket (`/v1/stream`) and the in-process engine

Use Edge from any language or stack without a Tenms framework integration: custom media servers, telephony
gateways, Node / Go / Rust services. One WebSocket per call. PCM16 goes in; cleaned audio and JSON events come
back. From Python, `tenms.edge.EdgeClient` implements the same protocol.

## Contents

- [Connect](#connect)
- [Messages](#messages)
- [Rules](#rules)
- [Raw example (Python websockets)](#raw-example-python-websockets)
- [EdgeClient (Python)](#edgeclient-python)
- [Errors and close codes](#errors-and-close-codes)
- [In-process Python engine](#in-process-python-engine)

## Connect

```
GET wss://edge.tenms.cloud/v1/stream
Authorization: Bearer <license or session token>      (servers)
wss://edge.tenms.cloud/v1/stream?token=<session token>  (browsers / devices: they cannot set headers)
```

Only 5-minute session tokens belong in URLs, because URLs end up in logs. See [licensing.md](licensing.md).

## Messages

**Binary** messages carry one tag byte, then PCM16 little-endian mono at the negotiated rate (any length,
even byte count, at most 256 KiB):

| Direction | Tag | Payload |
|---|---|---|
| client → Edge | `0x01` | caller (mic) audio |
| client → Edge | `0x02` | the audio the agent is playing (echo reference), same format |
| Edge → client | `0x01` | processed caller audio (Focus output), one message per 10 ms frame, when Focus is on and `return_audio` is not false |

**Text** messages are JSON, at most 16 KiB. Client → Edge:

| `type` | Fields | Notes |
|---|---|---|
| `start` | `sample_rate` (8000, 16000 default, 24000, 48000), `call_id?` (`[A-Za-z0-9._:-]{1,128}`), `modules?` (subset of `["focus","turn","pulse"]`), `return_audio?` (default true with Focus), `focus_profile?` (`"stt"` default, `"listening"`, `"listening_mini"`, `"listening_pro"`) | must be the first message, within 10 s. Without `focus_profile`, the key's project default applies |
| `agent_speaking` | `speaking: bool` | each false → true starts a new agent utterance and re-arms one barge-in |
| `agent_text` | `text` | the agent's TTS text; its own words heard back as echo never confirm a barge-in |
| `transcript` | `text`, `confidence?`, `is_final` (**default false**) | finals feed Pulse; interim and final transcripts during agent speech confirm or downgrade interruptions |
| `end` | | Edge sends `summary`, then closes 1000 |

Edge → client:

| `type` | Fields |
|---|---|
| `ready` | `session_id`, `call_id`, `modules`, `sample_rate`, `frame_ms` (10), `region`, `focus_profile` (only when Focus is on) |
| `speech_start` | `at_ms` |
| `turn_prediction` | `at_ms`, `probability` |
| `turn_end` | `at_ms`, `probability`, `silence_ms` |
| `interruption` | `at_ms`, `kind`: `barge_in` \| `backchannel` \| `noise` \| `other_speaker` |
| `quality` | `at_ms`, `snr_db`, `mos`, `asr_risk` (each second) |
| `summary` | `summary`: the `CallSummary` ([events-and-summary.md](events-and-summary.md)) |
| `error` | `code`, `message` |

`at_ms` is stream time at the end of the frame that raised the event. Check `ready.focus_profile` to confirm
the profile you asked for.

## Rules

* **Send audio in real time.** Edge allows 1.5x real time after a 5 s burst. Beyond that it stops reading the
  socket until you are back within budget. There is no error, but the audio stalls.
* **Reference before or with the matching mic audio**, at most 1 s ahead (the newest overflow is dropped).
  Send only what actually plays. Do not send audio that was cancelled by an interruption.
* **Read continuously.** A client that does not read for 10 s is disconnected.
* **No audio for 30 s** ends the session (`idle_timeout`). The maximum session length is 4 h.
* **Act only on `barge_in`** and send `agent_speaking` at each agent utterance.
* Set `is_final: true` on final transcripts.

## Raw example (Python websockets)

```python
import asyncio
import json

import websockets

EDGE = "wss://edge.tenms.cloud/v1/stream"


async def call(token, mic_frames, ref_frames, on_audio, on_event):
    async with websockets.connect(EDGE, additional_headers={"Authorization": f"Bearer {token}"}) as ws:
        await ws.send(json.dumps({"type": "start", "sample_rate": 16000,
                                  "call_id": "call-123", "focus_profile": "stt"}))
        ready = json.loads(await ws.recv())   # {"type": "ready", ..., "focus_profile": "stt"}
        if ready["type"] == "error":
            raise RuntimeError(ready["code"])

        async def read():                     # keep reading: Edge drops a client that stops for 10 s
            async for msg in ws:
                if isinstance(msg, bytes):
                    on_audio(msg[1:])         # 0x01 + cleaned caller audio (10 ms)
                    continue
                ev = json.loads(msg)
                if ev["type"] == "summary":
                    return ev["summary"]
                on_event(ev)                  # speech_start, turn_end, interruption, quality, error

        reader = asyncio.create_task(read())
        await ws.send(json.dumps({"type": "agent_speaking", "speaking": True}))
        for mic, ref in zip(mic_frames, ref_frames):  # PCM16 LE mono, 16 kHz
            if ref:
                await ws.send(b"\x02" + ref)           # agent playback first, as played
            await ws.send(b"\x01" + mic)               # caller audio
            await asyncio.sleep(len(mic) / 32000)      # real time: 2 bytes x 16000 per second
        await ws.send(json.dumps({"type": "transcript", "text": "hold on", "is_final": True}))
        await ws.send(json.dumps({"type": "end"}))
        return await reader
```

## EdgeClient (Python)

`pip install "tenms[edge]"`.

```python
from tenms.edge import EdgeClient, EdgeError


async def run(token, mic_chunk, agent_chunk):
    try:
        async with EdgeClient(token=token, sample_rate=16000, focus_profile="stt",
                              on_event=print, connect_retries=3) as edge:   # url: $TENMS_EDGE_URL or wss://edge.tenms.cloud
            print(edge.ready["focus_profile"])
            await edge.set_agent_speaking(True)
            await edge.send_reference(agent_chunk)      # optional, aligned with the mic stream
            await edge.send_audio(mic_chunk)            # int16 bytes or numpy int16/float32
            await edge.push_transcript("hello", confidence=0.9, is_final=True)
            cleaned = edge.audio_nowait()               # processed audio received so far (bytes)
            events = edge.events_nowait()               # list of tenms.Event
            summary = await edge.end()                  # CallSummary dict
    except EdgeError as e:
        if e.needs_new_token:
            ...   # mint a new session token, then reconnect
        elif e.retryable:
            ...   # back off (e.retry_after) and reconnect
        else:
            raise  # fatal: fix credentials or configuration
```

* `EdgeClient(url=None, token=None, *, sample_rate=16000, modules=None, call_id=None, return_audio=None,
  focus_profile=None, on_event=None, connect_timeout=10.0, max_queue=1000, connect_retries=0,
  retry_backoff=0.5)`. `token` defaults to `$TENMS_LICENSE`. `/v1/stream` is appended to a bare host.
* `focus_profile` must be one of `tenms.edge.FOCUS_PROFILES`, otherwise it raises `ValueError` before
  connecting.
* Streams: `async for ev in edge.events()` and `async for chunk in edge.audio()`. Non-blocking:
  `events_nowait()` and `audio_nowait()`.
* `push_transcript(..., is_final=False)` is the default, as on the wire.

## Errors and close codes

`EdgeError` has `.code`, `.message`, `.status`, `.close_code`, `.retry_after`, `.fatal`, `.retryable` and
`.needs_new_token`. The classification lives in `tenms.control` (`is_fatal`, `is_retryable`). See
[troubleshooting.md](troubleshooting.md#edge-error-codes) for every code.

Close codes: 1000 `end` (normal, after `summary`), 1001 `server_shutdown`, 1008 for `start_timeout`,
`expected_start`, `bad_start`, `engine_error`, `idle_timeout`, `license_revoked`, `license_expired` and
`max_session_duration`.

## In-process Python engine

For a custom Python audio loop, run the engine in process. No network is involved:

```python
import tenms

engine = tenms.Engine(16000, modules=["focus", "turn", "pulse"], focus_profile="stt")  # license: $TENMS_LICENSE


def on_audio(mic_chunk, agent_playback_chunk):
    out, res = engine.process(mic_chunk, reference=agent_playback_chunk)  # any length; bytes or numpy
    for ev in res.events:                                  # tenms.Event
        if ev.type == "turn_prediction":
            ...                                            # start the LLM speculatively
        elif ev.type == "turn_end":
            ...                                            # commit the user turn
        elif ev.type == "interruption" and ev.kind == "barge_in":
            ...                                            # stop TTS
    return out                                             # same type and length as mic_chunk


engine.set_agent_speaking(True)                            # on each agent utterance
engine.push_agent_text("Your order ships tomorrow.")
kind = engine.push_transcript("wait", confidence=0.9, is_final=False)  # "barge_in" / "backchannel" / None
summary = engine.finish("call-123", source="sdk")           # CallSummary dict
```

* Input: `bytes` (int16 LE PCM) or numpy `int16` / `float32`. `reference` must match `mic` in type and length.
  The output has the same type and length as the input.
* **`tenms.Engine` defaults to `focus_profile="listening"`.** Pass `"stt"` when the output feeds a recogniser.
  `"listening_pro"` raises `ValueError` here. It needs Edge.
* Delay: `engine.total_delay_ms` is 5 ms at 16 kHz with Focus (0 without Focus), plus less than 10 ms once if
  chunks are not whole 10 ms frames. `engine.flush()` returns held-back samples.
* `FrameResult` fields: `frames`, `at_ms`, `vad`, `vad_max`, `frame_vad`, `eot_probability`,
  `speaker_match`, `events`, `compute_us`.
* Options: `focus={...}` (`noise_suppression`, `echo_cancellation`, `speaker_lock`, `min_gain_db`,
  `echo_tail_ms`, `reference_delay_ms`, `auto_reference_delay`, ...) and `turn={...}` (`max_silence_ms`,
  `confirm_threshold`, `barge_in_ms`, `transcript_confirm_words`, ...), applied on top of the profile.
* To post the summary to the dashboard, use
  `tenms.ControlPlaneReporter(tenms.endpoints.control_url()).submit(summary)`. It is batched, runs in the
  background and never blocks. The token comes from `TENMS_LICENSE`. `ControlPlaneReporter()` with no URL
  is disabled unless `TENMS_CONTROL_URL` is set.
