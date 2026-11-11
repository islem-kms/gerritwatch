"""
GerritWatch monitor: watches the open changes of a Gerrit instance and flags revised
change descriptions. It runs in a loop:

  1. Ask the Gerrit REST API for all open changes, with the current patchset and its
     commit message (the change description).
  2. Clean each description (remove lines that tools add to every patchset) and compare it with the
     description seen at the previous poll.
  3. If it changed, compute the character-level edit distance. A revision is flagged if
     the distance is above --min-distance and, with --llm, if GPT-4o-mini (with the
     few-shot prompt, Final_Prompt.txt) says the meaning changed.
  4. Write the flags to flags.json, which serve_cors.py passes to the browser extension,
     and append every revision and poll to events_<instance>.jsonl.
  5. Sleep, then repeat.

Usage (run serve_cors.py in the same folder):
    python monitor_open_changes.py onap
    python monitor_open_changes.py libreoffice --min-distance 20
    python monitor_open_changes.py wikimedia --project-filter mediawiki/core --min-distance 20 --llm

Options:
    --interval S       seconds between polls (default 900)
    --once             run one poll and exit
    --project-filter P only watch repository P
    --min-distance N   flag a revision only if its edit distance is > N (default 20, the best
                       setting on our labeled benchmark; 0 flags every revision)
    --llm              also ask GPT-4o-mini (needs OPENAI_API_KEY and Final_Prompt.txt)
    --llm-mode M       decide (default): only revisions the LLM labels semantic are flagged;
                       record: flag by distance alone and only log the LLM's answer

Files are written to the folder in ENRICH_DATA_DIR (default: the current folder).
ONAP, LibreOffice, and Wikimedia allow anonymous reads; set GERRIT_HTTP_USERNAME and
GERRIT_HTTP_PASSWORD only for an instance that requires authentication.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Optional

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

# ================================================================================
# CONFIG
# ================================================================================

PROJECTS: dict[str, dict] = {
    "onap": {
        "base_url": "https://gerrit.onap.org/r",
        "api_prefix": "",
        "http_username": None,
        "pacing_delay": 0.3,
    },
    "libreoffice": {
        "base_url": "https://gerrit.libreoffice.org",
        "api_prefix": "",
        "http_username": None,
        "pacing_delay": 0.3,
    },
    "wikimedia": {
        "base_url": "https://gerrit.wikimedia.org/r",
        "api_prefix": "",
        "http_username": None,      # anonymous read access works; see GERRIT_HTTP_USERNAME below
        "pacing_delay": 5.0,        # Wikimedia answers HTTP 429 to bursts of 100-change pages:
        "page_size": 500,           # fewer, larger requests spaced 5 s apart stay under its limit
    },
}

USER_AGENT = "GerritWatch/0.1 (research prototype; read-only polling of open changes)"
PAGE_SIZE = 100
MAX_RETRIES = 6
RETRY_BACKOFF_BASE = 2.0
RETRY_BACKOFF_MAX = 60.0
REQUEST_TIMEOUT = 60

POLL_INTERVAL_SECONDS = 15 * 60   # re-check every 15 minutes
DEFAULT_MIN_DISTANCE = 20         # best F1 on our labeled benchmark; --min-distance 0 flags every edit
OPENAI_MODEL = "gpt-4o-mini-2024-07-18"   # the snapshot we evaluated, pinned so results stay comparable
OPENAI_TIMEOUT = 60

# ================================================================================
# CLEANING PIPELINE -- copied EXACTLY from extract_and_clean.py (Levels 1-4).
# This must stay identical to that script's cleaning logic. If the training
# data was cleaned one way and live data is cleaned differently, the
# classifier is being run on a different distribution than it was validated
# on -- silently degrading its reliability. If extract_and_clean.py's
# CLEANING_CONFIG or patterns ever change, mirror the change here too.
# ================================================================================

_METADATA_PREFIXES: tuple[str, ...] = (
    "change-id:", "signed-off-by:", "reviewed-by:", "tested-by:", "acked-by:",
    "depends-on:", "hosts:", "co-authored-by:", "reported-by:", "suggested-by:",
    "cc:", "reviewed-on:", "see-also:",
)


def remove_metadata(text: str) -> str:
    out = []
    for line in text.splitlines():
        if not any(line.strip().lower().startswith(p) for p in _METADATA_PREFIXES):
            out.append(line)
    return "\n".join(out)


_KEYWORD_LINE_PATTERNS: list[re.Pattern] = [
    re.compile(r"^\s*\(cherry picked from commit\s+[0-9a-f]+\)\s*$", re.IGNORECASE),
    re.compile(r"^\s*cherry[- ]picked[- ]from\s*:?\s*\S*\s*$", re.IGNORECASE),
    re.compile(r"^\s*cherry picked from commit\s+\S+\s*$", re.IGNORECASE),
    re.compile(r"^\s*(WIP|Work In Progress|DO NOT MERGE)\s*$", re.IGNORECASE),
]
_INLINE_CHERRY_PICK: re.Pattern = re.compile(
    r"\(cherry picked from commit\s+[0-9a-f]+\)", re.IGNORECASE
)


def remove_keywords(text: str) -> str:
    out = []
    for line in text.splitlines():
        if any(pat.match(line) for pat in _KEYWORD_LINE_PATTERNS):
            continue
        line = _INLINE_CHERRY_PICK.sub("", line).rstrip()
        out.append(line)
    return "\n".join(out)


_STANDALONE_ISSUE_PATTERNS: list[re.Pattern] = [
    re.compile(r"^\s*bug\s*[:#]?\s*\S+\s*$", re.IGNORECASE),
    re.compile(r"^\s*issue\s*[:#]?\s*\S+\s*$", re.IGNORECASE),
    re.compile(r"^\s*closes\s*[:#]?\s*\S+\s*$", re.IGNORECASE),
    re.compile(r"^\s*related(?:-to)?\s*[:#]?\s*\S+\s*$", re.IGNORECASE),
    re.compile(r"^\s*fixes\s*[:#]\s*\S+\s*$", re.IGNORECASE),
    re.compile(r"^\s*see-also\s*[:#]?\s*\S+\s*$", re.IGNORECASE),
]


def remove_bug_references(text: str) -> str:
    out = []
    for line in text.splitlines():
        if not any(pat.match(line) for pat in _STANDALONE_ISSUE_PATTERNS):
            out.append(line)
    return "\n".join(out)


def normalize_whitespace(text: str) -> str:
    lines = []
    for line in text.splitlines():
        line = re.sub(r"  +", " ", line.strip())
        lines.append(line)
    collapsed = []
    prev_blank = False
    for line in lines:
        is_blank = (line == "")
        if is_blank and prev_blank:
            continue
        collapsed.append(line)
        prev_blank = is_blank
    return "\n".join(collapsed).strip()


try:
    from rapidfuzz.distance import Levenshtein as _rf_levenshtein
except ImportError:          # pure-Python fallback below: same result, much slower on long texts
    _rf_levenshtein = None


def levenshtein(a: str, b: str) -> int:
    """Character-level Levenshtein distance (unit cost for insert, delete, substitute),
    the measure used in our benchmark (rapidfuzz Levenshtein.distance)."""
    if _rf_levenshtein is not None:
        return _rf_levenshtein.distance(a, b)
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def clean_message(raw: str) -> str:
    """Applies L1 -> L2 -> L3 -> L4 in the same fixed order as
    extract_and_clean.py's clean_description(), all levels enabled."""
    text = raw or ""
    text = remove_metadata(text)
    text = remove_keywords(text)
    text = remove_bug_references(text)
    text = normalize_whitespace(text)
    return text


