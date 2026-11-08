# Tenms in LiveKit Agents

`pip install livekit-plugins-tenms` (same code as `pip install "tenms[livekit]"`; needs livekit-agents
1.8.x). Import it as `from livekit.plugins import tenms`, or use `from tenms.livekit import ...`.

## Contents

- [What to change in an existing agent](#what-to-change-in-an-existing-agent)
- [Full example](#full-example)
- [Existing Agent subclasses](#existing-agent-subclasses)
- [Components](#components)
- [Options](#options)
- [Events](#events)

## What to change in an existing agent

One `TenmsLiveKit` per call (create it inside the entrypoint, not at module level). It creates one shared
session with `focus_profile="stt"` and `source="livekit"`. Then:

1. `vad=call.vad()` in `AgentSession(...)`. This replaces `silero.VAD.load()`.
2. `turn_handling={"turn_detection": call.turn_detector(), "interruption": {"mode": "vad", "min_duration": 0.3}}`.
   This replaces `MultilingualModel()` / `EnglishModel()` turn detectors.
3. `agent=tenms.TenmsAgent(call, instructions=...)`, or subclass `TenmsAgent` (below).
4. `room_options=room_io.RoomOptions(audio_input=room_io.AudioInputOptions(noise_cancellation=call.noise_cancellation()))`.
   This replaces `noise_cancellation.BVC()` / Krisp / ai-coustics.
5. `call.attach(session)` **after** `await session.start(...)`. It wires agent state, the echo-reference
   output tap, transcripts and the summary at session close.

## Full example

```python
# pip install livekit-plugins-tenms "livekit-agents[deepgram,openai,elevenlabs]"
from livekit import agents
from livekit.agents import AgentSession, room_io
from livekit.plugins import deepgram, elevenlabs, openai
from livekit.plugins import tenms


async def entrypoint(ctx: agents.JobContext):
    call = tenms.TenmsLiveKit()   # license from TENMS_LICENSE, focus_profile="stt"
    session = AgentSession(
        stt=deepgram.STT(), llm=openai.LLM(model="gpt-4o-mini"), tts=elevenlabs.TTS(),
        vad=call.vad(),
        turn_handling={"turn_detection": call.turn_detector(),
                       "interruption": {"mode": "vad", "min_duration": 0.3}},
    )
    await session.start(
        agent=tenms.TenmsAgent(call, instructions="You are a helpful phone agent."),
        room=ctx.room,
        room_options=room_io.RoomOptions(audio_input=room_io.AudioInputOptions(
            noise_cancellation=call.noise_cancellation())),   # TenmsFocus
    )
    call.attach(session)   # agent speaking state, echo reference, transcripts, summary

    @call.on_summary
    def _(summary):        # CallSummary dict, once, when the session closes
        print(summary["failure"], summary["quality"])


if __name__ == "__main__":
    agents.cli.run_app(agents.WorkerOptions(entrypoint_fnc=entrypoint))
```

## Existing Agent subclasses

LiveKit interrupts on **any** final transcript while the agent speaks, so an "mm-hmm" would interrupt it.
`TenmsAgent` overrides `stt_node` to drop transcripts of backchannels, noise, other speakers and the agent's
own echo. It also overrides `tts_node` to record the agent's text. Change the base class:

```python
from livekit.plugins import tenms


class SupportAgent(tenms.TenmsAgent):           # was: class SupportAgent(Agent)
    def __init__(self, call: tenms.TenmsLiveKit):
        super().__init__(call, instructions="You are a support agent.")
```

If the agent already overrides `stt_node`, wrap its events with `gate_stt_events` instead:

```python
from livekit.agents import Agent
from tenms.livekit import gate_stt_events


class MyAgent(Agent):
    def __init__(self, call, **kwargs):
        super().__init__(**kwargs)
        self.tenms_session = call.session

    async def stt_node(self, audio, model_settings):
        events = Agent.default.stt_node(self, audio, model_settings)
        async for ev in gate_stt_events(self.tenms_session, events):
            yield ev
```

Without `TenmsAgent`'s `tts_node`, call `call.session.note_agent_text(text)` with the agent's text yourself.

## Components

| Factory | Class | Role |
|---|---|---|
| `call.noise_cancellation()` | `TenmsFocus` (`rtc.FrameProcessor[rtc.AudioFrame]`, alias `tenms.Focus`) | Focus on the caller track, through the same interface as the Krisp / ai-coustics plugins. Frames keep their size, rate and channel count. Stereo is averaged to mono and written back to every channel. A rate mismatch is resampled both ways. |
| `call.vad()` | `VAD` (`livekit.agents.vad.VAD`) | Engine VAD, gated while the agent speaks. On a barge-in it reports `START_OF_SPEECH` at once and credits the speech already heard to `interruption.min_duration`. |
| `call.turn_detector()` | `TurnDetector` | Streaming turn detector that resolves on `turn_end`. It is prosodic, so it works in any language. It waits up to 1 s (`max_stt_wait`) for a pending STT final and always answers before LiveKit's `prediction_timeout`. |
| `call.attach(session)` | `TenmsAudioOutput` | Wraps `session.output.audio` and records queued playout as the echo reference. Handles `pause()`, `resume()` and `clear_buffer()`. |
| `TenmsAgent(call, ...)` | `Agent` subclass | Gates `stt_node`, records `tts_node` text. |

If Focus is disabled (`focus.enabled = False`), or the processor was never installed, the `VAD` and
`TurnDetector` feed the engine themselves. VAD, Turn and Pulse keep working.

## Options

`tenms.TenmsLiveKit(**kwargs)` passes keyword arguments to `TenmsSession`. They are the same as for Pipecat:
`license`, `focus_profile` (`"stt"` default), `modules`, `focus`, `turn`, `call_id`, `reporter`,
`reference_delay_ms`, plus `sample_rate`. `TenmsLiveKit(sample_rate=16000)` runs the engine at 16 kHz and
resamples a 24/48 kHz track both ways, which adds a few ms.

`call.vad(...)` takes `activation_threshold=0.5`, `min_speech_duration=0.05`, `min_silence_duration=0.35`
and `gate_during_agent_speech=True`. `call.turn_detector(...)` takes `unlikely_threshold=0.5` and
`prediction_timeout=None`.

## Events

LiveKit has no Tenms frame type. To see engine events, register a listener on the shared session. It is
called from the audio path, so keep it fast and never block:

```python
from livekit.plugins import tenms


def on_tenms_event(session, event):   # (tenms.TenmsSession, tenms.Event)
    if event.type == "quality" and event.asr_risk is not None and event.asr_risk > 0.5:
        print("poor audio at", event.at_ms, "ms")


async def entrypoint(ctx):
    call = tenms.TenmsLiveKit()
    call.session.add_listener(on_tenms_event)
    ...  # AgentSession, session.start(...), call.attach(session) as above
```

The summary goes to `@call.on_summary` callbacks. `call.finish()` builds it on demand; it is idempotent.
