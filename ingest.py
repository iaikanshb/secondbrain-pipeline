#!/usr/bin/env python3
"""Ingest PDFs into the second-brain vault. Two drop zones, not one --
which folder a file lands in decides how it's processed:

    00-Inbox-Lectures/  lecture slides, tutorials, exam papers.
                         Classified lecture-vs-practice by content, then
                         either split into atomic notes (01-Notes/) or kept
                         intact as practice material (04-Practice/).
    00-Inbox-Sources/    textbooks, research papers, any reference material.
                         Always indexed locally for grounding lookups --
                         never atomized into notes, no filing LLM call at
                         all, regardless of length.

    ./venv/bin/python ingest.py --inbox     # process both inboxes
    ./venv/bin/python ingest.py <path.pdf> --source   # one file, source path
    ./venv/bin/python ingest.py <path.pdf>             # one file, lecture path

Nothing here touches llama-server or the GPU -- every model call is a
cloud request to Gemini.
"""
import datetime
import json
import os
import subprocess
import sys

import audit
import classify
import config
import coverage
import extract
import filing
import flashcards
import moc
import practice
import rebuild
import textbook_index
import vault


def _notify(title: str, body: str, urgency: str = "normal") -> None:
    try:
        subprocess.run(["notify-send", "-u", urgency, "-a", "secondbrain", title, body],
                        timeout=5, check=False)
    except (FileNotFoundError, subprocess.SubprocessError):
        pass