# ================================================================================
# GERRIT CLIENT
# ================================================================================

class GerritClient:
    def __init__(self, project_cfg: dict):
        self.base_url = project_cfg["base_url"].rstrip("/")
        self.api_prefix = project_cfg.get("api_prefix", "")
        self.page_size = project_cfg.get("page_size", PAGE_SIZE)
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        # Authenticated access is optional: set GERRIT_HTTP_USERNAME and GERRIT_HTTP_PASSWORD
        # (a Gerrit HTTP password) to use /a/ endpoints; otherwise requests are anonymous.
        http_username = project_cfg.get("http_username") or os.environ.get("GERRIT_HTTP_USERNAME")
        if http_username:
            password = os.environ.get("GERRIT_HTTP_PASSWORD")
            if not password:
                log.error("GERRIT_HTTP_PASSWORD env var required when a Gerrit username is set.")
                sys.exit(1)
            self.session.auth = (http_username, password)
            self.auth_prefix = "/a"
        else:
            self.auth_prefix = ""

    def _get(self, path: str, params: dict) -> Optional[Any]:
        url = f"{self.base_url}{self.api_prefix}{self.auth_prefix}{path}"
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = self.session.get(url, params=params, timeout=REQUEST_TIMEOUT)
                if resp.status_code == 200:
                    text = resp.text
                    if text.startswith(")]}'"):
                        text = text[4:]
                    return json.loads(text)
                if resp.status_code in (403, 429) or resp.status_code >= 500:
                    wait = min(RETRY_BACKOFF_BASE ** attempt, RETRY_BACKOFF_MAX)
                    log.warning("HTTP %s (attempt %d/%d) -- backing off %.1fs",
                                resp.status_code, attempt, MAX_RETRIES, wait)
                    time.sleep(wait)
                    continue
                log.warning("HTTP %s (non-retryable) for %s", resp.status_code, url)
                return None
            except requests.RequestException as exc:
                wait = min(RETRY_BACKOFF_BASE ** attempt, RETRY_BACKOFF_MAX)
                log.warning("Request error (%s) attempt %d/%d -- backing off %.1fs",
                            exc, attempt, MAX_RETRIES, wait)
                time.sleep(wait)
        return None

    def get_messages(self, project: str, change_number: str) -> Optional[list[dict]]:
        """All change messages (votes, review comments, bot reports), each tagged with the
        patchset it was posted on (_revision_number). None if the request fails."""
        from urllib.parse import quote
        return self._get(f"/changes/{quote(project, safe='')}~{change_number}/messages", {})

    def list_open_changes(self, query: str, pacing_delay: float) -> Optional[list[dict]]:
        """Fetches ALL open changes matching `query`, paginated, each already
        including its CURRENT patchset's commit message + subject + owner --
        so no extra per-change call is needed just to read the description.
        Returns None if any page fails, so a Gerrit outage is never mistaken
        for "no open changes" (which would wipe the saved state and flags)."""
        results: list[dict] = []
        start = 0
        while True:
            params = {
                "q": query,
                "n": self.page_size,
                "S": start,
                "o": ["CURRENT_REVISION", "CURRENT_COMMIT", "DETAILED_ACCOUNTS"],
            }
            page = self._get("/changes/", params)
            time.sleep(pacing_delay)
            if page is None:
                return None
            if not page:
                break
            results.extend(page)
            if not page or not page[-1].get("_more_changes"):
                break
            start += self.page_size
        return results


