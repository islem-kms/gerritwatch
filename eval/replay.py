"""Replay of 30 days of Gerrit history through GerritWatch's detector (Section III of the paper).

The live run is short and fell at night, so it shows reliability, not alert volume. This
replay applies the detector to 30 days of recorded history on the same three scopes (ONAP and LibreOffice: all repositories; Wikimedia: mediawiki/core).
It reuses the monitor's own cleaning, edit distance, and LLM call.

Steps (run in order; each writes to replay/, or data/replay/ in the release):
    python eval/replay.py fetch   # Gerrit REST API, all revisions + commit messages
    python eval/replay.py pairs   # every description revision recorded in the window
    python eval/replay.py llm     # GPT-4o-mini on each revision (needs OPENAI_API_KEY)
    python eval/replay.py stats   # per-setting counts, alerts per day, labeling sample

No polling is simulated: a revision is a pair of consecutive patchsets whose cleaned
descriptions differ (the DescEvo unit). The live run measures what polling merges.
"""
import csv
import json
import os
import random
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
# Two layouts: the research repo (tool_paper/: tool/backend, live_run/, replay/) and the
# release (backend/, data/live_run/, data/replay/).
RELEASE = (ROOT / "backend").is_dir()
BACKEND = ROOT / "backend" if RELEASE else ROOT / "tool" / "backend"
DATA = ROOT / "data" if RELEASE else ROOT
sys.path.insert(0, str(BACKEND))
import monitor_open_changes as mon  # noqa: E402

OUT = DATA / "replay"
W0 = datetime(2026, 10, 11, tzinfo=timezone.utc)   # window: 30 days before the live run
W1 = datetime(2026, 11, 10, tzinfo=timezone.utc)
POLL = 300
T = 20
UA = "GerritWatch-replay/0.1 (research; read-only)"
INSTANCES = {
    "onap": ("https://gerrit.onap.org/r", "", 200, 1.0),
    "libreoffice": ("https://gerrit.libreoffice.org", "", 50, 1.0),
    "wikimedia": ("https://gerrit.wikimedia.org/r", " project:mediawiki/core", 100, 5.0),
}
BOT = re.compile(r"bot\b|bot\]|dependabot|renovate|jenkins|\bgha\b|github actions|github2gerrit|jobbuilder|"
                 r"weblate|translat|l10n|automation|\bci\b", re.I)
SAMPLE = 60
SEED = 2027


def gtime(s):
    return datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def get(url):
    for attempt in range(6):
        r = subprocess.run(["curl", "-s", "-w", "\n%{http_code}", "-A", UA, url], capture_output=True, text=True)
        body, _, code = r.stdout.rpartition("\n")
        if code == "200":
            return json.loads(body[4:])
        time.sleep(10 * (attempt + 1))
    raise RuntimeError(f"HTTP {code} for {url}")


def fetch():
    OUT.mkdir(exist_ok=True)
    days = (datetime.now(timezone.utc) - W0).days + 1
    for name, (base, extra, n, pause) in INSTANCES.items():
        if (OUT / f"raw_{name}.json").exists():
            print(f"{name}: already fetched")
            continue
        changes, offset = [], 0
        while True:
            q = quote(f"-age:{days}d{extra}", safe=":/-")
            page = get(f"{base}/changes/?q={q}&o=ALL_REVISIONS&o=ALL_COMMITS&o=DETAILED_ACCOUNTS&n={n}&S={offset}")
            changes += page
            print(f"{name}: {len(changes)} changes", flush=True)
            if not page or not page[-1].get("_more_changes"):
                break
            offset += len(page)
            time.sleep(pause)
        slim = [{"number": c["_number"], "project": c["project"], "status": c["status"],
                 "created": c["created"], "updated": c["updated"], "submitted": c.get("submitted"),
                 "owner": (c.get("owner") or {}).get("name", ""),
                 "revisions": sorted(({"ps": r["_number"], "created": r["created"],
                                       "uploader": (r.get("uploader") or {}).get("name", ""),
                                       "message": r["commit"]["message"]}
                                      for r in c["revisions"].values()), key=lambda r: r["ps"])}
                for c in changes]
        (OUT / f"raw_{name}.json").write_text(json.dumps(slim))


