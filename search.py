#!/usr/bin/env python3
"""Semantic search over the vault's notes -- finds conceptually related
notes even when the wording doesn't match, using the local embedding
index (embeddings.py). Complements Omnisearch (fast keyword/fuzzy search
in Obsidian) rather than replacing it -- use Omnisearch when you remember
roughly the words used, this when you only remember the idea.

    ./.venv/bin/python search.py "how machines get told to trust each other"

Runs fine with no third-party dependencies on any machine that has the
vault synced (via Syncthing or otherwise) and EMBEDDING_URL/
EMBEDDING_API_KEY configured -- doesn't need to be the machine running
ingest.py.
"""
import sys

import config
import embeddings


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    query = " ".join(sys.argv[1:])

    if not embeddings.available():
        print("No embedding endpoint configured (set EMBEDDING_URL in .env). "
              "Semantic search is disabled -- try Omnisearch in Obsidian instead.")
        sys.exit(1)

    vector = embeddings.embed(query)
    if vector is None:
        print("Embedding server unreachable right now.")
        sys.exit(1)

    hits = embeddings.search(vector, limit=10)
    if not hits:
        print(f"Index is empty ({config.EMBEDDINGS_DB}) -- run reindex_embeddings.py first.")
        return

    for title, score in hits:
        print(f"{score:.3f}  {title}")


if __name__ == "__main__":
    main()
