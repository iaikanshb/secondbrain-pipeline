# Second Brain Pipeline

Turns dropped-in lecture PDFs into a cross-linked, exam-ready Obsidian
vault: topic notes, per-lecture study maps, and Anki flashcards, generated
automatically. The core pipeline needs nothing but Python and a network
connection — every filing/dedup/OCR call is a cloud request to Gemini. An
entirely optional layer (semantic search, a stronger duplicate check) can
additionally use any OpenAI-compatible embeddings endpoint, local or cloud
— everything works exactly the same without one configured.

## Local setup

Requires Python 3.10+ and an Obsidian vault (the default location is
`~/Documents/second-brain`).

```bash
./setup.sh
cp .env.example .env
# Edit .env -- it's auto-loaded by config.py, no sourcing needed (though
# `set -a; source .env; set +a` still works too, if you prefer that).
cp courses.txt.example courses.txt   # optional: list your actual courses
python ingest.py --inbox
```

Credentials are resolved in this order:

1. `GEMINI_API_KEY` from the environment (e.g. via `.env`).
2. Additional rotation keys from the first existing local file:
   `gemini-keys.txt`, then legacy `gemini_keys.txt`.

The local key files contain one key per line. They, `.env`, and `courses.txt`
are all ignored by Git; keep the key files' permissions at `600`. Set
`SECONDBRAIN_VAULT` to override the default vault location.

For automatic runs (the systemd timer, not manual invocations), `.env` is
loaded via the service's own `EnvironmentFile=` directive, since a
background timer has no shell to source anything into.

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

### Vault layout

```
00-Inbox-Lectures/   drop zone: slides, tutorials, exam papers
00-Inbox-Sources/    drop zone: textbooks, papers, reference material
01-Notes/            topic notes filed from lectures (the durable, linkable layer)
02-Resources/        archived original PDFs, one copy per genuinely distinct source
03-Attachments/      images/media referenced by notes
04-Practice/         problem sets/exam papers, kept intact, no answers generated
05-MOCs/             one map-of-content per lecture + one roll-up per course
06-Flashcards/       Anki-importable front\tback TSV, one file per course
```

## Pipeline