def _load_failure_state() -> dict:
    if not os.path.exists(config.FAILURE_STATE_FILE):
        return {}
    try:
        with open(config.FAILURE_STATE_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_failure_state(state: dict) -> None:
    try:
        with open(config.FAILURE_STATE_FILE, "w") as f:
            json.dump(state, f, indent=2, sort_keys=True)
    except OSError:
        pass


def _file_identity(path: str) -> tuple:
    st = os.stat(path)
    return (st.st_mtime_ns, st.st_size)


def _record_failure(path: str, err: Exception) -> int:
    """Increment the consecutive-failure count for this exact file content.
    Keyed by (mtime, size): re-dropping an edited file starts a fresh
    count -- only byte-identical repeat failures accumulate. Returns the
    new count."""
    state = _load_failure_state()
    ident = list(_file_identity(path))
    entry = state.get(path)
    if entry and entry.get("ident") == ident:
        entry["count"] += 1
    else:
        entry = {"ident": ident, "count": 1}
    state[path] = entry
    _save_failure_state(state)
    return entry["count"]


def _clear_failure(path: str) -> None:
    state = _load_failure_state()
    if state.pop(path, None) is not None:
        _save_failure_state(state)


def _held_out(path: str) -> bool:
    """True when this file has failed MAX_FILE_FAILURES times in a row with
    no modification in between. Held files are skipped (with a log line) so
    a poison file -- one that fails deterministically, like a deck that
    makes Gemini return empty content -- stops consuming a timer tick and
    API quota every run. It is NOT removed: fixing the underlying condition
    or re-dropping the file (fresh mtime/size) automatically re-queues it."""
    entry = _load_failure_state().get(path)
    if not entry:
        return False
    if entry.get("ident") != list(_file_identity(path)):
        return False  # file changed since the failures -- retry freely
    return entry.get("count", 0) >= config.MAX_FILE_FAILURES


def ingest_source(pdf_path: str, remove_source_after: bool = False) -> None:
    """A textbook, paper, or other reference material -- indexed for
    grounding lookups, never turned into notes."""
    # Stage first, before anything else references a filename: a genuine
    # collision (two different courses both dropping an "L0.pdf") gets a
    # disambiguated archived name back, and that -- not the original
    # filename -- has to be what every note/index entry cites as source,
    # or they end up pointing at whatever the *next* colliding upload was.
    staged_path, is_new = vault.stage_resource(pdf_path)
    name = os.path.basename(staged_path)

    if not is_new:
        print(f"[{os.path.basename(pdf_path)}] identical content already indexed as {name}, skipping.")
        if remove_source_after:
            os.remove(pdf_path)
        return

    try:
        print(f"[{name}] extracting (source, no OCR fallback)...")
        result = extract.extract_pdf(staged_path, skip_ocr=True)

        sample = "\n".join(p for p in result.pages[:5] if p)
        course_guess = classify.infer_course_for_source(sample, vault.known_courses(), filename=name)
        print(f"[{name}] inferred course: {course_guess}")

        indexed, course_norm = textbook_index.index_textbook(staged_path, course_guess, result=result)
        print(f"[{name}] indexed {indexed}/{result.page_count} pages for grounding lookups (course={course_norm})")

        vault.git_commit(
            f"Index {name} as source: {indexed}/{result.page_count} pages, course={course_norm}",
            # The manifest entry is half of what makes this index durable
            # (see config.TEXTBOOK_MANIFEST) -- it must be committed in the
            # same commit as the archived source, or a later cache loss has
            # nothing to heal from.
            paths=[staged_path, config.TEXTBOOK_MANIFEST],
        )
        print(f"[{name}] committed.")
    except Exception:
        # stage_resource() copies into 02-Resources/ *before* any of the
        # above -- that copy alone is what "is_new" checks against, so an
        # interruption anywhere in this block (network stall, a kill, a
        # crash) used to leave an uncommitted copy sitting there forever.
        # Every later run then saw "identical content already indexed"
        # and silently deleted the inbox original with nothing ever
        # filed -- confirmed live, cost two lecture PDFs their notes.
        # Remove the orphaned copy so a retry is treated as genuinely new.
        if os.path.exists(staged_path):
            os.remove(staged_path)
        raise

    # Only remove the original inbox file once everything above actually
    # succeeded -- staging alone isn't enough. A failure between staging
    # and here used to still remove the inbox original (staging+removal
    # were both done upfront), so a transient failure (a 429, a DNS blip)
    # silently dropped the file from the retry queue even though nothing
    # had actually been filed yet -- confirmed live, not hypothetical.
    # stage_resource() is idempotent on retry (identical content is a
    # no-op, returns the same already-staged path), so re-running this
    # whole function on the same still-present inbox file is always safe.
    if remove_source_after:
        os.remove(pdf_path)

    # The exact scenario this closes: a lecture got filed (and its terse
    # slides expanded from general knowledge, flagged llm-expanded) before
    # this course had any indexed source -- now one just landed, so
    # anything waiting on it gets re-grounded immediately, not left stale
    # until someone remembers to check.
    regrounded = rebuild.rebuild(course_filter=course_norm)
    if regrounded:
        print(f"[{name}] re-grounded {len(regrounded)} previously-unsourced note(s): {', '.join(regrounded)}")


def ingest_lecture(pdf_path: str, remove_source_after: bool = False) -> None:
    """Lecture slides, tutorials, or exam papers -- classified and filed
    either as atomic notes or as intact practice material."""
    # Stage first -- see ingest_source for why. A genuine filename
    # collision destroyed two real archived sources live before this
    # existed (two different courses both naming a lecture "L0.pdf"); every
    # note filed here must cite whatever the archive actually ended up
    # calling it, not the original (possibly colliding) filename.
    staged_path, is_new = vault.stage_resource(pdf_path)
    name = os.path.basename(staged_path)

    if not is_new:
        print(f"[{os.path.basename(pdf_path)}] identical content already in the vault as {name}, skipping.")
        if remove_source_after:
            os.remove(pdf_path)
        return

    try:
        print(f"[{name}] extracting...")
        result = extract.extract_pdf(staged_path)
        print(
            f"[{name}] {result.page_count} pages: "
            f"{result.pages_extracted_directly} text-layer, {result.pages_ocred} vision-OCR'd, "
            f"{len(result.review_flags)} flagged for review"
        )

        doc_type = classify.classify_document(result.text)
        print(f"[{name}] classified as: {doc_type}")

        touched = [staged_path]

        if doc_type == "practice":
            print(f"[{name}] filing as practice material...")
            path = practice.file_practice(result.text, name, result.review_flags)
            print(f"  wrote {os.path.relpath(path, config.VAULT)}")
            touched.append(path)
            flag_note = f", {len(result.review_flags)} page(s) need review" if result.review_flags else ""
            commit_msg = f"Ingest {name} as practice material{flag_note}"
        else:
            print(f"[{name}] filing into vault...")
            written = filing.file_transcript(result.text, name, result.review_flags)
            for p in written:
                print(f"  wrote {os.path.relpath(p, config.VAULT)}")

            print(f"[{name}] checking coverage...")
            note_bodies = []
            for p in written:
                with open(p, encoding="utf-8") as f:
                    note_bodies.append(f.read())
            missing = coverage.check_coverage(result.text, note_bodies)
            recovered = []
            if missing:
                print(f"[{name}] {len(missing)} page(s) with uncovered content, recovering...")
                recovered = filing.file_transcript(
                    coverage.missing_text(result.pages, missing), f"{name} (coverage recovery)", []
                )
                for p in recovered:
                    print(f"  recovered: {os.path.relpath(p, config.VAULT)}")
                written += recovered

            touched += written

            # Structural layer, purely additive: a per-lecture MOC narrating
            # the notes just written, plus a full regeneration of that
            # course's roll-up MOC. Skipped (returns None) if too few notes
            # came out of this source for a walkthrough to be worth anything.
            today = datetime.date.today().isoformat()
            course = vault.note_course(written[0]) if written else ""
            note_titles = [os.path.splitext(os.path.basename(p))[0] for p in written]
            moc_path = moc.build_lecture_moc(name, course, note_titles, today)
            if moc_path:
                print(f"[{name}] wrote lecture MOC: {os.path.relpath(moc_path, config.VAULT)}")
                touched.append(moc_path)
                course_moc_path = moc.build_course_moc(course, today)
                if course_moc_path:
                    touched.append(course_moc_path)

            # Retrieval practice, generated from the same notes -- appended
            # to this course's Anki-importable deck, not scheduled here.
            fresh_bodies = []
            for p in written:
                with open(p, encoding="utf-8") as f:
                    fresh_bodies.append(f.read())
            cards = flashcards.generate_flashcards(fresh_bodies)
            card_path = flashcards.append_flashcards(course, cards)
            if card_path:
                print(f"[{name}] added {len(cards)} flashcard(s) to {os.path.relpath(card_path, config.VAULT)}")
                touched.append(card_path)

            flag_note = f", {len(result.review_flags)} page(s) need review" if result.review_flags else ""
            recovery_note = f", {len(recovered)} recovered by coverage check" if recovered else ""
            commit_msg = (
                f"Ingest {name}: {len(written)} note(s) "
                f"({result.pages_extracted_directly} text-layer, {result.pages_ocred} OCR'd{flag_note}{recovery_note})"
            )

        vault.git_commit(commit_msg, paths=touched)
        print(f"[{name}] committed.")
    except Exception:
        # See ingest_source() for why this matters: stage_resource() copies
        # into 02-Resources/ before any of the above runs, and that copy
        # alone is what a future run's "is_new" check sees. An interruption
        # here (this exact bug cost EML_Lecture1_GC.pdf and
        # EML_Lectures2-3_GC.pdf their notes, twice, live) used to leave
        # that copy behind forever, silently skipped on every retry with
        # the inbox original already deleted. Remove it so a retry is
        # treated as genuinely new content.
        if os.path.exists(staged_path):
            os.remove(staged_path)
        raise

    # Only remove the original inbox file once everything above actually
    # succeeded -- see ingest_source() for why this can't happen any
    # earlier. Confirmed live: a DNS blip mid-coverage-check silently
    # dropped a file from the retry queue when staging+removal happened
    # upfront instead.
    if remove_source_after:
        os.remove(pdf_path)


def _process_inbox(dir_path: str, handler) -> tuple[list[str], int]:
    """Returns (failed filenames, count that succeeded)."""
    pdfs = [
        os.path.join(dir_path, f)
        for f in sorted(os.listdir(dir_path))
        if f.lower().endswith(".pdf")
    ]
    failures = []
    held = 0
    for p in pdfs:
        try:
            if _held_out(p):
                # A file that has failed MAX_FILE_FAILURES times in a row
                # (unchanged) stops being retried every tick -- it stays in
                # the inbox, but silent. Fix the condition or re-drop the
                # file and it re-queues itself (see _held_out).
                held += 1
                print(f"[{os.path.basename(p)}] failed {config.MAX_FILE_FAILURES}+ "
                      f"times in a row -- held out of the retry queue (will "
                      f"re-queue if the file changes)")
                continue

            handler(p, remove_source_after=True)
            _clear_failure(p)
        except FileNotFoundError:
            # Vanished between the listdir() above and this file's turn
            # (e.g. a sync client mid-delete) -- there's nothing left to
            # retry, so this isn't a real failure. Confirmed live: a
            # stale/synced-away "L3.pdf" produced a misleading "FAILED,
            # left in inbox for retry" for a file that was never coming
            # back.
            print(f"[{os.path.basename(p)}] no longer in the inbox, skipping.")
        except Exception as e:
            # One bad/rate-limited file must not block the rest of the
            # batch -- it stays in the inbox (not removed on failure) and
            # the timer's next run retries it automatically. The failure
            # count is keyed to the file's (mtime, size), so an
            # edited/re-dropped file starts a fresh count instead of
            # inheriting the old file's failures.
            count = _record_failure(p, e)
            if count >= config.MAX_FILE_FAILURES:
                _notify(
                    "second-brain: file held out of retry queue",
                    f"{os.path.basename(p)} failed {count} times in a row "
                    f"({e}) -- NOT retrying until it changes. See ingest.log",
                    urgency="critical",
                )
            print(f"[{os.path.basename(p)}] FAILED, left in inbox for retry: {e}")
            failures.append(os.path.basename(p))
    if held:
        print(f"({held} file(s) held out of retry -- see ingest.log / notifications)")
    return failures, len(pdfs) - len(failures) - held


def _run_audit() -> None:
    """Read-only, no LLM calls -- cheap enough to run after every batch that
    actually changed the vault. Broken links are a real bug (a link was
    supposed to resolve and doesn't) and get a critical desktop notification.
    Orphans are only ever informational here --
    a new orphan isn't necessarily wrong, it needs a human call, not an
    alarm -- so it's logged, not pushed."""
    print("\n--- audit ---")
    broken, orphans = audit.audit()
    if broken:
        _notify("second-brain: broken links found",
                f"{len(broken)} link(s) point to notes that don't exist -- check ingest.log",
                urgency="critical")
    if orphans:
        print(f"({len(orphans)} orphan(s) -- not necessarily a problem, see README)")


def _self_heal_index() -> None:
    """Repair the grounding index before anything consults it. The DB is a
    rebuildable cache; the manifest (committed in the vault) is durable --
    so a lost/corrupt/truncated cache is rebuilt here from the archived
    sources, instead of silently degrading every filing decision to "no
    source for this course" (the exact failure that cost every course its
    textbook grounding between 2026-08-25 and 2026-09-01)."""
    healed = textbook_index.self_heal()
    if healed:
        print(f"[self-heal] rebuilt {len(healed)} missing source(s) into the index: "
              f"{', '.join(os.path.basename(h) for h in healed)}")
        vault.git_commit(
            f"Self-heal textbook index: rebuilt {len(healed)} source(s) from manifest",
            paths=[config.TEXTBOOK_MANIFEST],
        )


def main() -> None:
    if len(sys.argv) == 2 and sys.argv[1] == "--inbox":
        _self_heal_index()
        # Lectures first: lecture material usually self-identifies its
        # course clearly (a title/header), while a paper or textbook often
        # doesn't -- so lectures establish the course name known_courses()
        # offers, and a source dropped in the same batch can then actually
        # match it instead of guessing its own new label. (Grounding lookups
        # aren't affected by this order -- they happen per-note at filing
        # time regardless of when a source was indexed.)
        lecture_failures, lecture_ok = _process_inbox(config.INBOX_LECTURES, ingest_lecture)
        source_failures, source_ok = _process_inbox(config.INBOX_SOURCES, ingest_source)
        total_failed = len(lecture_failures) + len(source_failures)
        total_ok = lecture_ok + source_ok

        if total_failed:
            print(f"{total_failed} file(s) failed this run, left for retry: "
                  f"{', '.join(lecture_failures + source_failures)}")
        if total_ok:
            print(f"done: {total_ok} file(s) processed.")
        elif not total_failed:
            print("both inboxes empty.")

        if total_ok:
            # Only worth the (cheap, local, no-LLM) check when the vault
            # actually changed -- a pure no-op run has nothing new to audit.
            _run_audit()
        return

    if len(sys.argv) == 3 and sys.argv[2] == "--source":
        ingest_source(sys.argv[1])
        _run_audit()
        return

    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)

    ingest_lecture(sys.argv[1])
    _run_audit()


if __name__ == "__main__":
    main()
