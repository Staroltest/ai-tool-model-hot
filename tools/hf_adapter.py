#!/usr/bin/env python3
"""Hugging Face 组织页适配器（AI工具与模型HOT）。

Qwen / DeepSeek / Kimi 没有可用官方 RSS，改轮询 Hugging Face 公开组织页
API（无需 key）：
  GET https://huggingface.co/api/models?author=<org>&sort=createdAt&direction=-1&limit=50
把符合条件的新模型转成与 RSS 条目同格式的入库项，写进 data/hf_pending.json，
由 tools/generate.py 在 RSS 拉取后按同一路径合入 data/store.json（去重、
排除词、分级/分类回填都一样走）。

过滤（2026-11-19，用户批准）：
- 只要 30 天内发布（config.json scoring.history_days），除非 --backfill
  指定的一次性例外；
- 跳过带 base_model:* 标签的派生/微调变体（Turbo 这类微调版都带它）；
- 同系列去重：去掉 -Turbo/-PE-I2I/-PE-T2I/-FP8/-GGUF/-AWQ/-GPTQ 等后缀后
  同属一个系列的，只留本体（无后缀的）；都没有本体时留最新的一条；
- 已入库（data/store.json）或已在 pending 里的跳过。

摘要：取模型 README 的第一段正文（截到 200 字，与 RSS 短摘要同规则），
只作逐字重合检查的参照与详情页导读；页面上显示的简介仍是
data/zh_draft.json 里的自写 zh_oneliner。

用法（在仓库根目录跑；每半小时更新先跑本脚本再跑 generate.py）：
  python3 tools/hf_adapter.py
  python3 tools/hf_adapter.py --backfill moonshotai/Kimi-K3.1-Pro deepseek-ai/DeepSeek-V4.1-Flash-1017
      # 一次性补回指定模型（无视 30 天窗口）；超窗条目的 id 同时写入
      # data/hf_exceptions.json，generate.py 淘汰旧条目时保留它们。
"""
import hashlib
import html
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
ORGS = [
    ("Qwen", "hf-qwen", "Qwen (Hugging Face)"),
    ("deepseek-ai", "hf-deepseek", "DeepSeek (Hugging Face)"),
    ("moonshotai", "hf-moonshotai", "Kimi (Hugging Face)"),
]
VARIANT_SUFFIXES = ("-turbo", "-pe-i2i", "-pe-t2i", "-fp8",
                    "-gguf", "-awq", "-gptq", "-int4", "-int8")
API_LIST = ("https://huggingface.co/api/models?author={org}"
            "&sort=createdAt&direction=-1&limit=50")
API_ONE = "https://huggingface.co/api/models/{model_id}"
README_RAW = "https://huggingface.co/{model_id}/raw/main/README.md"


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def curl(url, timeout=25):
    """curl 取回（urllib 走出口代理不稳定，API 一律用 curl，与 cf-deploy.py 一致）。"""
    r = subprocess.run(["curl", "-sS", "--max-time", str(timeout), url],
                       capture_output=True, text=True, timeout=timeout + 10)
    if r.returncode != 0:
        raise RuntimeError(f"curl {url} failed: {r.stderr.strip()[:120]}")
    return r.stdout


def strip_html(text):
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def shorten(text, limit=200):
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def norm_link(link):
    """与 generate.py 完全一致（去 utm_*，保留 fragment），否则去重 id 对不上。"""
    link = (link or "").strip()
    if "?" in link:
        base, qs = link.split("?", 1)
        kept = [p for p in qs.split("&") if p and not p.lower().startswith("utm_")]
        link = base + ("?" + "&".join(kept) if kept else "")
    return link.rstrip("/")


def item_id(link):
    return hashlib.sha1(norm_link(link).encode()).hexdigest()


def readme_first_para(model_id):
    """模型 README 第一段正文（去掉 frontmatter/徽章 HTML/图片链接/导航行）。
    实体先解码再量长度，否则导航行里的 &nbsp; 会把它撑过 60 字门槛。"""
    try:
        raw = curl(README_RAW.format(model_id=model_id))
    except Exception:
        return ""
    raw = re.sub(r"^---\n.*?\n---\n", "", raw, flags=re.S)
    raw = re.sub(r"<div.*?</div>", "", raw, flags=re.S)
    raw = re.sub(r"<[^>]+>", " ", raw)
    raw = re.sub(r"!\[.*?\]\(.*?\)", " ", raw)
    raw = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", raw)
    for para in raw.split("\n\n"):
        p = html.unescape(para)
        p = re.sub(r"\s+", " ", p).strip()
        p = p.lstrip("#*-> ").strip()
        p = re.sub(r"\*\*([^*]+)\*\*", r"\1", p)  # **加粗** 去标记
        p = p.replace("**", "")  # 行首 lstrip 掉开标记后剩下的闭标记
        p = re.sub(r"`([^`]+)`", r"\1", p)  # `代码` 去标记
        if len(p) <= 60:
            continue
        if "Tech Blog" in p or "API Quickstart" in p:
            continue  # 导航行，不是正文
        if p.startswith(("http", "---")):
            continue
        return shorten(p)
    return ""


def series_key(model_id):
    """同系列归组键：去掉变体后缀后的小写模型名（日期修订版不去掉，是新发布）。"""
    name = model_id.split("/", 1)[-1].lower()
    changed = True
    while changed:
        changed = False
        for suf in VARIANT_SUFFIXES:
            if name.endswith(suf):
                name = name[:-len(suf)]
                changed = True
    return name


