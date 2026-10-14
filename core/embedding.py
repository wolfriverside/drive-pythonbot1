import ollama
import numpy as np
from functools import lru_cache

# ── Embedding 設定 ──────────────────────────────────────────────────────────
_EMBED_MODEL = "nomic-embed-text:latest"


def chunk_text(text: str, chunk_size: int = 800, overlap: int = 100, max_chunks: int = 40) -> list:
    """Simple character-based chunking with overlap and maximum chunk limit."""
    chunks = []
    start = 0
    while start < len(text) and len(chunks) < max_chunks:
        end = start + chunk_size
        chunks.append(text[start:end])
        start += chunk_size - overlap
    return chunks


@lru_cache(maxsize=512)
def _get_embedding_cached(text: str) -> tuple:
    """
    帶 LRU 快取的內部 embedding 函式。
    回傳 tuple（可 hash），lru_cache 才能使用。
    最多快取 512 筆，重複查詢省去 Ollama 往返延遲。
    """
    try:
        response = ollama.embeddings(model=_EMBED_MODEL, prompt=text)
        return tuple(response.get("embedding", []))
    except Exception as e:
        print(f"[embedding] Embedding error: {e}")
        return ()


def get_embedding(text: str) -> list:
    """Call Ollama to get embeddings (with LRU cache)."""
    return list(_get_embedding_cached(text))


def get_embeddings_batch(texts: list) -> list:
    """Call Ollama to get batch embeddings efficiently."""
    if not texts:
        return []
    try:
        # Use official batch embed API if available
        if hasattr(ollama, "embed"):
            response = ollama.embed(model=_EMBED_MODEL, input=texts)
            return response.get("embeddings", [])
    except Exception as e:
        print(f"[embedding] Batch embedding fallback due to: {e}")

    # Fallback to serial embeddings (uses LRU cache)
    return [get_embedding(t) for t in texts]


def cosine_similarity(a: list, b: list) -> float:
    """Calculate cosine similarity between two vectors."""
    if not a or not b:
        return 0.0
    dot = np.dot(a, b)
    norm_a = np.linalg.norm(a)
    norm_b = np.linalg.norm(b)
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return float(dot / (norm_a * norm_b))


def retrieve_relevant_chunks(query: str, doc_text: str, top_k: int = 4) -> str:
    """Chunk the document, embed chunks in batch, and find the top_k most relevant to the query."""
    if not query.strip():
        return doc_text[:4000]

    query_emb = get_embedding(query)
    if not query_emb:
        return doc_text[:4000]  # Fallback

    chunks = chunk_text(doc_text)
    if not chunks:
        return ""

    chunk_embeddings = get_embeddings_batch(chunks)
    scored_chunks = []

    for i, c in enumerate(chunks):
        if i < len(chunk_embeddings) and chunk_embeddings[i]:
            score = cosine_similarity(query_emb, chunk_embeddings[i])
            scored_chunks.append((score, c))

    scored_chunks.sort(key=lambda x: x[0], reverse=True)
    best_chunks = [c for score, c in scored_chunks[:top_k]]
    return "\n\n--- [相關內文段落] ---\n\n".join(best_chunks)


def clear_embedding_cache():
    """清除 LRU 快取（例如切換模型時呼叫）。"""
    _get_embedding_cached.cache_clear()