# ================================================================================
# LLM CLASSIFIER -- same prompt/call shape as Run_llm_filter.py
# ================================================================================

def load_prompt(prompt_path: str) -> str:
    if not os.path.isfile(prompt_path):
        raise FileNotFoundError(
            f"Prompt file not found: {prompt_path}\n"
            f"Copy Final_Prompt.txt (used by Run_llm_filter.py) into this folder."
        )
    with open(prompt_path, "r", encoding="utf-8") as fh:
        raw = fh.read()
    template = raw.strip()
    if template.startswith('"') and template.endswith('"'):
        template = template[1:-1]
    template = template.replace('""', '"')
    if "{old_description}" not in template or "{new_description}" not in template:
        raise ValueError(f"Prompt '{prompt_path}' is missing placeholders.")
    return template


def fill_prompt(template: str, old_desc: str, new_desc: str) -> str:
    return template.replace("{old_description}", old_desc).replace("{new_description}", new_desc)


def classify_pair(template: str, old_desc: str, new_desc: str) -> dict:
    """Returns {"llm_prediction": "KEEP"/"REMOVE"/"ERROR", "confidence": ..., "reason": ...}"""
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        raise EnvironmentError("OPENAI_API_KEY environment variable is not set.")

    prompt = fill_prompt(template, old_desc, new_desc)
    url = "https://api.openai.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": OPENAI_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": 512,
        "seed": 42,
    }

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=OPENAI_TIMEOUT)
            if resp.status_code == 429:
                wait = min(RETRY_BACKOFF_BASE ** attempt, RETRY_BACKOFF_MAX)
                log.warning("  [RETRY] OpenAI rate limited -- waiting %.1fs", wait)
                time.sleep(wait)
                continue
            if resp.status_code != 200:
                log.warning("  OpenAI HTTP %s: %s", resp.status_code, resp.text[:200])
                wait = min(RETRY_BACKOFF_BASE ** attempt, RETRY_BACKOFF_MAX)
                time.sleep(wait)
                continue

            body = resp.json()
            content = body["choices"][0]["message"]["content"].strip()
            content = re.sub(r"^```json\s*|\s*```$", "", content.strip())
            parsed = json.loads(content)
            # Final_Prompt.txt answers {"semantic_change": bool, "confidence",
            # "decision_reason"}; older prompts answered {"label": "KEEP"/"REMOVE", "reason"}.
            if isinstance(parsed.get("semantic_change"), bool):
                prediction = "KEEP" if parsed["semantic_change"] else "REMOVE"
            else:
                prediction = str(parsed.get("label", parsed.get("prediction", "ERROR"))).upper()
            return {
                "llm_prediction": prediction,
                "confidence": parsed.get("confidence", ""),
                "reason": parsed.get("decision_reason", parsed.get("reason", "")),
                "model": body.get("model", OPENAI_MODEL),
                "usage": body.get("usage", {}),   # prompt/completion tokens, for the cost report
            }
        except (requests.RequestException, json.JSONDecodeError, KeyError, IndexError) as exc:
            wait = min(RETRY_BACKOFF_BASE ** attempt, RETRY_BACKOFF_MAX)
            log.warning("  Classifier call failed (%s) attempt %d/%d -- waiting %.1fs",
                        exc, attempt, MAX_RETRIES, wait)
            time.sleep(wait)

    return {"llm_prediction": "ERROR", "confidence": "", "reason": "classification failed after retries"}