def is_derived(model):
    """带 base_model:* 标签 = 派生/微调变体。"""
    return any(str(t).startswith("base_model:") for t in model.get("tags", []))


def to_entry(model, source_id, source_name):
    model_id = model["id"]
    link = f"https://huggingface.co/{model_id}"
    created = model.get("createdAt", "")
    try:
        pub = datetime.fromisoformat(created.replace("Z", "+00:00"))
        if pub.tzinfo is None:
            pub = pub.replace(tzinfo=timezone.utc)
    except ValueError:
        pub = datetime.now(timezone.utc)
    return {
        "title": model_id,
        "link": link,
        "source": source_name,
        "source_id": source_id,
        "published": pub.astimezone(timezone.utc).isoformat(),
        "summary": readme_first_para(model_id),
    }


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", nargs="*", default=[],
                    help="一次性补回指定模型 id（无视 30 天窗口）")
    args = ap.parse_args()
    cfg = load_json(os.path.join(ROOT, "config.json"))
    history_days = cfg["scoring"]["history_days"]
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=history_days)

    store_path = os.path.join(DATA, "store.json")
    stored_ids = set()
    if os.path.exists(store_path):
        try:
            for it in load_json(store_path).get("items", []):
                stored_ids.add(it["id"])
        except (json.JSONDecodeError, KeyError):
            pass
    pending_path = os.path.join(DATA, "hf_pending.json")
    pending = []
    pending_ids = set()
    if os.path.exists(pending_path):
        try:
            pending = load_json(pending_path) or []
            pending_ids = {item_id(e.get("link", "")) for e in pending}
        except (json.JSONDecodeError, AttributeError):
            pending, pending_ids = [], set()

    added, skipped = [], []

    def admit(model, source_id, source_name, why):
        entry = to_entry(model, source_id, source_name)
        eid = item_id(entry["link"])
        if eid in stored_ids or eid in pending_ids:
            skipped.append((model["id"], "已入库"))
            return None
        pending.append(entry)
        pending_ids.add(eid)
        added.append((model["id"], why))
        return eid

    if args.backfill:
        # 一次性补回：按 id 直取，不过 30 天窗口；超窗的记入例外清单。
        exc_path = os.path.join(DATA, "hf_exceptions.json")
        exceptions = set()
        if os.path.exists(exc_path):
            try:
                exceptions = set(load_json(exc_path) or [])
            except (json.JSONDecodeError, AttributeError):
                pass
        for model_id in args.backfill:
            org = model_id.split("/", 1)[0]
            match = [o for o in ORGS if o[0] == org]
            if not match:
                print(f"backfill 跳过 {model_id}：不在 Qwen/deepseek-ai/moonshotai 三个组织内",
                      file=sys.stderr)
                continue
            _, source_id, source_name = match[0]
            try:
                model = json.loads(curl(API_ONE.format(model_id=model_id)))
            except Exception as e:
                print(f"backfill {model_id} 拉取失败：{e}", file=sys.stderr)
                continue
            eid = admit(model, source_id, source_name, "backfill")
            if eid:
                try:
                    pub = datetime.fromisoformat(
                        model["createdAt"].replace("Z", "+00:00"))
                except (KeyError, ValueError):
                    pub = now
                if pub.replace(tzinfo=timezone.utc) < cutoff:
                    exceptions.add(eid)
                    print(f"backfill {model_id} 超 30 天窗口，已记入例外清单")
        save_json(exc_path, sorted(exceptions))
    else:
        for org, source_id, source_name in ORGS:
            try:
                models = json.loads(curl(API_LIST.format(org=org)))
            except Exception as e:
                print(f"{org} 拉取失败：{e}", file=sys.stderr)
                continue
            cands = []
            for m in models:
                try:
                    pub = datetime.fromisoformat(
                        m["createdAt"].replace("Z", "+00:00"))
                except (KeyError, ValueError):
                    continue
                if pub.replace(tzinfo=timezone.utc) < cutoff:
                    continue
                if is_derived(m):
                    continue
                cands.append(m)
            # 同系列去重：优先本体（无后缀），否则最新
            def _created_ts(m):
                try:
                    return datetime.fromisoformat(
                        m.get("createdAt", "").replace("Z", "+00:00")).timestamp()
                except ValueError:
                    return 0
            groups = {}
            for m in cands:
                groups.setdefault(series_key(m["id"]), []).append(m)
            for key, ms in groups.items():
                ms.sort(key=lambda m: (
                    series_key(m["id"]) != m["id"].split("/", 1)[-1].lower(),
                    -_created_ts(m)))
                best = ms[0]
                admit(best, source_id, source_name, f"新模型（{key}）")
            for m in cands:
                if all(m["id"] != a[0] for a in added) and \
                        item_id(f"https://huggingface.co/{m['id']}") not in pending_ids \
                        and item_id(f"https://huggingface.co/{m['id']}") not in stored_ids:
                    skipped.append((m["id"], "同系列已有本体"))

    if added:
        save_json(pending_path, pending)
    for mid, why in added:
        print(f"HF + {mid}（{why}）")
    for mid, why in skipped:
        print(f"HF - {mid}（{why}）")
    print(f"HF 适配器：新增 {len(added)} 条，跳过 {len(skipped)} 条，"
          f"pending 共 {len(pending)} 条")


if __name__ == "__main__":
    main()
