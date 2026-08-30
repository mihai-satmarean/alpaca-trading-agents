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

## 4. Claude Code: which model runs where

Two models, split by the kind of work, because they are not interchangeable.

**Opus stays the default everywhere, including inside this repo.** Diagnosis is
where the cluster models are weakest, and it is where a wrong answer is most
expensive: the defects fixed in #4, #7 and #8 were all cases where the code did
something other than what it claimed, which is a class of bug you find by
reading for actual behaviour rather than intent.

**The cluster is opt-in per session**, via a shell function rather than a
directory setting. Routing by directory means a session opened in this repo to
debug something silently gets the weaker model. Opt-in inverts that: forgetting
costs tokens instead of costing you a missed bug.

Add to `~/.zshrc` (see `scripts/dell4-shell.zsh` in this repo):

```bash
claude                  # Opus, anywhere, including here
dell4                   # Qwen3-Coder-30B on the cluster
dell4-devstral          # Devstral-24B, better at multi-file edits, slower
```

`dell4` checks the proxy is reachable before launching and tells you to connect
Tailscale if it is not, rather than failing inside the session.

| Work | Run it on |
|---|---|
| Root-causing behaviour, reading code for what it does | `claude` (Opus) |
| Reviewing a diff for correctness | `claude` (Opus) |
| Writing tests against an agreed spec | `dell4` |
| MCP/CLI adapter, boilerplate, config plumbing | `dell4` |
| Streamlit dashboard, presentation assets | `dell4` |
| Multi-file refactor with a known shape | `dell4-devstral` |

Subagents inherit the session's routing, so a `dell4` session dispatches
subagents to the cluster and an Opus session dispatches them to Opus. There is
no mixing within one session; use two terminals.

`scripts/use-dell4.sh on` still exists to flip the whole directory to the
cluster if you want that, but the shell function is the better default.

### Model notes

| Model | Notes |
|---|---|
| `dell4-coder` | Qwen3-Coder-30B, 3B active. Fast, 256K context. |
| `dell4-devstral-cc` | Devstral-24B. ~68% SWE-bench vs ~50% for the coder model. Use the **`-cc` alias**; the bare name returns 400s under Claude Code. |
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
