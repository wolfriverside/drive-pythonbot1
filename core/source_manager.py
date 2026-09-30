"""
core/source_manager.py

Knowledge Source Library - per-user knowledge base manager.
Part of the core layer (same as memory_manager), NOT an AI-callable tool.
Manages external documents/URLs added by the user or suggested by AI.

Independent from active_memory (long-term memory):
  active_memory  <- AI自動摘要的對話記憶
  sources        <- 使用者手動加入的外部文獻/網址

All data lives INSIDE the existing user folder:
  database/users/{uid}/
  ├── profile.json         <- existing user profile (unchanged)
  ├── uploads/             <- existing uploads folder (unchanged)
  └── sources/             <- NEW subfolder (created on first use)
      └── {source_id}.json  <- metadata + embedded chunks
"""

import os
import json
import uuid
from datetime import datetime, timezone


SOURCES_DIRNAME = "sources"


def _sources_dir(uid: str) -> str:
    d = os.path.join("database", "users", uid, SOURCES_DIRNAME)
    os.makedirs(d, exist_ok=True)
    return d


def list_sources(uid: str) -> list:
    """Return list of source metadata dicts (no chunks/embeddings)."""
    d = _sources_dir(uid)
    sources = []
    for fname in sorted(os.listdir(d)):
        if not fname.endswith(".json"):
            continue
        try:
            with open(os.path.join(d, fname), "r", encoding="utf-8") as f:
                s = json.load(f)
            sources.append({
                "source_id": s["source_id"],
                "type": s["type"],
                "label": s["label"],
                "path_or_url": s["path_or_url"],
                "added_at": s["added_at"],
                "summary": s.get("summary", ""),
                "chunk_count": len(s.get("chunks", [])),
            })
        except Exception:
            continue
    return sources


def get_source(uid: str, source_id: str) -> dict:
    """Return full source data including chunks."""
    path = os.path.join(_sources_dir(uid), f"{source_id}.json")
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def remove_source(uid: str, source_id: str) -> bool:
    path = os.path.join(_sources_dir(uid), f"{source_id}.json")
    if os.path.exists(path):
        os.remove(path)
        return True
    return False


def add_source(uid: str, path_or_url: str, label: str = "") -> dict:
    """
    Add a new knowledge source (URL or local file path).
    Downloads/reads content, chunks and embeds it, persists to disk.
    Returns {"ok": bool, "source_id": str, "message": str}
    """
    from tools.file_reader import read_local_file
    from core.embedding import chunk_text, get_embeddings_batch

    try:
        raw_text = read_local_file(path_or_url)
    except Exception as e:
        return {"ok": False, "source_id": "", "message": f"讀取失敗：{e}"}

    if not raw_text or raw_text.startswith("Error:"):
        return {"ok": False, "source_id": "", "message": raw_text or "無法取得內容"}

    if not label:
        if path_or_url.startswith("http"):
            from urllib.parse import urlparse
            parsed = urlparse(path_or_url)
            label = parsed.netloc + parsed.path[:40]
        else:
            label = os.path.basename(path_or_url)

    chunks_text = chunk_text(raw_text, chunk_size=600, overlap=80, max_chunks=50)
    embeddings = get_embeddings_batch(chunks_text)

    chunks = []
    for i, ct in enumerate(chunks_text):
        emb = embeddings[i] if i < len(embeddings) else []
        chunks.append({"text": ct, "embedding": emb if emb else []})

    source_id = uuid.uuid4().hex[:10]
    entry = {
        "source_id": source_id,
        "type": "url" if path_or_url.startswith("http") else "file",
        "label": label,
        "path_or_url": path_or_url,
        "added_at": datetime.now(timezone.utc).isoformat(),
        "summary": raw_text[:200].replace("\n", " "),
        "chunks": chunks,
    }

    dest = os.path.join(_sources_dir(uid), f"{source_id}.json")
    with open(dest, "w", encoding="utf-8") as f:
        json.dump(entry, f, ensure_ascii=False, indent=2)

    return {
        "ok": True,
        "source_id": source_id,
        "message": f"已加入知識來源「{label}」（{len(chunks)} 段落）",
    }


def retrieve_from_sources(uid: str, query: str, top_k: int = 4, min_score: float = 0.65) -> str:
    """
    Semantic search across all user sources.
    Returns formatted relevant chunks to inject into AI system context.
    Only returns chunks with cosine similarity >= min_score to avoid polluting the prompt.
    """
    from core.embedding import get_embedding, cosine_similarity

    d = _sources_dir(uid)
    if not os.path.isdir(d) or not os.listdir(d):
        return ""

    query_emb = get_embedding(query)
    if not query_emb:
        return ""

    scored = []
    for fname in os.listdir(d):
        if not fname.endswith(".json"):
            continue
        try:
            with open(os.path.join(d, fname), "r", encoding="utf-8") as f:
                s = json.load(f)
            label = s.get("label", fname)
            for chunk in s.get("chunks", []):
                emb = chunk.get("embedding", [])
                if emb:
                    score = cosine_similarity(query_emb, emb)
                    if score >= min_score:
                        scored.append((score, label, chunk["text"]))
        except Exception:
            continue

    if not scored:
        return ""

    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:top_k]

    lines = ["[知識來源相關段落]"]
    for score, label, text in top:
        lines.append(f"\n來源：{label}（相關度 {score:.2f}）\n{text}")
    return "\n".join(lines)
