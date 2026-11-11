# GerritWatch

GerritWatch alerts code reviewers, inside Gerrit, when the description of an open change is
revised. During review, a change gets new patchsets, and its description (the commit message)
can be revised with each one. Gerrit does not tell a reviewer who read an earlier version.
GerritWatch does.

It has three parts, all running on the reviewer's machine:

1. **Monitor** (`backend/monitor_open_changes.py`): polls the Gerrit REST API for all open
   changes, compares each description with the one seen at the previous poll, and flags
   revisions. It only reads from Gerrit and needs no Gerrit account.
2. **Local server** (`backend/serve_cors.py`): passes the flags to the browser extension and
   stores the reviewer's feedback. It listens on `127.0.0.1:8000` only.
3. **Chrome extension** (`extension/`): shows an icon next to each flagged change in Gerrit's
   change lists. A click shows the old and new description, the reason for the flag, and
   feedback buttons.

Demo video (about 1 minute): https://drive.google.com/drive/folders/1bs2epH1vo0xOzBhVOaVWhWUVc0WEM9Ur

It is configured for ONAP, LibreOffice, and Wikimedia (`PROJECTS` in the monitor) and works on
other Gerrit instances after adding them there and in `extension/manifest.json`.

## Installation

You need Chrome (or another Chromium-based browser) for the extension, and either Docker or
Python 3.9+ for the backend. The LLM option also needs an OpenAI API key.

### 1. Backend with Docker (recommended)

```bash
docker compose up -d --build                     # local server + ONAP monitor
docker compose --profile libreoffice --profile wikimedia up -d   # optional: the other two instances
docker compose logs -f monitor-onap              # watch the polls
docker compose down                              # stop (add -v to delete the stored flags)
```

The server is published on `127.0.0.1:8000` only. The monitors use the default `--min-distance 20`; to
change a setting, or to add `--llm`, edit the `command` lines in `docker-compose.yml`. For
`--llm`, put `OPENAI_API_KEY=sk-...` in a `.env` file next to `docker-compose.yml`.

### 1. Backend with Python (alternative)

```bash
pip install -r requirements.txt
cd backend
python serve_cors.py                                  # terminal 1: local server
python monitor_open_changes.py onap                  # terminal 2: one monitor per instance
```

Run the monitor and the server in the same folder: the monitor writes `flags.json` there, and
the server serves it.

### 2. Chrome extension

Open `chrome://extensions`, turn on *Developer mode*, click *Load unpacked*, and choose the
`extension/` folder. Open the change list of a watched Gerrit instance (for example
https://gerrit.onap.org/r/q/status:open). Flagged changes show an icon; click it for details.

The first poll only records the current descriptions; revisions are flagged from the second
poll on (every 15 minutes by default).

### Try it without waiting for a revision

Serve the example flag (without a monitor, which would overwrite it):

```bash
# with Docker
docker compose up -d server
docker compose cp samples/sample_flags.json server:/data/flags.json

# or with Python
cd backend && cp ../samples/sample_flags.json flags.json && python serve_cors.py
```

Then open https://gerrit.onap.org/r/q/project:oom+status:open with the extension loaded. The
example is a real revision of ONAP change 149377 (patchset 1 to 2), with the reason given by
GPT-4o-mini.

## Choosing the detector setting

A revision is flagged if its character-level edit distance is above `--min-distance` (default
20, the recommended value; `--min-distance 0` flags every revision). With `--llm`, GPT-4o-mini (`gpt-4o-mini-2024-07-18`, temperature 0, the
few-shot prompt in `backend/Final_Prompt.txt`) also has to say that the meaning changed; its
reason is shown in the popup. The LLM needs `OPENAI_API_KEY` and sends both descriptions to
OpenAI.

Expected accuracy of each setting on the DescEvo benchmark (379 revisions labeled by two
annotators; P = precision, R = recall):

| `--min-distance` | P | R | F1 | with `--llm`: P | R | F1 |
|---:|---:|---:|---:|---:|---:|---:|
| 0  | 0.549 | 1.000 | 0.709 | 0.872 | 0.822 | 0.847 |
| 10 | 0.823 | 0.986 | 0.897 | 0.909 | 0.817 | 0.861 |
| 20 | 0.878 | 0.966 | **0.920** | 0.929 | 0.812 | 0.867 |
| 30 | 0.880 | 0.880 | 0.880 | 0.935 | 0.764 | 0.841 |
| 40 | 0.878 | 0.798 | 0.836 | 0.936 | 0.702 | 0.802 |

The default, `--min-distance 20`, has the best F1. Adding `--llm` gives fewer false alarms but misses about one
semantic revision in five. `eval/detector_table.py` reproduces this table.

## Monitor options

| Option | Meaning |
|---|---|
| `onap` / `libreoffice` / `wikimedia` | Gerrit instance to watch |
| `--interval S` | seconds between polls (default 900) |
| `--once` | run one poll and exit |
| `--project-filter P` | watch only repository `P` (e.g. `mediawiki/core`) |
| `--min-distance N` | flag only revisions with edit distance above `N` (default 20; 0 flags every revision) |
| `--llm` | also ask GPT-4o-mini |
| `--llm-mode decide\|record` | `decide` (default): the LLM decides; `record`: flag by distance, only log the LLM's answer |

Files written in the folder given by `ENRICH_DATA_DIR` (default: the current folder):
`flags.json`, `monitor_state_<instance>.json`, and `events_<instance>.jsonl` (every revision
and poll, with both descriptions, the edit distance, the decision, and the LLM's answer).

## Extension option

Click the extension's icon in the toolbar to turn on **Only changes I opened**. A change is
then flagged only if its description changed after the patchset you last opened in this
browser.

## Evaluation scripts and data

| Script | Produces |
|---|---|
| `eval/detector_table.py` | the table above, from DescEvo's benchmark and GPT-4o-mini run (`data/descevo/`) |
| `eval/live_run_stats.py` | statistics of the live run, from `data/live_run/events_*.jsonl` |
| `eval/live_run_crosscheck.py` | checks the live run against Gerrit's revision history |
| `eval/replay.py` | the 30-day replay: `fetch`, `pairs`, `llm`, `stats` (`data/replay/`) |
| `eval/cleaning_check.py` | checks that the monitor cleans descriptions as DescEvo does (needs DescEvo's `pairs.csv`) |

Run them from this folder, e.g. `python eval/detector_table.py`. In the data files, e-mail
addresses are replaced by `<email>`.

## Limitations

The monitor polls, so a revision is reported up to one interval after upload, and patchsets
uploaded between two polls are compared as one revision. A change seen for the first time is
only recorded. A flag is removed when a later revision of the change is not flagged. Lines that
only reference an issue are removed before comparing, as in DescEvo.

## License

MIT, see `LICENSE`.

## Citation

GerritWatch: Alerting Reviewers to Revised Change Descriptions in Gerrit. Under submission.
Software archive: Zenodo, DOI 10.5281/zenodo.24125508 (https://doi.org/10.5281/zenodo.24125508).
The detector settings are evaluated on DescEvo (Zenodo, DOI 10.5281/zenodo.24094881).
