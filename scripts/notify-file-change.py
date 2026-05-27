#!/usr/bin/env python3
"""PostToolUse hook: notify Isabelle-MCP server about file changes."""
import json
import sys
import urllib.request

try:
    event = json.load(sys.stdin)
    file_path = event.get("tool_input", {}).get("file_path", "")
    if file_path:
        req = urllib.request.Request(
            "http://127.0.0.1:8371/notify-file-change",
            data=json.dumps({"file_path": file_path}).encode(),
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=2)
except Exception:
    pass
