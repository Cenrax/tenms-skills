# Focus profiles

A Focus profile picks the Focus tuning. Choose it by **who consumes the processed audio**:

* a speech recogniser (every voice agent's STT) → **`stt`**
* people (recordings, QA review, supervisor listen-in) → `listening`, `listening_mini` or `listening_pro`

The listening profiles suppress more and raised word error rate in Tenms' measurements. **Never put them in
front of STT.**

| Profile | What it does | Added delay (end to end) | CPU | Use for | Available in |
|---|---|---|---|---|---|
| `stt` | DSP: echo cancellation + residual-echo suppression, main-speaker gate, noise suppression with a gentle -10 dB floor | 5 ms at 16 kHz, 7 ms at 24/48 kHz, 8 ms at 8 kHz | ~0.01-0.05 ms per 10 ms frame | audio that goes to a recogniser. Default of Pipecat, LiveKit, Edge `/v1/stream` and the ElevenLabs bridge | everywhere |
| `listening` | the same with full noise suppression (-20 dB floor) | same as `stt` | same as `stt` | audio people hear, no model. Default of bare `tenms.Engine` / `TenmsSession` | everywhere |
| `listening_mini` | `listening` + the Focus head of Tenms' own trained model (0.22 MB, bundled in the wheel) | same as `listening` | +~0.035 ms per frame | audio people hear with music / babble / noise behind it: about 2x the background removal of `listening` (12.1 vs 5.9 dB) at the same delay | Edge, Python `tenms.Engine`, `EdgeClient` |
| `listening_pro` | echo cancellation, then the FastEnhancer-M neural enhancer | 27 ms at 16 kHz, 29 ms at 48 kHz, 30 ms at 8 kHz | ~0.8 ms per frame (~15x a DSP session) | best listening quality, where 27-30 ms is acceptable | **Edge only** |

Measured on Tenms Arena's sets:

* STT, Deepgram nova-3, 110 noisy and echo-heavy calls: corpus WER 0.233 with `stt`, 0.250 without Focus and
  0.255 with `listening`.
* Listening, 48 calls with music, babble or stationary backgrounds:

| | `listening` | `listening_mini` | `listening_pro` |
|---|---|---|---|
| background removed in speech pauses | 5.9 dB | 12.1 dB | 37.6 dB |
| UTMOS | 1.50 | 1.55 | 2.12 |
| latency at 8 / 16 kHz | 8 / 5 ms | 8 / 5 ms | 30 / 27 ms |

## How to set it

| Integration | Setting |
|---|---|
| Pipecat | `TenmsCall(focus_profile="stt")` (the default) |
| LiveKit | `tenms.TenmsLiveKit(focus_profile="stt")` (the default) |
| Python engine | `tenms.Engine(16000, focus_profile="stt")`; the default here is `"listening"` |
| Edge client | `EdgeClient(token=..., focus_profile="listening_pro")` |
| Edge raw | `{"type": "start", "sample_rate": 16000, "focus_profile": "listening_mini"}` |
| ElevenLabs bridge | `...&focus_profile=stt` or `listening` only; anything else is `400 bad_request` |
| Project default | app.tenms.cloud → Projects → Settings → default Focus profile. New keys carry it, and Edge applies it when `start` names no profile. An explicit profile always wins. |

Confirm what ran: `ready.focus_profile` on Edge, `engine.focus_profile` in process, and `focus_profile` plus
`processing.algorithmic_delay_ms` in the `CallSummary`.

`focus={...}` options apply on top of the profile, for example
`focus_profile="stt", focus={"speaker_lock": False}` when several people share one microphone on purpose.

## Edge availability errors

* `profile_unavailable`: this Edge replica has not loaded that profile's model. It is classed as fatal: do
  not retry the same request. Use another profile.
* `profile_capacity`: Edge caps concurrent `listening_pro` sessions per replica, and this one is full. It is
  retryable: back off, or fall back to `listening_mini`.
* An unknown profile name in `start` is `bad_message`, and the connection closes with 1008. `EdgeClient`
  rejects unknown names locally with `ValueError`.

## Patterns

* **Agent + recording.** Run the agent on `stt`, the default. To record a cleaner copy for humans, open a
  second Edge session with `listening_mini` / `listening_pro` on the same mic audio and reference. Never
  feed that output back into STT. Each session counts against the key's `max_sessions`.
* **Telephony at 8 kHz.** `stt` adds 8 ms. `listening_pro` adds 30 ms.
