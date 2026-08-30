# Local Setup — ProductAdvisors / Alpaca Hackathon

Everything below is per-developer. Nothing here is committed except this file
and the two helper scripts; keys live in `.env` and `.claude/settings.local.json`,
both gitignored.

## 1. VPN (required first)

The Dell4 LLM cluster sits on a Tailscale tailnet. `100.69.81.102` is not
routable without it.

```bash
brew install --cask tailscale-app     # or download from tailscale.com/download
open -a Tailscale                     # sign in via the menu bar icon
```

Then open Mihai's tailnet invite link and accept it. Verify:

```bash
/Applications/Tailscale.app/Contents/MacOS/Tailscale status
```

## 2. Python environment

```bash
uv venv --python 3.12
uv pip install -e ".[dev]"
uv run pytest                          # expect 22 passed
```

## 3. Environment variables

Copy `.env.example` to `.env` and fill in:

- `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` — paper account keys from alpaca.markets
- `OPENAI_API_KEY` — the shared LiteLLM virtual key from Mihai
- `OPENAI_BASE_URL` — `http://100.69.81.102:4000/v1`

Load it into your shell with:

```bash
source scripts/dell4-env.sh
```

## 4. Claude Code against the Dell4 cluster

The Dell4 env vars live in `.claude/settings.local.json`, **project-scoped on
purpose** — sessions started in any other directory keep using the normal
Anthropic API. Do not move these into `~/.claude/settings.json`.

Pointing Claude Code at the cluster while the VPN is down makes it fail in this
repo, so the config ships **parked** at `.claude/settings.dell4.json` and is
activated by a toggle that first checks the proxy actually answers:

```bash
./scripts/use-dell4.sh          # report current state
./scripts/use-dell4.sh on       # -> Dell4 (refuses if the proxy is unreachable)
./scripts/use-dell4.sh off      # -> back to the normal Anthropic API
```

Restart any Claude Code session in this repo after toggling.

Model choice:

| Model | Notes |
|---|---|
| `dell4-coder` | Qwen3-Coder-30B. Works with Claude Code today. Fast, 256K context. |
| `dell4-devstral-cc` | Devstral-24B. Higher accuracy (~68% vs ~50% SWE-bench). Use the **`-cc` alias**, which reorders messages for Claude Code; the bare `dell4-devstral` returns 400s. |
| `dell4-chat` | Qwen3.6-35B, general reasoning. |
| `dell4-fast` | Qwen3.5-9B. OpenAI-compatible clients only. |

### Joining the right tailnet

If your Tailscale account already has its own tailnet, the macOS client binds
the device to **your own** tailnet, not the one you were invited to. Accepting
the invite adds your *account*; it does not move the *device*. The console
tailnet switcher and logout/login do not change this, and `--auth-key` is the
only flag that targets a specific tailnet. Symptom: `tailscale status` shows
only your own node and `tailscale ping 100.69.81.102` says `no matching peer`.

Fix, from the tailnet admin: an auth key generated in *their* tailnet
(`tailscale up --auth-key=tskey-...`), or **Share…** on the LLM node from their
Machines page.

## 5. Verify the whole path

```bash
./scripts/check-dell4.sh
```

Checks VPN → proxy → model list → a real chat completion.
