---
name: tenms
description: Add Tenms (CPU-only voice-agent audio layer - Focus noise/echo/speaker isolation, Turn turn-taking and barge-in, Pulse call quality) to a Pipecat, LiveKit Agents, ElevenLabs Agents or raw WebSocket voice pipeline, choose a Focus profile, mint session tokens, or debug Tenms errors.
license: MIT (this skill's text and scripts). Tenms itself is a proprietary product that needs a license key.
---

# Tenms integration

Tenms is an audio layer for voice agents. It works in 10 ms frames on CPU and has three modules:

| Module | What it gives the agent |
|---|---|
| **Focus** | caller audio with the agent's own echo removed, noise suppressed, other voices attenuated |
| **Turn** | VAD, `turn_prediction` (start the LLM early), `turn_end`, and interruption kinds `barge_in` / `backchannel` / `noise` / `other_speaker` |
| **Pulse** | live `quality` events and a `CallSummary` per call (MOS, SNR, echo, ASR risk, failure cause) |

Public docs: https://www.tenms.cloud/docs/ (one-file version: https://www.tenms.cloud/llms-full.txt).
Dashboard and keys: https://app.tenms.cloud. Edge (hosted WebSocket service): `wss://edge.tenms.cloud`.

Only use the API names in this skill and its references. They were checked against the published
packages `tenms==0.1.1` and `livekit-plugins-tenms==0.1.1`. Do not invent parameters.

## Workflow

Copy this checklist and tick it off:

```
- [ ] 1. Identify the stack and pick the integration
- [ ] 2. Install the package (check Python and platform)
- [ ] 3. Wire credentials: TENMS_LICENSE on the server, session tokens for clients
- [ ] 4. Choose the Focus profile (stt unless humans listen to the output)
- [ ] 5. Wire it in: echo reference, agent-speaking state, act only on barge_in
- [ ] 6. Handle events and the CallSummary
- [ ] 7. Verify: check_license.py, edge_smoke.py, one real call, dashboard
```

### 1. Pick the integration

Look at the project's imports and dependencies first.

| The project uses | Integrate with | Read |
|---|---|---|
| `pipecat` (`pipecat-ai`) | `tenms.pipecat.TenmsCall`, in process | [references/pipecat.md](references/pipecat.md) |
| `livekit.agents` (`livekit-agents` 1.8) | `livekit.plugins.tenms` (same as `tenms.livekit`), in process | [references/livekit.md](references/livekit.md) |
| ElevenLabs Agents / Conversational AI (`@elevenlabs/client`, `elevenlabs` SDK) | the Edge ElevenLabs bridge `wss://edge.tenms.cloud/v1/elevenlabs/stream` | [references/elevenlabs.md](references/elevenlabs.md) |
| Anything else (custom WebSocket/RTP/telephony server, Node, Go, ...) | Edge `/v1/stream` over WebSocket, or `tenms.edge.EdgeClient` from Python | [references/edge-websocket.md](references/edge-websocket.md) |
| A custom Python audio loop | `tenms.Engine`, in process | [references/edge-websocket.md](references/edge-websocket.md#in-process-python-engine) |

In-process integrations (Pipecat, LiveKit, `tenms.Engine`) keep the audio in your process. Edge and the
ElevenLabs bridge stream audio to Tenms Edge. The `listening_pro` profile exists only on Edge.

### 2. Install

```bash
pip install "tenms[pipecat]"         # Pipecat (pipecat-ai 1.15.x)
pip install livekit-plugins-tenms    # LiveKit Agents (livekit-agents 1.8.x); same as "tenms[livekit]"
pip install "tenms[edge]"            # Edge WebSocket client (tenms.edge)
pip install tenms                    # engine, session tokens (tenms.control), telemetry
```

* CPython 3.10 or newer. Wheels: Linux x86_64 / aarch64 (glibc 2.28+), macOS arm64 / x86_64, Windows x86_64.
* The ElevenLabs bridge needs no Tenms package in the audio path. The backend that mints session tokens
  uses `tenms` (or a plain HTTP POST).
* Intel Macs cannot install `tenms[pipecat]`: see [troubleshooting](references/troubleshooting.md#intel-mac-cannot-install-tenmspipecat).

### 3. Credentials

* Create a key at https://app.tenms.cloud: Projects → API keys. It is a license token `tenms1.…`, shown once.
  Calls made with it land in that project.
* Servers read it from `TENMS_LICENSE` (every component defaults to that variable). Put it in the
  project's secret store or `.env`. Never commit it.
* **Never ship the license to a browser, mobile app or device.** The backend mints a 5-minute session token
  per conversation (`tenms.control.mint_session_token` / `amint_session_token`, or
  `POST https://app.tenms.cloud/v1/session-tokens`) and hands only that to the client.
* In-process SDK calls appear in the dashboard only when `TENMS_CONTROL_URL=https://app.tenms.cloud` is set.
  Edge and the bridge post summaries themselves.

Details: [references/licensing.md](references/licensing.md).

### 4. Choose the Focus profile

| Profile | Use when the processed audio... | Added delay | Where |
|---|---|---|---|
| `stt` | goes to a speech recogniser (every voice agent). **Default** of Pipecat, LiveKit, Edge and the bridge | 5 ms at 16 kHz (7 ms at 24/48 kHz, 8 ms at 8 kHz) | everywhere |
| `listening` | is heard by people; full noise suppression, no model. Default of bare `tenms.Engine` | same as `stt` | everywhere |
| `listening_mini` | is heard by people and has music / babble behind it (recordings, QA, listen-in) | same as `stt` | Edge, Python `Engine`, `EdgeClient` |
| `listening_pro` | is heard by people and 27-30 ms is acceptable; best listening quality | 27-30 ms | Edge only (`EdgeClient` or the `start` message) |

Rule: **if the output feeds STT, use `stt`.** The listening profiles raised word error rate in Tenms'
measurements. Never put `listening_mini` / `listening_pro` in front of a recogniser. The ElevenLabs bridge
accepts only `stt` and `listening`. Bare `tenms.Engine(...)` defaults to `listening`, so pass
`focus_profile="stt"` when its output goes to STT. More: [references/profiles.md](references/profiles.md).

### 5. Wire it in: three things every integration must get right

1. **Echo reference.** Focus removes the agent's echo only if it gets the audio the agent plays, as it is
   played. The engine finds the delay itself (0-1000 ms), but audio that was queued and then cancelled must
   not be fed. Pipecat: `tenms.reference_filter()` as `audio_out_filter`. LiveKit: `call.attach(session)`.
   Edge: `0x02` frames, sent before or with the matching mic audio.
2. **Agent-speaking state.** Interruptions are classified only while the engine knows the agent is
   speaking. Pipecat and LiveKit do this for you. Edge and `Engine` need
   `agent_speaking` / `set_agent_speaking(True)` on each new agent utterance.
3. **Interruption policy.** Stop TTS only on `interruption` with `kind == "barge_in"`. Ignore
   `backchannel`, `noise` and `other_speaker`. A provisional `backchannel` / `noise` may later be replaced by
   a `barge_in` for the same utterance.

Optional but worth it: send caller transcripts (`push_transcript`, `is_final=True` for finals) and the agent's
TTS text (`push_agent_text` / Edge `agent_text`). They confirm barge-ins faster and stop the agent's own
echo from counting as speech. Pipecat (`tenms.observer()`, `tenms.processor()`) and LiveKit (`TenmsAgent`,
`attach`) already do this.

Keep the framework's own STT, LLM and TTS. Tenms fills slots the framework already has: audio filters, VAD,
turn detection and noise cancellation. **Remove other noise cancellers and VADs in the same slot**, such as a
Silero VAD passed as `vad=` or a Krisp filter. Two components in one slot fight each other.

### 6. Events and the summary

| Event | Fields | Typical action |
|---|---|---|
| `speech_start` | `at_ms` | UI / metrics |
| `turn_prediction` | `at_ms`, `probability` | optionally start the LLM speculatively |
| `turn_end` | `at_ms`, `probability`, `silence_ms` | commit the user turn |
| `interruption` | `at_ms`, `kind` | `barge_in` → stop TTS, flush playback; ignore other kinds |
| `quality` | `at_ms`, `snr_db`, `mos`, `asr_risk` (about once per second) | live call-quality monitoring |

The `CallSummary` (a dict / JSON) arrives at the end of the call: Pipecat `on_call_summary`, LiveKit
`@call.on_summary`, Edge `summary` message, `Engine.finish()`. Fields and per-integration delivery:
[references/events-and-summary.md](references/events-and-summary.md).

### 7. Verify

Run these from the skill directory:

```bash
python scripts/check_license.py                  # reads TENMS_LICENSE; prints modules + expiry, never the key
python scripts/check_license.py --online         # also asks the control plane (GET /v1/whoami)
python scripts/edge_smoke.py --profile stt       # 5 s synthetic call on wss://edge.tenms.cloud
```

Then make one real call and check that:

* the agent no longer hears itself and does not stop for "mm-hmm";
* a real "wait, stop" interrupts it;
* the call shows up at https://app.tenms.cloud (with `TENMS_CONTROL_URL` set for in-process SDKs).

The SDK logger names are `tenms`, `tenms.pipecat`, `tenms.livekit` and `tenms.edge`. Set them to DEBUG to see
dropped transcripts and decisions.

## Gotchas

* `LicenseError: tenms: a license is required` → `TENMS_LICENSE` is not set in the process that builds the
  engine. Release wheels have no unlicensed mode.
* Sample rates are 8000, 16000, 24000 and 48000 Hz only (mono PCM16). One session keeps the rate it started
  with.
* `listening_pro` with `tenms.Engine(...)` raises `ValueError`. Use Edge (`EdgeClient(...,
  focus_profile="listening_pro")`).
* Edge: send audio in real time (up to 1.5x after a 5 s burst), keep reading the socket (a client that stops
  reading for 10 s is dropped), and send `start` within 10 s.
* `transcript.is_final` defaults to `false` on the wire and in `EdgeClient.push_transcript`. Pulse counts
  finals only.
* ElevenLabs: WebSocket transport only (`connectionType: "websocket"`). WebRTC bypasses Edge.

Error codes and fixes: [references/troubleshooting.md](references/troubleshooting.md).

## Reference files

* [references/pipecat.md](references/pipecat.md): full Pipecat bot, components, options
* [references/livekit.md](references/livekit.md): LiveKit Agents entrypoint, custom agents, options
* [references/elevenlabs.md](references/elevenlabs.md): bridge URL, backend token endpoint, browser client, errors
* [references/edge-websocket.md](references/edge-websocket.md): raw protocol (start / ready / audio / events / summary), `EdgeClient`, `tenms.Engine`
* [references/profiles.md](references/profiles.md): Focus profiles: delay, CPU, quality, when to use
* [references/events-and-summary.md](references/events-and-summary.md): Turn / Pulse events, `CallSummary` fields, webhooks
* [references/licensing.md](references/licensing.md): keys, `TENMS_LICENSE`, session tokens, limits
* [references/troubleshooting.md](references/troubleshooting.md): error codes, install problems, symptoms
