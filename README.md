# Second Brain Pipeline

Ingests PDFs into `~/Documents/second-brain`. Fully decoupled from
the local GPU — every model call is a cloud request to Gemini. For backward
compatibility it can read a primary key from
`~/llmstack/config/cloud-keys.env`, but that path is configurable and no
llmstack service is started, stopped, or queued by this project.

## Local setup

Requires Python 3.10+ and an Obsidian vault (the default location is
`~/Documents/second-brain`).

```bash
./setup.sh
cp .env.example .env
# Edit .env, then load it into the current shell:
set -a; source .env; set +a
python ingest.py --inbox
```

Credentials are resolved in this order:

1. `GEMINI_API_KEY` from the environment.
2. `GEMINI_API_KEY` from `SECONDBRAIN_KEYFILE` (defaults to the legacy
   `~/llmstack/config/cloud-keys.env`).
3. Additional rotation keys from the first existing local file:
   `gemini-keys.txt`, then legacy `gemini_keys.txt`.

The local key files contain one key per line. They and `.env` are ignored by
Git; keep their permissions at `600`. Set `SECONDBRAIN_VAULT` to override the
default vault location.

## Usage

Automatic: drop files into `~/Documents/second-brain/00-Inbox-Lectures/`
(slides, tutorials, exam papers) or `00-Inbox-Sources/` (textbooks, papers,
reference material). A `systemd --user` timer (`secondbrain-ingest.timer`)
checks both every 2 minutes and processes anything it finds — no command
needed. Which folder a file lands in *is* the classification — a long
lecture deck stays a lecture, a short paper is still a grounding source; no
page-count guessing.

Manual, one specific file:
```bash
./.venv/bin/python ingest.py /path/to/lecture.pdf          # lecture path
./.venv/bin/python ingest.py /path/to/textbook.pdf --source  # source path
```

Manual, process both inboxes right now instead of waiting for the timer:
```bash
./.venv/bin/python ingest.py --inbox
```

Logs: `logs/ingest.log`. Timer control: `systemctl --user {status,stop,disable} secondbrain-ingest.timer`.

## Pipeline

