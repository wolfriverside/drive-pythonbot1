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
  ├── profile.json               <- existing user profile (unchanged)
  ├── uploads/                   <- existing uploads folder (unchanged)
  ├── sources/                   <- metadata + embedded chunks per source
  │   └── {source_id}.json
  ├── html_cache/                <- 原始 HTML 快取（供重新解析連結）
  │   └── {source_id}.html
  └── attachment_cache/          <- 從頁面下載的附件（由 attachment_downloader 管理）
      └── {source_id}/
"""

import os
import re
import json
import uuid
import threading
from datetime import datetime, timezone
from urllib.parse import urlparse

from core.config_manager import config
from core.db_utils import read_json, write_json


SOURCES_DIRNAME = "sources"
HTML_CACHE_DIRNAME = "html_cache"

# ── Source Index 記憶體快取 ──────────────────────────────────────────────────
# 結構: {uid: {"chunks": [...], "mtime": float}}
# 每次查詢前比對 sources/ 目錄最新 mtime；只有檔案有變更才重新載入
_source_index_cache: dict[str, dict] = {}
_cache_lock = threading.Lock()


def _sources_dir(uid: str) -> str:
    d = os.path.join("database", "users", uid, SOURCES_DIRNAME)
    os.makedirs(d, exist_ok=True)
    return d


def _html_cache_dir(uid: str) -> str:
    d = os.path.join("database", "users", uid, HTML_CACHE_DIRNAME)
    os.makedirs(d, exist_ok=True)
    return d


def _get_sources_mtime(uid: str) -> float:
    """取得 sources/ 目錄下所有 JSON 檔案最新的 mtime（有變動才重建索引）。"""
    d = _sources_dir(uid)
    try:
        mtimes = [
            os.path.getmtime(os.path.join(d, f))
            for f in os.listdir(d)
            if f.endswith(".json")
        ]
        return max(mtimes) if mtimes else 0.0
    except Exception:
        return 0.0


def _load_source_index(uid: str) -> list:
    """
    載入（或從快取取得）使用者知識庫所有 chunk。
    回傳 [(score_placeholder, label, text, embedding), ...] 供向量搜尋使用。

    僅在 sources/ 目錄有新增/刪除/修改時才重新讀磁碟，其他情況直接回傳快取。
    """
    current_mtime = _get_sources_mtime(uid)

    with _cache_lock:
        cached = _source_index_cache.get(uid)
        if cached and cached["mtime"] == current_mtime:
            return cached["chunks"]

    # 需要重建索引（在鎖外讀取以減少持鎖時間）
    d = _sources_dir(uid)
    all_chunks = []
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
                    all_chunks.append((label, chunk["text"], emb))
        except Exception:
            continue

    with _cache_lock:
        _source_index_cache[uid] = {"chunks": all_chunks, "mtime": current_mtime}

    return all_chunks


def _invalidate_source_cache(uid: str):
    """強制使該使用者的 source index 快取失效（新增/刪除 source 後呼叫）。"""
    with _cache_lock:
        _source_index_cache.pop(uid, None)


def _save_raw_html(uid: str, source_id: str, html: str) -> None:
    """將原始 HTML 存至 html_cache/{source_id}.html，供後續重新解析連結使用。"""
    try:
        path = os.path.join(_html_cache_dir(uid), f"{source_id}.html")
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
    except Exception as e:
        print(f"[source_manager] 無法儲存 HTML 快取: {e}")


def get_raw_html(uid: str, source_id: str) -> str:
    """讀取快取的原始 HTML，若不存在則回傳空字串。"""
    path = os.path.join(_html_cache_dir(uid), f"{source_id}.html")
    if not os.path.exists(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


def delete_source(uid: str, source_id: str) -> bool:
    """Delete a specific source and its cache files."""
    try:
        # Delete JSON source file
        d = _sources_dir(uid)
        file_path = os.path.join(d, f"{source_id}.json")
        if os.path.exists(file_path):
            os.remove(file_path)

        # Clean up HTML cache if exists
        html_path = os.path.join(_html_cache_dir(uid), f"{source_id}.html")
        if os.path.exists(html_path):
            os.remove(html_path)

        _invalidate_source_cache(uid)
        return True
    except Exception as e:
        print(f"Error deleting source {source_id}: {e}")
        return False


def normalize_source_meta(path_or_url: str, raw_title: str = "", raw_snippet: str = "", raw_text: str = "") -> dict:
    """
    Standardizes source metadata into a fixed, structured schema:
    Returns {
        "category": str,       # 資料類別：政府統計 / 統計數據 / 法規規章 / 政府機關 / 學術教育 / 新聞媒體 / 百科辭典 / 民間平台 / 一般網頁 / PDF 文件 等
        "site_name": str,      # 來源網站或機關名稱
        "domain": str,         # 網域名稱（若為 URL）
        "clean_title": str,    # 規整清理後的標題（去除贅詞與流水號前綴）
        "clean_snippet": str,  # 規整後的實質摘要（去除導覽雜訊）
    }
    """
    is_url = path_or_url.startswith("http://") or path_or_url.startswith("https://")

    # 清理前綴序號（例如 "8. 中華民國統計資訊網-人口" -> "中華民國統計資訊網-人口"）
    clean_title = re.sub(r'^\d+[\.、\s\-]+', '', (raw_title or '').strip()).strip()

    domain = ""
    site_name = ""
    category = "一般網頁"

    if is_url:
        parsed = urlparse(path_or_url)
        domain = parsed.netloc.lower()
        if domain.startswith("www."):
            domain = domain[4:]

        full_meta_text = (clean_title + " " + path_or_url + " " + (raw_snippet or "") + " " + (raw_text[:500] if raw_text else "")).lower()

        # 類別與站點智慧推論
        if "stat.gov.tw" in domain or "dgbas.gov.tw" in domain:
            category = "政府統計"
            site_name = "中華民國統計資訊網"
        elif "data.gov.tw" in domain or "opendata" in domain:
            category = "開放數據"
            site_name = "政府資料開放平台"
        elif "law.moj.gov.tw" in domain or "laws.moi.gov.tw" in domain:
            category = "法規規章"
            site_name = "全國法規資料庫"
        elif "stats.tw" in domain:
            category = "統計數據"
            site_name = "Taiwan Stats 台灣統計"
        elif domain.endswith(".gov.tw"):
            if any(k in full_meta_text for k in ["統計", "stat", "census", "數據", "指標"]):
                category = "政府統計"
            elif any(k in full_meta_text for k in ["法規", "條例", "辦法", "細則", "law"]):
                category = "法規規章"
            else:
                category = "政府機關"
            site_name = domain
        elif domain.endswith(".edu.tw") or "arxiv.org" in domain or "doi.org" in domain:
            category = "學術教育"
            site_name = domain
        elif any(news in domain for news in ["cna.com.tw", "udn.com", "ltn.com.tw", "chinatimes.com", "ettoday.net", "storm.mg", "tvbs.com.tw", "news"]):
            category = "新聞媒體"
            site_name = domain
        elif "wikipedia.org" in domain:
            category = "百科辭典"
            site_name = "維基百科"
        elif any(k in full_meta_text for k in ["統計", "指數", "人口數據", "指標", "趨勢"]):
            category = "統計數據"
            site_name = domain
        else:
            category = "一般網頁"
            site_name = domain

        if not clean_title:
            clean_title = site_name or domain
    else:
        ext = os.path.splitext(path_or_url)[1].lower() if '.' in path_or_url else ''
        cat_map = {
            '.pdf': 'PDF 文件',
            '.xlsx': 'Excel 表格',
            '.xls': 'Excel 表格',
            '.csv': 'CSV 數據',
            '.docx': 'Word 文書',
            '.doc': 'Word 文書',
            '.txt': '純文字檔',
            '.json': 'JSON 資料'
        }
        category = cat_map.get(ext, '文件檔案')
        site_name = '本地文件'
        clean_title = clean_title or os.path.basename(path_or_url)

    # 摘要清洗（移除「回上一頁 回最上面 行政院主計總處影音 統計網rss」等導覽雜訊）
    snippet = (raw_snippet or '').strip()
    snippet = re.sub(r'^(回上一頁|回最上面|行政院主計總處影音|統計網rss|\s+)+', '', snippet).strip()
    if not snippet or len(snippet) < 10:
        if category == '政府統計':
            snippet = f'中華民國統計資訊網官方專頁，彙整官方統計指標、歷史序列與公開數據。'
        elif category == '統計數據':
            snippet = f'{site_name or domain} 統計數據與分析指標專頁。'
        elif is_url:
            snippet = f'{site_name or domain} 相關公開資訊與參考網頁內容。'
        else:
            snippet = f'本地收錄檔案（{category}）：{clean_title}'

    return {
        'category': category,
        'site_name': site_name,
        'clean_title': clean_title,
        'domain': domain,
        'clean_snippet': snippet
    }


def list_sources(uid: str) -> list:
    """Return list of source metadata dicts with normalized category, clean formatting, and deduplication."""
    from tools.web_search import canonicalize_url
    d = _sources_dir(uid)
    sources = []
    seen_keys = set()

    for fname in sorted(os.listdir(d)):
        if not fname.endswith(".json"):
            continue
        try:
            with open(os.path.join(d, fname), "r", encoding="utf-8") as f:
                s = json.load(f)
            meta = normalize_source_meta(
                s.get("path_or_url", ""),
                raw_title=s.get("label", ""),
                raw_snippet=s.get("summary", "")
            )
            category = s.get("category") or meta["category"]
            site_name = s.get("site_name") or meta["site_name"]
            domain = s.get("domain") or meta["domain"]
            label_val = s.get("label") or meta["clean_title"]
            s_type = s.get("type", "url")
            path_or_url = s.get("path_or_url", "")

            # ── 呈現層去重防護：相同標準化網址或相同檔案只呈現一筆 ──
            if s_type == "url":
                canon_k = ("url", canonicalize_url(path_or_url))
            else:
                base_fn = os.path.basename(path_or_url).lower()
                clean_fn = re.sub(r'_\d+(\.[^.]+)$', r'\1', base_fn)
                canon_k = ("file", clean_fn or label_val.strip().lower())

            if canon_k in seen_keys:
                continue
            seen_keys.add(canon_k)

            sources.append({
                "source_id":        s["source_id"],
                "type":             s_type,
                "category":         category,
                "site_name":        site_name,
                "domain":           domain,
                "label":            label_val,
                "path_or_url":      path_or_url,
                "added_at":         s["added_at"],
                "summary":          s.get("summary", ""),
                "chunk_count":      len(s.get("chunks", [])),
                "discovered_files": s.get("discovered_files", []),
                "has_raw_html":     s.get("has_raw_html", False),
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
    removed = False
    if os.path.exists(path):
        os.remove(path)
        removed = True
    # 同步清理關聯的 HTML 快取檔案
    html_path = os.path.join(_html_cache_dir(uid), f"{source_id}.html")
    if os.path.exists(html_path):
        try:
            os.remove(html_path)
        except Exception:
            pass
    _invalidate_source_cache(uid)
    return removed


def find_existing_source_id(uid: str, path_or_url: str, label: str = "") -> str:
    """Checks if a URL or local file path is already indexed by this user to prevent duplication."""
    from tools.web_search import canonicalize_url
    d = _sources_dir(uid)
    if not os.path.exists(d):
        return ""

    is_url = path_or_url.startswith("http://") or path_or_url.startswith("https://")
    target_canon_url = canonicalize_url(path_or_url) if is_url else ""
    target_base = os.path.basename(path_or_url).lower() if not is_url else ""
    clean_target_base = re.sub(r'_\d+(\.[^.]+)$', r'\1', target_base) if target_base else ""

    for fname in os.listdir(d):
        if not fname.endswith(".json"):
            continue
        try:
            with open(os.path.join(d, fname), "r", encoding="utf-8") as f:
                s = json.load(f)
            s_path = s.get("path_or_url", "")
            s_type = s.get("type", "")
            s_label = s.get("label", "").strip()

            if is_url and s_type == "url":
                if canonicalize_url(s_path) == target_canon_url:
                    return s.get("source_id", "")
            elif not is_url and s_type == "file":
                s_base = os.path.basename(s_path).lower()
                clean_s_base = re.sub(r'_\d+(\.[^.]+)$', r'\1', s_base)
                if s_path == path_or_url or s_base == target_base or (clean_target_base and clean_s_base == clean_target_base):
                    return s.get("source_id", "")
                if label and s_label and s_label == label.strip():
                    return s.get("source_id", "")
        except Exception:
            continue
    return ""


def mark_attachment_downloaded(uid: str, parent_source_id: str, att_url: str, child_source_id: str = "", att_text: str = "") -> bool:
    """Marks an attachment item inside parent source's discovered_files as downloaded."""
    path = os.path.join(_sources_dir(uid), f"{parent_source_id}.json")
    if not os.path.exists(path):
        return False
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        modified = False
        for att in data.get("discovered_files", []):
            u_match = (
                att.get("url") == att_url or
                (att.get("url") and att_url and att["url"].split("?")[0] == att_url.split("?")[0])
            )
            t_match = (
                att_text and att.get("text") and att.get("text").strip() == att_text.strip()
            )
            if u_match or t_match:
                att["downloaded"] = True
                if child_source_id:
                    att["downloaded_source_id"] = child_source_id
                modified = True

        if modified:
            write_json(path, data)
            return True
    except Exception as e:
        print(f"[source_manager] mark_attachment_downloaded error: {e}")
    return False


