"""Live-run numbers for Section III.B of the GerritWatch paper.

Reads tool_paper/live_run/events_<instance>.jsonl and reports, per instance and in total:
open changes watched, polls, failed polls, poll duration, description revisions, flags under
four detector settings, delay from patchset upload to detection, LLM tokens and latency,
and the review messages posted before each revision.

The run starts at START, when the ONAP and LibreOffice monitors were launched (t = 0, LLM
called on every revision, record mode). They were restarted at 23:12:55 UTC with the LLM
deciding (state kept; no poll between 22:55:31 and 23:12:55), and Wikimedia started then.
Flags are recomputed here for every setting, so the mode at the time does not matter.

    python eval/live_run_stats.py
"""
import json
import statistics as st
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RELEASE = (ROOT / "backend").is_dir()  # release layout: data/live_run/
RUN = ROOT / "data" / "live_run" if RELEASE else ROOT / "live_run"
OUT = RUN / "live_run_stats.json" if RELEASE else Path(__file__).with_name("live_run_stats.json")
START = "2026-11-10T22:50:17Z"
END = "2026-11-11T05:50:17Z"    # the paper reports the first 7 hours; later events are ignored
INSTANCES = ["onap", "libreoffice", "wikimedia"]
T = 20  # recommended minimum edit distance


def ts(s):
    """Event timestamps are ISO with Z; Gerrit's are 'YYYY-MM-DD HH:MM:SS.nnnnnnnnn' in UTC."""
    if s.endswith("Z"):
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def semantic(e):
    return (e.get("llm") or {}).get("llm_prediction") == "KEEP"


def summarize(events):
    polls = [e for e in events if e["type"] == "poll"]
    edits = [e for e in events if e["type"] == "edit"]
    llm = [e["llm"] for e in edits if e.get("llm")]
    act = [e["reviewer_activity_before"] for e in edits if e.get("reviewer_activity_before")]
    return {
        "first_poll": polls[0]["ts"] if polls else None,
        "last_poll": polls[-1]["ts"] if polls else None,
        "polls": len(polls),
        "failed_polls": sum(e["type"] == "poll_failed" for e in events),
        "open_changes_min": min((p["open_changes"] for p in polls), default=None),
        "open_changes_max": max((p["open_changes"] for p in polls), default=None),
        "poll_seconds_median": round(st.median(p["duration_s"] for p in polls), 1) if polls else None,
        "poll_seconds_max": max((p["duration_s"] for p in polls), default=None),
        "revisions": len(edits),
        "flags_every_revision": len(edits),
        f"flags_t{T}": sum(e["edit_distance"] > T for e in edits),
        "flags_llm_only": sum(semantic(e) for e in edits),
        f"flags_t{T}_llm": sum(e["edit_distance"] > T and semantic(e) for e in edits),
        "flagged_in_run": sum(bool(e["flagged"]) for e in edits),
        "detection_delay_s": [round((ts(e["ts"]) - ts(e["patchset_created"])).total_seconds())
                              for e in edits if e.get("patchset_created")],
        "llm_calls": len(llm),
        "llm_tokens": [l["usage"]["total_tokens"] for l in llm if l.get("usage")],
        "llm_latency_s": [l["latency_s"] for l in llm if "latency_s" in l],
        "review_messages_before": [a["n_messages"] for a in act],
        "edits": [{"change": e["change_number"], "patchsets": f'{e["old_patchset"]}-{e["new_patchset"]}',
                   "edit_distance": e["edit_distance"], "llm": (e.get("llm") or {}).get("llm_prediction"),
                   "flagged": e["flagged"], "url": e["change_url"]} for e in edits],
    }


def main():
    result = {}
    for name in INSTANCES:
        events = [json.loads(l) for l in open(RUN / f"events_{name}.jsonl", encoding="utf-8")]
        events = [e for e in events if START <= e["ts"] < END]
        result[name] = summarize(events)
    total = {k: sum(result[n][k] for n in INSTANCES)
             for k in ("polls", "failed_polls", "revisions", "flags_every_revision", f"flags_t{T}",
                       "flags_llm_only", f"flags_t{T}_llm", "llm_calls")}
    total["open_changes_max"] = sum(result[n]["open_changes_max"] or 0 for n in INSTANCES)
    total["hours"] = round((ts(END) - ts(START)).total_seconds() / 3600, 1)
    result["total"] = total
    OUT.write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
