"""Adapter: any OpenAI-compatible chat endpoint (hosted APIs, enterprise AI gateways, local open-weight servers
such as vLLM or Ollama). Standard library only.

Reads the prompt on stdin, prints the answer. Configure with environment variables:
  OR_BASE_URL   e.g. https://api.openai.com/v1  or  http://localhost:11434/v1
  OR_MODEL      the model name the endpoint expects
  OR_API_KEY    optional; sent as a Bearer token if set (never stored by this tool)
Use with --cmd "python validate/adapters/openai_compatible.py".
"""
import json
import os
import sys
import urllib.request

base = os.environ["OR_BASE_URL"].rstrip("/")
body = {"model": os.environ["OR_MODEL"], "temperature": 0,
        "messages": [{"role": "user", "content": sys.stdin.read()}]}
req = urllib.request.Request(base + "/chat/completions", data=json.dumps(body).encode("utf-8"),
                             headers={"Content-Type": "application/json"})
if os.environ.get("OR_API_KEY"):
    req.add_header("Authorization", "Bearer " + os.environ["OR_API_KEY"])
with urllib.request.urlopen(req, timeout=600) as r:
    print(json.load(r)["choices"][0]["message"]["content"])
