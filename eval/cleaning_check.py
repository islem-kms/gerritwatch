"""Checks that the monitor cleans descriptions the same way as DescEvo (Section II-A).

Draws 40 random DescEvo pairs from ONAP and LibreOffice (seed 1), fetches the two commit
messages of each pair again from Gerrit, cleans them with the monitor's own code, and compares
the cleaned texts and the edit distance with DescEvo's released values. Pairs whose patchsets
can no longer be fetched (deleted or private changes) are reported as failed.

    python eval/cleaning_check.py [--pairs PATH_TO_DESCEVO_pairs.csv]

pairs.csv is DescEvo's released file (Zenodo, DOI 10.5281/zenodo.24094881); in the research
repo it is ../release/pairs.csv.
"""
import argparse
import csv
import json
import random
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend" if (ROOT / "backend").is_dir() else ROOT / "tool" / "backend"
sys.path.insert(0, str(BACKEND))
from monitor_open_changes import clean_message, levenshtein  # noqa: E402

BASE = {"onap": "https://gerrit.onap.org/r", "libreoffice": "https://gerrit.libreoffice.org"}
UA = "GerritWatch-check/0.1 (research; read-only)"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pairs", default=str(ROOT.parent / "release" / "pairs.csv"))
    ap.add_argument("-n", type=int, default=40)
    args = ap.parse_args()
    csv.field_size_limit(sys.maxsize)
    rows = [r for r in csv.DictReader(open(args.pairs, encoding="utf-8")) if r["project"] in BASE]
    sample = random.Random(1).sample(rows, args.n)
    fetched = same_text = same_ed = 0
    diffs = []
    for r in sample:
        out = []
        for ps in (r["old_patchset"], r["new_patchset"]):
            url = f"{BASE[r['project']]}/changes/{quote(r['change_id'], safe='~')}/revisions/{ps}/commit"
            body = subprocess.run(["curl", "-s", "-A", UA, url], capture_output=True, text=True).stdout
            try:
                out.append(clean_message(json.loads(body[4:])["message"]))
            except (ValueError, KeyError):
                out.append(None)
            time.sleep(0.3)
        if None in out:
            continue
        fetched += 1
        same_text += out[0] == r["old_description"] and out[1] == r["new_description"]
        ed = levenshtein(*out)
        same_ed += ed == int(r["edit_distance"])
        if ed != int(r["edit_distance"]):
            diffs.append((r["pair_id"], ed, r["edit_distance"]))
    print(f"sampled {args.n}, fetched {fetched}, same edit distance {same_ed}, same cleaned text {same_text}")
    for d in diffs:
        print("  different:", d)


if __name__ == "__main__":
    main()
