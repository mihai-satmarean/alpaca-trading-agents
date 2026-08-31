#!/usr/bin/env python3
"""Delegate a generation task to the Dell4 cluster instead of a local model.

Orchestration and review stay wherever you are; the code generation happens on
Mihai's GPUs and is not billed to anyone's Claude subscription.

  ./scripts/dell4-gen.py --spec spec.md --out src/core/mcp_client.py
  ./scripts/dell4-gen.py --spec spec.md            # print to stdout
"""
from __future__ import annotations

import argparse, json, os, pathlib, re, sys, time, urllib.request

BASE = os.environ.get("DELL4_BASE", "http://100.69.81.102:4000")
KEY = os.environ.get("LITELLM_KEY") or os.environ.get("OPENAI_API_KEY")

SYSTEM = (
    "You are a senior Python engineer. Output ONLY the requested file contents. "
    "No prose, no explanation, no markdown fences. Python 3.12, standard library "
    "unless the spec says otherwise. Type hints on public functions. Docstrings "
    "that say why, not what. Follow the spec exactly; do not invent extra features."
)


def _load_key() -> str:
    if KEY:
        return KEY
    env = pathlib.Path(__file__).resolve().parents[1] / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith("OPENAI_API_KEY="):
                return line.split("=", 1)[1].strip()
    sys.exit("No key. Set LITELLM_KEY or put OPENAI_API_KEY in .env")


def generate(spec: str, model: str, max_tokens: int, temperature: float) -> tuple[str, dict]:
    body = json.dumps({
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": spec},
        ],
    }).encode()
    req = urllib.request.Request(
        f"{BASE}/v1/chat/completions", data=body,
        headers={"Authorization": f"Bearer {_load_key()}", "Content-Type": "application/json"},
    )
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=600) as r:
        payload = json.load(r)
    text = payload["choices"][0]["message"]["content"]
    usage = payload.get("usage", {})
    usage["seconds"] = round(time.time() - t0, 1)
    return strip_fences(text), usage


def strip_fences(t: str) -> str:
    """Models add fences despite instructions. Take the largest fenced block if present."""
    blocks = re.findall(r"```(?:python|py)?\n(.*?)```", t, re.S)
    if blocks:
        return max(blocks, key=len).strip() + "\n"
    return t.strip() + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", required=True, help="file containing the spec")
    ap.add_argument("--out", help="write here; default stdout")
    ap.add_argument("--model", default="dell4-coder")
    ap.add_argument("--max-tokens", type=int, default=8192)
    ap.add_argument("--temperature", type=float, default=0.1)
    args = ap.parse_args()

    spec = pathlib.Path(args.spec).read_text()
    code, usage = generate(spec, args.model, args.max_tokens, args.temperature)

    if args.out:
        p = pathlib.Path(args.out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(code)
        print(f"wrote {p} ({len(code.splitlines())} lines)", file=sys.stderr)
    else:
        print(code)
    print(
        f"[{args.model}] in={usage.get('prompt_tokens')} out={usage.get('completion_tokens')} "
        f"{usage.get('seconds')}s  (billed to the cluster, not your subscription)",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
