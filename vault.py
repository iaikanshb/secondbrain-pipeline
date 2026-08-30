import os
import re
import subprocess

import yaml

import config
import courses as courses_mod


def sanitize_title(title: str) -> str:
    """The one place a title gets cleaned for filesystem/link safety --
    every caller that needs to know what a note's real, on-disk title will
    be (before it's actually written) must go through this, not just the
    write path itself, or a same-batch [[link]] built from the raw title
    can validate successfully against a title that never actually exists
    on disk."""
    return re.sub(r'[\\/:*?"<>|]', "", title).strip()


def existing_notes() -> list[dict]:
    """title + tags for every note already in the vault, for cross-linking."""
    notes = []
    if not os.path.isdir(config.NOTES):
        return notes
    for fname in sorted(os.listdir(config.NOTES)):
        if not fname.endswith(".md"):
            continue
        path = os.path.join(config.NOTES, fname)
        with open(path, encoding="utf-8") as f:
            content = f.read()
        if content.startswith("---"):
            end = content.find("\n---", 3)
            if end != -1:
                try:
                    fm = yaml.safe_load(content[3:end]) or {}
                except yaml.YAMLError:
                    fm = {}
                notes.append({
                    "title": fm.get("title", fname[:-3]),
                    "tags": fm.get("tags", []),
                    "course": fm.get("course", ""),
                })
    return notes


def known_courses() -> list[str]:
    """Already-normalized course codes -- every write path normalizes before
    storing (see _write_markdown), so this reflects real, deduplicated
    course identity, not raw LLM-improvised strings. Always includes the
    canonical list (courses.txt) even before a course has any content yet,
    so the very first document for a new course can still match correctly
    instead of guessing at a fresh label."""
    found = set(courses_mod.canonical()) | {n["course"] for n in existing_notes() if n.get("course")}
    if os.path.isdir(config.PRACTICE):
        for fname in os.listdir(config.PRACTICE):
            if not fname.endswith(".md"):
                continue
            with open(os.path.join(config.PRACTICE, fname), encoding="utf-8") as f:
                content = f.read()
            if content.startswith("---"):
                end = content.find("\n---", 3)
                if end != -1:
                    try:
                        fm = yaml.safe_load(content[3:end]) or {}
                        if fm.get("course"):
                            found.add(fm["course"])
                    except yaml.YAMLError:
                        pass
    return sorted(found)


_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|([^\]]+))?\]\]")


def defuse_invalid_links(body: str, valid_titles: set) -> str:
    """Replace any [[Title]] whose target isn't in valid_titles with plain
    text (the alias if given, else the target name) -- enforces the 'don't
    invent links' prompt instruction instead of just hoping the model
    follows it. A dead link into a note that doesn't exist is worse than no
    link at all."""
    def _repl(m: "re.Match") -> str:
        target, alias = m.group(1).strip(), m.group(2)
        if target in valid_titles:
            return m.group(0)
        return alias if alias else target

    return _WIKILINK_RE.sub(_repl, body)


def _read_markdown(path: str) -> tuple[dict, str]:
    with open(path, encoding="utf-8") as f:
        content = f.read()
    end = content.find("\n---", 3)
    fm = yaml.safe_load(content[3:end]) or {} if end != -1 else {}
    body = content[end + 4:].lstrip("\n") if end != -1 else content
    return fm, body


_MERGE_PROMPT = """Two versions of a note titled "{title}" need to become one -- they were filed \
separately (different source lectures covering the same course) and must not silently overwrite \
each other.

Existing body:
---
{existing}
---

New content to merge in:
---
{new}
---

Merge into one coherent body: keep everything factually distinct from both, deduplicate anything \
stated twice, resolve into clean prose/structure -- don't just concatenate with a separator. \
Preserve existing [[wikilinks]] and citations from either side. Start with a ## heading.

Output only the merged markdown body, nothing else."""


def _merge_status(a: str, b: str) -> str:
    flags = {f for f in (a or "clean").split("+") if f and f != "clean"}
    flags |= {f for f in (b or "clean").split("+") if f and f != "clean"}
    return "+".join(sorted(flags)) if flags else "clean"


def _write_markdown(target_dir: str, doc_type: str, title: str, course: str,
                     tags: list[str], source: str, created: str, status: str, body: str) -> str:
    # Obsidian resolves [[links]] against filenames, not the frontmatter
    # title field -- if a title has a character sanitize_title() strips from
    # the filename (e.g. a colon), a link using the raw title text would
    # point at a file that doesn't exist. Sanitize once, use it as both
    # filename and stored title, so the two can never drift apart.
    title = sanitize_title(title)
    path = os.path.join(target_dir, f"{title}.md")

    if os.path.exists(path):
        # A same-title note already exists -- merge instead of the previous
        # behavior (silent overwrite), which destroyed real content live:
        # a second lecture's recovery pass clobbered a hand-reconstructed
        # logistics note because both happened to produce the same title.
        import gemini_client
        existing_fm, existing_body = _read_markdown(path)
        body = gemini_client.text_call(
            _MERGE_PROMPT.format(title=title, existing=existing_body, new=body)
        )
        tags = sorted(set(existing_fm.get("tags", [])) | set(tags))
        existing_sources = {s.strip() for s in existing_fm.get("source", "").split(",") if s.strip()}
        source = ", ".join(sorted(existing_sources | {source}))
        status = _merge_status(existing_fm.get("status", ""), status)
        created = existing_fm.get("created", created)  # keep the earlier date
        course = existing_fm.get("course", course) or course

    fm = {
        "title": title,
        "type": doc_type,
        "course": courses_mod.reconcile(course, body),
        "tags": tags,
        "source": source,
        "created": created,
        "status": status,
    }
    frontmatter = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True)
    content = f"---\n{frontmatter}---\n\n{body.strip()}\n"
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


