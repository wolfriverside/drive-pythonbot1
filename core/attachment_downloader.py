"""
core/attachment_downloader.py

從已分析的網頁中批次下載附件 (PDF, DOCX, XLSX, XLS 等)，
存入使用者的持久快取，並自動建立知識來源索引。
"""

import os
import re
import requests
from urllib.parse import urlparse, unquote


def _safe_filename(url: str, link_text: str, ext: str) -> str:
    """根據連結文字或 URL 產生安全且符合 Windows 命名規範的本地檔名。"""
    ext = ext.strip().lower()
    if ext and not ext.startswith("."):
        ext = "." + ext

    name = ""
    # 優先用連結文字
    if link_text and len(link_text.strip()) <= 80:
        clean_text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', link_text.strip())
        clean_text = clean_text.strip(" .")
        if clean_text:
            name = clean_text

    if not name:
        # 從 URL path 取得原始檔名
        path = unquote(urlparse(url).path)
        base_name = os.path.splitext(os.path.basename(path))[0]
        clean_base = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', base_name).strip(" .")
        name = clean_base or "attachment"

    # 避免檔名過長導致 Windows 260 字元路徑上限
    if len(name) > 60:
        name = name[:60].strip(" .")

    # 確保副檔名正確
    if ext and not name.lower().endswith(ext):
        name = name + ext
    elif not ext and not os.path.splitext(name)[1]:
        name = name + ".bin"

    return name


