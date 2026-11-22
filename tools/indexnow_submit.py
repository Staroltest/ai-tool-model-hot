#!/usr/bin/env python3
"""IndexNow incremental submit for ai-tool-model-hot (DRAFT - not yet in repo).

Reads the queue written by tools/generate.py (data/indexnow_queue.json),
POSTs new-item URLs to the IndexNow endpoint, and clears the queue on success.

- Only submits URLs on the canonical host ai-tool-model-hot.pages.dev.
- Queue is capped at 40 URLs by generate.py (20 new items x zh + en).
- 200 = accepted, 202 = accepted while the key file is being verified.
  Either way the submission was received; indexing itself takes 1-2 weeks
  and is NOT confirmed by the response code.
- On any other status / network error: exit non-zero, queue file is left
  intact so the next half-hourly round retries it.
- stdlib only. No secrets on the command line: key comes from config.json.
"""
import json
import os
import sys
import urllib.request
import urllib.error

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
    req = urllib.request.Request(ENDPOINT, data=body,
                                 headers={"Content-Type": "application/json; charset=utf-8"},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            status = resp.status
    except urllib.error.HTTPError as e:
        print(f"indexnow: HTTP {e.code} for {len(urls)} urls, queue kept", file=sys.stderr)
        return 1
    except Exception as e:  # network error etc.
        print(f"indexnow: {e}, queue kept", file=sys.stderr)
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
