"""Adapter: OpenAI models through the Codex command line (ChatGPT login or API key, configured in Codex).

Reads the prompt on stdin, prints the model's answer. Use with --cmd, e.g.
  python validate/id_audit.py --cmd "python validate/adapters/codex_cli.py gpt-6.1-sol medium" --tag _gpt
Arguments: MODEL [EFFORT]. Runs ephemeral, read-only sessions with the user's Codex config ignored.
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

model = sys.argv[1] if len(sys.argv) > 1 else "gpt-6.1-sol"
effort = sys.argv[2] if len(sys.argv) > 2 else "medium"
js = Path(os.environ.get("CODEX_JS", Path(os.environ.get("APPDATA", "")) / "npm/node_modules/@openai/codex/bin/codex.js"))
base = ["node", str(js)] if js.exists() else [shutil.which("codex") or "codex"]
args = base + ["exec", "--json", "--ephemeral", "--ignore-user-config", "--skip-git-repo-check", "-s", "read-only",
               "-m", model, "-c", f'model_reasoning_effort="{effort}"', "-"]
p = subprocess.run(args, input=sys.stdin.read(), capture_output=True, text=True, encoding="utf-8", errors="replace")
out = []
for line in p.stdout.splitlines():
    try:
        ev = json.loads(line)
    except ValueError:
        continue
    if ev.get("type") == "item.completed" and (ev.get("item") or {}).get("type") == "agent_message":
        out.append(ev["item"].get("text", ""))
    elif ev.get("type") in ("turn.failed", "error"):
        sys.stderr.write(json.dumps(ev)[:500])
        sys.exit(1)
if not out:
    sys.stderr.write(p.stderr[-500:])
    sys.exit(1)
print("\n".join(out))
