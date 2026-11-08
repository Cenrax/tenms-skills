# Keys, licenses and session tokens

## Get a key

1. Sign up at https://app.tenms.cloud/signup, or join a teammate's organisation through an invite link.
   Tenms approves new organisations before keys can be created.
2. An organisation has **projects** (for example "Support bot" or "Staging"). Under Projects → API keys,
   create a key. It is shown **once**.
3. The key is a license token: `tenms1.<claims>.<signature>`, with an id of `lic_…`. It is Ed25519-signed and
   verified offline by the SDK and by Edge, so audio never waits on a license check. It carries the customer,
   the modules (`focus`, `turn`, `pulse`), the expiry, `max_sessions` and the project. Calls made with it, or
   with session tokens minted from it, land in that project.

Project settings apply to keys created **after** the change. These settings are the default Focus profile,
max concurrent sessions, your own ElevenLabs key and agent ids, and the webhook.

## Where the key goes

| Component | How it gets the key |
|---|---|
| `tenms.Engine`, `TenmsCall`, `TenmsLiveKit`, `TenmsSession` | `license=` or `$TENMS_LICENSE` |
| `tenms.edge.EdgeClient` | `token=` or `$TENMS_LICENSE` (sent as `Authorization: Bearer`) |
| Raw Edge WebSocket | `Authorization: Bearer <token>` header, or `?token=` (session tokens only) |
| Control plane (`https://app.tenms.cloud`) | `Authorization: Bearer <token>` |

Rules for the agent doing the integration:

* Read the key from the environment or the project's secret manager. Add `TENMS_LICENSE=` to `.env.example`,
  never the value. Never print, log or commit the key.
* **Never put a license in a web page, mobile app, desktop client or device firmware.** Anyone who reads it
  can use it until it is revoked. Clients get session tokens.
* SDK (in-process) licenses are checked offline against the vendor keys built into the wheel and the system
  clock. A revoked SDK license keeps working in process until it expires, so keep SDK key lifetimes short.
  Edge enforces revocation mid-call.
* `public_key=` / `TENMS_LICENSE_PUBLIC_KEY` are for self-hosted deployments that sign their own licenses.
  Customers using Tenms-issued keys never set them.

## Session tokens (browsers, mobile, devices)

The backend that holds the license mints a short-lived session token per conversation and returns only that
to the client:

```python
import os

from tenms.control import ControlError, mint_session_token


def new_session_token() -> dict:
    # authenticate *your* user before calling this
    try:
        token, expires_at = mint_session_token(os.environ["TENMS_LICENSE"], ttl_secs=300)
    except ControlError as e:
        # e.fatal: bad, revoked or expired license -> fix it, do not retry
        # e.retryable: network / 429 / 5xx still failing after the SDK's own retries
        raise RuntimeError(f"tenms session token: {e.code}") from e
    return {"token": token, "expires_at": expires_at}   # expires_at: unix seconds
```

* Async: `await amint_session_token(license, ttl_secs=300)`. Both retry network errors, 429 and 5xx, and
  honour `Retry-After`. `control_url` defaults to `$TENMS_CONTROL_URL`, else `https://app.tenms.cloud`.
* Raw HTTP: `POST https://app.tenms.cloud/v1/session-tokens` with `Authorization: Bearer <license>` and an
  optional body `{"ttl_secs": n}`. Unknown fields are rejected with 400. The answer is
  `{"token", "expires_at", "session_id", "parent", "customer_id", "project_id"}`.
* A session token (`lid = ses_…`, `parent = lic_…`) must be **used to connect within 5 minutes**. A
  conversation that started in time can run up to 4 h.
* It inherits the parent's modules, ElevenLabs agents, `max_sessions`, project and default Focus profile. It
  shares the parent's per-license limits, and revoking the parent ends it within one revocation poll (about
  90 s).
* A session token cannot mint further session tokens (`403 forbidden`).
* On `license_expired` (the token's 5 minutes passed before connecting), mint a new one.

Errors are `tenms.control.ControlError` with `.code`, `.message`, `.status`, `.retry_after`, `.fatal` and
`.retryable`. The control plane's own errors look like `{"error": {"code": "unauthorized", "message":
"invalid token"}}`.

## Checking a key

`scripts/check_license.py` (in this skill) verifies a key offline with `tenms.verify_license`, against the
vendor keys built into the wheel. It prints the token kind, modules, expiry and claims, never the key itself.
With `--online` it also calls `GET https://app.tenms.cloud/v1/whoami`, which answers with role, customer,
modules, expiry, `project_id` and `default_focus_profile`:

```bash
python scripts/check_license.py                 # uses $TENMS_LICENSE
python scripts/check_license.py --online
python scripts/check_license.py --env-var MY_TENMS_KEY
```

In Python, `tenms.verify_license(token)` returns `{"lid", "sub", "modules", "iat", "exp"}` or raises
`tenms.LicenseError` for malformed, forged or expired tokens.

## Limits

| Limit | Value | Error |
|---|---|---|
| Concurrent sessions per license, per Edge replica | `max_sessions` claim, else 100 | `429 license_session_limit` |
| Connection rate per license | 10 per second, bursts of 20 | `429 rate_limited` |
| Session length | 4 h | `max_session_duration` |
| `start` message | within 10 s | `start_timeout` |
| Audio pacing | 1.5x real time after a 5 s burst | none (Edge stops reading) |

## Telemetry

* Edge and the ElevenLabs bridge post the `CallSummary` themselves.
* In-process SDK integrations post it to `$TENMS_CONTROL_URL/v1/calls:batch` with the license, but **only when
  `TENMS_CONTROL_URL` is set** (opt-in). Set `TENMS_CONTROL_URL=https://app.tenms.cloud` to see SDK calls in
  the dashboard. Pass `reporter=False` to disable it per call.
* `TENMS_EDGE_URL` overrides the Edge URL for `EdgeClient`. The default is `wss://edge.tenms.cloud`.
* Summaries contain no audio and no transcripts.
