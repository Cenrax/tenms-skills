# Troubleshooting

## Contents

- [Install problems](#install-problems)
- [License errors in process](#license-errors-in-process)
- [Focus profile errors](#focus-profile-errors)
- [Sample rates](#sample-rates)
- [Edge error codes](#edge-error-codes)
- [Control-plane errors](#control-plane-errors)
- [Symptoms](#symptoms)

## Install problems

### Intel Mac cannot install `tenms[pipecat]`

`pip install "tenms[pipecat]"` fails to resolve on macOS x86_64. The cause is Pipecat, not Tenms:
`tenms[pipecat]` needs `pipecat-ai>=1.15,<1.16`, pipecat-ai 1.15 requires `onnxruntime>=1.24.3`, and those
onnxruntime releases ship no macOS x86_64 wheels. Options:

* develop in a Linux container (for example `python:3.12-slim` in Docker), on Linux, or on an Apple Silicon Mac;
* on the Intel Mac itself, `pip install tenms`, `"tenms[edge]"` and `livekit-plugins-tenms` still install.

### Other install issues

* `tenms` needs CPython 3.10 or newer. Wheels exist for Linux x86_64 / aarch64 (manylinux_2_28, glibc 2.28 or
  newer), macOS arm64 / x86_64 and Windows x86_64. Alpine (musl) and older glibc have no wheel.
* `ImportError: tenms.edge needs the "edge" extra` → `pip install "tenms[edge]"` (it adds `websockets`).
* `ImportError: tenms.pipecat requires pipecat-ai` → `pip install "tenms[pipecat]"`.
* Version conflicts: `tenms[pipecat]` pins pipecat-ai 1.15.x and `tenms[livekit]` / `livekit-plugins-tenms`
  pin livekit-agents 1.8.x. A project on another major or minor version must move to these versions.

## License errors in process

| Message / exception | Cause | Fix |
|---|---|---|
| `tenms.LicenseError: tenms: a license is required. Pass license=... or set TENMS_LICENSE; ...` | no key in the process that creates the engine (`Engine`, `TenmsCall`, `TenmsLiveKit`) | set `TENMS_LICENSE` in that process's environment (check `.env` loading order, Docker / systemd / k8s env), or pass `license=` |
| `LicenseError` ... `license rejected by the vendor keys built into tenms: malformed license token` | truncated or quoted key, wrong variable, stray whitespace / newline | copy the full `tenms1.…` key again; strip quotes; run `scripts/check_license.py` |
| `LicenseError` ... expired | the key's `exp` passed (trial keys last up to 30 days) | create a new key at app.tenms.cloud |
| Engine runs but Focus / Turn / Pulse is missing | the engine runs requested ∩ licensed modules | check `engine.modules`; `scripts/check_license.py` lists the licensed modules |

`tenms.LicenseError` is a subclass of `tenms.TenmsError` (and of `ValueError`). Release wheels from PyPI have
no unlicensed mode: `tenms.DEV_UNLICENSED` is `False`.

## Focus profile errors

| Where | Error | Fix |
|---|---|---|
| `tenms.Engine(focus_profile="listening_pro")` | `ValueError` | `listening_pro` exists only on Edge: use `EdgeClient(..., focus_profile="listening_pro")` |
| `EdgeClient(focus_profile="...")` with an unknown name | `ValueError: focus_profile must be one of ...` | use `stt`, `listening`, `listening_mini` or `listening_pro` |
| Edge `start` with an unknown `focus_profile` (or any invalid `start` JSON) | `error` `bad_message`, close 1008 `bad_start` | fix the JSON; profile names are lower-case with underscores |
| Edge `start` with `listening_mini` / `listening_pro` | `error` `profile_unavailable`, close 1008 | this replica has not loaded the model: use another profile (fatal: do not retry the same request) |
| Edge `start` with `listening_pro` | `error` `profile_capacity`, close 1008 | the replica's `listening_pro` sessions are full: retry with backoff, or fall back to `listening_mini` |
| ElevenLabs bridge `?focus_profile=listening_mini` / `listening_pro` / unknown | HTTP `400 bad_request` | the bridge takes `stt` or `listening` only |

WER got worse after adding Tenms? Check that the STT input runs `stt` and not a listening profile. Bare
`tenms.Engine` defaults to `listening`. Look at `ready.focus_profile` or the summary's `focus_profile`.

## Sample rates

* Supported: 8000, 16000, 24000 and 48000 Hz, mono PCM16 (`tenms.SUPPORTED_SAMPLE_RATES`).
* Edge `start` with another rate: `error` `unsupported_sample_rate`, close 1008. Resample first, or change the
  rate.
* A `TenmsSession` keeps its first rate: `ValueError: tenms session runs at 16000 Hz; got audio at 24000 Hz`.
  Use one rate per call. In LiveKit, `TenmsLiveKit(sample_rate=16000)` resamples a track whose rate differs.
* Pipecat: set `audio_in_sample_rate` on the transport params to one of the supported rates.
* ElevenLabs bridge: `pcm_22050` / `pcm_44100` caller audio is relayed untouched, with no Focus. Use
  `pcm_16000` (or 8/24/48 kHz, `ulaw_8000`).
* Edge binary audio with an odd byte count, empty, or with an unknown tag: `error` `bad_audio` (the session
  continues).

## Edge error codes

From `EdgeError.code` (Python), HTTP upgrade bodies `{"error": "<code>", "message": "..."}`, `error` messages
and close reasons.

| Code | Where | Class | Fix |
|---|---|---|---|
| `missing_license` | 401 upgrade | fatal | send `Authorization: Bearer` or `?token=` |
| `invalid_license` | 401 upgrade | fatal, new token | malformed or forged token: check the key |
| `license_expired` | 401 upgrade / in call | fatal, new token | mint a new session token (5-minute window) or renew the key |
| `license_revoked` | 403 upgrade / in call | fatal | the key (or a session token's parent) was revoked: create a new key |
| `agent_not_allowed`, `elevenlabs_key_missing`, `project_not_allowed` | 403 (bridge) | fatal | see [elevenlabs.md](elevenlabs.md#prerequisites) |
| `no_modules` | 403 / start | fatal | request modules the key has |
| `rate_limited` | 429 | retryable | more than 10 connects/s (burst 20) per key: back off |
| `license_session_limit` | 429 | retryable after a session ends | concurrent sessions over `max_sessions` (else 100) per replica: close idle sessions |
| `at_capacity`, `draining`, `not_ready` | 503 | retryable | back off and reconnect (`EdgeClient(connect_retries=3)`) |
| `start_timeout`, `expected_start` | in call | | send the JSON `start` first, within 10 s |
| `bad_message` | in call | 1008 at start, else continues | invalid JSON or unknown field values |
| `unsupported_sample_rate`, `invalid_call_id` | start | fatal | fix the `start` message |
| `profile_unavailable` / `profile_capacity` | start | fatal / retryable | see [Focus profile errors](#focus-profile-errors) |
| `message_too_large` | in call | continues | text messages must be 16 KiB or less |
| `idle_timeout` | in call | summary, then close 1008 | no audio for 30 s |
| `max_session_duration` | in call | summary, then close 1008 | 4 h reached |
| `server_shutdown` | in call | retryable | Edge is draining: reconnect |

`EdgeError` has `.fatal`, `.retryable` and `.needs_new_token`. After a fatal error the client refuses
further sends.

## Control-plane errors

`https://app.tenms.cloud` answers `{"error": {"code": "...", "message": "..."}}`:

* `401 unauthorized` with `missing bearer token`, `invalid token`, `license expired` or `license revoked`;
* `403 forbidden` when a session token tries to mint a session token, or for another customer's data;
* `400 bad_request` for `ttl_secs: 0` or unknown fields in `POST /v1/session-tokens`;
* `429 rate_limited` and `503 unavailable` (honour `Retry-After`).

## Symptoms

| Symptom | Fix |
|---|---|
| The agent's own voice still reaches STT, or it interrupts itself | feed the echo reference as audio plays: Pipecat `reference_filter()`, LiveKit `call.attach(session)` after `session.start`, Edge `0x02` frames. Do not feed queued audio that was later cancelled |
| Every "mm-hmm" interrupts the agent | act only on `barge_in`; tell the engine when the agent speaks; in LiveKit use `TenmsAgent` (or `gate_stt_events`) so backchannel transcripts are dropped; in Pipecat use `tenms.processor()` and `tenms.user_turn_strategies()` |
| A real barge-in takes about 1 s | expected without transcripts (one continuous burst needs 800 ms of voicing). Feed interim transcripts (`push_transcript(..., is_final=False)`): an intent word such as "stop" or "wait" confirms at once |
| Turns end too early on digit strings | end of turn is prosodic only. Raise `turn={"max_silence_ms": ...}` / `min_silence_ms`, or hold the turn in your own logic |
| Edge disconnects after about 10 s | read the socket continuously |
| Streaming a file stalls | send in real time (1.5x after a 5 s burst) |
| No calls in the dashboard | in-process SDKs post only when `TENMS_CONTROL_URL=https://app.tenms.cloud` is set and `reporter` is not `False` |
| `EdgeClient` hangs or times out on connect | check `TENMS_EDGE_URL`; the default is `wss://edge.tenms.cloud` (outbound 443) |