# ================================================================================
# STATE + FLAGS FILE MANAGEMENT
# ================================================================================

def load_json(path: Path, default):
    if path.exists():
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    return default


def append_event(path: Path, event: dict) -> None:
    """Append one JSON line to the event log. Unlike flags.json, nothing here is ever
    rewritten or dropped, so the log is the record used to evaluate a live run."""
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False) + "\n")


def reviewer_activity(messages: Optional[list[dict]], owner_id, new_patchset) -> Optional[dict]:
    """Who voted or commented on the change before the patchset with the new description.
    Messages by the change owner are left out; bots are kept (filter them by name later)."""
    if messages is None:
        return None
    before = [m for m in messages
              if (m.get("_revision_number") or 0) < (new_patchset or 0)
              and (m.get("author") or {}).get("_account_id") != owner_id]
    return {
        "n_messages": len(before),
        "n_people": len({(m.get("author") or {}).get("_account_id") for m in before}),
        "messages": [{"patchset": m.get("_revision_number"), "date": m.get("date"),
                      "author_id": (m.get("author") or {}).get("_account_id"),
                      "author": (m.get("author") or {}).get("name", ""),
                      "text": (m.get("message") or "").split("\n", 1)[0][:160]}
                     for m in before],
    }


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def save_json(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


# ================================================================================
# ONE POLL CYCLE
# ================================================================================

def run_poll_cycle(client: GerritClient, project_name: str, query: str,
                    pacing_delay: float, prompt_template: Optional[str],
                    state_path: Path, flags_path: Path,
                    min_distance: int = DEFAULT_MIN_DISTANCE,
                    llm_mode: str = "decide",
                    events_path: Optional[Path] = None) -> None:
    """prompt_template=None means the LLM is off: every edit with distance > min_distance is flagged.
    With the LLM on, llm_mode="decide" flags only the edits it labels semantic; llm_mode="record"
    flags by distance alone and only stores the LLM's answer (and uses its reason as the explanation)."""

    cycle_start = time.time()
    log.info("Fetching open changes for %s (query: %s) ...", project_name, query)
    open_changes = client.list_open_changes(query, pacing_delay)
    if open_changes is None:
        log.warning("  Could not fetch the open changes -- keeping the previous state and flags.")
        if events_path:
            append_event(events_path, {"type": "poll_failed", "ts": utc_now(), "instance": project_name,
                                       "duration_s": round(time.time() - cycle_start, 1)})
        return
    log.info("  %d open changes found", len(open_changes))
    n_edits = 0

    state = load_json(state_path, {})   # change_number(str) -> {last_description, flagged, ...}
    new_state: dict[str, dict] = {}
    new_flags: list[dict] = []

    for change in open_changes:
        change_number = str(change.get("_number"))
        current_rev_sha = change.get("current_revision")
        if not current_rev_sha:
            continue
        revision = change["revisions"][current_rev_sha]
        patchset_number = revision.get("_number")
        commit_message = revision.get("commit", {}).get("message", "")
        current_desc = clean_message(commit_message)

        subject = change.get("subject", "")
        owner = change.get("owner", {}).get("name", "unknown")
        prior = state.get(change_number)

        if prior is None:
            # First time we've ever seen this open change -- nothing to compare
            # against yet, just record its current state as the baseline.
            new_state[change_number] = {
                "last_description": current_desc,
                "last_patchset": patchset_number,
                "flagged": False,
                "flag_data": None,
            }
            continue

        if current_desc == prior["last_description"]:
            # No description change since we last checked -- carry state forward.
            new_state[change_number] = prior
            if prior.get("flagged") and prior.get("flag_data"):
                new_flags.append(prior["flag_data"])
            continue

        # Description changed since last check -- apply the detector.
        n_edits += 1
        distance = levenshtein(prior["last_description"], current_desc)
        log.info("  Description changed on #%s (%s) -- edit distance %d",
                 change_number, subject[:60], distance)
        llm = None
        if distance > min_distance and prompt_template is not None:
            t0 = time.time()
            llm = classify_pair(prompt_template, prior["last_description"], current_desc)
            llm["latency_s"] = round(time.time() - t0, 2)
            log.info("    LLM: %s", llm["llm_prediction"])
        if distance <= min_distance:
            flagged, reason = False, f"edit distance {distance} <= {min_distance}"
        elif llm is not None and llm_mode == "decide":
            flagged, reason = llm["llm_prediction"] == "KEEP", llm.get("reason", "")
        else:
            flagged = True
            reason = (llm or {}).get("reason") or \
                f"The description changed by {distance} character{'s' if distance != 1 else ''}."
        result = {"llm_prediction": "KEEP" if flagged else "REMOVE",
                  "confidence": (llm or {}).get("confidence", ""), "reason": reason}

        change_project = change.get("project", project_name)
        change_url = f"{client.base_url}/c/{change_project}/+/{change_number}"
        if events_path:
            activity = reviewer_activity(client.get_messages(change_project, change_number),
                                         change.get("owner", {}).get("_account_id"), patchset_number)
            time.sleep(pacing_delay)
            append_event(events_path, {
                "type": "edit", "ts": utc_now(), "instance": project_name,
                "reviewer_activity_before": activity,   # votes/comments by others on earlier patchsets
                "change_number": change_number, "change_id": change.get("change_id", ""),
                "project": change_project, "change_url": change_url,
                "old_patchset": prior.get("last_patchset"), "new_patchset": patchset_number,
                "patchset_created": revision.get("created"),   # Gerrit time (UTC) of the new patchset
                "old_description": prior["last_description"], "new_description": current_desc,
                "edit_distance": distance, "min_distance": min_distance,
                "llm_mode": llm_mode if prompt_template is not None else None,
                "flagged": flagged, "llm": llm,
            })

        if result["llm_prediction"] == "KEEP":
            gerrit_host = client.base_url.split("//", 1)[-1].split("/", 1)[0]
            flag_id = f"{project_name}:{change_number}:{prior.get('last_patchset')}-{patchset_number}"
            flag_data = {
                "flag_id": flag_id,
                "change_number": change_number,
                "change_id": change.get("change_id", ""),
                "project": change_project,
                "source_project": project_name,   # "onap" / "libreoffice" / "wikimedia" -- used to merge safely
                "gerrit_host": gerrit_host,        # e.g. "gerrit.onap.org" -- used by the extension to avoid cross-instance mismatches
                "change_url": change_url,
                "subject": subject,
                "owner": owner,
                "old_patchset": prior.get("last_patchset"),
                "new_patchset": patchset_number,
                "old_description": prior["last_description"],
                "new_description": current_desc,
                "reason": result.get("reason", ""),
                "confidence": result.get("confidence", ""),
                "edit_distance": distance,
                "detector": {"min_distance": min_distance,
                             "llm": OPENAI_MODEL if prompt_template is not None else None},
            }
            new_state[change_number] = {
                "last_description": current_desc,
                "last_patchset": patchset_number,
                "flagged": True,
                "flag_data": flag_data,
            }
            new_flags.append(flag_data)
            log.info("    -> flagged")
        else:
            new_state[change_number] = {
                "last_description": current_desc,
                "last_patchset": patchset_number,
                "flagged": False,
                "flag_data": None,
            }
            log.info("    -> %s (not flagged)", result["llm_prediction"])

    # Changes that dropped out of the open-changes list (merged/abandoned)
    # are simply not carried into new_state -- they age out automatically.
    save_json(state_path, new_state)

    # --- Merge into the SHARED flags.json instead of overwriting it ---
    # Multiple monitor processes (one per project) can write to the same
    # flags.json. Each one only ever replaces the entries it itself owns
    # (tagged by source_project), leaving the other projects' flags intact.
    # Note: if two processes happen to write within the same instant, one
    # write can be briefly overwritten by the other -- acceptable for a
    # demo; the next poll cycle (15 min later) corrects it either way.
    existing_flags = load_json(flags_path, [])
    other_projects_flags = [f for f in existing_flags if f.get("source_project") != project_name]
    merged_flags = other_projects_flags + new_flags
    save_json(flags_path, merged_flags)

    log.info("Poll cycle done. This project flagged: %d. Combined flags.json total: %d.",
              len(new_flags), len(merged_flags))
    if events_path:
        append_event(events_path, {"type": "poll", "ts": utc_now(), "instance": project_name,
                                   "open_changes": len(open_changes), "edits": n_edits,
                                   "active_flags": len(new_flags),
                                   "duration_s": round(time.time() - cycle_start, 1)})


# ================================================================================
# MAIN
# ================================================================================

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("project", choices=list(PROJECTS.keys()))
    parser.add_argument("--project-filter", default=None,
                         help='Optional Gerrit sub-project, e.g. "so/nbi" -- adds project:<value> to the query')
    parser.add_argument("--interval", type=int, default=POLL_INTERVAL_SECONDS,
                         help="Seconds between poll cycles (default: 900 = 15 min)")
    parser.add_argument("--once", action="store_true", help="Run a single poll cycle and exit (useful for testing)")
    parser.add_argument("--min-distance", type=int, default=DEFAULT_MIN_DISTANCE,
                         help="Flag an edit only if its Levenshtein distance is > N (default: 20; 0 = every edit)")
    parser.add_argument("--llm", action="store_true",
                         help=f"Ask the LLM ({OPENAI_MODEL}) about every candidate edit")
    parser.add_argument("--llm-mode", choices=["decide", "record"], default="decide",
                         help="decide: flag only what the LLM labels semantic (default); "
                              "record: flag by distance, store the LLM answer and use its reason")
    args = parser.parse_args()
    if args.min_distance < 0:
        parser.error("--min-distance must be >= 0")

    cfg = PROJECTS[args.project]
    pacing_delay = cfg["pacing_delay"]

    query = "status:open"
    if args.project_filter:
        query += f" project:{args.project_filter}"

    data_dir = Path(os.environ.get("ENRICH_DATA_DIR", "."))
    state_path = data_dir / f"monitor_state_{args.project}.json"
    flags_path = data_dir / "flags.json"
    events_path = data_dir / f"events_{args.project}.jsonl"

    prompt_path = data_dir / "Final_Prompt.txt"
    if not prompt_path.is_file():           # fall back to the prompt shipped next to this script
        prompt_path = Path(__file__).resolve().parent / "Final_Prompt.txt"
    prompt_template = load_prompt(str(prompt_path)) if args.llm else None
    client = GerritClient(cfg)

    log.info("=" * 70)
    log.info("GerritWatch monitor starting: project=%s, interval=%ds", args.project, args.interval)
    log.info("Detector: edit distance > %d, LLM %s", args.min_distance,
             f"{OPENAI_MODEL} ({args.llm_mode})" if args.llm else "off")
    log.info("Event log: %s", events_path.resolve())
    log.info("Writing flags to: %s", flags_path.resolve())
    log.info("=" * 70)

    while True:
        try:
            run_poll_cycle(client, args.project, query, pacing_delay,
                            prompt_template, state_path, flags_path,
                            min_distance=args.min_distance, llm_mode=args.llm_mode,
                            events_path=events_path)
        except Exception as exc:
            log.error("Poll cycle failed: %s", exc)

        if args.once:
            break
        log.info("Sleeping %ds until next check...", args.interval)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
