# Tenms in Pipecat

`pip install "tenms[pipecat]"` (needs pipecat-ai 1.15.x; `tenms` pins `pipecat-ai>=1.15,<1.16`).
On Intel Macs this install fails: see [troubleshooting](troubleshooting.md#intel-mac-cannot-install-tenmspipecat).

## Contents

- [What to change in an existing bot](#what-to-change-in-an-existing-bot)
- [Full example (Daily)](#full-example-daily)
- [Components](#components)
- [Options](#options)
- [Events inside the pipeline](#events-inside-the-pipeline)
- [Other transports](#other-transports)

## What to change in an existing bot

One `TenmsCall` per call. It creates one shared `TenmsSession` (with `focus_profile="stt"` and
`source="pipecat"`) and hands out the components:

1. `audio_in_filter=tenms.focus_filter()` on the transport params. This is Focus, and it drives the engine.
2. `audio_out_filter=tenms.reference_filter()` on the transport params. It feeds the agent's playback to the
   engine as the echo reference.
3. `vad_analyzer=tenms.vad_analyzer()` and `user_turn_strategies=tenms.user_turn_strategies()` in
   `LLMUserAggregatorParams`. They replace Silero VAD and the default turn strategies.
4. `proc = tenms.processor()` in the pipeline **after STT, before the user aggregator**.
5. `observers=[tenms.observer()]` on the `PipelineTask`. It records the bot's text, so the bot's own echo
   never counts as a barge-in.

Remove any other `audio_in_filter` (Krisp, noisereduce) and any other `vad_analyzer`. Do not add a separate
smart-turn analyzer: `user_turn_strategies()` already contains Tenms' stop strategy.

## Full example (Daily)

```python
# pip install "tenms[pipecat]" "pipecat-ai[daily,deepgram,openai,elevenlabs]"
import os

from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair, LLMUserAggregatorParams
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.elevenlabs.tts import ElevenLabsTTSService
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.transports.daily.transport import DailyParams, DailyTransport

from tenms.pipecat import TenmsCall


async def run_bot(room_url: str, token: str | None):
    tenms = TenmsCall()   # one per call; license from TENMS_LICENSE, focus_profile="stt"

    transport = DailyTransport(room_url, token, "Tenms Bot", DailyParams(
        audio_in_enabled=True, audio_out_enabled=True,
        audio_in_filter=tenms.focus_filter(),        # Focus: cleaned caller audio, drives the engine
        audio_out_filter=tenms.reference_filter(),   # agent playback -> echo reference
    ))
    stt = DeepgramSTTService(api_key=os.environ["DEEPGRAM_API_KEY"])
    llm = OpenAILLMService(api_key=os.environ["OPENAI_API_KEY"],
                           settings=OpenAILLMService.Settings(model="gpt-4o-mini"))
    tts = ElevenLabsTTSService(api_key=os.environ["ELEVENLABS_API_KEY"],
                               settings=ElevenLabsTTSService.Settings(voice=os.environ["ELEVENLABS_VOICE_ID"],
                                                                     model="eleven_flash_v2_5"))

    context = LLMContext([{"role": "system", "content": "You are a helpful phone agent."}])
    user_agg, assistant_agg = LLMContextAggregatorPair(context, user_params=LLMUserAggregatorParams(
        vad_analyzer=tenms.vad_analyzer(),                   # gated VAD
        user_turn_strategies=tenms.user_turn_strategies(),   # Tenms start + end-of-turn strategies
    ))
    proc = tenms.processor()                                 # after STT, before the user aggregator
    pipeline = Pipeline([transport.input(), stt, proc, user_agg, llm, tts,
                         transport.output(), assistant_agg])
    task = PipelineTask(pipeline, observers=[tenms.observer()])   # bot text -> echo-transcript rejection

    @proc.event_handler("on_call_summary")
    async def on_summary(processor, summary):
        print(summary["failure"], summary["quality"])        # dict; also posted if TENMS_CONTROL_URL is set

    @proc.event_handler("on_tenms_event")
    async def on_event(processor, event):                    # tenms.Event
        if event.type == "interruption" and event.kind == "barge_in":
            print("barge-in at", event.at_ms, "ms")

    await PipelineRunner().run(task)
```

## Components

All of them share `tenms.session` (a `tenms.TenmsSession`).

| Factory | Class | Role |
|---|---|---|
| `focus_filter()` | `TenmsFocusFilter` (`BaseAudioFilter`) | Processes every input chunk once and returns the cleaned audio. `FilterEnableFrame(False)` passes audio through while analysis continues. |
| `reference_filter()` | `TenmsReferenceFilter` | Records the bot's output audio as the transport writes it. This is the echo reference. Audio is unchanged. |
| `vad_analyzer()` | `TenmsVADAnalyzer` (`VADAnalyzer`) | Engine VAD. While the bot speaks it reads as silence until the engine decides `barge_in`, so backchannels, noise and other speakers never produce `VADUserStartedSpeakingFrame`. |
| `user_turn_strategies()` | `UserTurnStrategies` with `TenmsUserTurnStartStrategy` + `TurnAnalyzerUserTurnStopStrategy(TenmsTurnAnalyzer)` | Start on gated VAD or immediately on `barge_in`. Stop on the engine's `turn_end`. `wait_for_transcript=True` by default. |
| `turn_analyzer()` | `TenmsTurnAnalyzer` (`BaseTurnAnalyzer`) | Returns `COMPLETE` on `turn_end`. Only needed if you build the strategies yourself. |
| `processor()` | `TenmsProcessor` (`FrameProcessor`) | Tracks bot speaking and feeds final transcripts to Pulse. Turns backchannel transcripts into `UserBackchannelFrame`, so they stay out of the LLM context. Drops noise, other-speaker and echo transcripts. Pushes `TenmsEventFrame`s and reports the summary on `EndFrame` / `CancelFrame`. |
| `observer()` | `TenmsObserver` | Feeds `TTSTextFrame` text to the session (agent text). |

`tenms.pipecat` also exports the frame classes `TenmsEventFrame`, `TenmsTurnPredictionFrame` and
`TenmsInterruptionFrame`.

## Options

Keyword arguments of `TenmsCall(...)` go to `TenmsSession`:

| Argument | Default | Notes |
|---|---|---|
| `license` | `$TENMS_LICENSE` | license or session token |
| `focus_profile` | `"stt"` | `"listening"` / `"listening_mini"` only if humans hear this audio, never STT |
| `modules` | all licensed | e.g. `["turn", "pulse"]` to skip Focus |
| `focus` | `None` | e.g. `{"echo_tail_ms": 320}`, applied on top of the profile |
| `turn` | `None` | e.g. `{"max_silence_ms": 1200}` |
| `call_id` | random UUID | the summary's `call_id` |
| `reporter` | process default | `False` disables the summary upload |
| `reference_delay_ms` | `0.0` | initial output-path latency, as a head start for the echo-delay estimator |

`processor(...)` takes `push_events=True`, `suppress_non_barge_in_transcripts=True` and `flush_timeout=1.0`.

Sample rates: the transport's input rate must be 8000, 16000, 24000 or 48000 Hz. Set
`audio_in_sample_rate=16000` on the transport params if the default is something else.

## Events inside the pipeline

`TenmsProcessor` pushes engine events downstream as frames (`SystemFrame` subclasses):

* `TenmsTurnPredictionFrame` (`.probability`): end of turn is likely. You can start the LLM speculatively.
* `TenmsInterruptionFrame` (`.kind`): `barge_in` / `backchannel` / `noise` / `other_speaker`. You do not need
  to act on it: the gated VAD and start strategy already interrupt the bot on `barge_in` only.
* `TenmsEventFrame` (`.event`, a `tenms.Event`; `.wall_time`): any other event (`speech_start`, `turn_end`,
  `quality`).

The same events go to `@proc.event_handler("on_tenms_event")` as `(processor, event)`. On `EndFrame` /
`CancelFrame` the processor first delivers queued events (for up to `flush_timeout`), then fires
`on_call_summary(processor, summary)`.

## Other transports

Every Pipecat transport whose params subclass `TransportParams` takes `audio_in_filter` and
`audio_out_filter`. That includes `DailyParams`, `FastAPIWebsocketParams` (Twilio / Telnyx / Plivo
serializers), `WebsocketClientParams`, `LiveKitParams` and `LocalAudioTransportParams`. Wire them the same way.
For telephony, keep the transport's input rate at 8000 or 16000 Hz (not 44100).
