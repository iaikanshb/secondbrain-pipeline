#!/usr/bin/env python3
"""Pushes new flashcards (06-Flashcards/*.tsv) into a locally running Anki
via AnkiConnect, then triggers a sync to AnkiWeb so phone apps (AnkiDroid /
AnkiMobile) pick them up on their own next sync.

Meant to run on a timer, same pattern as the ingest timer: no-ops quietly
if Anki isn't running (AnkiConnect only listens while the Anki GUI process
is open) rather than erroring, since that's the expected common case.

Only ever runs on whichever machine actually has Anki + AnkiConnect
installed -- not part of the ingest/filing pipeline itself, and doesn't
need to run on the same machine that produces the flashcards, only on
whichever one you review from (Syncthing already keeps 06-Flashcards/ in
sync between machines).

One-time manual prerequisite this script cannot do for you: open Anki once
and log into AnkiWeb yourself (the Sync button) -- that's your own
credential, not something to automate. After that, this script only ever
adds cards locally and asks Anki to sync; it never touches your login.
"""
import csv
import json
import os
import urllib.error
import urllib.request

import config

ANKICONNECT_URL = "http://127.0.0.1:8765"
STATE_FILE = os.path.join(config.PROJECT_ROOT, ".anki_sync_state.json")
DECK_PREFIX = "Second Brain"


def _call(action: str, **params):
    payload = json.dumps({"action": action, "version": 6, "params": params}).encode("utf-8")
    req = urllib.request.Request(ANKICONNECT_URL, data=payload)
    with urllib.request.urlopen(req, timeout=5) as resp:
        result = json.loads(resp.read())
    if result.get("error"):
        raise RuntimeError(result["error"])
    return result["result"]


def _load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def _save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def _add_one(note: dict) -> str:
    """Returns 'added', 'duplicate', or an error string. Deliberately uses
    the singular addNote, not the batch addNotes: confirmed live that this
    AnkiConnect build aborts an *entire* addNotes batch when even one note
    in it is a duplicate (contradicting canAddNotesWithErrorDetail, which
    had already cleared the same batch as addable moments earlier) --
    a run that had already partially succeeded on one course crashed
    outright on the next rather than skipping the one bad note. Singular
    calls cost more requests but isolate failures per card instead of per
    course, and at this card volume the extra requests are free."""
    payload = json.dumps({"action": "addNote", "version": 6, "params": {"note": note}}).encode("utf-8")
    req = urllib.request.Request(ANKICONNECT_URL, data=payload)
    with urllib.request.urlopen(req, timeout=5) as resp:
        result = json.loads(resp.read())
    err = result.get("error")
    if not err:
        return "added"
    return "duplicate" if "duplicate" in str(err).lower() else f"error: {err}"


def sync_course(course: str, path: str, state: dict) -> int:
    """Flashcards are only ever appended (see flashcards.py), never edited
    or reordered -- so a plain line-count watermark per course is enough
    to know exactly which rows are new, no hashing needed."""
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f, delimiter="\t"))

    already = state.get(course, 0)
    new_rows = rows[already:]
    if not new_rows:
        return 0

    deck = f"{DECK_PREFIX}::{course}"
    _call("createDeck", deck=deck)

    added = duplicates = 0
    failures = []
    for row in new_rows:
        if len(row) < 2:
            continue
        note = {
            "deckName": deck,
            "modelName": "KaTeX and Markdown Basic (Color)",
            "fields": {"Front": row[0], "Back": row[1]},
            "options": {"allowDuplicate": False, "duplicateScope": "deck"},
        }
        status = _add_one(note)
        if status == "added":
            added += 1
        elif status == "duplicate":
            duplicates += 1
        else:
            failures.append((row[0][:60], status))

    # Every row got a definite outcome (added/duplicate/logged failure), so
    # the watermark can safely advance past all of them -- nothing here is
    # silently lost, a genuine failure is just printed rather than retried
    # forever (matches ingest.py's own "one bad item logs, doesn't block
    # the batch" pattern rather than a stricter never-happens-again retry).
    state[course] = len(rows)
    if duplicates:
        print(f"  [{course}] {duplicates} already present in Anki, skipped")
    if failures:
        print(f"  [{course}] {len(failures)} card(s) failed to add:")
        for front, err in failures[:5]:
            print(f"    {front!r}: {err}")
    return added


def main() -> None:
    try:
        _call("version")
    except (urllib.error.URLError, ConnectionRefusedError, OSError):
        print("Anki not running / AnkiConnect unreachable, skipping.")
        return

    if not os.path.isdir(config.FLASHCARDS):
        print("no flashcards folder yet.")
        return

    state = _load_state()
    total_new = 0
    for fname in sorted(os.listdir(config.FLASHCARDS)):
        if not fname.endswith(".tsv"):
            continue
        course = fname[:-4]
        try:
            added = sync_course(course, os.path.join(config.FLASHCARDS, fname), state)
        except Exception as e:
            # One course's failure must not lose progress already made on
            # others or block the rest of the batch -- state is saved after
            # every course specifically so a crash here still keeps
            # whatever succeeded before it.
            print(f"[{course}] FAILED, will retry next run: {e}")
            _save_state(state)
            continue
        _save_state(state)
        if added:
            print(f"[{course}] pushed {added} new card(s)")
        total_new += added

    if total_new:
        try:
            _call("sync")
            print(f"synced to AnkiWeb ({total_new} new card(s) total)")
        except Exception as e:
            print(f"pushed {total_new} card(s) locally, but AnkiWeb sync failed: {e}")
    else:
        print("no new cards.")


if __name__ == "__main__":
    main()
