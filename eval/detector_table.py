"""Table I of the GerritWatch paper: expected accuracy of each detector setting.

Reads the DescEvo benchmark and the GPT-4o-mini few-shot run (no API calls) and scores,
on the 379-pair evaluation set (in_prompt_v1 == 0):
  - edit distance > t, for several t (the monitor's --min-distance)
  - edit distance > t AND GPT-4o-mini says semantic (--llm, decide mode)
It also counts how many of the t = 20 errors GPT-4o-mini labels correctly.

    python eval/detector_table.py
"""
import csv
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
if (HERE / "data" / "descevo").is_dir():   # release layout: copies of the three DescEvo files
    D = HERE / "data" / "descevo"
    BENCH, LLM, SWEEP = D / "benchmark.csv", D / "gpt-4o-mini-2024-07-18__v1_final.csv", D / "edit_distance_sweep.csv"
    OUT = D.parent / "detector_table.csv"
else:                                       # research repo: DescEvo's experiments/cc
    CC = HERE.parent / "experiments" / "cc"
    BENCH = CC / "data" / "benchmark.csv"
    LLM = CC / "runs" / "benchmark" / "gpt-4o-mini-2024-07-18__v1_final.csv"
    SWEEP = CC / "runs" / "edit_distance_sweep.csv"
    OUT = Path(__file__).with_name("detector_table.csv")
ROOT = HERE
SEM = "semantic"
THRESHOLDS = [0, 10, 20, 30, 40]

csv.field_size_limit(sys.maxsize)


def prf(gold, pred):
    tp = sum(g == SEM and p == SEM for g, p in zip(gold, pred))
    fp = sum(g != SEM and p == SEM for g, p in zip(gold, pred))
    fn = sum(g == SEM and p != SEM for g, p in zip(gold, pred))
    pr = tp / (tp + fp) if tp + fp else 0.0
    rc = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * pr * rc / (pr + rc) if pr + rc else 0.0
    return pr, rc, f1, tp + fp


def main():
    bench = [r for r in csv.DictReader(open(BENCH, encoding="utf-8")) if r["in_prompt_v1"] == "0"]
    llm = {r["pair_id"]: r["label"] for r in csv.DictReader(open(LLM, encoding="utf-8"))}
    sweep = {int(r["threshold"]): r for r in csv.DictReader(open(SWEEP, encoding="utf-8"))}
    gold = [r["final_label"] for r in bench]
    ed = [float(r["edit_distance"]) for r in bench]
    lab = [llm[r["pair_id"]] for r in bench]

    rows = []
    for use_llm in (False, True):
        for t in THRESHOLDS:
            pred = [SEM if d > t and (not use_llm or l == SEM) else "non-semantic"
                    for d, l in zip(ed, lab)]
            pr, rc, f1, flags = prf(gold, pred)
            if not use_llm:  # must reproduce DescEvo's own sweep
                s = sweep[t]
                assert (f"{pr:.3f}", f"{rc:.3f}", f"{f1:.3f}") == (s["precision"], s["recall"], s["f1"]), t
            rows.append({"min_distance": t, "llm": int(use_llm), "flags": flags,
                         "precision": f"{pr:.3f}", "recall": f"{rc:.3f}", "f1": f"{f1:.3f}"})

    with open(OUT, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)

    errors = [i for i, d in enumerate(ed) if (SEM if d > 20 else "non-semantic") != gold[i]]
    fixed = sum(lab[i] == gold[i] for i in errors)
    print(f"evaluation pairs: {len(bench)}")
    for r in rows:
        print(r)
    print(f"t = 20 errors: {len(errors)}; GPT-4o-mini correct on {fixed} of them")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