def is_attachment_downloaded(uid: str, parent_source_id: str, att_url: str, att_text: str = "") -> bool:
    """Checks if an attachment has already been downloaded or indexed."""
    # 1. 檢查母來源中的標記
    parent = get_source(uid, parent_source_id)
    if parent:
        for att in parent.get("discovered_files", []):
            u_match = (
                att.get("url") == att_url or
                (att.get("url") and att_url and att["url"].split("?")[0] == att_url.split("?")[0])
            )
            t_match = (
                att_text and att.get("text") and att.get("text").strip() == att_text.strip()
            )
            if (u_match or t_match) and att.get("downloaded"):
                return True

    # 2. 檢查知識庫現有來源
    d = _sources_dir(uid)
    if os.path.exists(d):
        att_filename = os.path.basename(urlparse(att_url).path).lower() if att_url else ""
        clean_att_fn = re.sub(r'_\d+(\.[^.]+)$', r'\1', att_filename) if att_filename else ""
        for fname in os.listdir(d):
            if not fname.endswith(".json"):
                continue
            try:
                with open(os.path.join(d, fname), "r", encoding="utf-8") as f:
                    s = json.load(f)
                s_path = s.get("path_or_url", "")
                if s_path == att_url:
                    return True
                s_base = os.path.basename(s_path).lower()
                clean_s_base = re.sub(r'_\d+(\.[^.]+)$', r'\1', s_base)
                if att_filename and (s_base == att_filename or clean_s_base == clean_att_fn):
                    return True
                if att_text and s.get("label", "").strip() == att_text.strip():
                    return True
            except Exception:
                continue
    return False