**`00-Inbox-Sources/` → `ingest_source()`** (`ingest.py`): direct text
extraction only (`extract.py`, `skip_ocr=True` — sources are typed PDFs;
the rare non-text page is skipped, not OCR'd), course inferred from a
content sample weighted toward the filename (`classify.infer_course_for_source`),
indexed locally into SQLite FTS5 (`textbook_index.py`). No filing LLM call,
no notes generated, regardless of length — an 864-page textbook indexes in
~8s with exactly one LLM call total (course inference). Re-dropping the same
filename+course replaces its entries rather than duplicating them. Once
indexed, any existing note with `status: llm-expanded` (expanded from
general knowledge for lack of a source) for that course is automatically
re-grounded (`rebuild.py`) — see below.

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
4. **`filing.py`** (lecture) — splits the transcript into topic-sized
   notes, not one-fact atoms and not the whole document (see
   [Methodology](#methodology) below for why), cross-links/tags against the
   *entire* vault regardless of course. Notes proposed in the same batch can
   link each other (the model is shown its own sibling titles). Course
   logistics (instructor, schedule, grading weights, policies) get captured
   as their own note instead of being silently dropped for not being an
   "academic concept" — a real failure mode this was built to close, not a
   hypothetical one. A note flagged topic-label-only gets expanded in a
   second pass: grounded against that course's indexed source when
   `textbook_index.search()` finds one, cited by page number; falls back to
   general knowledge — flagged `status: llm-expanded` — only when no source
   covers that course. **`practice.py`** (practice) — kept as one intact
   document with original question numbering, questions linked to relevant
   concept notes, no answers generated.
5. **`dedupe.py`** — before each note is actually written, two cheap,
   no-LLM passes narrow the existing vault down to a handful of plausible
   candidates: title-token/tag overlap, plus (if `EMBEDDING_URL` is
   configured) semantic similarity via `embeddings.py`, which catches a
   genuine duplicate worded so differently the first pass shares no
   vocabulary with it at all. One LLM call per candidate then asks whether
   the new content genuinely restates an existing note. If so, the note is
   written under the *existing* note's exact title instead of its own,
   which routes it into `vault.py`'s normal same-title merge path (below)
   rather than creating a near-duplicate file under a differently worded
   title — the actual mechanism behind duplicates observed live (two
   independently-run filing passes agreeing on content but not on phrasing).
   Deliberately conservative either way: a concept taught differently by two
   courses is *not* a duplicate and must stay two linked notes — see
   Methodology.
6. **`coverage.py`** — after filing, one call compares every page of the
   source against everything just written and flags any page whose content
   isn't reflected anywhere. Anything flagged gets a recovery filing pass
   automatically, in the same run (through the same filing + dedupe path
   above, so a recovered note that overlaps a sibling from the initial pass
   merges instead of duplicating it). This exists because filing's prompt
   alone silently dropped a whole category of real content once (course
   logistics, before item 4 above was fixed) — this is the structural
   check that catches the *next* unknown category, not just that one.
7. **`vault.py`** — writes the result. A same-title collision merges into
   the existing note (via LLM) instead of overwriting it — a previous
   version silently destroyed a hand-written note this way; also handles
   a source-filename collision (two different courses both naming a
   lecture "L0.pdf") by archiving under a disambiguated name rather than
   overwriting the earlier archive — also happened live, twice, before the
   fix.
8. **`moc.py`** — once filing (and any coverage recovery) is done, builds a
   lecture MOC: a short narrated walkthrough linking that lecture's notes in
   reading order, skipped if fewer than two notes came out of the source
   (nothing to walk through). Then fully regenerates that course's roll-up
   MOC — a deterministic, no-LLM listing of every lecture MOC for the
   course, sorted by date, so it can never drift from what's actually on
   disk.
9. **`flashcards.py`** — generates retrieval-practice questions from the
   same notes and appends them to that course's Anki-importable
   `06-Flashcards/<course>.tsv` (plain `front\tback`, Basic note type).
   `git commit`s the result (notes, MOC, flashcards, archived source
   together); every automated write is a one-command revert.

## Semantic search (optional)

Obsidian's own search (or the Omnisearch plugin) is fast keyword/fuzzy
matching — great when you remember roughly the words a note uses, useless
when you only remember the idea. If `EMBEDDING_URL` is set to any
OpenAI-compatible embeddings endpoint (a local `llama.cpp`/Ollama server,
a cloud provider, anything that answers `POST <url>` with
`{"input": "..."}` the way `/v1/embeddings` does), three things activate:

- **`embeddings.py`** — every note gets embedded and stored the moment
  it's written or merged (called from `filing.py`, no separate step),
  into `.embeddings.db` in the vault root. That file lives *inside* the
  vault specifically so it rides along on whatever already syncs the vault
  (Syncthing, etc.) rather than needing its own sync mechanism — written
  only by whichever machine runs `ingest.py`, read from any machine that
  has the vault. Plain SQLite, not WAL mode, since WAL's sidecar
  `-wal`/`-shm` files being copied non-atomically by a file-sync tool is a
  real corruption risk; a rollback-journal file being caught mid-write by
  a sync scan is at worst a stale read, never structurally broken.
- **`search.py`** — `./.venv/bin/python search.py "some vague description
  of what I'm after"` returns the actual matching notes, even with zero
  vocabulary overlap. No third-party dependencies, so it runs fine from
  any machine with the vault synced and this endpoint reachable, not just
  the one running `ingest.py`.
- **`dedupe.py`** gets a second, semantic candidate-finding pass (see
  Pipeline above) — strictly additive to the existing heuristic, so it can
  only catch more genuine duplicates, never fewer, and never changes the
  final LLM confirmation step that actually decides.

`reindex_embeddings.py` backfills the index for a vault that predates
this, or repairs it if it's ever lost/corrupted:
```bash
./.venv/bin/python reindex_embeddings.py         # only notes missing from the index
./.venv/bin/python reindex_embeddings.py --all   # re-embed everything
```

Every one of these treats an unset or unreachable `EMBEDDING_URL` as
"feature unavailable," never an error — the vault has to work identically
for anyone who hasn't configured one.

## Anki automation (optional)

`06-Flashcards/*.tsv` are plain files — the simplest path is importing them
into Anki by hand whenever you want (File → Import, tab-separated, Basic
note type, "Allow HTML in fields" on since multi-line answers use `<br>`).

For hands-off syncing to a phone (AnkiDroid/AnkiMobile via AnkiWeb),
`anki_sync.py` pushes any new rows into a locally running Anki via
[AnkiConnect](https://ankiweb.net/shared/info/2055492159) and triggers a
sync to AnkiWeb, so the phone app picks them up on its own next sync. It
only needs to run on whichever machine you actually have Anki installed on
— not necessarily the same one running `ingest.py`, since Syncthing (or
any sync of your choice) already keeps `06-Flashcards/` current everywhere.
No third-party dependencies (stdlib only), so it doesn't need the `.venv`.

One-time setup, done manually (this needs your own AnkiWeb login, which
isn't something to automate):
1. Install AnkiConnect: `Tools → Add-ons → Get Add-ons…` in Anki, code
   `2055492159`, restart Anki when prompted.
2. Log into your AnkiWeb account via Anki's Sync button, once.
3. Install AnkiDroid or AnkiMobile on your phone and log into the same
   account.

After that, `anki_sync.py` only ever adds cards locally (deduped by a
per-course line-count watermark in `.anki_sync_state.json`, plus
AnkiConnect's own duplicate check as a second safety net) and asks Anki to
sync — it never touches your login. It no-ops quietly if Anki isn't
running, so it's safe to run on a timer (a `systemd --user` unit pair,
`anki-sync.timer` firing every 15 minutes, checking every 15 minutes
whether Anki happens to be open, works well).

## Methodology

The note model is a deliberate three-layer design, arrived at after the
first version (pure one-fact-per-note atomism) produced two real problems:
notes too small and fragmented to actually study from, and duplicate
content under differently-worded titles.

**Layer 1 — topic notes (`01-Notes/`).** Not one-fact atomism, not
whole-lecture consolidation: each note is one coherent subtopic — a concept
with its explanation and any worked examples that belong with it, roughly
textbook-subsection-sized. This is still cross-linked and tagged against
the *entire* vault regardless of course, because that cross-course linking
is the actual point: connecting new material to something already known is
one of the better-supported memorization techniques, and a concept taught
in two different courses is more useful side-by-side than merged away. This
is why `dedupe.py` is deliberately conservative — it only merges genuine
restatement of the *same* explanation, never two courses' distinct
treatments of a related idea. When unsure, it keeps notes separate.

**Layer 2 — Maps of Content (`05-MOCs/`).** Pure atomism has a
well-documented failure mode at scale: once a vault has hundreds of notes,
finding anything (or reading one lecture start-to-finish before an exam)
gets hard, because nothing sits above the atom layer. A MOC doesn't hold
content — it's a short, curated, linked walkthrough per lecture, plus a
roll-up per course, so "study this lecture" or "review this whole course"
is one document to open, while the underlying notes stay the reusable,
cross-linkable layer. Purely additive: MOCs never affect how notes
themselves are written, linked, or merged.

**Layer 3 — retrieval practice (`06-Flashcards/`).** Note architecture
alone doesn't drive exam performance as much as retrieval practice does —
testing yourself beats re-reading by a wide margin in the learning-science
literature. Rather than reimplementing spaced-repetition scheduling here,
each lecture's notes get turned into plain Anki-importable flashcards; Anki
already does scheduling well, so this only generates the cards.

Net effect: the vault stays a genuine cross-course knowledge graph (the
"second brain" premise), while still functioning as something you can
actually sit down and study from — a lecture's MOC to read, its notes for
depth, its flashcards for recall — rather than either a pile of unfindable
index cards or a set of disconnected per-lecture documents.

## Re-grounding and coverage recovery

- **`rebuild.py`** — re-grounds any note marked `status: llm-expanded`
  once a matching course source becomes available (auto-triggered after
  every source finishes indexing; also runnable standalone with
  `--course` or `--sources` to scope it).
- **`recover_missing.py`** — retroactive version of the coverage check in
  step 6 above, for lecture sources that were processed before that check
  existed (or as a periodic sanity pass). Re-extracts each source and
  re-runs OCR where needed, so it costs real calls — a backfill tool, not
  something to run on every timer tick.

## Integrity auditing

`audit.py` (broken `[[links]]`, orphan notes) runs automatically after any
batch that actually changed the vault — no manual step. A broken link fires
a critical desktop notification (`notify-send`); orphans are logged only,
not alerted, since a new orphan isn't
necessarily wrong — it needs a human call, not an alarm. Deliberately does
**not** auto-rewrite anything it finds — a fuzzy-match "auto-repair" was
tried and reverted after it silently mangled real `[[Title|alias]]` links
with no diff shown and no way to review. Report, don't guess. Run it by
hand anytime with `./.venv/bin/python audit.py` (read-only, no LLM calls,
free).

The one exception to "never auto-rewrite" is `dedupe.py`'s own merges: when
it redirects a new note into an existing title, that's an *exact* literal
title substitution it made itself in the same run (never a fuzzy guess
applied after the fact to content it didn't produce), and it's always one
`git revert` away.

## Model and rate limits

Currently `gemini-3.7-flash` (`config.GEMINI_MODEL`). Was `gemini-3.6-flash`
until its free-tier quota (20 requests, confirmed live to be tracked **per
model per project**, not just per key) got fully exhausted across all
configured keys by same-day testing, with no relief after real waits.
Switching model tag immediately unblocked the pipeline on the same keys —
worth remembering if this happens again: try a different model tag on the
same key before assuming the key itself is dead.

`gemini_client.py` rotates across multiple API keys (separate Google Cloud
projects — a single project's quota is shared across its own keys, so only
*separate* projects help): the primary key comes from the environment; add
more in `gemini-keys.txt` or the legacy `gemini_keys.txt` (one key per
line, mode `600`). The hyphenated filename takes precedence if both exist.
On a 429, it rotates to the next key immediately with no sleep,
records a cooldown for that (key, model) pair in `key_cooldowns.json` so a
*separate process* (the timer's next tick is always a fresh process) skips
straight past a key already known to be exhausted instead of wasting a
request reconfirming it. Only once every key is exhausted does it fall
back to honoring Google's own `retryDelay`.

## Known gaps

- **Semantic search (`search.py`, `embeddings.py`) is optional and
  unconfigured by default** — needs an OpenAI-compatible embeddings
  endpoint you supply yourself (`EMBEDDING_URL`); with nothing configured
  the vault behaves exactly as it did before this existed. Deliberately
  did *not* build GraphRAG-scale infrastructure (entity extraction,
  community detection, hierarchical summarization) — massive overkill,
  and expensive, for a personal vault this size when plain embeddings
  solve the actual gap (vocabulary-mismatch search) for a fraction of the
  cost.
- **`dedupe.py`'s final call is always an LLM judgment, not a
  guarantee** — both candidate-finding passes (title/tag heuristic, and
  semantic similarity when configured) are just narrowing; a genuine
  duplicate that clears neither pass can still slip through undetected.
  Tuned deliberately toward false negatives (missed duplicates) over false
  positives (wrongly merged, distinct cross-course notes), since a
  wrongful merge is the more destructive failure of the two.
- **JSON-mode LaTeX escaping**: Gemini's JSON output routinely ships
  single backslashes in LaTeX (`\text` instead of `\\text`), which breaks
  strict `json.loads`. Mitigated with an explicit prompt instruction plus
  `json_repair.loads` as a fallback (`gemini_client.json_call`).
- **`coverage.py`'s check shares some blind spots with the filing pass it's
  checking** — an LLM verifying its own output isn't a mathematical
  guarantee of completeness, just a structural catch for whole
  skipped categories/pages, which is the actual failure mode observed live.
- **MOCs and flashcards add LLM calls per lecture** (one narrative call, one
  flashcard-generation call, on top of filing/coverage/dedup) — a small,
  known cost for the study-usability gain, worth knowing if a rate-limited
  run needs to be triaged.
