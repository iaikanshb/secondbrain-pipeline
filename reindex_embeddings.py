#!/usr/bin/env python3
"""Backfill/repair the embedding index (embeddings.py) for every note
already in the vault. Going forward this happens automatically per note
in filing.py -- this script exists for the one-time initial backfill, and
as a periodic sanity pass if the index file is ever lost, corrupted, or
just out of sync (e.g. a manual edit to a note in Obsidian doesn't trigger
a reindex on its own).

    ./.venv/bin/python reindex_embeddings.py            # only notes missing from the index
    ./.venv/bin/python reindex_embeddings.py --all       # re-embed everything, even if already indexed
"""
import os
import sys

import config
import embeddings


def main() -> None:
    if not embeddings.available():
        print("No embedding endpoint configured (set EMBEDDING_URL in .env). Nothing to do.")
        sys.exit(1)

    force = "--all" in sys.argv
    already = set() if force else embeddings.indexed_titles()

    if not os.path.isdir(config.NOTES):
        print("no notes directory found.")
        return

    done = skipped = failed = 0
    for fname in sorted(os.listdir(config.NOTES)):
        if not fname.endswith(".md"):
            continue
        title = fname[:-3]
        if title in already:
            skipped += 1
            continue
        with open(os.path.join(config.NOTES, fname), encoding="utf-8") as f:
            content = f.read()
        vector = embeddings.embed(f"{title}\n\n{content[:4000]}")
        if vector:
            embeddings.store(title, vector)
            done += 1
        else:
            failed += 1
            print(f"  failed to embed: {title}")

    print(f"indexed {done}, already up to date {skipped}, failed {failed}")


if __name__ == "__main__":
    main()
