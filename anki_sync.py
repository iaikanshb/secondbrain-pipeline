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


def sync_course(course: str, path: str, state: dict) -> int:
    """Flashcards are only ever appended (see flashcards.py), never edited
    or reordered -- so a plain line-count watermark per course is enough
    to know exactly which rows are new, no hashing needed. AnkiConnect's
    own allowDuplicate=False is kept as a second safety net, not the only
    mechanism, in case the state file is ever missing or reset."""
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f, delimiter="\t"))

    already = state.get(course, 0)
    new_rows = rows[already:]
    state[course] = len(rows)
    if not new_rows:
        return 0

    deck = f"{DECK_PREFIX}::{course}"
    _call("createDeck", deck=deck)

    notes = [
        {
            "deckName": deck,
            "modelName": "Basic",
            "fields": {"Front": row[0], "Back": row[1]},
            "options": {"allowDuplicate": False, "duplicateScope": "deck"},
        }
        for row in new_rows if len(row) >= 2
    ]
    if notes:
        _call("addNotes", notes=notes)
    return len(notes)


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
        added = sync_course(course, os.path.join(config.FLASHCARDS, fname), state)
        if added:
            print(f"[{course}] pushed {added} new card(s)")
        total_new += added

    _save_state(state)

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