def download_attachment(url: str, dest_dir: str, link_text: str = "",
                        ext: str = "", parent_url: str = "") -> dict:
    """
    下載單一附件至 dest_dir，支援防盜連 Referer 注入與容錯重試。

    回傳:
        {"ok": bool, "path": str, "filename": str, "message": str}
    """
    parsed_origin = urlparse(url)
    origin_url = f"{parsed_origin.scheme}://{parsed_origin.netloc}"

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/128.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml,application/pdf,application/vnd.ms-excel,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,*/*;q=0.8",
        "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": parent_url or origin_url,
    }

    try:
        try:
            resp = requests.get(url, headers=headers, timeout=(10, 40), stream=True)
        except (requests.exceptions.SSLError, requests.exceptions.ConnectionError):
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            resp = requests.get(url, headers=headers, verify=False,
                                timeout=(10, 40), stream=True)
        resp.raise_for_status()

        # 從 Content-Disposition 取得真實檔名與副檔名
        cd = resp.headers.get("Content-Disposition", "")
        if cd:
            # 先找 RFC 5987 filename*=UTF-8''...
            m_star = re.search(r"filename\*\s*=\s*([^;']+''|UTF-8''|utf-8'')([^;\n]+)", cd, re.IGNORECASE)
            if m_star:
                raw_cd_fn = unquote(m_star.group(2).strip().strip('"\''))
                ext_cd = os.path.splitext(raw_cd_fn)[1].lower()
                if ext_cd:
                    ext = ext_cd
                    if not link_text:
                        link_text = os.path.splitext(raw_cd_fn)[0]
            if not ext:
                m_norm = re.search(r'filename\s*=\s*(["\'])(.*?)\1|filename\s*=\s*([^;\s\n]+)', cd, re.IGNORECASE)
                if m_norm:
                    raw_cd_fn = m_norm.group(2) or m_norm.group(3) or ""
                    ext_cd = os.path.splitext(raw_cd_fn.strip())[1].lower()
                    if ext_cd:
                        ext = ext_cd
                        if not link_text:
                            link_text = os.path.splitext(raw_cd_fn.strip())[0]

        # 若仍無副檔名，從 Content-Type 或 URL 推斷
        content_type = resp.headers.get("Content-Type", "").lower()
        if not ext or ext in [".php", ".asp", ".aspx", ".jsp", ".cgi", ".bin"]:
            type_map = {
                "application/pdf": ".pdf",
                "application/vnd.ms-excel": ".xls",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
                "application/msexcel": ".xls",
                "application/x-msexcel": ".xls",
                "application/x-ms-excel": ".xls",
                "application/x-excel": ".xls",
                "application/xls": ".xls",
                "application/msword": ".docx",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
                "text/csv": ".csv",
                "text/plain": ".txt",
                "application/json": ".json",
            }
            pure_ct = content_type.split(";")[0].strip()
            ext = type_map.get(pure_ct, os.path.splitext(urlparse(url).path)[1].lower() or ".bin")

        filename = _safe_filename(url, link_text, ext)
        dest_path = os.path.join(dest_dir, filename)

        # 若同名檔案已存在且非空，直接重用既有檔案避免重複建立副本
        if os.path.exists(dest_path) and os.path.getsize(dest_path) > 0:
            return {"ok": True, "path": dest_path, "filename": filename,
                    "message": f"使用既有附件：{filename}"}

        os.makedirs(dest_dir, exist_ok=True)
        with open(dest_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)

        # 下載完成後，檢查檔案魔法字節校正副檔名（特別是二進位 XLS 與偽裝 XLS）
        try:
            with open(dest_path, "rb") as f_chk:
                magic = f_chk.read(16)
            # OLE Compound File 標頭 (標準 .xls / .doc)
            if magic.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1") and ext in [".bin", ".txt", ".csv"]:
                correct_ext = ".xls"
                new_dest = os.path.splitext(dest_path)[0] + correct_ext
                os.replace(dest_path, new_dest)
                dest_path = new_dest
                filename = os.path.basename(new_dest)
        except Exception:
            pass

        return {"ok": True, "path": dest_path, "filename": filename,
                "message": f"下載成功：{filename}"}

    except Exception as e:
        return {"ok": False, "path": "", "filename": "",
                "message": f"下載失敗 ({url}): {e}"}


def download_and_index_attachments(uid: str, parent_source_id: str,
                                   files: list, max_files: int = 5) -> list:
    """
    批次下載頁面上發現的附件，並各自建立知識來源索引。
    具備重複下載防護與母來源狀態持久化。

    參數:
        uid:              使用者 ID
        parent_source_id: 母頁面的 source_id（用於命名快取目錄與提供 Referer）
        files:            analyze_page() 回傳的 downloadable_files 清單
        max_files:        最多自動下載幾個附件（避免過度爬取）

    回傳:
        [{"filename": ..., "source_id": ..., "status": "ok"/"error",
          "message": ...}, ...]
    """
    from core.source_manager import add_source, get_source, is_attachment_downloaded, mark_attachment_downloaded

    # 取得母頁面的 URL 作為 Referer，繞過網站防盜連
    parent_url = ""
    try:
        p_src = get_source(uid, parent_source_id)
        if p_src:
            parent_url = p_src.get("path_or_url", "")
    except Exception:
        pass

    # 使用者專屬的附件快取目錄
    dest_dir = os.path.join("database", "users", uid,
                            "attachment_cache", parent_source_id)
    os.makedirs(dest_dir, exist_ok=True)

    results = []
    for file_info in files[:max_files]:
        url  = file_info.get("url", "")
        text = file_info.get("text", "")
        ext  = file_info.get("ext", "")

        if not url:
            continue

        # ── 1. 檢查是否已下載索引過 ─────────────────────────────────────
        if is_attachment_downloaded(uid, parent_source_id, url, text):
            mark_attachment_downloaded(uid, parent_source_id, url, att_text=text)
            results.append({
                "filename":  text or url,
                "source_id": "",
                "status":    "ok",
                "message":   f"「{text or url}」已在知識庫中，已確認索引",
                "already_downloaded": True,
            })
            continue

        # ── 2. 下載檔案（自動傳入 parent_url 作為 Referer） ───────────
        dl = download_attachment(url, dest_dir, link_text=text, ext=ext, parent_url=parent_url)
        if not dl["ok"]:
            results.append({
                "filename":  text or url,
                "source_id": "",
                "status":    "error",
                "message":   dl["message"],
            })
            continue

        # ── 3. 建立知識來源索引 ─────────────────────────────────────────
        label = text.strip()[:50] if text.strip() else dl["filename"]
        index_res = add_source(uid, dl["path"], label=label)
        child_id = index_res.get("source_id", "")

        # ── 4. 標記母頁面中的附件狀態，持久化防止重複 ───────────────────
        mark_attachment_downloaded(uid, parent_source_id, url, child_source_id=child_id, att_text=text)

        results.append({
            "filename":  dl["filename"],
            "source_id": child_id,
            "status":    "ok" if index_res.get("ok") else "error",
            "message":   index_res.get("message", dl["message"]),
        })

    return results


_ATTACHMENT_PREVIEW_CACHE = {}


def get_attachment_preview_text(url: str, ext: str = "") -> str:
    """下載並擷取附件檔案的預覽文字（附記憶體快取）。"""
    if not url:
        return ""
    if url in _ATTACHMENT_PREVIEW_CACHE:
        return _ATTACHMENT_PREVIEW_CACHE[url]

    try:
        from tools.file_reader import read_local_file
        text = read_local_file(url)
        if isinstance(text, str) and not text.startswith("Error:"):
            clean = text.strip()
            _ATTACHMENT_PREVIEW_CACHE[url] = clean
            return clean
        elif isinstance(text, str) and text.startswith("注意："):
            return text
        else:
            return ""
    except Exception:
        return ""
