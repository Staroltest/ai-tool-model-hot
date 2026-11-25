#!/usr/bin/env python3
"""IndexNow incremental submit for ai-tool-model-hot.

Reads the queue written by tools/generate.py (data/indexnow_queue.json),
POSTs new-item URLs to the IndexNow endpoint, and clears the queue on success.

- Only submits URLs on the canonical host ai-tool-model-hot.pages.dev.
- Queue is capped at 1000 URLs by generate.py (retention buffer while
  submission is blocked; the IndexNow protocol allows 10000 per submit).
- 200 = accepted, 202 = accepted while the key file is being verified.
  Either way the submission was received; indexing itself takes 1-2 weeks
  and is NOT confirmed by the response code.
- On any other status / network error: exit non-zero, queue file is left
  intact so the next half-hourly round retries it.
- POST goes through a curl subprocess: urllib is unreliable through this
  VM's egress proxy (same reason tools/hf_adapter.py uses curl).
  No secrets on the command line: key comes from config.json.
"""
import json
import os
import subprocess
import sys

REPO = os.path.expanduser("~/workspace/ai-tool-model-hot")
ENDPOINT = os.environ.get("INDEXNOW_ENDPOINT", "https://api.indexnow.org/indexnow")


def main():
    with open(os.path.join(REPO, "config.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    key = (cfg.get("indexnow_key") or "").strip()
    if not key:
        print("indexnow: no indexnow_key in config.json, skipping", file=sys.stderr)
        return 0
    host = cfg.get("url", "https://ai-tool-model-hot.pages.dev").split("://", 1)[-1].rstrip("/")
    queue_path = os.path.join(REPO, "data", "indexnow_queue.json")
    try:
        with open(queue_path, encoding="utf-8") as f:
            urls = json.load(f)
    except FileNotFoundError:
        return 0
    urls = sorted({u for u in urls if isinstance(u, str) and u.startswith("https://" + host + "/")})
    if not urls:
        with open(queue_path, "w", encoding="utf-8") as f:
            json.dump([], f)
        return 0
    body = json.dumps({
        "host": host,
        "key": key,
        "keyLocation": f"https://{host}/{key}.txt",
        "urlList": urls,
    }).encode("utf-8")
    try:
        proc = subprocess.run(
            ["curl", "-sS", "-o", "/dev/null", "-w", "%{http_code}",
             "--max-time", "60",
             "-X", "POST", ENDPOINT,
             "-H", "Content-Type: application/json; charset=utf-8",
             "--data-binary", "@-"],
            input=body, capture_output=True, timeout=90)
    except Exception as e:  # curl spawn / timeout etc.
        print(f"indexnow: {e}, queue kept", file=sys.stderr)
        return 1
    if proc.returncode != 0:
        print(f"indexnow: curl failed ({proc.returncode}): "
              f"{proc.stderr.decode(errors='replace').strip()}, queue kept",
              file=sys.stderr)
        return 1
    try:
        status = int(proc.stdout.decode().strip())
    except ValueError:
        print("indexnow: no HTTP status from curl, queue kept", file=sys.stderr)
        return 1
    print(f"indexnow: HTTP {status} for {len(urls)} urls")
    if status in (200, 202):
        with open(queue_path, "w", encoding="utf-8") as f:
            json.dump([], f)
        return 0
    print(f"indexnow: unexpected HTTP {status}, queue kept", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