**`00-Inbox-Sources/` → `ingest_source()`** (`ingest.py`): direct text
extraction only (`extract.py`, `skip_ocr=True` — sources are typed PDFs;
the rare non-text page is skipped, not OCR'd), course inferred from a
content sample weighted toward the filename (`classify.infer_course_for_source`),
indexed locally into SQLite FTS5 (`textbook_index.py`). No filing LLM call,
no atomic notes generated, regardless of length — an 864-page textbook
indexes in ~8s with exactly one LLM call total (course inference).
Re-dropping the same filename+course replaces its entries rather than
duplicating them. Once indexed, any existing note with `status:
llm-expanded` (expanded from general knowledge for lack of a source) for
that course is automatically re-grounded (`rebuild.py`) — see below.

**`00-Inbox-Lectures/` → `ingest_lecture()`**:
1. **`extract.py`** — per page, not per document: if the page has a real
   text layer, extract it directly with PyMuPDF (lossless, free, instant,
   no model call). Only pages with no text layer (scans, photographed
   handwriting) fall through to vision OCR. Confirmed exercised in
   production, not just tested in isolation: a real lecture deck with 4
   non-text pages went through OCR+verification cleanly.
2. **`ocr.py`** — for those pages: Gemini vision transcribes verbatim, then
   a second pass re-checks the transcript against the same image and
   corrects it. Anything still uncertain (illegible handwriting, a
   correction needed) is flagged, not silently trusted — no OCR pipeline is
   provably lossless.
3. **`classify.py`** — one cheap call: lecture-shaped content vs. a
   problem set/exam paper.
4. **`filing.py`** (lecture) — splits the transcript into atomic notes,
   cross-links/tags against the *entire* vault regardless of course.
   Notes proposed in the same batch can link each other (the model is
   shown its own sibling titles). Course logistics (instructor, schedule,
   grading weights, policies) get captured as their own note instead of
   being silently dropped for not being an "academic concept" — a real
   failure mode this was built to close, not a hypothetical one. A note
   flagged topic-label-only gets expanded in a second pass: grounded
   against that course's indexed source when `textbook_index.search()`
   finds one, cited by page number; falls back to general knowledge —
   flagged `status: llm-expanded` — only when no source covers that course.
   **`practice.py`** (practice) — kept as one intact document with original
   question numbering, questions linked to relevant concept notes, no
   answers generated.
5. **`coverage.py`** — after filing, one call compares every page of the
   source against everything just written and flags any page whose content
   isn't reflected anywhere. Anything flagged gets a recovery filing pass
   automatically, in the same run. This exists because filing's prompt
   alone silently dropped a whole category of real content once (course
   logistics, before item 4 above was fixed) — this is the structural
   check that catches the *next* unknown category, not just that one.
6. **`vault.py`** — writes the result. A same-title collision merges into
   the existing note (via LLM) instead of overwriting it — a previous
   version silently destroyed a hand-written note this way; also handles
   a source-filename collision (two different courses both naming a
   lecture "L0.pdf") by archiving under a disambiguated name rather than
   overwriting the earlier archive — also happened live, twice, before the
   fix. `git commit`s the result; every automated write is a one-command
   revert.

## Re-grounding and coverage recovery

- **`rebuild.py`** — re-grounds any note marked `status: llm-expanded`
  once a matching course source becomes available (auto-triggered after
  every source finishes indexing; also runnable standalone with
  `--course` or `--sources` to scope it).
- **`recover_missing.py`** — retroactive version of the coverage check in
  step 5 above, for lecture sources that were processed before that check
  existed (or as a periodic sanity pass). Re-extracts each source and
  re-runs OCR where needed, so it costs real calls — a backfill tool, not
  something to run on every timer tick.

## Integrity auditing

`audit.py` (broken `[[links]]`, orphan notes) runs automatically after any
batch that actually changed the vault — no manual step. A broken link fires
a critical desktop notification (`notify-send`, same pattern `llmstack`
uses); orphans are logged only, not alerted, since a new orphan isn't
necessarily wrong — it needs a human call, not an alarm. Deliberately does
**not** auto-rewrite anything it finds — a fuzzy-match "auto-repair" was
tried and reverted after it silently mangled real `[[Title|alias]]` links
with no diff shown and no way to review. Report, don't guess. Run it by
hand anytime with `./.venv/bin/python audit.py` (read-only, no LLM calls,
free).

## Model and rate limits

Currently `gemini-3.7-flash` (`config.GEMINI_MODEL`) — the same model
`llmstack`'s own router uses. Was `gemini-3.6-flash` until its free-tier
quota (20 requests, confirmed live to be tracked **per model per
project**, not just per key) got fully exhausted across all 5 configured
keys by same-day testing, with no relief after real waits. Switching model
tag immediately unblocked the pipeline on the same keys — worth
remembering if this happens again: try a different model tag on the same
key before assuming the key itself is dead.

`gemini_client.py` rotates across multiple API keys (separate Google Cloud
projects — a single project's quota is shared across its own keys, so only
*separate* projects help): the primary key comes from the environment or the
configured backward-compatible key file; add more in
`gemini-keys.txt` or the legacy `gemini_keys.txt` (one key per line, mode
`600`, never touches llmstack's own config). The hyphenated filename takes
precedence if both exist. On a 429, it rotates to the next key immediately with no sleep,
records a cooldown for that (key, model) pair in `key_cooldowns.json` so a
*separate process* (the timer's next tick is always a fresh process) skips
straight past a key already known to be exhausted instead of wasting a
request reconfirming it. Only once every key is exhausted does it fall
back to honoring Google's own `retryDelay`.

## Known gaps

- **No semantic/vector search** — deferred, per the earlier decision not
  to build GraphRAG-scale infrastructure for a vault this size. Revisit only
  if flat tags/backlinks stop being enough.
- **JSON-mode LaTeX escaping**: Gemini's JSON output routinely ships
  single backslashes in LaTeX (`\text` instead of `\\text`), which breaks
  strict `json.loads`. Mitigated with an explicit prompt instruction plus
  `json_repair.loads` as a fallback (`gemini_client.json_call`).
- **`coverage.py`'s check shares some blind spots with the filing pass it's
  checking** — an LLM verifying its own output isn't a mathematical
  guarantee of completeness, just a structural catch for whole
  skipped categories/pages, which is the actual failure mode observed live.
