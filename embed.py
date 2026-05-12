#!/usr/bin/env python3
"""
embed.py — Generate local embeddings for all content items and
pre-compute top-5 nearest neighbors for each item.

Uses sentence-transformers (all-MiniLM-L6-v2, ~80MB, free, local).
No API key required.

Usage:
    python3 embed.py
    python3 embed.py --force     # re-embed everything (ignores cache)
    python3 embed.py --model all-mpnet-base-v2   # larger/slower/better model

Writes:
    static/data/embeddings.json     — {item_id: [384 floats]}
    static/data/similarities.json   — {item_id: [{id, score, type, title}×5]}
"""
import argparse
import json
from pathlib import Path

import numpy as np
from sentence_transformers import SentenceTransformer

DATA_DIR = Path("static/data")
DEFAULT_MODEL = "all-MiniLM-L6-v2"
TOP_K = 5
BATCH_SIZE = 64


# ── Text builders ─────────────────────────────────────────────────────────────

def doc_text(doc):
    parts = [doc.get("title") or ""]
    abstract = doc.get("abstract") or doc.get("docAbstract") or ""
    if abstract:
        parts.append(abstract[:600])
    return "\n".join(p for p in parts if p).strip()


def fr_doc_text(doc):
    parts = [doc.get("title") or ""]
    if doc.get("action"):
        parts.append(doc["action"])
    if doc.get("abstract"):
        parts.append(doc["abstract"][:600])
    return "\n".join(p for p in parts if p).strip()


def press_text(pr):
    parts = [pr.get("title") or ""]
    if pr.get("body"):
        parts.append(pr["body"][:600])
    return "\n".join(p for p in parts if p).strip()


# ── Similarity ────────────────────────────────────────────────────────────────

def cosine_similarity_matrix(matrix):
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1
    normalized = matrix / norms
    return (normalized @ normalized.T).astype(np.float32)


def build_similarities(embeddings_dict, item_meta):
    ids = list(embeddings_dict.keys())
    matrix = np.array([embeddings_dict[i] for i in ids], dtype=np.float32)
    sim_matrix = cosine_similarity_matrix(matrix)

    similarities = {}
    for i, item_id in enumerate(ids):
        row = sim_matrix[i]
        sorted_idx = np.argsort(row)[::-1]
        neighbors = []
        for j in sorted_idx:
            if ids[j] == item_id:
                continue
            neighbors.append({
                "id": ids[j],
                "score": float(row[j]),
                "type": item_meta.get(ids[j], {}).get("type", "unknown"),
                "title": item_meta.get(ids[j], {}).get("title", ""),
            })
            if len(neighbors) >= TOP_K:
                break
        similarities[item_id] = neighbors

    return similarities


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true",
                        help="Re-embed everything (ignore cache)")
    parser.add_argument("--model", default=DEFAULT_MODEL,
                        help=f"sentence-transformers model name (default: {DEFAULT_MODEL})")
    args = parser.parse_args()

    print(f"Loading model '{args.model}' (downloads ~80MB on first run)…")
    model = SentenceTransformer(args.model)

    # Load existing embeddings (cache)
    emb_path = DATA_DIR / "embeddings.json"
    embeddings = {}
    if emb_path.exists() and not args.force:
        embeddings = json.load(open(emb_path))
        print(f"Loaded {len(embeddings)} cached embeddings")

    # Collect all items
    items_text = []   # [(id, text), ...]
    item_meta = {}    # {id: {type, title}}

    doc_path = DATA_DIR / "documents.json"
    if doc_path.exists():
        docs = json.load(open(doc_path))
        substantive = [d for d in docs
                       if d.get("documentType") in ("Rule", "Proposed Rule", "Notice")]
        print(f"documents.json: {len(substantive)} substantive docs")
        for doc in substantive:
            item_id = doc["documentId"]
            items_text.append((item_id, doc_text(doc)))
            item_meta[item_id] = {"type": "regulation", "title": doc.get("title", "")}

    fr_path = DATA_DIR / "fr_documents.json"
    if fr_path.exists():
        fr_docs = json.load(open(fr_path))
        print(f"fr_documents.json: {len(fr_docs)} FR docs")
        for num, doc in fr_docs.items():
            item_id = f"FR-{num}"
            items_text.append((item_id, fr_doc_text(doc)))
            item_meta[item_id] = {"type": "fr_rule", "title": doc.get("title", "")}

    press_path = DATA_DIR / "press_releases.json"
    if press_path.exists():
        press = json.load(open(press_path))
        print(f"press_releases.json: {len(press)} press releases")
        for pr in press:
            item_id = f"PR-{pr['pressId']}"
            items_text.append((item_id, press_text(pr)))
            item_meta[item_id] = {"type": "press_release", "title": pr.get("title", "")}

    if not items_text:
        print("No items to embed. Run the fetch scripts first.")
        return

    print(f"\nTotal items: {len(items_text)}")

    # Only embed items not already cached
    to_embed = [(item_id, text) for item_id, text in items_text
                if args.force or item_id not in embeddings]
    print(f"Items to embed: {len(to_embed)} (cached: {len(items_text) - len(to_embed)})")

    if to_embed:
        ids   = [x[0] for x in to_embed]
        texts = [x[1] if x[1].strip() else "(no text)" for x in to_embed]

        print("Encoding… (this takes 30–120s on CPU)")
        vecs = model.encode(
            texts,
            batch_size=BATCH_SIZE,
            show_progress_bar=True,
            convert_to_numpy=True,
        )
        for item_id, vec in zip(ids, vecs):
            embeddings[item_id] = vec.tolist()

    print(f"\nEmbedding complete. Total: {len(embeddings)}")
    with open(emb_path, "w") as f:
        json.dump(embeddings, f)
    print(f"Saved {emb_path}")

    print("\nBuilding similarity index…")
    sims = build_similarities(embeddings, item_meta)
    sim_path = DATA_DIR / "similarities.json"
    with open(sim_path, "w") as f:
        json.dump(sims, f, indent=2)
    print(f"Saved {sim_path} ({len(sims)} items)")

    sample_ids = list(sims.keys())[:3]
    print("\nSample nearest neighbors:")
    for item_id in sample_ids:
        print(f"  {item_id[:60]}")
        for n in sims[item_id][:2]:
            print(f"    → [{n['score']:.3f}] {n['id'][:50]} ({n['type']})")


if __name__ == "__main__":
    main()