def write_note(title: str, course: str, tags: list[str], source: str,
                created: str, status: str, body: str) -> str:
    return _write_markdown(config.NOTES, "note", title, course, tags, source, created, status, body)


def write_practice(title: str, course: str, tags: list[str], source: str,
                    created: str, status: str, body: str) -> str:
    return _write_markdown(config.PRACTICE, "practice", title, course, tags, source, created, status, body)


def note_course(path: str) -> str:
    fm, _ = _read_markdown(path)
    return fm.get("course", "") or ""


def write_moc(title: str, course: str, tags: list[str], source: str, created: str,
              moc_type: str, body: str, overwrite: bool = False) -> str:
    """overwrite=True skips the normal same-title merge path -- used for the
    course-level roll-up, which is meant to be fully regenerated each time
    (a deterministic link list, not narrative content worth LLM-merging
    with its own previous version). Lecture MOCs go through the normal
    merge-safe path since a genuine title collision there should behave
    like everything else in the vault."""
    os.makedirs(config.MOCS, exist_ok=True)
    if overwrite:
        title = sanitize_title(title)
        path = os.path.join(config.MOCS, f"{title}.md")
        fm = {
            "title": title,
            "type": moc_type,
            "course": courses_mod.reconcile(course, body) if course else course,
            "tags": tags,
            "source": source,
            "created": created,
            "status": "clean",
        }
        frontmatter = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True)
        content = f"---\n{frontmatter}---\n\n{body.strip()}\n"
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return path
    return _write_markdown(config.MOCS, moc_type, title, course, tags, source, created, "clean", body)


def _sha256(path: str) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _content_hash(path: str) -> str:
    """Hash of the PDF's visible text, not its raw bytes -- a lecture PDF
    re-saved or re-exported (a browser re-download, "print to PDF" again)
    keeps identical rendered content but can get a fresh trailer/ID, so a
    raw byte hash misses the duplicate. Confirmed live: "L3.pdf" and
    "L3 (2).pdf" were byte-different (different sha256, same size) but
    text-identical, so the old byte-hash check filed both -- 7 redundant
    LLM calls and two duplicate lecture MOCs for one lecture. Falls back to
    a raw byte hash for anything pymupdf can't open as a PDF."""
    try:
        import hashlib

        import pymupdf
        doc = pymupdf.open(path)
        text = "".join(page.get_text() for page in doc)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()
    except Exception:
        return _sha256(path)


def stage_resource(source_path: str) -> tuple[str, bool]:
    """Returns (archived_path, is_new). archived_path may differ from the
    source's own filename. A genuine filename collision (two different
    courses both naming a lecture "L0.pdf", say) destroyed two real
    archived sources live before this existed: stage_resource did a blind
    overwrite, and every note filed from the first "L0.pdf" was left
    pointing at a completely different PDF with no warning. Now: a genuine
    collision gets a disambiguated name instead of overwriting anything.

    is_new is False whenever this content is already archived anywhere in
    02-Resources/, not just under the same filename -- an accidental
    re-drop under a different name (browser "(2)" suffix, a re-downloaded
    or re-exported file) must not silently re-run the whole filing
    pipeline against content that's already in the vault. Identity is by
    extracted text (see _content_hash), not raw bytes, since a re-export
    can change the file byte-for-byte while rendering identically -- so no
    byte-size prefilter either, a differing trailer can shift the size by
    a few bytes without changing a single visible character."""
    import shutil
    base = os.path.basename(source_path)
    dest = os.path.join(config.RESOURCES, base)
    src_hash = _content_hash(source_path)

    if os.path.exists(dest) and _content_hash(dest) == src_hash:
        return dest, False

    if os.path.isdir(config.RESOURCES):
        for fname in os.listdir(config.RESOURCES):
            existing = os.path.join(config.RESOURCES, fname)
            if existing == dest or not os.path.isfile(existing):
                continue
            if _content_hash(existing) == src_hash:
                return existing, False

    if os.path.exists(dest):
        name, ext = os.path.splitext(base)
        n = 2
        while os.path.exists(dest):
            dest = os.path.join(config.RESOURCES, f"{name} ({n}){ext}")
            n += 1

    shutil.copy2(source_path, dest)
    return dest, True


def git_commit(message: str, paths: list[str]) -> None:
    """paths must be exactly what this run touched -- notes written/merged
    plus the archived source. A blanket `git add -A` used to sweep in
    whatever else happened to be sitting in the working tree at commit
    time (a file still queued in an inbox, an in-progress Obsidian edit),
    which meant `git revert` on an automated commit could delete content
    that commit never touched."""
    rel = [os.path.relpath(p, config.VAULT) for p in paths]
    subprocess.run(["git", "add", "--", *rel], cwd=config.VAULT, check=True, capture_output=True)
    result = subprocess.run(
        ["git", "diff", "--cached", "--quiet"], cwd=config.VAULT
    )
    if result.returncode == 0:
        return  # nothing staged, nothing to commit
    subprocess.run(
        ["git", "commit", "-q", "-m", message], cwd=config.VAULT, check=True, capture_output=True
    )
