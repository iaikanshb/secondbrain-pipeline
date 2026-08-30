"""Retrieval-practice generation: a handful of recall questions per lecture,
appended to a plain front\\tback TSV that Anki imports directly as Basic
cards. Retrieval practice is separately one of the highest-yield exam
techniques, and Anki already does spaced-repetition scheduling well, so
this only generates the cards -- it doesn't reimplement a scheduler.
"""
import csv
import os

import config
import gemini_client

FLASHCARD_PROMPT = """Here are study notes from one lecture. Write concise retrieval-practice \
flashcards -- one clear question per fact/concept worth being able to recall cold for an exam, \
not vague or overly broad questions. Skip pure logistics (schedule, grading weights) unless \
it's the kind of thing that's actually tested.

Notes:
{notes}

Respond with a JSON array: [{{"front": "question", "back": "concise answer"}}, ...]. Aim for \
roughly one card per genuinely distinct testable fact -- don't pad, don't skip real content."""


def generate_flashcards(note_bodies: list[str]) -> list[dict]:
    if not note_bodies:
        return []
    result = gemini_client.json_call(FLASHCARD_PROMPT.format(notes="\n\n---\n\n".join(note_bodies)))
    return result if isinstance(result, list) else []


def append_flashcards(course: str, cards: list[dict]) -> str | None:
    if not cards:
        return None
    os.makedirs(config.FLASHCARDS, exist_ok=True)
    path = os.path.join(config.FLASHCARDS, f"{course or 'uncategorized'}.tsv")
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, delimiter="\t")
        for card in cards:
            front = (card.get("front") or "").strip().replace("\t", " ").replace("\n", "<br>")
            back = (card.get("back") or "").strip().replace("\t", " ").replace("\n", "<br>")
            if front and back:
                writer.writerow([front, back])
    return path
