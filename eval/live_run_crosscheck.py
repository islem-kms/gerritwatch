"""Correctness check of the live run (Section III.B of the GerritWatch paper).

Lists from Gerrit every change updated since the run started (any status, so changes
merged or abandoned during the run are included), with all revisions and commit messages.
For each patchset uploaded during the run, it compares the cleaned description with the
previous patchset's, using the monitor's own cleaning and edit distance, and reports
whether the monitor's event log has a revision for that change.

Only reads public Gerrit data.

    python eval/live_run_crosscheck.py
"""
import json
import subprocess
import sys
import time
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
from monitor_open_changes import clean_message, levenshtein  # noqa: E402

RUN = DATA / "live_run"
OUT = RUN / "live_run_crosscheck.json" if RELEASE else Path(__file__).with_name("live_run_crosscheck.json")
START = "2026-11-10 22:50:17"  # UTC, Gerrit's timestamp format
INSTANCES = {
    "onap": ("https://gerrit.onap.org/r", ""),
    "libreoffice": ("https://gerrit.libreoffice.org", ""),
    "wikimedia": ("https://gerrit.wikimedia.org/r", " project:mediawiki/core"),
}
UA = "GerritWatch-crosscheck/0.1 (research; read-only)"


def get(url):
    out = subprocess.run(["curl", "-s", "-A", UA, url], capture_output=True, text=True, check=True).stdout
    return json.loads(out[4:])  # strip Gerrit's )]}' prefix


def main():
    start = datetime.strptime(START, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    hours = int((datetime.now(timezone.utc) - start).total_seconds() // 3600) + 1
    result = {}
    for name, (base, extra) in INSTANCES.items():
        logged = {}
        for line in open(RUN / f"events_{name}.jsonl", encoding="utf-8"):
            e = json.loads(line)
            if e["type"] == "edit":
                logged.setdefault(str(e["change_number"]), []).append((e["old_patchset"], e["new_patchset"]))
        changes, offset = [], 0
        while True:
            q = quote(f"-age:{hours}h{extra}", safe=":/-")
            page = get(f"{base}/changes/?q={q}&o=ALL_REVISIONS&o=ALL_COMMITS&n=200&S={offset}")
            changes += page
            if not page or not page[-1].get("_more_changes"):
                break
            offset += len(page)
            time.sleep(3)
        new_ps, revisions = 0, []
        for c in changes:
            revs = sorted(c["revisions"].values(), key=lambda r: r["_number"])
            for prev, cur in zip(revs, revs[1:]):
                if cur["created"][:19] < START:
                    continue
                new_ps += 1
                a, b = clean_message(prev["commit"]["message"]), clean_message(cur["commit"]["message"])
                if a != b:
                    num = str(c["_number"])
                    revisions.append({
                        "change": num, "status": c["status"], "patchsets": f'{prev["_number"]}-{cur["_number"]}',
                        "uploaded": cur["created"][:19], "edit_distance": levenshtein(a, b),
                        "in_event_log": num in logged, "logged_pairs": logged.get(num, []),
                    })
        result[name] = {"changes_updated": len(changes), "patchsets_uploaded": new_ps,
                        "description_revisions": revisions}
        print(name, "changes updated:", len(changes), "patchsets uploaded during run:", new_ps)
        for r in revisions:
            print("   ", r)
        time.sleep(3)
    OUT.write_text(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