def add_source(uid: str, path_or_url: str, label: str = "", category: str = "", snippet: str = "") -> dict:
    """
    Add a new knowledge source (URL or local file path).
    Enforces standardized fixed formatting, deduplication, and auto data categorization.

    Returns {"ok": bool, "source_id": str, "message": str}
    """
    from core.embedding import chunk_text, get_embeddings_batch

    is_url = path_or_url.startswith("http://") or path_or_url.startswith("https://")
    raw_text = ""
    raw_html = ""
    discovered_files = []  # 頁面上發現的可下載附件

    # 正規化詮釋資料
    meta = normalize_source_meta(path_or_url, raw_title=label, raw_snippet=snippet)
    final_category = category.strip() if category else meta["category"]
    final_label = meta["clean_title"]
    final_site = meta["site_name"]
    final_domain = meta["domain"]
    clean_snippet = meta["clean_snippet"]

    # ── 去重防護：檢查該使用者是否已收錄過相同的網址或檔案 ─────────────
    existing_id = find_existing_source_id(uid, path_or_url, label=final_label)
    if existing_id:
        return {
            "ok": True,
            "source_id": existing_id,
            "message": f"「[{final_category}] {final_label}」已在知識庫中，已確認索引",
            "is_duplicate": True,
        }

    if is_url:
        # ── URL 來源：混合爬取 + 結構化分析 ──────────────────────
        try:
            from tools.web_scraper import scrape_url
            scrape_result = scrape_url(path_or_url)
            raw_html = scrape_result.get("raw_html", "")

            if raw_html:
                from core.web_content_analyzer import analyze_page

                analysis = analyze_page(raw_html, path_or_url)
                raw_text = analysis.get("text", "").strip()
                discovered_files = analysis.get("downloadable_files", [])
            else:
                raw_text = scrape_result.get("text", "").strip()
        except Exception as e:
            return {"ok": False, "source_id": "", "message": f"爬取網址失敗：{e}"}
    else:
        # ── 本地檔案來源：直接讀取 ──────────────────────────────────────
        from tools.file_reader import read_local_file
        try:
            raw_text = read_local_file(path_or_url)
        except Exception as e:
            return {"ok": False, "source_id": "", "message": f"讀取失敗：{e}"}

    # 偏差防護：若爬取因網站特殊保護而字數極少，或僅為錯誤訊息，但有搜尋摘要時，自動以標準摘要補強
    if not raw_text or (isinstance(raw_text, str) and (raw_text.startswith("Error:") or len(raw_text.strip()) < 30)):
        if clean_snippet:
            raw_text = (
                f"【{final_label}】\n"
                f"資料類別：{final_category}\n"
                f"來源網站：{final_site} ({path_or_url})\n\n"
                f"內容說明與摘要：\n{clean_snippet}"
            )
        else:
            return {"ok": False, "source_id": "", "message": raw_text or "無法取得有效內容"}

    chunks_text = chunk_text(raw_text, chunk_size=600, overlap=80, max_chunks=50)
    embeddings = get_embeddings_batch(chunks_text)

    chunks = []
    for i, ct in enumerate(chunks_text):
        emb = embeddings[i] if i < len(embeddings) else []
        chunks.append({"text": ct, "embedding": emb if emb else []})

    # 嘗試使用 Ollama 生成 AI 摘要
    ai_summary = ""
    if raw_text and len(raw_text) > 100:
        try:
            import ollama
            main_model = config.model_name
            prompt = f"請以繁體中文詳細整理以下文章的核心重點，請務必確保摘要的總字數嚴格落在 400 到 800 字之間，內容需豐富、結構清晰且深入：\n\n{raw_text[:8000]}"
            resp = ollama.chat(
                model=main_model,
                messages=[{"role": "user", "content": prompt}],
                stream=False
            )
            ai_summary = resp.get("message", {}).get("content", "").strip()
        except Exception as e:
            print(f"[AI Summary Error] {e}")
            ai_summary = ""

    final_summary = ai_summary if ai_summary else (clean_snippet[:200] if clean_snippet else raw_text[:200]).replace("\n", " ")

    source_id = uuid.uuid4().hex[:10]
    entry = {
        "source_id":        source_id,
        "type":             "url" if is_url else "file",
        "category":         final_category,
        "site_name":        final_site,
        "domain":           final_domain,
        "label":            final_label,
        "path_or_url":      path_or_url,
        "added_at":         datetime.now(timezone.utc).isoformat(),
        "summary":          final_summary,
        "chunks":           chunks,
        "discovered_files": discovered_files,
        "has_raw_html":     is_url and bool(raw_html),
    }

    dest = os.path.join(_sources_dir(uid), f"{source_id}.json")
    write_json(dest, entry)

    # 保存原始 HTML 至快取
    if raw_html:
        _save_raw_html(uid, source_id, raw_html)

    # 新增 source 後清除快取，確保下次查詢取得最新索引
    _invalidate_source_cache(uid)

    attachment_hint = ""
    if discovered_files:
        attachment_hint = f"，並發現 {len(discovered_files)} 個可下載附件"

    return {
        "ok":        True,
        "source_id": source_id,
        "message":   f"已加入知識來源「[{final_category}] {final_label}」（{len(chunks)} 段落{attachment_hint}）",
    }


def retrieve_from_sources(uid: str, query: str, top_k: int = 4, min_score: float = 0.65) -> str:
    """
    Semantic search across all user sources.
    Returns formatted relevant chunks to inject into AI system context.
    Only returns chunks with cosine similarity >= min_score to avoid polluting the prompt.

    使用記憶體 Index Cache（_load_source_index），避免每次請求都全量讀磁碟。
    """
    from core.embedding import get_embedding, cosine_similarity

    d = _sources_dir(uid)
    if not os.path.isdir(d) or not os.listdir(d):
        return ""

    query_emb = get_embedding(query)
    if not query_emb:
        return ""

    # 從快取取得所有 chunks（有變動才重新讀磁碟）
    all_chunks = _load_source_index(uid)
    if not all_chunks:
        return ""

    scored = []
    for label, text, emb in all_chunks:
        score = cosine_similarity(query_emb, emb)
        if score >= min_score:
            scored.append((score, label, text))

    if not scored:
        return ""

    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:top_k]

    lines = ["[知識來源相關段落]"]
    for score, label, text in top:
        lines.append(f"\n來源：{label}（相關度 {score:.2f}）\n{text}")
    return "\n".join(lines)
