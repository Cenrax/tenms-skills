"""Checks for the Tenms Agent Skill (skills/tenms).

Run against the published packages, in a scratch virtualenv (not the repository's python/ build):

    uv venv /tmp/tenms-skill-venv --python 3.12
    uv pip install --python /tmp/tenms-skill-venv/bin/python \\
        "tenms[edge,pipecat,livekit]==0.1.1" "livekit-plugins-tenms==0.1.1" pytest
    /tmp/tenms-skill-venv/bin/python -m pytest skills/tests -q

What is checked:

* SKILL.md frontmatter (name = folder, short description, body length) and every relative link / anchor.
* Every ```python block in SKILL.md and references/ parses, and every import in it resolves: tenms /
  livekit.plugins.tenms names are imported and looked up; framework modules (pipecat, livekit) are found
  with importlib; a short list of web-framework packages that the examples only mention may be absent.
* Attribute accesses on Tenms objects in those blocks exist (``tenms.Engine``, ``tenms.edge.EdgeClient``,
  ``call.vad()``, ``edge.events_nowait()``, ``ev.kind``, ...), and so do dotted ``tenms.…`` names in prose.
* Focus profile names, event types and interruption kinds used anywhere are the real ones.
* The scripts parse, import only the standard library + tenms + websockets, and behave without a token.
* The website's publish guard (website/scripts/sync-docs.mjs, PUBLISH_FORBIDDEN) passes on every file.
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import inspect
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SKILLS = Path(__file__).resolve().parents[1]
SKILL = SKILLS / "tenms"
REPO = SKILLS.parent
SYNC_DOCS = REPO / "website" / "scripts" / "sync-docs.mjs"

tenms = pytest.importorskip("tenms")

MD_FILES = sorted([SKILL / "SKILL.md", *(SKILL / "references").glob("*.md")])
SCRIPTS = sorted((SKILL / "scripts").glob("*.py"))
PROFILES = {"stt", "listening", "listening_mini", "listening_pro"}
EVENT_TYPES = {"speech_start", "turn_prediction", "turn_end", "interruption", "quality"}
KINDS = {"barge_in", "backchannel", "noise", "other_speaker"}
# Packages the examples import only as the user's own web framework; not Tenms dependencies.
OPTIONAL_THIRD_PARTY = {"fastapi", "flask", "requests", "uvicorn", "django"}


def read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def python_blocks() -> list[tuple[str, int, str]]:
    out = []
    for f in MD_FILES:
        text = read(f)
        for m in re.finditer(r"^```python\n(.*?)^```", text, re.S | re.M):
            line = text[: m.start()].count("\n") + 1
            out.append((f"{f.relative_to(SKILL)}:{line}", line, m.group(1)))
    return out


BLOCKS = python_blocks()
BLOCK_IDS = [b[0] for b in BLOCKS]


# -- installed packages ----------------------------------------------------------------------


def test_published_versions_are_installed():
    assert tenms.__version__ == "0.1.1", "run against tenms==0.1.1 from PyPI"
    from livekit.plugins.tenms import __version__ as lk

    assert lk == "0.1.1", "run against livekit-plugins-tenms==0.1.1 from PyPI"


# -- frontmatter and structure ---------------------------------------------------------------


def frontmatter(text: str) -> tuple[dict[str, str], str]:
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    assert m, "SKILL.md must start with YAML frontmatter"
    fields = {}
    for line in m.group(1).splitlines():
        k, sep, v = line.partition(":")
        assert sep and re.fullmatch(r"[a-z_-]+", k), f"bad frontmatter line {line!r}"
        fields[k] = v.strip()
    return fields, m.group(2)


def test_frontmatter():
    fields, body = frontmatter(read(SKILL / "SKILL.md"))
    assert set(fields) <= {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}
    assert fields["name"] == SKILL.name == "tenms"
    assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", fields["name"]) and len(fields["name"]) <= 64
    d = fields["description"]
    assert 50 <= len(d) <= 300, f"description is {len(d)} characters"
    assert "<" not in d and ">" not in d
    for word in ("Pipecat", "LiveKit", "ElevenLabs", "WebSocket", "debug"):
        assert word in d, f"description should say when to use the skill ({word})"
    assert len((SKILL / "SKILL.md").read_text().splitlines()) <= 250
    assert body.lstrip().startswith("# ")


def github_slug(heading: str) -> str:
    s = heading.strip().lower()
    s = re.sub(r"[^\w\- ]", "", s)
    return s.replace(" ", "-")


def anchors(p: Path) -> set[str]:
    text = re.sub(r"^```.*?^```", "", read(p), flags=re.S | re.M)
    return {github_slug(m.group(1)) for m in re.finditer(r"^#{1,6} (.+)$", text, re.M)}


def test_relative_links_and_anchors_resolve():
    n = 0
    for f in MD_FILES:
        text = re.sub(r"^```.*?^```", "", read(f), flags=re.S | re.M)
        for m in re.finditer(r"\]\(([^)\s]+)\)", text):
            href = m.group(1)
            if re.match(r"^(https?:|mailto:)", href):
                continue
            path, _, frag = href.partition("#")
            target = (f.parent / path).resolve() if path else f
            assert target.is_file(), f"{f.name}: broken link {href}"
            if frag:
                assert frag in anchors(target), f"{f.name}: no anchor #{frag} in {target.name}"
            n += 1
    assert n >= 15
    # Every reference file is reachable from SKILL.md.
    skill = read(SKILL / "SKILL.md")
    for ref in (SKILL / "references").glob("*.md"):
        assert f"references/{ref.name}" in skill, f"SKILL.md does not link {ref.name}"


# -- python blocks -----------------------------------------------------------------------------


def test_there_are_python_blocks():
    assert len(BLOCKS) >= 10
    files = {b[0].split(":")[0] for b in BLOCKS}
    for name in ("pipecat.md", "livekit.md", "elevenlabs.md", "edge-websocket.md", "licensing.md"):
        assert f"references/{name}" in files


@pytest.mark.parametrize("where,line,code", BLOCKS, ids=BLOCK_IDS)
def test_python_block_parses(where, line, code):
    ast.parse(code, filename=where)


def resolve_tenms_module(modname: str):
    """Import a tenms / livekit.plugins.tenms module (they must exist in the published wheels)."""
    return importlib.import_module(modname)


def is_tenms_module(modname: str) -> bool:
    return modname == "tenms" or modname.startswith("tenms.") or modname.startswith("livekit.plugins.tenms")


def check_import(modname: str, names: list[str], where: str) -> None:
    top = modname.split(".")[0]
    if is_tenms_module(modname):
        mod = resolve_tenms_module(modname)
        for n in names:
            assert hasattr(mod, n) or importlib.util.find_spec(f"{modname}.{n}"), f"{where}: {modname}.{n} missing"
        return
    if modname == "livekit.plugins":
        for n in names:
            if n == "tenms":
                importlib.import_module("livekit.plugins.tenms")
            # deepgram / openai / elevenlabs plugins are the user's choice; not installed here.
        return
    if top in OPTIONAL_THIRD_PARTY:
        return
    assert importlib.util.find_spec(modname) is not None, f"{where}: module {modname} not found"


def imports_of(tree: ast.AST) -> list[tuple[str, list[str]]]:
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out += [(a.name, []) for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.append((node.module, [a.name for a in node.names]))
    return out


@pytest.mark.parametrize("where,line,code", BLOCKS, ids=BLOCK_IDS)
def test_python_block_imports_resolve(where, line, code):
    for mod, names in imports_of(ast.parse(code)):
        check_import(mod, names, where)


# A tiny type inference over each block: which local names hold Tenms modules, classes or objects.

def known_types(tree: ast.AST) -> dict[str, object]:
    import tenms.edge
    import tenms.livekit
    import tenms.pipecat

    env: dict[str, object] = {}
    for mod, names in imports_of(tree):
        if not is_tenms_module(mod) and mod != "livekit.plugins":
            continue
        if not names:
            if is_tenms_module(mod):
                env[mod.split(".")[0]] = importlib.import_module(mod.split(".")[0])
            continue
        for n in names:
            if mod == "livekit.plugins" and n != "tenms":
                continue
            src = importlib.import_module(mod)
            obj = getattr(src, n, None)
            if obj is None:
                obj = importlib.import_module(f"{mod}.{n}")
            env[n] = obj

    factories = {
        "TenmsCall": tenms.pipecat.TenmsCall,
        "TenmsLiveKit": tenms.livekit.TenmsLiveKit,
        "Engine": tenms.Engine,
        "EdgeClient": tenms.edge.EdgeClient,
        "TenmsSession": tenms.TenmsSession,
    }

    def value_type(expr: ast.AST):
        if isinstance(expr, ast.Call):
            f = expr.func
            name = f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else None
            if name in factories:
                return InstanceOf(factories[name])
            if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
                base = env.get(f.value.id)
                if isinstance(base, InstanceOf) and base.cls is tenms.pipecat.TenmsCall and name == "processor":
                    return InstanceOf(tenms.pipecat.TenmsProcessor)
        return None

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            t = value_type(node.value)
            if t is not None:
                env[node.targets[0].id] = t
            elif node.targets[0].id in ("ev", "event"):
                env[node.targets[0].id] = None  # e.g. ev = json.loads(msg): not a tenms.Event
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if isinstance(item.optional_vars, ast.Name):
                    t = value_type(item.context_expr)
                    if t is not None:
                        env[item.optional_vars.id] = t
        elif isinstance(node, ast.For) and isinstance(node.target, ast.Name) and node.target.id in ("ev", "event"):
            env.setdefault(node.target.id, InstanceOf(tenms.Event))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for arg in node.args.args:
                if arg.arg in ("ev", "event"):
                    env.setdefault(arg.arg, InstanceOf(tenms.Event))
                if arg.arg == "call" and "TenmsLiveKit" in env:
                    env.setdefault("call", InstanceOf(tenms.livekit.TenmsLiveKit))
    return env


class InstanceOf:
    def __init__(self, cls):
        self.cls = cls


def instance_has(cls, attr: str) -> bool:
    if hasattr(cls, attr):
        return True
    for klass in inspect.getmro(cls):
        try:
            src = inspect.getsource(klass)
        except (OSError, TypeError):
            continue
        if re.search(rf"self\.{re.escape(attr)}\s*(:[^=\n]+)?=", src):
            return True
    return False


def attr_chain(node: ast.Attribute) -> tuple[str, list[str]] | None:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        return node.id, parts[::-1]
    return None


def check_attribute_accesses(where: str, code: str) -> int:
    """Assert every attribute access on a known Tenms module / class / object exists; return the count."""
    tree = ast.parse(code)
    env = known_types(tree)
    checked = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        chain = attr_chain(node)
        if chain is None or chain[0] not in env or env[chain[0]] is None:
            continue
        root, parts = chain
        obj = env[root]
        if isinstance(obj, InstanceOf):
            assert instance_has(obj.cls, parts[0]), f"{where}: {root}.{parts[0]} ({obj.cls.__name__}) does not exist"
            checked += 1
            continue
        for p in parts:
            if inspect.ismodule(obj) and not hasattr(obj, p):
                try:
                    obj = importlib.import_module(f"{obj.__name__}.{p}")
                    continue
                except ImportError:
                    pass
            if inspect.isclass(obj):
                assert instance_has(obj, p), f"{where}: {root}.{'.'.join(parts)} does not exist"
                break
            assert hasattr(obj, p), f"{where}: {root}.{'.'.join(parts)} does not exist"
            obj = getattr(obj, p)
        checked += 1
    return checked


@pytest.mark.parametrize("where,line,code", BLOCKS, ids=BLOCK_IDS)
def test_tenms_attribute_accesses_exist(where, line, code):
    check_attribute_accesses(where, code)


def test_attribute_checks_cover_the_examples():
    total = sum(check_attribute_accesses(w, c) for w, _, c in BLOCKS)
    assert total >= 40, f"only {total} Tenms attribute accesses were checked"


def string_uses(tree: ast.AST):
    """(kind, value) for focus_profile values, event types and interruption kinds in a block."""
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == "focus_profile" and isinstance(node.value, ast.Constant):
            yield "profile", node.value.value
        if isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if isinstance(k, ast.Constant) and isinstance(v, ast.Constant):
                    if k.value == "focus_profile":
                        yield "profile", v.value
                    if k.value == "type" and v.value in EVENT_TYPES | {"interruption"}:
                        yield "event", v.value
        if isinstance(node, ast.Compare) and len(node.ops) == 1 and isinstance(node.ops[0], ast.Eq):
            left, right = node.left, node.comparators[0]
            if isinstance(left, ast.Attribute) and isinstance(right, ast.Constant):
                if left.attr == "type":
                    yield "event", right.value
                elif left.attr == "kind":
                    yield "kind", right.value


@pytest.mark.parametrize("where,line,code", BLOCKS, ids=BLOCK_IDS)
def test_profile_event_and_kind_literals_are_real(where, line, code):
    import tenms.edge

    assert set(tenms.edge.FOCUS_PROFILES) == PROFILES
    for kind, value in string_uses(ast.parse(code)):
        allowed = {"profile": PROFILES, "event": EVENT_TYPES | {"summary", "error", "ready"}, "kind": KINDS}[kind]
        assert value in allowed, f"{where}: {kind} {value!r} is not real"


def test_profile_and_event_names_in_prose_are_real():
    for f in MD_FILES + SCRIPTS:
        text = read(f)
        for m in re.finditer(r"\blistening_[a-z]+\b", text):
            assert m.group(0) in PROFILES, f"{f.name}: unknown profile {m.group(0)}"
        for m in re.finditer(r'focus_profile[=:]\s*"?([a-z_]+)', text):
            assert m.group(1) in PROFILES | {"none"}, f"{f.name}: focus_profile={m.group(1)}"
        for m in re.finditer(r'kind == "([a-z_]+)"', text):
            assert m.group(1) in KINDS, f"{f.name}: kind {m.group(1)}"


def test_dotted_tenms_names_in_prose_exist():
    seen = set()
    for f in MD_FILES:
        for m in re.finditer(r"`(?<![\w./])(tenms(?:\.[A-Za-z_][A-Za-z_0-9]*)+)", read(f)):
            seen.add((f.name, m.group(1)))
    assert len(seen) >= 10
    import livekit.plugins.tenms as lk_plugin
    from tenms.pipecat import TenmsCall

    def resolves(root, parts: list[str], prefix: str) -> bool:
        obj = root
        for i, p in enumerate(parts):
            if inspect.ismodule(obj) and not hasattr(obj, p):
                try:
                    obj = importlib.import_module(f"{obj.__name__}.{p}")
                    continue
                except ImportError:
                    return False
            if inspect.isclass(obj):
                return instance_has(obj, p)
            if not hasattr(obj, p):
                return False
            obj = getattr(obj, p)
        return True

    # In prose `tenms.` is the module, the LiveKit plugin (`from livekit.plugins import tenms`) or, in the
    # Pipecat guide, the `tenms = TenmsCall()` object.
    roots = [importlib.import_module("tenms"), lk_plugin, TenmsCall]
    for fname, path in sorted(seen):
        parts = path.split(".")[1:]
        assert any(resolves(r, parts, path) for r in roots), f"{fname}: {path} does not exist"


def test_pipecat_and_livekit_factories_documented_exist():
    from tenms.livekit import TenmsLiveKit
    from tenms.pipecat import TenmsCall

    for name in ("focus_filter", "reference_filter", "vad_analyzer", "turn_analyzer", "user_turn_strategies",
                 "observer", "processor"):
        assert hasattr(TenmsCall, name)
    for name in ("noise_cancellation", "vad", "turn_detector", "attach", "on_summary", "finish"):
        assert hasattr(TenmsLiveKit, name)
    import livekit.plugins.tenms as lk

    for name in ("TenmsLiveKit", "TenmsAgent", "TenmsFocus", "Focus", "VAD", "TurnDetector", "gate_stt_events"):
        assert hasattr(lk, name)


def test_control_error_codes_documented_match_sdk():
    from tenms import control

    text = read(SKILL / "references" / "troubleshooting.md")
    for code in ("profile_unavailable", "license_revoked", "agent_not_allowed", "invalid_license"):
        assert code in control.FATAL_CODES and code in text
    for code in ("profile_capacity", "rate_limited", "at_capacity", "draining", "server_shutdown"):
        assert code in control.RETRYABLE_CODES and code in text
    assert "license_expired" in control.REAUTH_CODES


# -- scripts -----------------------------------------------------------------------------------

ALLOWED_SCRIPT_IMPORTS = {"tenms", "websockets"} | set(sys.stdlib_module_names) | {"__future__"}


@pytest.mark.parametrize("script", SCRIPTS, ids=[s.name for s in SCRIPTS])
def test_script_parses_and_imports_only_stdlib_tenms_websockets(script):
    tree = ast.parse(read(script))
    for mod, _ in imports_of(tree):
        assert mod.split(".")[0] in ALLOWED_SCRIPT_IMPORTS, f"{script.name} imports {mod}"
    assert ast.get_docstring(tree), f"{script.name} needs a usage docstring"


@pytest.mark.parametrize("script", SCRIPTS, ids=[s.name for s in SCRIPTS])
def test_script_help(script):
    r = subprocess.run([sys.executable, str(script), "--help"], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    assert "usage" in r.stdout


def run_script(name: str, *args: str, env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SKILL / "scripts" / name), *args], capture_output=True, text=True,
                          timeout=30, env={"PATH": "/usr/bin:/bin", **env})


def test_scripts_without_a_token_exit_2():
    assert run_script("check_license.py", env={}).returncode == 2
    assert run_script("edge_smoke.py", env={}).returncode == 2


def test_check_license_rejects_a_bad_token_without_printing_it():
    import base64

    claims = base64.urlsafe_b64encode(json.dumps({"lid": "lic_x", "sub": "t", "modules": ["focus"],
                                                  "iat": 1, "exp": 2}).encode()).decode().rstrip("=")
    secret_sig = "SECRETSIGNATUREdoNotPrint1234567890"
    token = f"tenms1.{claims}.{secret_sig}"
    r = run_script("check_license.py", env={"TENMS_LICENSE": token})
    assert r.returncode == 1
    out = r.stdout + r.stderr
    assert token not in out and secret_sig not in out
    assert "REJECTED" in out and "focus" in out
    r = run_script("check_license.py", env={"TENMS_LICENSE": "garbage-token-value"})
    assert r.returncode == 1 and "garbage-token-value" not in r.stdout + r.stderr


def test_edge_smoke_rejects_unknown_profiles():
    r = run_script("edge_smoke.py", "--profile", "listening_ultra", env={"TENMS_LICENSE": "x"})
    assert r.returncode == 2 and "invalid choice" in r.stderr


# -- publish guard (shared with the website build) ---------------------------------------------


@pytest.mark.skipif(shutil.which("node") is None or not SYNC_DOCS.exists(), reason="needs node and website/")
def test_publish_guard_passes_on_every_skill_file():
    # Every file that may be published, including this test file. (The guard's own probes for the hidden
    # terms live in website/scripts/sync-docs.test.mjs, which is never published.)
    files = [p for p in SKILLS.rglob("*") if p.is_file() and "__pycache__" not in p.parts and ".pytest_cache" not in p.parts]
    assert len(files) >= 12
    js = (
        "const m = await import(process.argv[1]);"
        "const fs = await import('node:fs');"
        "let bad = [];"
        "for (const f of process.argv.slice(2)) { const h = m.publishHit(fs.readFileSync(f, 'utf8'));"
        " if (h) bad.push(`${f}:${h.line} ${h.re}`); }"
        "console.log(JSON.stringify(bad));"
    )
    r = subprocess.run(["node", "--input-type=module", "-e", js, SYNC_DOCS.as_uri(), *map(str, files)],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == []
