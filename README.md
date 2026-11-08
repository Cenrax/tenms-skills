# Tenms Agent Skills

An [Agent Skill](https://agentskills.io) that teaches coding agents (Claude Code, Cursor, Codex and others) to
add [Tenms](https://tenms.cloud) to a voice agent correctly. Tenms is a CPU-only audio layer with three
modules: Focus (noise, echo and other-speaker removal), Turn (turn-taking, barge-in) and Pulse (call quality).

Ask your agent, for example:

> Add Tenms to my Pipecat bot.
> Put Tenms noise suppression in front of my LiveKit agent.
> Route my ElevenLabs agent through Tenms. The key must not reach the browser.
> Why does Tenms Edge close my socket with profile_capacity?

| Path | What it is |
|---|---|
| [`tenms/SKILL.md`](tenms/SKILL.md) | the workflow the agent follows |
| [`tenms/references/`](tenms/references/) | Pipecat, LiveKit, ElevenLabs, raw Edge WebSocket, Focus profiles, events and summary, licensing, troubleshooting |
| [`tenms/scripts/check_license.py`](tenms/scripts/check_license.py) | verifies a key offline (and optionally online), never prints it |
| [`tenms/scripts/edge_smoke.py`](tenms/scripts/edge_smoke.py) | 5-second synthetic call against `wss://edge.tenms.cloud` |
| [`tests/`](tests/) | checks that every example resolves against the published `tenms` / `livekit-plugins-tenms` |

## Install

### Claude Code

For your user (every project):

```bash
mkdir -p ~/.claude/skills
curl -fsSL https://tenms.cloud/skills/tenms.zip -o /tmp/tenms-skill.zip
unzip -o /tmp/tenms-skill.zip -d ~/.claude/skills/        # creates ~/.claude/skills/tenms/SKILL.md
```

For one project (commit it, so the whole team gets it):

```bash
mkdir -p .claude/skills
curl -fsSL https://tenms.cloud/skills/tenms.zip -o /tmp/tenms-skill.zip
unzip -o /tmp/tenms-skill.zip -d .claude/skills/
```

From this repository:

```bash
git clone https://github.com/Cenrax/tenms-skills
cp -R tenms-skills/tenms ~/.claude/skills/        # or .claude/skills/ in a project
```

Claude Code picks the skill up on its next start. Ask it to add Tenms, and it loads the skill when the task
matches.

### Other agents

* **Agents that read Agent Skills folders.** Copy `tenms/` into the agent's skills directory.
* **Cursor, Windsurf, Copilot and similar.** Add a project rule that points at the skill, for example
  "When integrating Tenms, follow https://tenms.cloud/skills/tenms/SKILL.md and its references", or paste
  `SKILL.md` into the rule.
* **Any agent with web access.** https://tenms.cloud/llms.txt indexes the docs, and
  https://tenms.cloud/llms-full.txt has all integration docs in one file.

## What the agent needs from you

A Tenms key. Create one at https://app.tenms.cloud (Projects → API keys) and put it in the server environment
as `TENMS_LICENSE`. The skill tells the agent never to put it in client code. Browsers and devices get
short-lived session tokens.

## Development

```bash
uv venv /tmp/tenms-skill-venv --python 3.12
uv pip install --python /tmp/tenms-skill-venv/bin/python \
    "tenms[edge,pipecat,livekit]==0.1.1" "livekit-plugins-tenms==0.1.1" pytest
/tmp/tenms-skill-venv/bin/python -m pytest tests -q
```

The tests parse every Python example and check its imports and Tenms attribute names against the installed
packages. They also check the frontmatter, links and anchors, and profile and event names, and they run the
scripts without credentials. With a key, try the scripts live:

```bash
TENMS_LICENSE=... python tenms/scripts/check_license.py --online
TENMS_LICENSE=... python tenms/scripts/edge_smoke.py --profile stt
```

## License

The skill (its text, examples and scripts) is MIT licensed: see [LICENSE](LICENSE). Tenms itself, meaning
the `tenms` packages, Tenms Edge and the control plane, is a proprietary product and needs a license key.
The MIT license here does not cover it.