def pairs():
    """Every description revision recorded by Gerrit in the window: consecutive patchsets
    (i, i+1) with the newer one uploaded in [W0, W1) and different cleaned descriptions."""
    for name, (base, *_rest) in INSTANCES.items():
        if not (OUT / f"raw_{name}.json").exists():
            print(f"{name}: not fetched yet")
            continue
        changes = json.loads((OUT / f"raw_{name}.json").read_text())
        out, uploaders, n_ps = [], {}, 0
        for c in changes:
            revs = c["revisions"]
            for a, b in zip(revs, revs[1:]):
                if not (W0 <= gtime(b["created"]) < W1):
                    continue
                n_ps += 1
                old, new = mon.clean_message(a["message"]), mon.clean_message(b["message"])
                if old == new:
                    continue
                up = b["uploader"] or c["owner"]
                bot = bool(BOT.search(up))
                uploaders.setdefault(up, [bot, 0])[1] += 1
                out.append({
                    "pair_id": f"{name}|{c['number']}|{a['ps']}-{b['ps']}", "instance": name,
                    "change": c["number"], "project": c["project"], "old_ps": a["ps"], "new_ps": b["ps"],
                    "uploaded": b["created"][:16], "uploader": up, "bot": bot,
                    "edit_distance": mon.levenshtein(old, new),
                    "url": f"{base}/c/{c['project']}/+/{c['number']}",
                    "old_description": old, "new_description": new})
        (OUT / f"revisions_{name}.json").write_text(json.dumps(out, indent=1))
        with open(OUT / f"uploaders_{name}.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["uploader", "bot", "revisions"])
            for up, (bot, n) in sorted(uploaders.items(), key=lambda x: -x[1][1]):
                w.writerow([up, bot, n])
        print(f"{name}: changes fetched {len(changes)}, new patchsets in window {n_ps}, "
              f"description revisions {len(out)} in {len({r['change'] for r in out})} changes")


def llm():
    tmpl = mon.load_prompt(str(BACKEND / "Final_Prompt.txt"))
    for name in INSTANCES:
        if not (OUT / f"revisions_{name}.json").exists():
            print(f"{name}: no revisions file yet")
            continue
        revs = json.loads((OUT / f"revisions_{name}.json").read_text())
        cache_path = OUT / f"llm_{name}.jsonl"
        done = {}
        if cache_path.exists():
            for line in open(cache_path, encoding="utf-8"):
                e = json.loads(line)
                if e["llm"]["llm_prediction"] != "ERROR":
                    done[e["pair_id"]] = e
        todo = [r for r in revs if r["pair_id"] not in done]
        print(f"{name}: {len(done)} cached, {len(todo)} to classify", flush=True)

        def one(r):
            t0 = time.time()
            res = mon.classify_pair(tmpl, r["old_description"], r["new_description"])
            res["latency_s"] = round(time.time() - t0, 2)
            return {"pair_id": r["pair_id"], "llm": res}

        with ThreadPoolExecutor(4) as ex, open(cache_path, "a", encoding="utf-8") as f:
            for i, e in enumerate(ex.map(one, todo), 1):
                f.write(json.dumps(e) + "\n")
                f.flush()
                if i % 25 == 0:
                    print(f"  {i}/{len(todo)}", flush=True)


def stats():
    days = (W1 - W0).days
    rows, summary = [], {"window": [W0.isoformat(), W1.isoformat()], "days": days}
    for name in INSTANCES:
        revs = json.loads((OUT / f"revisions_{name}.json").read_text())
        lab = {}
        if (OUT / f"llm_{name}.jsonl").exists():
            for line in open(OUT / f"llm_{name}.jsonl", encoding="utf-8"):
                e = json.loads(line)
                lab[e["pair_id"]] = e["llm"]
        for r in revs:
            l = lab.get(r["pair_id"], {})
            r["llm"] = l.get("llm_prediction")
            r["llm_reason"] = l.get("reason", "")
            r["tokens"] = (l.get("usage") or {}).get("total_tokens")
        rows += revs
        s = {"revisions": len(revs), "bot_revisions": sum(r["bot"] for r in revs),
             "llm_labeled": sum(r["llm"] in ("KEEP", "REMOVE") for r in revs),
             "flags_every": len(revs),
             f"flags_t{T}": sum(r["edit_distance"] > T for r in revs),
             "flags_llm": sum(r["llm"] == "KEEP" for r in revs),
             f"flags_t{T}_llm": sum(r["edit_distance"] > T and r["llm"] == "KEEP" for r in revs)}
        lab_ok = [r for r in revs if r["llm"] in ("KEEP", "REMOVE")]
        s["disagree_t20_llm"] = sum((r["edit_distance"] > T) != (r["llm"] == "KEEP") for r in lab_ok)
        s["changes_with_revision"] = len({r["change"] for r in revs})
        s[f"changes_alerted_t{T}"] = len({r["change"] for r in revs if r["edit_distance"] > T})
        s[f"changes_alerted_t{T}_llm"] = len({r["change"] for r in revs if r["edit_distance"] > T and r["llm"] == "KEEP"})
        s["per_day"] = {k: round(v / days, 1) for k, v in s.items() if k.startswith("flags_")}
        tok = [r["tokens"] for r in revs if r["tokens"]]
        s["llm_tokens_mean"] = round(sum(tok) / len(tok)) if tok else None
        summary[name] = s
    keys = ["revisions", "bot_revisions", "flags_every", f"flags_t{T}", "flags_llm", f"flags_t{T}_llm",
            "changes_with_revision"]
    tot = {k: sum(summary[n][k] for n in INSTANCES) for k in keys}
    tot["per_day"] = {k: round(tot[k] / days, 1) for k in keys if k.startswith("flags_")}
    summary["total"] = tot
    (OUT / "replay_stats.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))

    # Blind labeling sample: random revisions, no detector output shown.
    rnd = random.Random(SEED)
    sample = rnd.sample(rows, min(SAMPLE, len(rows)))
    with open(OUT / "label_sample.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["pair_id", "url", "old_patchset", "new_patchset", "old_description", "new_description",
                    "label (semantic / non-semantic)", "note"])
        for r in sample:
            w.writerow([r["pair_id"], r["url"], r["old_ps"], r["new_ps"], r["old_description"],
                        r["new_description"], "", ""])
    with open(OUT / "label_sample_key.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["pair_id", "edit_distance", "llm", "bot", "uploader"])
        for r in sample:
            w.writerow([r["pair_id"], r["edit_distance"], r["llm"], r["bot"], r["uploader"]])
    print(f"wrote {OUT / 'label_sample.csv'} ({len(sample)} pairs)")


if __name__ == "__main__":
    {"fetch": fetch, "pairs": pairs, "llm": llm, "stats": stats}[sys.argv[1]]()
