#!/usr/bin/env python3
"""Check a Tenms license or session token without printing it.

    python scripts/check_license.py                    # reads $TENMS_LICENSE
    python scripts/check_license.py --env-var MY_KEY   # reads $MY_KEY
    python scripts/check_license.py --online           # also asks the control plane (GET /v1/whoami)
    echo "$TENMS_LICENSE" | python scripts/check_license.py -   # from stdin

Offline, the token's signature and expiry are verified with ``tenms.verify_license`` (the vendor keys
built into the ``tenms`` wheel; ``pip install tenms``). Without ``tenms`` installed, the claims are only
decoded, not verified, and the script says so. ``--online`` sends the token to
``$TENMS_CONTROL_URL`` (default https://app.tenms.cloud) as a bearer token and prints what the control
plane reports.

The token itself is never printed: only a hint in the dashboard's format (``tenms1…`` + last 6 characters).
Exit status: 0 valid, 1 invalid / expired / rejected, 2 usage error.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

DEFAULT_CONTROL_URL = "https://app.tenms.cloud"
# Claims worth showing. Anything else in the token is left out on purpose.
SHOWN_CLAIMS = ("lid", "sub", "parent", "project_id", "modules", "max_sessions", "elevenlabs_agents",
                "default_focus_profile", "iat", "exp")
SHOWN_WHOAMI = ("role", "customer_id", "license_id", "parent", "project_id", "modules", "max_sessions",
                "elevenlabs_agents", "default_focus_profile", "expires_at")
KINDS = {"lic_": "license (server-side key)", "ses_": "session token (short-lived, for clients)",
         "pg_": "playground token (5 minutes)"}


def hint(token: str) -> str:
    return "tenms1…" + token[-6:] if len(token) > 12 else "(too short)"


def redact(text: str, token: str) -> str:
    return text.replace(token, hint(token)) if token else text


def decode_claims(token: str) -> dict:
    """Unverified payload of a ``tenms1.<claims>.<sig>`` token."""
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != "tenms1":
        raise ValueError("not a tenms1.<claims>.<signature> token")
    raw = parts[1] + "=" * (-len(parts[1]) % 4)
    claims = json.loads(base64.urlsafe_b64decode(raw))
    if not isinstance(claims, dict):
        raise ValueError("claims are not a JSON object")
    return claims


def when(ts, expiry: bool = True) -> str:
    if not isinstance(ts, (int, float)):
        return str(ts)
    t = float(ts) / 1000 if ts > 1e11 else float(ts)
    left = t - time.time()
    iso = datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    if left < 0:
        return f"{iso} ({'EXPIRED ' if expiry else ''}{fmt_span(-left)} ago)"
    return f"{iso} (in {fmt_span(left)})"


def fmt_span(s: float) -> str:
    if s < 120:
        return f"{int(s)} s"
    if s < 7200:
        return f"{int(s // 60)} min"
    if s < 172800:
        return f"{s / 3600:.1f} h"
    return f"{s / 86400:.1f} days"


def show(title: str, d: dict, keys: tuple) -> None:
    print(title)
    for k in keys:
        if k not in d or d[k] is None:
            continue
        v = d[k]
        if k in ("iat", "exp", "expires_at"):
            v = when(v, expiry=k != "iat")
        elif isinstance(v, list):
            v = ", ".join(map(str, v)) or "(none)"
        print(f"  {k:<22} {v}")


def check_offline(token: str) -> bool:
    try:
        claims = decode_claims(token)
    except ValueError as e:  # also JSONDecodeError, UnicodeDecodeError, binascii.Error
        reason = str(e) if str(e).startswith("not a tenms1") else f"claims cannot be decoded ({type(e).__name__})"
        print(f"offline: INVALID - {reason}")
        return False
    kind = next((v for p, v in KINDS.items() if str(claims.get("lid", "")).startswith(p)), "unknown kind")
    print(f"token:   {hint(token)}  ({kind})")
    try:
        import tenms
    except ImportError:
        print("offline: claims decoded but signature NOT verified (pip install tenms to verify)")
        show("claims (unverified):", claims, SHOWN_CLAIMS)
        exp = claims.get("exp")
        return isinstance(exp, (int, float)) and exp > time.time()
    try:
        verified = tenms.verify_license(token)
    except tenms.LicenseError as e:
        print(f"offline: REJECTED by tenms {tenms.__version__} - {redact(str(e), token)}")
        show("claims (unverified):", claims, SHOWN_CLAIMS)
        return False
    print(f"offline: OK - signature and expiry verified by tenms {tenms.__version__}")
    show("claims:", {**claims, **dict(verified)}, SHOWN_CLAIMS)
    return True


def check_online(token: str, control_url: str) -> bool:
    url = control_url.rstrip("/") + "/v1/whoami"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 - https URL from config
            body = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            err = json.loads(raw).get("error")
            code = err.get("code") if isinstance(err, dict) else err
            msg = err.get("message", "") if isinstance(err, dict) else ""
        except (ValueError, AttributeError):
            code, msg = f"http_{e.code}", ""
        print(f"online:  REJECTED by {control_url} - HTTP {e.code} {code} {redact(str(msg), token)}".rstrip())
        return False
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        print(f"online:  could not reach {control_url}: {getattr(e, 'reason', e)}")
        return False
    print(f"online:  OK - {control_url} accepts the token")
    show("whoami:", body if isinstance(body, dict) else {}, SHOWN_WHOAMI)
    return True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Check a Tenms license / session token (never prints it).")
    ap.add_argument("token", nargs="?", help="'-' to read the token from stdin; default: the environment variable")
    ap.add_argument("--env-var", default="TENMS_LICENSE", help="variable holding the token (default TENMS_LICENSE)")
    ap.add_argument("--online", action="store_true", help="also call GET /v1/whoami on the control plane")
    ap.add_argument("--control-url", default=os.environ.get("TENMS_CONTROL_URL") or DEFAULT_CONTROL_URL,
                    help="control plane (default $TENMS_CONTROL_URL, else https://app.tenms.cloud)")
    a = ap.parse_args(argv)

    if a.token == "-":
        token = sys.stdin.readline()
    elif a.token:
        token = a.token
    else:
        token = os.environ.get(a.env_var, "")
    token = token.strip().strip("\"'")
    if not token:
        print(f"no token: set {a.env_var} (create a key at https://app.tenms.cloud) or pass '-' and pipe it in",
              file=sys.stderr)
        return 2

    ok = check_offline(token)
    if a.online:
        ok = check_online(token, a.control_url) and ok
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
