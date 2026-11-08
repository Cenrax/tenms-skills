#!/usr/bin/env python3
"""Run a short synthetic call through Tenms Edge and print what came back.

    python scripts/edge_smoke.py                          # stt profile, $TENMS_LICENSE, wss://edge.tenms.cloud
    python scripts/edge_smoke.py --profile listening_pro
    python scripts/edge_smoke.py --profile listening_mini --seconds 8 --sample-rate 8000

It speaks the raw Edge protocol (``/v1/stream``) with the ``websockets`` package
(``pip install "tenms[edge]"`` or ``pip install websockets``). It sends ``start``, then streams synthetic
PCM16 in real time: agent playback with its echo (as ``0x02`` reference + ``0x01`` mic), a caller "voice",
a final transcript and ``end``. It then prints the ``ready`` profile, the processed audio returned, the events
by type and the ``CallSummary`` highlights. The token is read from an environment variable and never printed.

Exit status: 0 ok, 1 Edge refused or failed the call, 2 usage error.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import random
import struct
import sys
import time
from collections import Counter

try:
    import websockets
    from websockets.exceptions import ConnectionClosed, InvalidStatus
except ImportError:  # pragma: no cover - depends on the environment
    print('edge_smoke.py needs websockets: pip install "tenms[edge]"  (or: pip install websockets)', file=sys.stderr)
    sys.exit(2)

PROFILES = ("stt", "listening", "listening_mini", "listening_pro")
RATES = (8000, 16000, 24000, 48000)
FRAME_MS = 20
HINTS = {
    "missing_license": "no token sent: set the environment variable",
    "invalid_license": "malformed or forged token: run scripts/check_license.py",
    "license_expired": "expired: mint a new session token or create a new key",
    "license_revoked": "revoked: create a new key at https://app.tenms.cloud",
    "no_modules": "the key grants none of the requested modules",
    "rate_limited": "too many connections: back off and retry",
    "license_session_limit": "too many concurrent sessions for this key: close some, retry",
    "at_capacity": "replica full: retry with backoff",
    "draining": "replica shutting down: retry",
    "not_ready": "replica starting: retry",
    "profile_unavailable": "this replica has not loaded the profile's model: use another profile",
    "profile_capacity": "listening_pro sessions full on this replica: retry, or use listening_mini",
    "bad_message": "invalid start message (unknown focus_profile?)",
    "unsupported_sample_rate": "use 8000, 16000, 24000 or 48000",
}


def tone(n: int, rate: int, t0: int, f0: float, amp: float, syllable_hz: float) -> list[float]:
    """A voiced, syllable-modulated harmonic tone (a crude stand-in for speech)."""
    out = []
    for i in range(n):
        t = (t0 + i) / rate
        env = 0.5 - 0.5 * math.cos(2 * math.pi * syllable_hz * t)
        f = f0 * (1 + 0.06 * math.sin(2 * math.pi * 3 * t))
        s = sum(math.sin(2 * math.pi * f * k * t) / k for k in (1, 2, 3, 4))
        out.append(amp * env * s)
    return out


def pcm(samples: list[float]) -> bytes:
    return struct.pack(f"<{len(samples)}h", *(max(-32768, min(32767, int(x * 32767))) for x in samples))


def script(seconds: float):
    """Timeline (s): agent speaks [0, 1.6); caller speaks [2.0, 3.4); silence to the end."""
    agent = (0.0, min(1.6, seconds))
    caller = (min(2.0, seconds), min(3.4, seconds))
    return agent, caller


async def run(a: argparse.Namespace, token: str) -> int:
    url = a.url.rstrip("/")
    if "/v1/" not in url:
        url += "/v1/stream"
    rate, n = a.sample_rate, a.sample_rate * FRAME_MS // 1000
    rng = random.Random(7)
    agent, caller = script(a.seconds)
    events: Counter[str] = Counter()
    kinds: Counter[str] = Counter()
    audio_bytes = 0
    summary = None
    errors: list[str] = []

    t_connect = time.monotonic()
    try:
        ws = await websockets.connect(url, additional_headers={"Authorization": f"Bearer {token}"},
                                      open_timeout=10, max_size=2**22)
    except InvalidStatus as e:
        status = e.response.status_code
        try:
            body = json.loads(e.response.body.decode())
            code = body.get("error") if isinstance(body.get("error"), str) else body["error"]["code"]
        except Exception:
            code = f"http_{status}"
        print(f"REFUSED  HTTP {status} {code}" + (f" - {HINTS[code]}" if code in HINTS else ""))
        return 1
    except (OSError, asyncio.TimeoutError) as e:
        print(f"FAILED   cannot connect to {url}: {e}")
        return 1

    async with ws:
        start = {"type": "start", "sample_rate": rate, "call_id": f"skill-smoke-{int(time.time())}",
                 "focus_profile": a.profile}
        await ws.send(json.dumps(start))
        ready = json.loads(await asyncio.wait_for(ws.recv(), 10))
        if ready.get("type") != "ready":
            code = ready.get("code", ready.get("type"))
            print(f"FAILED   start -> {code}" + (f" - {HINTS[code]}" if code in HINTS else ""))
            if a.verbose:
                print(f"         message: {ready.get('message', '')}")
            return 1
        ready_ms = (time.monotonic() - t_connect) * 1000
        print(f"ready    profile={ready.get('focus_profile')} region={ready.get('region')} "
              f"modules={','.join(ready.get('modules', []))} rate={ready.get('sample_rate')} "
              f"connect+ready={ready_ms:.0f} ms")

        async def reader():
            nonlocal audio_bytes, summary
            try:
                async for msg in ws:
                    if isinstance(msg, (bytes, bytearray)):
                        audio_bytes += len(msg) - 1
                        continue
                    ev = json.loads(msg)
                    t = ev.get("type")
                    if t == "summary":
                        summary = ev.get("summary")
                        return
                    if t == "error":
                        errors.append(ev.get("code", "error"))
                        continue
                    events[t] += 1
                    if t == "interruption":
                        kinds[ev.get("kind", "?")] += 1
                    if a.verbose:
                        print(f"  event  {json.dumps(ev)}")
            except ConnectionClosed:
                return

        task = asyncio.create_task(reader())
        total = int(a.seconds * rate)
        pos, t0 = 0, time.monotonic()
        speaking = False
        while pos < total:
            ts = pos / rate
            in_agent = agent[0] <= ts < agent[1]
            in_caller = caller[0] <= ts < caller[1]
            if in_agent != speaking:
                speaking = in_agent
                await ws.send(json.dumps({"type": "agent_speaking", "speaking": speaking}))
                if speaking:
                    await ws.send(json.dumps({"type": "agent_text", "text": "Thanks for calling, how can I help?"}))
            ref = tone(n, rate, pos, 140.0, 0.30, 4.0) if in_agent else [0.0] * n
            voice = tone(n, rate, pos, 210.0, 0.25, 3.0) if in_caller else [0.0] * n
            mic = [0.25 * r + v + rng.gauss(0, 0.004) for r, v in zip(ref, voice)]  # echo + caller + noise
            if in_agent:
                await ws.send(b"\x02" + pcm(ref))
            await ws.send(b"\x01" + pcm(mic))
            pos += n
            await asyncio.sleep(max(0.0, t0 + pos / rate - time.monotonic()))  # real time
        await ws.send(json.dumps({"type": "transcript", "text": "this is a smoke test", "is_final": True}))
        await ws.send(json.dumps({"type": "end"}))
        try:
            await asyncio.wait_for(task, 10)
        except asyncio.TimeoutError:
            task.cancel()

    sent_ms = total * 1000 // rate
    got_ms = audio_bytes // 2 * 1000 // rate
    print(f"audio    sent {sent_ms} ms, processed audio back {got_ms} ms")
    print(f"events   {sum(events.values())} total: " + (", ".join(f"{k}={v}" for k, v in sorted(events.items())) or "none")
          + (f" (interruption kinds: {', '.join(f'{k}={v}' for k, v in sorted(kinds.items()))})" if kinds else ""))
    if errors:
        print(f"errors   {', '.join(errors)}")
    if summary is None:
        print("FAILED   no summary received")
        return 1
    proc = summary.get("processing") or {}
    q = summary.get("quality") or {}
    turns = summary.get("turns") or {}
    print(f"summary  call_id={summary.get('call_id')} focus_profile={summary.get('focus_profile')} "
          f"duration={summary.get('duration_ms')} ms")
    print(f"delay    algorithmic_delay_ms={proc.get('algorithmic_delay_ms')} "
          f"compute_us p50={proc.get('compute_us_p50')} p99={proc.get('compute_us_p99')}")
    if q:
        print(f"quality  mos={q.get('mos')} snr_db={q.get('snr_db')} asr_risk={q.get('asr_risk')}")
    if turns:
        print(f"turns    user_turns={turns.get('user_turns')} barge_ins={turns.get('barge_ins')} "
              f"backchannels={turns.get('backchannels')}")
    failure = (summary.get("failure") or {}).get("cause")
    print(f"OK       Edge processed the call (failure cause: {failure})")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Synthetic call against Tenms Edge /v1/stream.")
    ap.add_argument("--profile", default="stt", choices=PROFILES, help="Focus profile (default stt)")
    ap.add_argument("--url", default=os.environ.get("TENMS_EDGE_URL") or "wss://edge.tenms.cloud",
                    help="Edge URL (default $TENMS_EDGE_URL, else wss://edge.tenms.cloud)")
    ap.add_argument("--env-var", default="TENMS_LICENSE", help="variable holding the token (default TENMS_LICENSE)")
    ap.add_argument("--seconds", type=float, default=5.0, help="call length in seconds (default 5)")
    ap.add_argument("--sample-rate", type=int, default=16000, choices=RATES)
    ap.add_argument("--verbose", "-v", action="store_true", help="print every event")
    a = ap.parse_args(argv)
    token = os.environ.get(a.env_var, "").strip().strip("\"'")
    if not token:
        print(f"no token: set {a.env_var} (create a key at https://app.tenms.cloud)", file=sys.stderr)
        return 2
    if a.seconds <= 0 or a.seconds > 60:
        print("--seconds must be in (0, 60]", file=sys.stderr)
        return 2
    return asyncio.run(run(a, token))


if __name__ == "__main__":
    sys.exit(main())
