import os
import re
import json
import uuid
import html
from datetime import datetime, timezone
import time
import gradio as gr
from cachetools import TTLCache
from core.bot_engine import BotEngine  # will be implemented later
from core.health_check import check_ollama_status
from core.config_manager import config as app_config
from core.db_utils import read_json, write_json, update_json

# ---------------------------------------------------------------------------
# Rate limiting for session creation
# TTLCache: 自動過期，不會無限增長（maxsize=10000 個 IP，TTL=60 秒）
# ---------------------------------------------------------------------------
IP_CREATION_LOGS: TTLCache = TTLCache(maxsize=10000, ttl=60)
BLOCK_EXPIRY: dict = {}

def check_rate_limit(request: gr.Request):
    ip = request.client.host if request and request.client else "unknown"
    now = time.time()

    # Check if IP is blocked
    if ip in BLOCK_EXPIRY:
        if now < BLOCK_EXPIRY[ip]:
            raise gr.Error("429 Too Many Requests: 您的連線頻率過高，暫時被封鎖。")
        else:
            del BLOCK_EXPIRY[ip]

    # TTLCache 自動清除超過 1 秒的記錄，不需要手動管理
    if ip not in IP_CREATION_LOGS:
        IP_CREATION_LOGS[ip] = []

    # 手動清除 1 秒內記錄（TTLCache 管 key 過期，這裡管 value 內的時間戳）
    logs_for_ip = [t for t in IP_CREATION_LOGS.get(ip, []) if now - t <= 1.0]

    if len(logs_for_ip) >= 10:
        BLOCK_EXPIRY[ip] = now + 60.0  # Block for 60 seconds
        raise gr.Error("429 Too Many Requests: 您的連線頻率過高，暫時被封鎖。")

    logs_for_ip.append(now)
    IP_CREATION_LOGS[ip] = logs_for_ip

# ---------------------------------------------------------------------------
# Helper: Initialise or load a user session based on URL query parameter `uid`
# ---------------------------------------------------------------------------
def init_session(request: gr.Request):
    """Return a unique user id and initial log."""
    check_rate_limit(request)

    uid = None

    # 1. 優先從 Cookie 讀取 uid
    if request:
        try:
            if hasattr(request, "cookies") and request.cookies:
                uid = request.cookies.get("lpb_uid")
            if not uid and hasattr(request, "headers"):
                cookie_str = request.headers.get("cookie", "")
                for part in cookie_str.split(";"):
                    part = part.strip()
                    if part.startswith("lpb_uid="):
                        uid = part[len("lpb_uid="):].strip()
                        break
        except Exception:
            pass

        # 2. Cookie 沒有時，嘗試 URL query param（向後相容）
        if not uid and hasattr(request, "query_params"):
            uid = request.query_params.get("uid")

    # 3. 格式驗證：防止路徑遊歷或注入攻擊
    from core.security import validate_uid
    if not validate_uid(uid):
        uid = f"user_{uuid.uuid4().hex[:8]}"

    user_dir = os.path.join("database", "users", uid)
    user_path = os.path.join(user_dir, "profile.json")
    uploads_dir = os.path.join(user_dir, "uploads")
    os.makedirs(uploads_dir, exist_ok=True)
    if not os.path.isfile(user_path):
        # Initialise a fresh session file
        init_data = {
            "uid": uid,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "last_active_time": datetime.now(timezone.utc).isoformat(),
            "chat_log": [],
            "active_memory": [],
            "uploaded_files": []
        }
        write_json(user_path, init_data)

    return uid, f"[{get_time_str()}] [系統] 伺服器啟動，等待操作...\n"


# ---------------------------------------------------------------------------
# Core response handling (streaming) – placeholder, real logic lives in BotEngine
# ---------------------------------------------------------------------------
bot = BotEngine()  # BotEngine must expose generate_response_stream

def get_time_str():
    return datetime.now().strftime("%H:%M:%S")

from core.security import check_suspicious, sanitize_for_log

def respond(message, chat_history_ignored, logs, uid, request: gr.Request):
    check_rate_limit(request)
    if not uid:
        yield "", chat_history_ignored, logs + f"[{get_time_str()}] [系統錯誤] 使用者 UID 尚未建立，請重新整理網頁。\n"
        return
        
    if not message.strip():
        yield "", chat_history_ignored, logs
        return
    
    # Load pristine user JSON
    user_path = os.path.join("database", "users", uid, "profile.json")
    if not os.path.exists(user_path):
        yield "", chat_history_ignored, logs + f"[{get_time_str()}] [系統錯誤] 找不到使用者檔案，請重新整理網頁。\n"
        return
        
    data = read_json(user_path)
    if not data:
        yield "", chat_history_ignored, logs + f"[{get_time_str()}] [系統錯誤] 讀取使用者檔案失敗，請重新整理網頁。\n"
        return

    data["chat_log"].append({"role": "user", "content": message})
    data["last_active_time"] = datetime.now(timezone.utc).isoformat()
    logs += f"[{get_time_str()}] [User] {sanitize_for_log(message)}\n"

    # We yield the pristine data["chat_log"] directly to Gradio UI
    yield "", data["chat_log"], logs

    # Perform security check before querying AI
    is_suspicious = check_suspicious(message, uid)
    if is_suspicious:
        bot_response = "很抱歉，您的輸入似乎違反了系統規範，或者與學習 Python 無關。我無法為您解答這個問題。"
        data["chat_log"].append({"role": "assistant", "content": bot_response})
        write_json(user_path, data)
        logs += f"[{get_time_str()}] [System] 已攔截可疑輸入並記錄至背景。\n"
        yield "", data["chat_log"], logs
        return

    # Streaming from bot
    data["chat_log"].append({"role": "assistant", "content": ""})
    bot_response = ""
    # pass data["chat_log"] up to the last user message
    for log_update, is_final, text in bot.generate_response_stream(data["chat_log"][:-1], uid):
        if log_update:
            cleaned = sanitize_for_log(log_update)
            # 確保每條即時日誌均具備統一時間戳與安全遮蔽
            if cleaned.startswith("[") and len(cleaned) > 9 and cleaned[9] == "]":
                logs += f"{cleaned}\n"
            else:
                logs += f"[{get_time_str()}] {cleaned}\n"

        if text:
            data["chat_log"][-1]["content"] = text
            bot_response = text

        yield "", data["chat_log"], logs

    write_json(user_path, data)
    yield "", data["chat_log"], logs

# ---------------------------------------------------------------------------
# File upload handling – respects whitelist & size limit from built_in_db.json
# ---------------------------------------------------------------------------
# 使用 ConfigManager 取得上傳白名單與檔案大小上限（不再直接讀 JSON）
WHITELIST = app_config.upload_whitelist
MAX_SIZE_MB = app_config.upload_max_size_mb

def handle_like(evt: gr.LikeData, uid: str, logs: str):
    if not uid:
        return logs, []
    from core.memory_manager import add_active_memory, get_active_memory
    val = evt.value
    # In Gradio, evt.value can be a list of strings/chunks, dict, or string
    if isinstance(val, list):
        extracted = []
        for item in val:
            if isinstance(item, dict):
                extracted.append(str(item.get("content") or item.get("text") or ""))
            else:
                extracted.append(str(item))
        val = "".join(extracted)
    elif isinstance(val, dict):
        val = val.get("content") or val.get("text") or str(val)
    else:
        val = str(val)

    val = val.strip()
    t = get_time_str()
    if evt.liked and val:
        res = add_active_memory(uid, val)
        gr.Info("已成功將對話加入長期記憶庫！")
        logs += f"[{t}] [記憶庫] 已將對話加入長期記憶：{val[:30]}...\n"

    new_memories = get_active_memory(uid)
    return logs, new_memories

# ---------------------------------------------------------------------------
# Knowledge Source Library handlers
# ---------------------------------------------------------------------------
from core.source_manager import list_sources, remove_source, add_source

def add_file_source_modal(file_obj, uid: str, logs: str):
    """Add an uploaded file as a knowledge source from the modal."""
    if not uid:
        return list_sources(uid), logs, gr.update(), None
    if file_obj is None:
        gr.Warning("請先選擇或拖放欲上傳的檔案。")
        return list_sources(uid), logs, gr.update(visible=True), None

    if hasattr(file_obj, "orig_name") and file_obj.orig_name:
        filename = file_obj.orig_name
    elif hasattr(file_obj, "name") and file_obj.name:
        filename = os.path.basename(file_obj.name)
    elif isinstance(file_obj, str):
        filename = os.path.basename(file_obj)
    else:
        filename = "uploaded_file"

    src_path = file_obj.name if hasattr(file_obj, "name") else str(file_obj)
    ext = os.path.splitext(filename)[1].lower()

    if ext not in WHITELIST:
        error_msg = (
            "Oops，請再試一次，或確認此檔案在支援名單內。\n"
            "────────────────────────────────\n"
            "目前接受的檔案類型：PDF、TXT、CSV、DOCX、JSON"
        )
        gr.Warning(error_msg)
        logs += f"[{get_time_str()}] [系統] {error_msg}\n"
        return list_sources(uid), logs, gr.update(visible=True), None

    try:
        size_mb = os.path.getsize(src_path) / (1024 * 1024)
        if size_mb > MAX_SIZE_MB:
            error_msg = f"檔案大小超過上限 ({MAX_SIZE_MB} MB)。"
            gr.Warning(error_msg)
            logs += f"[{get_time_str()}] [系統] {error_msg}\n"
            return list_sources(uid), logs, gr.update(visible=True), None
    except Exception:
        pass

    # 【防禦二】上傳時使用 UUID 重命名磁碟實體路徑，完全解耦原始檔名與磁碟路徑
    # 使用者或 AI 只接觸到 file_id，無從得知磁碟上的實體路徑
    file_id = uuid.uuid4().hex
    uuid_filename = f"{file_id}{ext}"

    uploads_dir = os.path.join("database", "users", uid, "uploads")
    os.makedirs(uploads_dir, exist_ok=True)
    dest_path = os.path.join(uploads_dir, uuid_filename)

    try:
        with open(src_path, "rb") as s, open(dest_path, "wb") as d:
            d.write(s.read())
    except Exception as e:
        from core.security import mask_error
        msg = mask_error(e, context="add_file_source_modal/write")
        gr.Warning(msg)
        logs += f"[{get_time_str()}] [系統錯誤] {msg}\n"
        return list_sources(uid), logs, gr.update(visible=True), None

    # 在 profile.json 中記錄 file_id → display_name 的對應，不儲存磁碟路徑
    user_path = os.path.join("database", "users", uid, "profile.json")
    if os.path.exists(user_path):
        def _add_file_record(udata: dict):
            uploaded_files = udata.setdefault("uploaded_files", [])
            # 以 {file_id, display_name} 字典格式儲存（向後相容舊版純路徑格式）
            file_record = {"file_id": file_id, "display_name": filename, "ext": ext}
            if not any(r.get("file_id") == file_id for r in uploaded_files if isinstance(r, dict)):
                uploaded_files.append(file_record)
            udata["last_active_time"] = datetime.now(timezone.utc).isoformat()
        update_json(user_path, _add_file_record)

    logs += f"[{get_time_str()}] [來源庫] 正在索引檔案：{filename}\n"

    try:
        result = add_source(uid, dest_path, label=filename)
        logs += f"[{get_time_str()}] [來源庫] {result['message']}\n"
        if result.get("ok"):
            gr.Info(f"已成功加入檔案：{filename}")
        else:
            gr.Warning(result.get("message", "檔案索引失敗"))
    except Exception as e:
        from core.security import mask_error
        msg = mask_error(e, context="add_file_source_modal/add_source")
        logs += f"[{get_time_str()}] [來源庫錯誤] {msg}\n"
        gr.Warning(f"檔案處理失敗：{msg}")

    new_sources = list_sources(uid)
    # Do NOT auto exit modal! Keep it open and clear file input
    return new_sources, logs, gr.update(visible=True), None


def handle_keyword_search(kw: str, last_kw: str, uid: str, logs: str):
    """Search web for keywords or add direct URL, returning top 10 results."""
    q = (kw or "").strip()
    if not q:
        gr.Warning("請輸入搜尋關鍵字或網址。")
        return [], logs, list_sources(uid), last_kw

    if q == last_kw:
        return gr.skip(), gr.skip(), gr.skip(), gr.skip()

    # Direct URL detection
    if q.startswith("http://") or q.startswith("https://") or (q.startswith("www.") and "." in q):
        url = q if q.startswith("http") else "https://" + q
        from core.source_manager import add_source
        res = add_source(uid, url)
        new_s = list_sources(uid)
        if res.get("ok"):
            gr.Info(f"已直接將網址加入知識庫：{url}")
            logs += f"[{get_time_str()}] [來源庫] 已加入網址：{url}\n"
        else:
            gr.Warning(f"加入失敗：{res.get('message')}")
            logs += f"[{get_time_str()}] [來源庫錯誤] {res.get('message')}\n"
        return [], logs, new_s, q

    logs += f"[{get_time_str()}] [搜尋] 正在檢索關鍵字：{q}...\n"
    from tools.web_search import search_web
    results = search_web(q, max_results=10)
    if not results:
        gr.Warning("未找到相關網頁，請嘗試更換關鍵字。")
        logs += f"[{get_time_str()}] [搜尋] 關鍵字「{q}」未找到相關結果。\n"
    else:
        logs += f"[{get_time_str()}] [搜尋] 找到 {len(results)} 筆最相關結果。\n"
    return results, logs, list_sources(uid), q


def get_skills_html():
    """Generates a rich, intuitive scrollable list of skills currently held by the AI directly from tools/."""
    from tools import get_skills_display_info
    skills = get_skills_display_info()
    cards = []
    for s in (skills or []):
        friendly_name = html.escape(s.get("name", "skill"), quote=True)
        raw_name = html.escape(s.get("raw_name", ""), quote=True)
        file_path = html.escape(s.get("file", ""), quote=True)
        desc = html.escape(s.get("desc", ""), quote=True)
        full_desc = html.escape(s.get("full_desc", ""), quote=True).replace("\n", "<br>")
        params = s.get("parameters", [])

        params_html = ""
        if params:
            param_items = []
            for p in params:
                p_name = html.escape(p["name"], quote=True)
                p_type = html.escape(p["type"], quote=True)
                p_desc = html.escape(p["desc"], quote=True)
                req_badge = "<span class='skill-param-req'>必填</span>" if p["required"] else "<span class='skill-param-opt'>可選</span>"
                param_items.append(
                    f"<div class='skill-param-row'>"
                    f"  <span class='skill-param-name'><code>{p_name}</code></span>"
                    f"  <span class='skill-param-type'>({p_type})</span>"
                    f"  {req_badge}"
                    f"  <div class='skill-param-desc'>{p_desc}</div>"
                    f"</div>"
                )
            params_html = (
                f"<div class='skill-params-container'>"
                f"  <div class='skill-section-label'>調用參數規格 (Parameters)：</div>"
                f"  {''.join(param_items)}"
                f"</div>"
            )

        details_html = ""
        if full_desc and full_desc != desc:
            details_html = (
                f"<details class='skill-details-toggle'>"
                f"  <summary class='skill-details-summary'>調用指引與進階規範 ▼</summary>"
                f"  <div class='skill-full-desc'>{full_desc}</div>"
                f"</details>"
            )

        cards.append(
            f'<div class="skill-item-card">'
            f'  <div class="skill-card-top">'
            f'    <div class="skill-item-name">{friendly_name}</div>'
            f'    <span class="skill-fn-badge">{raw_name}()</span>'
            f'  </div>'
            f'  <div class="skill-file-badge">[檔案] {file_path}</div>'
            f'  <div class="skill-item-desc">{desc}</div>'
            f'  {params_html}'
            f'  {details_html}'
            f'</div>'
        )
    if not cards:
        cards.append('<div class="skill-empty-note">尚無可用技能</div>')
    return f'<div class="skills-scroll-container">{"".join(cards)}</div>'




custom_css = """
body {
    background-color: #ffffff;
    color: #1f2937;
}

.gradio-container {
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    color: #1f2937;
}

/* Remove rounded corners for clean, minimalist aesthetic */
.gradio-container * {
    border-radius: 0px !important;
}

/* Prevent horizontal scrolling globally */
.gradio-container {
    overflow-x: hidden !important;
}

/* ── App Header Bar: Strictly single line, never wrap ── */
.app-header-bar {
    display: flex !important;
    flex-direction: row !important;
    align-items: center !important;
    justify-content: space-between !important;
    flex-wrap: nowrap !important;
    width: 100% !important;
    margin-bottom: 6px !important;
    padding-bottom: 6px !important;
    border-bottom: 1px solid #e5e7eb !important;
}

.app-header-title {
    flex: 1 1 auto !important;
    white-space: nowrap !important;
    overflow: hidden !important;
    margin: 0 !important;
    padding: 0 !important;
}

.app-header-title h1, .app-header-bar h1 {
    white-space: nowrap !important;
    word-break: keep-all !important;
    overflow: hidden !important;
    text-overflow: ellipsis !important;
    font-size: clamp(20px, 3.2vw, 28px) !important;
    font-weight: 800 !important;
    color: #111827 !important;
    margin: 0 !important;
    padding: 0 !important;
    line-height: 1.2 !important;
    display: inline-block !important;
}

.app-clear-btn {
    white-space: nowrap !important;
    flex-shrink: 0 !important;
    min-width: 95px !important;
    height: 32px !important;
    font-size: 12px !important;
    color: #4b5563 !important;
    background-color: #f9fafb !important;
    border: 1px solid #d1d5db !important;
    cursor: pointer !important;
    transition: all 0.15s ease !important;
}

.app-clear-btn:hover {
    background-color: #f3f4f6 !important;
    color: #111827 !important;
    border-color: #9ca3af !important;
}

/* ── Main Workspace Layout & Minimal Gap ── */
.main-workspace-row {
    display: flex !important;
    flex-direction: row !important;
    gap: 8px !important; /* 無大間隔：緊湊俐落 */
    align-items: stretch !important;
    width: 100% !important;
}

.sources-sidebar-col {
    transition: all 0.2s ease !important;
}

.sidebar-header-row {
    display: flex !important;
    flex-direction: row !important;
    align-items: center !important;
    justify-content: space-between !important;
    margin-bottom: 6px !important;
    gap: 4px !important;
}

.sidebar-header-row h3 {
    margin: 0 !important;
    font-size: 15px !important;
    font-weight: 700 !important;
    color: #111827 !important;
    white-space: nowrap !important;
}

.sidebar-toggle-btn {
    min-width: 48px !important;
    max-width: 58px !important;
    height: 24px !important;
    padding: 0 !important;
    font-size: 11px !important;
    font-weight: 600 !important;
    line-height: 1 !important;
    color: #4b5563 !important;
    background-color: #f9fafb !important;
    border: 1px solid #d1d5db !important;
    cursor: pointer !important;
    transition: all 0.15s ease !important;
}

.sidebar-toggle-btn:hover {
    background-color: #f3f4f6 !important;
    border-color: #9ca3af !important;
    color: #111827 !important;
}

/* 當頁面寬度太小時，側邊欄移至對話框上方 */
@media (max-width: 920px) {
    .main-workspace-row {
        flex-direction: column !important;
        gap: 12px !important;
    }

    .sources-sidebar-col {
        order: -1 !important; /* 確保移至上方 */
        width: 100% !important;
        min-width: 100% !important;
    }

    .sources-box-wrapper {
        max-height: 220px !important;
        min-height: 120px !important;
    }

    .chat-center-column, .console-right-column {
        width: 100% !important;
    }
}

/* ── Primary Action Buttons (Royal Blue Accent) ── */
button.btn-primary,
button.btn-primary-orange,
.btn-primary,
.btn-primary button,
.btn-primary-orange,
.btn-primary-orange button,
button.primary {
    background-color: #2563eb !important;
    color: #ffffff !important;
    border: 1px solid #2563eb !important;
    font-weight: 600 !important;
    transition: background-color 0.15s ease, border-color 0.15s ease !important;
}

button.btn-primary:hover,
button.btn-primary-orange:hover,
.btn-primary:hover,
.btn-primary button:hover,
.btn-primary-orange:hover,
.btn-primary-orange button:hover,
button.primary:hover {
    background-color: #1d4ed8 !important;
    border-color: #1d4ed8 !important;
}

/* ── Selected Tab Highlighting (Royal Blue in Light, Sky Blue in Dark) ── */
button[role="tab"].selected,
.gradio-container button[role="tab"].selected {
    color: #2563eb !important;
    border-color: #2563eb !important;
    border-bottom: 2px solid #2563eb !important;
    font-weight: 600 !important;
}

.dark button[role="tab"].selected,
.dark .gradio-container button[role="tab"].selected {
    color: #38bdf8 !important;
    border-color: #38bdf8 !important;
    border-bottom: 2px solid #38bdf8 !important;
}

/* ── Neutral Gray Buttons ── */
.btn-neutral-gray,
.btn-neutral-gray button,
button.secondary {
    background-color: #f9fafb !important;
    color: #4b5563 !important;
    border: 1px solid #d1d5db !important;
    transition: all 0.15s ease !important;
}

.btn-neutral-gray:hover,
.btn-neutral-gray button:hover,
button.secondary:hover {
    background-color: #f3f4f6 !important;
    color: #111827 !important;
    border-color: #9ca3af !important;
}

/* ── Sources Library List Container Box ── */
.sources-box-wrapper {
    border: 1px solid #e2e8f0 !important;
    background-color: #fafbfc !important;
    padding: 6px !important;
    min-height: 48px !important;
    max-height: 460px !important;
    height: auto !important;
    overflow-y: auto !important;
    display: flex !important;
    flex-direction: column !important;
    justify-content: flex-start !important;
}

.sources-box-wrapper > * {
    flex-grow: 0 !important;
    flex-shrink: 0 !important;
    height: auto !important;
}

.sources-empty-box {
    display: flex !important;
    flex-direction: column !important;
    align-items: center !important;
    justify-content: center !important;
    min-height: 120px !important;
    padding: 16px 8px !important;
    flex-grow: 1 !important;
}

.sources-items-list {
    display: flex !important;
    flex-direction: column !important;
    justify-content: flex-start !important;
    align-items: stretch !important;
    gap: 4px !important;
    width: 100% !important;
    flex-grow: 0 !important;
    flex-shrink: 0 !important;
    min-height: 0 !important;
    height: auto !important;
}

.source-item-row {
    background-color: #ffffff !important;
    border: 1px solid #e2e8f0 !important;
    padding: 4px 6px !important;
    display: flex !important;
    flex-direction: row !important;
    flex-wrap: nowrap !important;
    align-items: center !important;
    justify-content: space-between !important;
    gap: 6px !important;
    margin-bottom: 3px !important;
    width: 100% !important;
    min-height: 0 !important;
    height: auto !important;
    box-sizing: border-box !important;
    transition: border-color 0.15s ease, background-color 0.15s ease !important;
}

.source-item-row:hover {
    border-color: #cbd5e1 !important;
    background-color: #f8fafc !important;
}

.source-item-card {
    background-color: #ffffff !important;
    border: 1px solid #e2e8f0 !important;
    border-radius: 3px !important;
    padding: 4px 6px !important;
    display: flex !important;
    flex-direction: column !important;
    align-self: flex-start !important;
    gap: 3px !important;
    margin-bottom: 3px !important;
    width: 100% !important;
    max-width: 100% !important;
    min-width: 0 !important;
    min-height: 0 !important;
    height: auto !important;
    max-height: max-content !important;
    flex: 0 0 auto !important;
    flex-grow: 0 !important;
    flex-shrink: 0 !important;
    box-sizing: border-box !important;
    transition: border-color 0.15s ease, background-color 0.15s ease !important;
}

.source-item-card:hover {
    border-color: #cbd5e1 !important;
    background-color: #f8fafc !important;
}

.source-item-header {
    display: flex !important;
    flex-direction: row !important;
    flex-wrap: nowrap !important; /* Force single straight row */
    align-items: center !important; /* Align vertically in the row */
    justify-content: space-between !important;
    gap: 6px !important;
    width: 100% !important;
    min-width: 0 !important;
    min-height: 0 !important;
    height: auto !important;
    flex: 0 0 auto !important;
    flex-grow: 0 !important;
    padding: 0 !important;
    margin: 0 !important;
    border: none !important;
    background: transparent !important;
    box-shadow: none !important;
    box-sizing: border-box !important;
}

.source-item-content {
    flex: 1 1 auto !important; /* Take remaining space */
    min-width: 0 !important; /* Allow shrinking so ellipsis can trigger */
    min-height: 0 !important;
    height: auto !important;
    width: auto !important;
    padding: 0 !important;
    margin: 0 !important;
    overflow: hidden !important;
    border: none !important;
    background: transparent !important;
    box-shadow: none !important;
}

.source-item-actions {
    flex: 0 0 auto !important;
    display: flex !important;
    flex-direction: row !important;
    flex-wrap: nowrap !important; /* PREVENT Gradio gr.Row from wrapping */
    align-items: center !important;
    justify-content: flex-end !important;
    gap: 8px !important;
    margin-left: auto !important; /* Always push to right side */
    width: auto !important;
    min-width: max-content !important; /* Ensure buttons don't shrink/wrap */
    min-height: 0 !important;
    padding: 0 !important;
    margin: 0 !important;
    background: transparent !important;
    border: none !important;
}

.source-item-actions > * {
    flex: 0 0 auto !important;
    width: auto !important;
    min-width: 0 !important;
    max-width: max-content !important;
}

.bulk-action-bar {
    display: flex !important;
    flex-direction: row !important;
    align-items: center !important;
    justify-content: flex-end !important;
    background-color: transparent !important;
    border: none !important;
    border-bottom: 1px solid #e2e8f0 !important;
    border-radius: 0 !important;
    padding: 2px 4px !important;
    margin: 0 0 4px 0 !important;
    min-height: 24px !important;
}

.dark .bulk-action-bar {
    background-color: transparent !important;
    border-color: #334155 !important;
}

.bulk-count-text {
    font-size: 11px !important;
    font-weight: 500 !important;
    color: #64748b !important;
    margin: 0 auto 0 0 !important;
}

.dark .bulk-count-text {
    color: #94a3b8 !important;
}

.bulk-del-btn {
    background-color: transparent !important;
    color: #475569 !important;
    border: 1px solid transparent !important;
    border-radius: 3px !important;
    font-weight: normal !important;
    font-size: 12px !important;
    padding: 0 6px !important;
    height: 20px !important;
    min-width: 0 !important;
    cursor: pointer !important;
    transition: all 0.15s ease !important;
}

.bulk-del-btn:hover {
    background-color: #fee2e2 !important;
    color: #dc2626 !important;
    border-color: #fca5a5 !important;
}

.bulk-del-btn:disabled {
    opacity: 0.5 !important;
    cursor: not-allowed !important;
}

.source-select-cb {
    margin: 0 !important;
    padding: 0 !important;
    min-width: 20px !important;
    width: 20px !important; /* Prevent stretching */
    flex: 0 0 auto !important; /* Prevent stretching */
    background: transparent !important;
    border: none !important;
    box-shadow: none !important;
}

.source-select-cb label {
    padding: 0 !important;
    margin: 0 !important;
    background: transparent !important;
    border: none !important;
}

.source-select-cb input {
    cursor: pointer !important;
}

.source-select-cb .label-text {
    display: none !important;
}

.source-item-tags-row {
    display: flex !important;
    flex-direction: row !important;
    align-items: center !important;
    margin-bottom: 2px !important;
}

.source-item-prev-btn {
    flex: 0 0 auto !important;
    padding: 0 5px !important;
    height: 20px !important;
    min-height: 20px !important;
    max-height: 20px !important;
    line-height: 18px !important;
    font-size: 10.5px !important;
    font-weight: 500 !important;
    border: 1px solid #cbd5e1 !important;
    border-radius: 3px !important;
    background-color: #ffffff !important;
    color: #475569 !important;
    cursor: pointer !important;
    white-space: nowrap !important;
    transition: all 0.15s ease !important;
}

.source-item-prev-btn:hover {
    background-color: #f1f5f9 !important;
    border-color: #94a3b8 !important;
    color: #0f172a !important;
}

.source-item-del-btn {
    flex: 0 0 20px !important;
    width: 20px !important;
    min-width: 20px !important;
    max-width: 20px !important;
    height: 20px !important;
    min-height: 20px !important;
    max-height: 20px !important;
    padding: 0 !important;
    font-size: 14px !important;
    font-weight: bold !important;
    line-height: 1 !important;
    color: #94a3b8 !important;
    background-color: #f8fafc !important;
    border: 1px solid #e2e8f0 !important;
    border-radius: 3px !important;
    display: flex !important;
    align-items: center !important;
    justify-content: center !important;
    cursor: pointer !important;
    margin: 0 !important;
    transition: all 0.15s ease !important;
}

.source-item-del-btn:hover {
    background-color: #fee2e2 !important;
    color: #dc2626 !important;
    border-color: #fca5a5 !important;
}

/* 徹底消除 Gradio 在卡片內部 Markdown 容器預設添加的 .block.padded、.prose 巨大留白與最低高度 */
.source-item-card .block,
.source-item-card .block.padded,
.source-item-row .block,
.source-item-row .block.padded,
.source-item-content .block,
.source-item-content .block.padded {
    padding: 0 !important;
    margin: 0 !important;
    min-height: 0 !important;
    border: none !important;
    background: transparent !important;
    box-shadow: none !important;
    overflow: hidden !important;
}

.source-item-card [data-testid="markdown-wrapper"],
.source-item-card [data-testid="markdown"],
.source-item-card .prose,
.source-item-card .prose p,
.source-item-card .md,
.source-item-row [data-testid="markdown-wrapper"],
.source-item-row [data-testid="markdown"],
.source-item-row .prose,
.source-item-row .prose p,
.source-item-row .md {
    padding: 0 !important;
    margin: 0 !important;
    min-height: 0 !important;
    line-height: 1.25 !important;
    border: none !important;
    background: transparent !important;
}

/* ── Auxiliary Colors: Blue for Web, Green for Doc ── */
.source-tag {
    font-size: 10.5px !important;
    font-weight: 700 !important;
    padding: 1px 4px !important;
    margin-right: 4px !important;
    display: inline-block !important;
    flex-shrink: 0 !important;
    line-height: 1.15 !important;
}

/* Auxiliary Color 2: 沉穩鈷藍 (網路) */
.source-tag-web {
    color: #1d4ed8 !important;
    background-color: #eff6ff !important;
    border: 1px solid #bfdbfe !important;
}

/* Auxiliary Color 3: 清新翡翠綠 (文件) */
.source-tag-doc {
    color: #047857 !important;
    background-color: #ecfdf5 !important;
    border: 1px solid #a7f3d0 !important;
}

.source-item-title {
    font-size: 12px !important;
    font-weight: 600 !important;
    color: #1e293b !important;
    white-space: nowrap !important;
    overflow: hidden !important;
    text-overflow: ellipsis !important;
    line-height: 1.35 !important;
    display: flex !important;
    align-items: flex-start !important;
    width: 100% !important;
    min-width: 0 !important; /* Allow container to shrink */
    cursor: help !important;
    margin: 0 !important;
    padding: 0 !important;
}

.source-item-text {
    white-space: nowrap !important;
    overflow: hidden !important;
    text-overflow: ellipsis !important;
    flex: 1 1 auto !important;
    min-width: 0 !important;
    line-height: 1.35 !important;
}

.source-item-meta {
    font-size: 10.5px !important;
    color: #64748b !important;
    margin-top: 1px !important;
    white-space: nowrap !important;
    overflow: hidden !important;
    text-overflow: ellipsis !important;
    line-height: 1.2 !important;
}

/* ── Adaptive Overlay Chat Input Bar (往上遮蓋自適應高度 & 送出鍵在右側) ── */
.chat-center-column {
    position: relative !important;
    display: flex !important;
    flex-direction: column !important;
    height: 580px !important;
    max-height: 580px !important;
    box-sizing: border-box !important;
}

.chat-main-area {
    position: relative !important;
    height: 580px !important;
    max-height: 580px !important;
    border: 1px solid #d1d5db !important;
    border-radius: 0px !important;
    background-color: #ffffff !important;
    box-sizing: border-box !important;
}

/* 確保歷史對話底部不會被靜止時的 48px 輸入欄永久遮擋 */
.chat-main-area > div,
.chat-main-area .bubble-wrap,
.chat-main-area .wrapper {
    padding-bottom: 58px !important;
}

.adaptive-chat-input-bar {
    position: absolute !important;
    bottom: 0 !important;
    left: 0 !important;
    right: 0 !important;
    z-index: 50 !important;
    display: flex !important;
    flex-direction: row !important;
    flex-wrap: nowrap !important; /* 嚴禁換行，保證按鈕永遠在右側 */
    align-items: flex-end !important; /* 送出按鈕對齊底部 */
    min-height: 48px !important;
    max-height: 180px !important; /* 到一定範圍向上覆蓋，超出採用滾輪 */
    background-color: #ffffff !important;
    border: 1px solid #d1d5db !important; /* 淺灰色外框 */
    box-sizing: border-box !important;
    box-shadow: 0 -2px 10px rgba(0, 0, 0, 0.05) !important;
    padding: 5px 8px 5px 12px !important;
    margin: 0 !important;
    transition: border-color 0.2s ease, box-shadow 0.2s ease !important;
}

.adaptive-chat-input-bar:focus-within {
    border-color: #2563eb !important;
    box-shadow: 0 -2px 10px rgba(37, 99, 235, 0.15) !important;
}

.chat-input-field {
    flex: 1 1 auto !important;
    min-width: 0 !important; /* 防止 flex item 溢出擠壓按鈕 */
    margin: 0 !important;
    padding: 0 !important;
    border: none !important;
    box-shadow: none !important;
    background: transparent !important;
    display: flex !important;
    align-items: flex-end !important;
}

.chat-input-field .input-container {
    width: 100% !important;
    border: none !important;
    box-shadow: none !important;
    background: transparent !important;
    padding: 0 !important;
    margin: 0 !important;
}

.chat-input-field textarea {
    width: 100% !important;
    min-height: 38px !important;
    max-height: 165px !important;
    line-height: 20px !important;
    padding: 9px 8px !important;
    font-size: 14px !important;
    font-family: inherit !important;
    color: #1f2937 !important;
    border: none !important;
    border-radius: 0px !important;
    outline: none !important;
    box-shadow: none !important;
    background: transparent !important;
    resize: none !important;
    box-sizing: border-box !important;
    overflow-y: auto !important; /* 超出上限時啟用滾輪 */
}

.chat-input-field textarea:focus {
    border: none !important;
    outline: none !important;
    box-shadow: none !important;
}

.chat-input-submit-btn {
    flex: 0 0 72px !important;
    width: 72px !important;
    min-width: 72px !important;
    max-width: 72px !important;
    height: 38px !important;
    min-height: 38px !important;
    max-height: 38px !important;
    margin: 0 0 1px 8px !important;
    padding: 0 !important;
    font-size: 13px !important;
    font-weight: 700 !important;
    color: #ffffff !important;
    background-color: #2563eb !important;
    border: none !important;
    border-radius: 0px !important;
    cursor: pointer !important;
    display: flex !important;
    align-items: center !important;
    justify-content: center !important;
    white-space: nowrap !important;
    transition: background-color 0.15s ease !important;
}

.chat-input-submit-btn:hover {
    background-color: #1d4ed8 !important;
    color: #ffffff !important;
}


/* ── AI Skills Floating Dropdown Overlay (覆蓋日誌而不擠壓排版) ── */
.console-right-column {
    position: relative !important;
    overflow: visible !important;
}

.skills-accordion,
.console-right-column .gr-accordion {
    position: relative !important;
    overflow: visible !important;
    z-index: 100 !important;
}

.skills-accordion .label-wrap,
.console-right-column .gr-accordion .label-wrap,
.skills-accordion .label-wrap.open,
.console-right-column .gr-accordion .label-wrap.open {
    margin-bottom: 0 !important;
    width: 100% !important;
    cursor: pointer !important;
}

.skills-accordion > div[data-testid="accordion-content"],
.console-right-column .gr-accordion > div[data-testid="accordion-content"] {
    position: absolute !important;
    top: calc(100% + 4px) !important;
    left: 0 !important;
    right: 0 !important;
    width: 100% !important;
    z-index: 500 !important;
    background-color: #ffffff !important;
    border: 1px solid #d1d5db !important;
    box-shadow: 0 12px 28px rgba(0, 0, 0, 0.16), 0 2px 6px rgba(0, 0, 0, 0.08) !important;
    box-sizing: border-box !important;
    padding: 6px !important;
}

.skills-accordion > div[data-testid="accordion-content"][style*="display: none"],
.console-right-column .gr-accordion > div[data-testid="accordion-content"][style*="display: none"] {
    display: none !important;
}

.skills-scroll-container {
    max-height: 220px !important;
    overflow-y: auto !important;
    display: flex !important;
    flex-direction: column !important;
    gap: 6px !important;
    padding: 4px !important;
    border: 1px solid #e5e7eb !important;
    background-color: #fafbfc !important;
}

.skill-item-card {
    background-color: #ffffff !important;
    border: 1px solid #e5e7eb !important;
    padding: 7px 9px !important;
    line-height: 1.4 !important;
}

.skill-item-name {
    font-size: 12px !important;
    font-weight: 700 !important;
    color: #1f2937 !important;
    margin-bottom: 2px !important;
}

.skill-item-desc {
    font-size: 11px !important;
    color: #6b7280 !important;
    line-height: 1.4 !important;
    white-space: pre-wrap !important;
    word-break: break-word !important;
}

/* ── Compact Logs Box (自適應高度範圍 240px ~ 460px) ── */
.compact-logs-box {
    display: flex !important;
    flex-direction: column !important;
    width: 100% !important;
}

.compact-logs-box > label {
    display: flex !important;
    flex-direction: column !important;
    flex: 1 1 auto !important;
}

.compact-logs-box textarea {
    font-family: 'Consolas', 'Courier New', monospace !important;
    font-size: 11px !important;
    line-height: 1.4 !important;
    background-color: #f8fafc !important;
    border: 1px solid #e5e7eb !important;
    color: #334155 !important;
    min-height: 240px !important;
    max-height: 460px !important;
    overflow-y: auto !important;
    resize: vertical !important;
    box-sizing: border-box !important;
}

/* ── Modal Overlay & Dialog Styles ── */
#source_modal_overlay {
    position: fixed !important;
    top: 0 !important;
    left: 0 !important;
    width: 100vw !important;
    height: 100vh !important;
    background-color: rgba(0, 0, 0, 0.45) !important;
    z-index: 10000 !important;
    display: flex !important;
    align-items: center !important;
    justify-content: center !important;
    padding: 20px !important;
    box-sizing: border-box !important;
}

#source_modal_overlay.hide-container,
#source_modal_overlay[style*="display: none"],
.hide-container #source_modal_overlay {
    display: none !important;
}

#source_modal_dialog {
    background-color: #ffffff !important;
    color: #1f2937 !important;
    border: 1px solid #d1d5db !important;
    border-radius: 0px !important;
    width: 720px !important;
    max-width: 96vw !important;
    max-height: 90vh !important;
    overflow-y: auto !important;
    padding: 24px !important;
    box-shadow: 0 16px 40px rgba(0, 0, 0, 0.12) !important;
}

#att_preview_modal_dialog {
    background-color: #ffffff !important;
    color: #1f2937 !important;
    border: 1px solid #d1d5db !important;
    border-radius: 0px !important;
    width: 800px !important;
    max-width: 96vw !important;
    height: 85vh !important;
    max-height: 85vh !important;
    overflow: hidden !important;
    display: flex !important;
    flex-direction: column !important;
    padding: 0 !important;
    box-shadow: 0 16px 40px rgba(0, 0, 0, 0.12) !important;
}

#att_preview_modal_dialog .modal-header-row {
    margin: 0 !important;
    padding: 20px 24px 12px 24px !important;
    background-color: #ffffff !important;
    border-bottom: 1px solid #e5e7eb !important;
    position: sticky !important;
    top: 0 !important;
    z-index: 10 !important;
    flex: 0 0 auto !important;
}

.dark #att_preview_modal_dialog .modal-header-row {
    background-color: #1e293b !important;
    border-color: #334155 !important;
}

#att_preview_modal_dialog .modal-subtext {
    padding: 12px 24px !important;
    margin: 0 !important;
    background-color: #f8fafc !important;
    border-bottom: 1px solid #e2e8f0 !important;
    z-index: 9 !important;
    flex: 0 0 auto !important;
    overflow: visible !important;
}

#att_preview_modal_dialog .modal-subtext .prose,
#att_preview_modal_dialog .modal-subtext [data-testid="markdown"],
#att_preview_modal_dialog .modal-subtext [data-testid="markdown-wrapper"] {
    overflow: visible !important;
    max-height: none !important;
    padding: 0 !important;
    border: none !important;
    box-shadow: none !important;
    background: transparent !important;
}

.dark #att_preview_modal_dialog .modal-subtext {
    background-color: #0f172a !important;
    border-color: #334155 !important;
}

.att-preview-content-box {
    padding: 24px !important;
    overflow-y: auto !important;
    overflow-x: hidden !important;
    flex: 1 1 auto !important;
    min-height: 0 !important;
    height: 100% !important;
}

.att-preview-content-box .prose,
.att-preview-content-box [data-testid="markdown"],
.att-preview-content-box [data-testid="markdown-wrapper"] {
    overflow: visible !important;
    max-height: none !important;
    min-height: auto !important;
    padding: 0 !important;
    border: none !important;
    box-shadow: none !important;
    background: transparent !important;
}

#att_preview_modal_dialog .modal-footer-row {
    margin: 0 !important;
    padding: 16px 24px !important;
    background-color: #ffffff !important;
    border-top: 1px solid #e5e7eb !important;
}

.dark #att_preview_modal_dialog .modal-footer-row {
    background-color: #1e293b !important;
    border-color: #334155 !important;
}

.modal-header-row {
    display: flex !important;
    align-items: center !important;
    justify-content: space-between !important;
    border-bottom: 1px solid #e5e7eb !important;
    padding-bottom: 8px !important;
    margin-bottom: 12px !important;
}

.modal-x-btn {
    min-width: 32px !important;
    max-width: 32px !important;
    height: 32px !important;
    padding: 0 !important;
    font-size: 16px !important;
    font-weight: bold !important;
    line-height: 1 !important;
    cursor: pointer !important;
    color: #6b7280 !important;
    background-color: #f9fafb !important;
    border: 1px solid #d1d5db !important;
}

.modal-x-btn:hover {
    background-color: #f3f4f6 !important;
    color: #111827 !important;
    border-color: #9ca3af !important;
}

.modal-subtext {
    font-size: 12px !important;
    color: #6b7280 !important;
    margin-bottom: 14px !important;
}

.modal-hint-box {
    font-size: 12px !important;
    color: #334155 !important;
    background-color: #f1f5f9 !important;
    border: 1px solid #e2e8f0 !important;
    border-left: 3px solid #2563eb !important;
    padding: 8px 12px !important;
    margin: 8px 0 14px 0 !important;
    line-height: 1.5 !important;
}

.modal-footer-row {
    margin-top: 18px !important;
    padding-top: 12px !important;
    border-top: 1px solid #e5e7eb !important;
    display: flex !important;
    justify-content: flex-end !important;
}

/* ── Search Results Box inside Modal ── */
.search-results-box {
    border: 1px solid #e5e7eb !important;
    background-color: #fafbfc !important;
    padding: 10px !important;
    margin-top: 10px !important;
    /* Removed max-height and overflow-y to prevent double scrollbars */
}

.search-results-list {
    display: flex !important;
    flex-direction: column !important;
    gap: 8px !important;
    width: 100% !important;
}

.search-result-item {
    background-color: #ffffff !important;
    border: 1px solid #e5e7eb !important;
    padding: 10px 12px !important;
    display: flex !important;
    flex-direction: row !important;
    align-items: flex-start !important;
    align-self: flex-start !important;
    height: auto !important;
    justify-content: space-between !important;
    gap: 12px !important;
    transition: border-color 0.15s ease !important;
}

.search-result-item:hover {
    border-color: #3b82f6 !important;
}

.search-result-info {
    flex: 1 1 auto !important;
    overflow: hidden !important;
}

.search-res-header {
    display: flex !important;
    flex-direction: row !important;
    align-items: center !important;
    flex-wrap: wrap !important;
    gap: 6px !important;
    margin-bottom: 4px !important;
}

.search-res-idx {
    font-size: 11px !important;
    font-weight: 700 !important;
    color: #64748b !important;
    background-color: #f1f5f9 !important;
    padding: 1px 5px !important;
    border-radius: 3px !important;
    border: 1px solid #e2e8f0 !important;
}

.source-cat-badge {
    display: inline-block !important;
    font-size: 11px !important;
    font-weight: 600 !important;
    padding: 1px 6px !important;
    border-radius: 4px !important;
    background-color: #eff6ff !important;
    color: #1d4ed8 !important;
    border: 1px solid #bfdbfe !important;
    margin-right: 4px !important;
    vertical-align: middle !important;
    white-space: nowrap !important;
    flex-shrink: 0 !important;
}

.search-res-site {
    font-size: 11px !important;
    font-weight: 600 !important;
    color: #0369a1 !important;
    background-color: #f0f9ff !important;
    border: 1px solid #bae6fd !important;
    padding: 1px 5px !important;
    border-radius: 3px !important;
}

.search-res-title {
    font-size: 13px !important;
    font-weight: 700 !important;
    color: #0f172a !important;
    line-height: 1.3 !important;
}

.search-res-url {
    font-size: 11px !important;
    color: #2563eb !important; /* 沉穩鈷藍 */
    word-break: break-all !important;
    margin-bottom: 4px !important;
}

.search-res-snippet {
    font-size: 11px !important;
    color: #64748b !important;
    line-height: 1.4 !important;
    display: -webkit-box !important;
    -webkit-line-clamp: 2 !important;
    line-clamp: 2 !important;
    -webkit-box-orient: vertical !important;
    overflow: hidden !important;
    text-overflow: ellipsis !important;
    min-height: 32px !important;
    max-height: 32px !important;
}

.search-add-btn {
    white-space: nowrap !important;
    font-size: 12px !important;
    font-weight: bold !important;
    min-width: 95px !important;
    height: 32px !important;
    background-color: #2563eb !important;
    color: #ffffff !important;
    border: 1px solid #2563eb !important;
}

.search-add-btn:hover {
    background-color: #1d4ed8 !important;
    border-color: #1d4ed8 !important;
}

.search-added-btn {
    white-space: nowrap !important;
    font-size: 12px !important;
    font-weight: 600 !important;
    min-width: 95px !important;
    height: 32px !important;
    background-color: #ecfdf5 !important;
    color: #047857 !important; /* 清新翡翠綠 */
    border: 1px solid #a7f3d0 !important;
    cursor: default !important;
}

/* Ensure toast notifications appear in top-right corner */
.toast-wrap {
    top: 24px !important;
    right: 24px !important;
    left: auto !important;
    bottom: auto !important;
    transform: none !important;
    z-index: 99999 !important;
}
.toast-wrap > * {
    border-radius: 0px !important;
    border: 1px solid #d1d5db !important;
    box-shadow: 0 4px 16px rgba(0, 0, 0, 0.12) !important;
}

/* Hide only component block header error labels, NOT toast notifications */
.block-label span.error {
    display: none !important;
}

/* Dislike / like / add adjustments */
button[aria-label*="dislike" i], button[title*="dislike" i], 
button[aria-label*="不讚"], button[title*="不讚"],
.message-buttons-left button:nth-child(2),
.message-buttons-right button:nth-child(2),
.dislike-btn {
    display: none !important;
}

button[aria-label*="like" i], button[title*="like" i],
button[aria-label*="讚"], button[title*="讚"],
button[aria-label*="add" i], button[title*="add" i],
.like-btn {
    position: relative !important;
}

button[aria-label*="like" i] svg, button[title*="like" i] svg,
button[aria-label*="讚"] svg, button[title*="讚"] svg,
button[aria-label*="add" i] svg, button[title*="add" i] svg,
.like-btn svg {
    display: none !important;
}

button[aria-label*="like" i]::after, button[title*="like" i]::after,
button[aria-label*="讚"]::after, button[title*="讚"]::after,
button[aria-label*="add" i]::after, button[title*="add" i]::after,
.like-btn::after {
    content: "+" !important;
    font-size: 20px !important;
    font-weight: bold !important;
    color: #6b7280 !important;
    line-height: 1 !important;
    display: flex !important;
    justify-content: center !important;
    align-items: center !important;
    width: 100% !important;
    height: 100% !important;
}

button[aria-label*="like" i]:hover::after, button[title*="like" i]:hover::after,
button[aria-label*="讚"]:hover::after, button[title*="讚"]:hover::after,
button[aria-label*="add" i]:hover::after, button[title*="add" i]:hover::after,
.like-btn:hover::after {
    color: #2563eb !important;
}

/* Center Tabs (對話區 & 記憶管理區) */
.chat-center-tabs {
    height: 100% !important;
    display: flex !important;
    flex-direction: column !important;
}

.chat-center-tabs > div[role="tabpanel"],
.chat-center-tabs .tabitem {
    position: relative !important;
    flex: 1 1 auto !important;
    height: calc(100% - 44px) !important;
    box-sizing: border-box !important;
}

.memory-tab-panel {
    height: auto !important;
    min-height: 520px !important;
    max-height: none !important;
    overflow: visible !important;
    padding: 16px 20px !important;
    background-color: #ffffff !important;
    border: 1px solid #d1d5db !important;
    box-sizing: border-box !important;
}

.memory-subtext {
    font-size: 13px !important;
    color: #4b5563 !important;
    margin-bottom: 12px !important;
    line-height: 1.5 !important;
}

.memory-subtext-hint {
    font-size: 12px !important;
    color: #6b7280 !important;
}

.memory-box-wrapper {
    background-color: #fafbfc !important;
    border: 1px solid #e5e7eb !important;
    padding: 8px !important;
    min-height: 380px !important;
    max-height: none !important;
    overflow: visible !important;
}

.skill-empty-note {
    color: #6b7280 !important;
    font-size: 11px !important;
    padding: 6px !important;
}

/* ── 附件區塊樣式 ──────────────────────────────────────────────────── */
.source-attachments-box {
    margin-top: 3px !important;
    padding: 4px 6px !important;
    background: #f8fafc !important;
    border: 1px dashed #cbd5e1 !important;
    border-radius: 4px !important;
    display: flex !important;
    flex-direction: column !important;
    gap: 3px !important;
    width: 100% !important;
    max-width: 100% !important;
    min-width: 0 !important;
    box-sizing: border-box !important;
}

.attachment-item-row {
    display: flex !important;
    flex-direction: row !important;
    flex-wrap: nowrap !important;
    align-items: center !important;
    justify-content: space-between !important;
    padding: 2px 2px !important;
    border-bottom: 1px solid #f1f5f9 !important;
    gap: 5px !important;
    width: 100% !important;
    max-width: 100% !important;
    min-width: 0 !important;
    box-sizing: border-box !important;
    background: transparent !important;
}

.attachment-item-row:last-child {
    border-bottom: none !important;
}

.attachment-item-row:hover {
    background-color: #f1f5f9 !important;
    border-radius: 3px !important;
}

/* 徹底消除 Gradio block 預設產生的突兀格子邊框與背景 */
.attachment-item-row .block.att-info,
.attachment-item-row .att-info,
.att-info {
    display: flex !important;
    align-items: center !important;
    gap: 5px !important;
    min-width: 0 !important;
    flex: 1 1 0% !important;
    width: 0 !important;
    border: none !important;
    background: transparent !important;
    box-shadow: none !important;
    padding: 0 !important;
    margin: 0 !important;
    overflow: hidden !important;
}

.att-info [data-testid="markdown-wrapper"],
.att-info [data-testid="markdown"],
.att-info .prose,
.att-info p {
    margin: 0 !important;
    padding: 0 !important;
    display: flex !important;
    align-items: center !important;
    gap: 5px !important;
    min-width: 0 !important;
    width: 100% !important;
    overflow: hidden !important;
    background: transparent !important;
    border: none !important;
}

.att-ext-badge {
    display: inline-flex !important;
    align-items: center !important;
    justify-content: center !important;
    font-size: 9.5px !important;
    font-weight: 700 !important;
    padding: 1px 3px !important;
    border-radius: 2px !important;
    background-color: #e0e7ff !important;
    color: #3730a3 !important;
    letter-spacing: 0.3px !important;
    flex-shrink: 0 !important;
    height: 16px !important;
    line-height: 1 !important;
}

.att-name {
    font-size: 11.5px !important;
    color: #334155 !important;
    white-space: nowrap !important;
    overflow: hidden !important;
    text-overflow: ellipsis !important;
    flex: 1 1 auto !important;
    min-width: 0 !important;
    max-width: none !important;
    display: block !important;
    cursor: help !important;
    text-decoration: underline dotted #94a3b8 !important;
    text-underline-offset: 2px !important;
    transition: color 0.15s ease, text-decoration-color 0.15s ease !important;
}

.att-name:hover {
    color: #2563eb !important;
    text-decoration-color: #2563eb !important;
}

.att-preview-btn {
    font-size: 10.5px !important;
    padding: 0 4px !important;
    height: 20px !important;
    min-height: 20px !important;
    line-height: 18px !important;
    background: #ffffff !important;
    color: #475569 !important;
    border: 1px solid #cbd5e1 !important;
    border-radius: 3px !important;
    cursor: pointer !important;
    flex-shrink: 0 !important;
    min-width: 32px !important;
    white-space: nowrap !important;
    word-break: keep-all !important;
    transition: all 0.15s ease !important;
}

.att-preview-btn:hover {
    background: #f1f5f9 !important;
    color: #0f172a !important;
    border-color: #94a3b8 !important;
}

.att-download-btn {
    font-size: 10.5px !important;
    padding: 0 5px !important;
    height: 20px !important;
    min-height: 20px !important;
    line-height: 18px !important;
    background: #ffffff !important;
    color: #2563eb !important;
    border: 1px solid #bfdbfe !important;
    border-radius: 3px !important;
    cursor: pointer !important;
    flex-shrink: 0 !important;
    min-width: 40px !important;
    white-space: nowrap !important;
    word-break: keep-all !important;
    transition: all 0.15s ease !important;
}

.att-download-btn:hover {
    background: #eff6ff !important;
    border-color: #2563eb !important;
}

.att-downloaded-btn, .att-downloaded-btn:hover {
    background: #f1f5f9 !important;
    color: #64748b !important;
    border: 1px solid #cbd5e1 !important;
    cursor: default !important;
    opacity: 0.8 !important;
}

/* ── 全域即時懸浮提示卡片 (Rich Floating Tooltip Card) ──────────────── */
.lpb-tooltip {
    position: fixed !important;
    display: none;
    max-width: 360px !important;
    background-color: #0f172a !important;
    color: #f8fafc !important;
    padding: 8px 12px !important;
    border: 1px solid #334155 !important;
    border-radius: 4px !important;
    box-shadow: 0 12px 28px rgba(0, 0, 0, 0.45) !important;
    font-size: 11.5px !important;
    line-height: 1.4 !important;
    pointer-events: none !important;
    z-index: 9999999 !important;
    word-break: break-word !important;
    opacity: 0;
    transition: opacity 0.12s ease !important;
}

.lpb-tooltip.lpb-tooltip-show {
    display: block !important;
    opacity: 1 !important;
}

.lpb-tooltip-head {
    display: flex !important;
    align-items: center !important;
    gap: 6px !important;
    font-weight: 700 !important;
    font-size: 12px !important;
    color: #ffffff !important;
    margin-bottom: 4px !important;
    line-height: 1.35 !important;
}

.lpb-tooltip-tag {
    font-size: 10px !important;
    font-weight: 700 !important;
    padding: 1px 4px !important;
    border-radius: 2px !important;
    flex-shrink: 0 !important;
}

.lpb-tag-web {
    background-color: #1e3a8a !important;
    color: #93c5fd !important;
}

.lpb-tag-doc {
    background-color: #064e3b !important;
    color: #6ee7b7 !important;
}

.lpb-tag-att {
    background-color: #312e81 !important;
    color: #c7d2fe !important;
}

.lpb-tooltip-title {
    flex: 1 1 auto !important;
    word-break: break-all !important;
}

.lpb-tooltip-meta {
    font-size: 11px !important;
    color: #94a3b8 !important;
    margin-bottom: 4px !important;
}

.lpb-tooltip-url {
    word-break: break-all !important;
    color: #cbd5e1 !important;
    font-family: Consolas, monospace !important;
    font-size: 10px !important;
}

.lpb-tooltip-status {
    margin-top: 2px !important;
    color: #94a3b8 !important;
}

.lpb-tooltip-ext {
    display: inline-block !important;
    font-family: Consolas, monospace !important;
    font-weight: 700 !important;
    color: #93c5fd !important;
    background-color: #1e3a8a !important;
    padding: 1px 5px !important;
    border-radius: 2px !important;
    font-size: 10px !important;
    margin-right: 4px !important;
}

.lpb-tooltip-tip {
    font-size: 10.5px !important;
    color: #93c5fd !important;
    border-top: 1px solid #334155 !important;
    padding-top: 4px !important;
    margin-top: 4px !important;
}

/* ── 附件預覽彈窗樣式 ──────────────────────────────────────────────── */
#att_preview_modal_overlay {
    position: fixed !important;
    top: 0 !important;
    left: 0 !important;
    width: 100vw !important;
    height: 100vh !important;
    background-color: rgba(0, 0, 0, 0.45) !important;
    z-index: 10001 !important;
    display: flex !important;
    align-items: center !important;
    justify-content: center !important;
    padding: 20px !important;
    box-sizing: border-box !important;
}

#att_preview_modal_overlay.hide-container,
#att_preview_modal_overlay[style*="display: none"],
.hide-container #att_preview_modal_overlay {
    display: none !important;
}

#att_preview_modal_dialog {
    background-color: #ffffff !important;
    color: #1f2937 !important;
    border: 1px solid #d1d5db !important;
    border-radius: 4px !important;
    width: 720px !important;
    max-width: 95vw !important;
    max-height: 88vh !important;
    overflow-y: auto !important;
    padding: 22px !important;
    box-shadow: 0 16px 40px rgba(0, 0, 0, 0.15) !important;
    box-sizing: border-box !important;
}

.att-modal-meta-box {
    background-color: #f8fafc !important;
    border: 1px solid #e2e8f0 !important;
    border-radius: 4px !important;
    padding: 10px 14px !important;
    margin-bottom: 12px !important;
    font-size: 12px !important;
    color: #334155 !important;
    line-height: 1.6 !important;
}

.att-external-link {
    color: #2563eb !important;
    text-decoration: underline !important;
    font-weight: 500 !important;
    word-break: break-all !important;
}

.att-external-link:hover {
    color: #1d4ed8 !important;
}

.att-preview-content-box {
    background-color: #fafbfc !important;
    border: 1px solid #e5e7eb !important;
    border-radius: 4px !important;
    padding: 14px 16px !important;
    max-height: 380px !important;
    overflow-y: auto !important;
    margin-bottom: 12px !important;
}

.att-preview-body-text {
    font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace !important;
    white-space: pre-wrap !important;
    word-break: break-word !important;
    font-size: 12px !important;
    color: #374151 !important;
    line-height: 1.6 !important;
}

.att-preview-empty-note {
    color: #6b7280 !important;
    font-size: 13px !important;
    padding: 24px 10px !important;
    text-align: center !important;
    line-height: 1.6 !important;
}

/* ── AI 技能清單 (Skills Accordion) 樣式 ────────────────────────────── */
.skills-accordion {
    margin-bottom: 8px !important;
}

.skills-scroll-container {
    max-height: 460px !important;
    overflow-y: auto !important;
    overflow-x: hidden !important;
    padding-right: 4px !important;
    display: flex !important;
    flex-direction: column !important;
    gap: 8px !important;
}

.skills-scroll-container * {
    white-space: normal !important;
    word-break: break-word !important;
}

.skill-item-card {
    background: #ffffff !important;
    border: 1px solid #e2e8f0 !important;
    border-left: 3px solid #2563eb !important;
    padding: 8px 10px !important;
    font-size: 12px !important;
    box-shadow: 0 1px 2px rgba(0, 0, 0, 0.05) !important;
}

.skill-card-top {
    display: flex !important;
    align-items: flex-start !important;
    flex-direction: column !important;
    gap: 4px !important;
    margin-bottom: 6px !important;
}

.skill-item-name {
    font-weight: 700 !important;
    color: #0f172a !important;
    font-size: 12px !important;
}

.skill-fn-badge {
    font-family: Consolas, monospace !important;
    font-size: 10px !important;
    background: #eff6ff !important;
    color: #1d4ed8 !important;
    border: 1px solid #bfdbfe !important;
    padding: 2px 5px !important;
    border-radius: 3px !important;
    font-weight: 600 !important;
    word-break: break-all !important;
}

.skill-file-badge {
    font-family: Consolas, monospace !important;
    font-size: 10px !important;
    color: #64748b !important;
    margin-bottom: 4px !important;
}

.skill-item-desc {
    color: #334155 !important;
    line-height: 1.45 !important;
    font-size: 11.5px !important;
    margin-bottom: 6px !important;
}

.skill-params-container {
    background: #f8fafc !important;
    border: 1px solid #e2e8f0 !important;
    padding: 5px 7px !important;
    margin-top: 5px !important;
    border-radius: 3px !important;
}

.skill-section-label {
    font-size: 10.5px !important;
    font-weight: 700 !important;
    color: #475569 !important;
    margin-bottom: 3px !important;
}

.skill-param-row {
    font-size: 11px !important;
    padding: 3px 0 !important;
    border-bottom: 1px dashed #e2e8f0 !important;
}

.skill-param-row:last-child {
    border-bottom: none !important;
}

.skill-param-name code {
    font-family: Consolas, monospace !important;
    font-size: 11px !important;
    color: #2563eb !important;
    font-weight: 700 !important;
}

.skill-param-type {
    font-size: 10px !important;
    color: #64748b !important;
    margin-left: 2px !important;
}

.skill-param-req {
    font-size: 9.5px !important;
    color: #dc2626 !important;
    background: #fee2e2 !important;
    padding: 0 4px !important;
    border-radius: 2px !important;
    margin-left: 3px !important;
    font-weight: 600 !important;
}

.skill-param-opt {
    font-size: 9.5px !important;
    color: #64748b !important;
    background: #f1f5f9 !important;
    padding: 0 4px !important;
    border-radius: 2px !important;
    margin-left: 3px !important;
}

.skill-param-desc {
    font-size: 10.5px !important;
    color: #475569 !important;
    margin-top: 1px !important;
    line-height: 1.35 !important;
}

.skill-details-toggle {
    margin-top: 5px !important;
    font-size: 11px !important;
}

.skill-details-summary {
    cursor: pointer !important;
    color: #2563eb !important;
    font-weight: 600 !important;
    font-size: 10.5px !important;
}

.skill-details-summary:hover {
    text-decoration: underline !important;
}

.skill-full-desc {
    margin-top: 4px !important;
    padding: 5px 7px !important;
    background: #f1f5f9 !important;
    border: 1px solid #cbd5e1 !important;
    border-radius: 3px !important;
    font-size: 10.5px !important;
    color: #1e293b !important;
    line-height: 1.4 !important;
}

/* ── 隱藏底部浮動 Gradio API 按鈕與 Cookie 橫幅 ── */
.show-api, #lpb-cookie-banner, .lpb-cookie-banner {
    display: none !important;
}

/* ══════════════════════════════════════════════════════════════════════
   ── 全站深色模式適配 (Dark Mode Comprehensive Support) ───────────────
   ══════════════════════════════════════════════════════════════════════ */

/* 深色模式通用全域設定 */
.dark body,
body.dark,
html.dark body,
html.dark,
.dark .gradio-container,
.gradio-container.dark {
    background-color: #0b0f19 !important;
    color: #f1f5f9 !important;
}

@media (prefers-color-scheme: dark) {
    body {
        background-color: #0b0f19 !important;
        color: #f1f5f9 !important;
    }
    .gradio-container {
        color: #f1f5f9 !important;
    }
}

/* 1. 頂部標題列與清除對話按鈕 */
.dark .app-header-bar {
    border-bottom-color: #334155 !important;
}
.dark .app-header-title h1,
.dark .app-header-bar h1 {
    color: #f8fafc !important;
}
.dark .app-clear-btn {
    color: #cbd5e1 !important;
    background-color: #1e293b !important;
    border-color: #334155 !important;
}
.dark .app-clear-btn:hover {
    background-color: #334155 !important;
    color: #ffffff !important;
    border-color: #475569 !important;
}

/* 2. 左側：知識來源庫側邊欄 */
.dark .sidebar-header-row h3 {
    color: #f8fafc !important;
}
.dark #src_desc {
    color: #94a3b8 !important;
}
.dark .sidebar-toggle-btn {
    color: #cbd5e1 !important;
    background-color: #1e293b !important;
    border-color: #334155 !important;
}
.dark .sidebar-toggle-btn:hover {
    background-color: #334155 !important;
    color: #ffffff !important;
    border-color: #475569 !important;
}
.dark .sources-box-wrapper {
    background-color: #0f172a !important;
    border-color: #334155 !important;
}
.dark .source-item-card,
.dark .source-item-row {
    background-color: #1e293b !important;
    border-color: #334155 !important;
}
.dark .source-item-card:hover,
.dark .source-item-row:hover {
    background-color: #243044 !important;
    border-color: #475569 !important;
}
.dark .source-item-title {
    color: #f1f5f9 !important;
}
.dark .source-item-meta {
    color: #94a3b8 !important;
}
.dark .source-item-prev-btn {
    background-color: #1e293b !important;
    border-color: #475569 !important;
    color: #cbd5e1 !important;
}
.dark .source-item-prev-btn:hover {
    background-color: #334155 !important;
    border-color: #64748b !important;
    color: #f8fafc !important;
}
.dark .source-item-del-btn {
    color: #94a3b8 !important;
    background-color: #0f172a !important;
    border-color: #334155 !important;
}
.dark .source-item-del-btn:hover {
    background-color: #450a0a !important;
    color: #fca5a5 !important;
    border-color: #dc2626 !important;
}

/* 3. 來源類型標籤 */
.dark .source-tag-web {
    color: #93c5fd !important;
    background-color: #1e3a8a !important;
    border-color: #3b82f6 !important;
}
.dark .source-tag-doc {
    color: #6ee7b7 !important;
    background-color: #064e3b !important;
    border-color: #10b981 !important;
}

/* 4. 附件清單區塊 */
.dark .source-attachments-box {
    background-color: #141e30 !important;
    border-color: #334155 !important;
}
.dark .attachment-item-row {
    border-bottom-color: #1e293b !important;
}
.dark .attachment-item-row:hover {
    background-color: #1e293b !important;
}
.dark .att-ext-badge {
    background-color: #312e81 !important;
    color: #c7d2fe !important;
}
.dark .att-name {
    color: #cbd5e1 !important;
    text-decoration-color: #64748b !important;
}
.dark .att-name:hover {
    color: #38bdf8 !important;
    text-decoration-color: #38bdf8 !important;
}
.dark .lpb-tooltip {
    background-color: #0f172a !important;
    border-color: #334155 !important;
    color: #f1f5f9 !important;
    box-shadow: 0 16px 36px rgba(0, 0, 0, 0.6) !important;
}
.dark .lpb-tooltip-head {
    color: #f8fafc !important;
}
.dark .lpb-tooltip-meta {
    color: #94a3b8 !important;
}
.dark .lpb-tooltip-tip {
    color: #93c5fd !important;
    border-top-color: #334155 !important;
}
.dark .att-preview-btn {
    background-color: #1e293b !important;
    color: #cbd5e1 !important;
    border-color: #475569 !important;
}
.dark .att-preview-btn:hover {
    background-color: #334155 !important;
    color: #ffffff !important;
    border-color: #64748b !important;
}
.dark .att-download-btn {
    background-color: #1e293b !important;
    color: #60a5fa !important;
    border-color: #3b82f6 !important;
}
.dark .att-download-btn:hover {
    background-color: #2563eb !important;
    color: #ffffff !important;
    border-color: #2563eb !important;
}

.dark .att-downloaded-btn, .dark .att-downloaded-btn:hover {
    background-color: #0f172a !important;
    color: #64748b !important;
    border-color: #334155 !important;
    cursor: default !important;
    opacity: 0.8 !important;
}

/* 5. 中央區：對話區與長期記憶庫分頁 */
.dark .chat-center-tabs {
    background-color: transparent !important;
}
.dark .chat-center-tabs button[role="tab"],
.dark .gradio-container button[role="tab"] {
    color: #94a3b8 !important;
    border-color: transparent !important;
    background-color: transparent !important;
}
.dark .chat-center-tabs button[role="tab"].selected,
.dark .gradio-container button[role="tab"].selected {
    color: #f8fafc !important;
    border-bottom: 2px solid #38bdf8 !important;
    background-color: transparent !important;
}
.dark .memory-tab-panel {
    background-color: #111827 !important;
    border-color: #334155 !important;
    color: #f1f5f9 !important;
}
.dark .memory-subtext {
    color: #cbd5e1 !important;
}
.dark .memory-subtext-hint {
    color: #94a3b8 !important;
}
.dark .memory-box-wrapper {
    background-color: #0f172a !important;
    border-color: #334155 !important;
}

.dark .chat-main-area {
    background-color: #111827 !important;
    border-color: #334155 !important;
}
.dark .adaptive-chat-input-bar {
    background-color: #1e293b !important;
    border-color: #334155 !important;
    box-shadow: 0 -2px 12px rgba(0, 0, 0, 0.4) !important;
}
.dark .adaptive-chat-input-bar:focus-within {
    border-color: #38bdf8 !important;
    box-shadow: 0 -2px 10px rgba(56, 189, 248, 0.2) !important;
}
.dark .chat-input-field textarea {
    color: #f8fafc !important;
}
.dark button[aria-label*="like"]::after,
.dark button[title*="like"]::after,
.dark .like-btn::after {
    color: #94a3b8 !important;
}
.dark button[aria-label*="like"]:hover::after,
.dark button[title*="like"]:hover::after,
.dark .like-btn:hover::after {
    color: #38bdf8 !important;
}

/* 6. 右側：開發者控制台與技能清單 */
.dark .console-right-column h3,
.dark .console-right-column strong {
    color: #f8fafc !important;
}
.dark .skills-accordion > div[data-testid="accordion-content"],
.dark .console-right-column .gr-accordion > div[data-testid="accordion-content"] {
    background-color: #1e293b !important;
    border-color: #334155 !important;
    box-shadow: 0 12px 28px rgba(0, 0, 0, 0.5) !important;
}
.dark .skills-scroll-container {
    background-color: #0f172a !important;
    border-color: #334155 !important;
}
.dark .skill-item-card {
    background-color: #1e293b !important;
    border-color: #334155 !important;
    border-left: 3px solid #38bdf8 !important;
}
.dark .skill-item-name {
    color: #f8fafc !important;
}
.dark .skill-fn-badge {
    background-color: #1e3a8a !important;
    color: #93c5fd !important;
    border-color: #3b82f6 !important;
}
.dark .skill-file-badge {
    color: #94a3b8 !important;
}
.dark .skill-item-desc {
    color: #cbd5e1 !important;
}
.dark .skill-params-container {
    background-color: #0f172a !important;
    border-color: #334155 !important;
}
.dark .skill-section-label {
    color: #94a3b8 !important;
}
.dark .skill-param-row {
    border-bottom-color: #334155 !important;
}
.dark .skill-param-name code {
    color: #93c5fd !important;
}
.dark .skill-param-type {
    color: #94a3b8 !important;
}
.dark .skill-param-opt {
    background-color: #334155 !important;
    color: #cbd5e1 !important;
}
.dark .skill-param-desc {
    color: #94a3b8 !important;
}
.dark .skill-details-summary {
    color: #38bdf8 !important;
}
.dark .skill-full-desc {
    background-color: #0f172a !important;
    border-color: #334155 !important;
    color: #cbd5e1 !important;
}
.dark .skill-empty-note {
    color: #94a3b8 !important;
}
.dark .compact-logs-box textarea {
    background-color: #0f172a !important;
    border-color: #334155 !important;
    color: #cbd5e1 !important;
}

/* 7. 新增知識來源對話框 (Source Addition Dialog) */
.dark #source_modal_dialog {
    background-color: #1e293b !important;
    border-color: #475569 !important;
    color: #f1f5f9 !important;
    box-shadow: 0 20px 50px rgba(0, 0, 0, 0.6) !important;
}
.dark #source_modal_dialog h3,
.dark #source_modal_dialog h4,
.dark #source_modal_dialog p,
.dark #source_modal_dialog span,
.dark #source_modal_dialog label,
.dark #source_modal_dialog strong {
    color: #f8fafc !important;
}
.dark #source_modal_dialog .modal-header-row {
    border-bottom-color: #334155 !important;
}
.dark #source_modal_dialog .modal-x-btn {
    color: #94a3b8 !important;
    background-color: #0f172a !important;
    border-color: #334155 !important;
}
.dark #source_modal_dialog .modal-x-btn:hover {
    background-color: #334155 !important;
    color: #f8fafc !important;
    border-color: #64748b !important;
}
.dark #source_modal_dialog .modal-subtext,
.dark #source_modal_dialog .modal-subtext span {
    color: #94a3b8 !important;
}
.dark #source_modal_dialog .modal-hint-box {
    color: #cbd5e1 !important;
    background-color: #0f172a !important;
    border: 1px solid #334155 !important;
    border-left: 3px solid #38bdf8 !important;
}
.dark #source_modal_dialog .modal-footer-row {
    border-top-color: #334155 !important;
}
.dark #source_modal_dialog button[role="tab"] {
    color: #94a3b8 !important;
    border-color: transparent !important;
    background-color: transparent !important;
}
.dark #source_modal_dialog button[role="tab"].selected {
    color: #f8fafc !important;
    border-bottom: 2px solid #38bdf8 !important;
    background-color: transparent !important;
}
.dark #source_modal_dialog div[role="tablist"] {
    border-bottom: 1px solid #334155 !important;
}
.dark #source_modal_dialog .file-preview-holder,
.dark #source_modal_dialog [data-testid="file-upload"],
.dark #source_modal_dialog .upload-container,
.dark #source_modal_dialog div[class*="upload"] {
    background-color: #0f172a !important;
    border-color: #334155 !important;
    color: #cbd5e1 !important;
}
.dark #source_modal_dialog input,
.dark #source_modal_dialog textarea {
    background-color: #0f172a !important;
    border: 1px solid #334155 !important;
    color: #f8fafc !important;
}
.dark #source_modal_dialog input::placeholder,
.dark #source_modal_dialog textarea::placeholder {
    color: #64748b !important;
}
.dark .search-results-box {
    background-color: #0f172a !important;
    border-color: #334155 !important;
}
.dark .search-result-item {
    background-color: #1e293b !important;
    border-color: #334155 !important;
}
.dark .search-result-item:hover {
    background-color: #243044 !important;
    border-color: #38bdf8 !important;
}
.dark .source-cat-badge {
    background-color: #1e3a8a !important;
    color: #93c5fd !important;
    border-color: #2563eb !important;
}
.dark .search-res-idx {
    background-color: #334155 !important;
    color: #cbd5e1 !important;
    border-color: #475569 !important;
}
.dark .search-res-site {
    background-color: #082f49 !important;
    color: #7dd3fc !important;
    border-color: #0284c7 !important;
}
.dark .search-res-title {
    color: #f8fafc !important;
}
.dark .search-res-url,
.dark .search-res-url a {
    color: #60a5fa !important;
}
.dark .search-res-snippet {
    color: #94a3b8 !important;
}
.dark .search-added-btn {
    background-color: #064e3b !important;
    color: #6ee7b7 !important;
    border-color: #059669 !important;
}

/* 8. 附件預覽對話框 (Attachment Preview Dialog) */
.dark #att_preview_modal_dialog {
    background-color: #1e293b !important;
    border-color: #475569 !important;
    color: #f1f5f9 !important;
    box-shadow: 0 20px 50px rgba(0, 0, 0, 0.6) !important;
}
.dark #att_preview_modal_dialog h3,
.dark #att_preview_modal_dialog p,
.dark #att_preview_modal_dialog span,
.dark #att_preview_modal_dialog strong {
    color: #f8fafc !important;
}
.dark .att-modal-meta-box {
    background-color: #0f172a !important;
    border-color: #334155 !important;
    color: #cbd5e1 !important;
}
.dark .att-external-link {
    color: #60a5fa !important;
}
.dark .att-external-link:hover {
    color: #93c5fd !important;
}
.dark .att-preview-content-box {
    background-color: #0f172a !important;
    border-color: #334155 !important;
}
.dark .att-preview-body-text {
    color: #e2e8f0 !important;
}
.dark .att-preview-empty-note {
    color: #94a3b8 !important;
}

/* 9. 通用次要灰色按鈕 */
.dark .btn-neutral-gray,
.dark .btn-neutral-gray button {
    background-color: #0f172a !important;
    color: #cbd5e1 !important;
    border-color: #334155 !important;
}
.dark .btn-neutral-gray:hover,
.dark .btn-neutral-gray button:hover {
    background-color: #334155 !important;
    color: #ffffff !important;
    border-color: #64748b !important;
}

/* 10. 空狀態提示樣式 */
.empty-state-notice {
    text-align: center !important;
    color: #6b7280 !important;
    padding: 36px 10px !important;
    font-size: 13px !important;
    line-height: 1.6 !important;
}
.empty-state-sub {
    font-size: 11px !important;
    color: #9ca3af !important;
}
.dark .empty-state-notice {
    color: #cbd5e1 !important;
}
.dark .empty-state-sub {
    color: #94a3b8 !important;
}



/* ══════════════════════════════════════════════════════════════════════
   ── 系統偏好深色模式媒體查詢 (@media prefers-color-scheme: dark) ────
   ══════════════════════════════════════════════════════════════════════ */
@media (prefers-color-scheme: dark) {
    .app-header-bar { border-bottom-color: #334155 !important; }
    .app-header-title h1, .app-header-bar h1 { color: #f8fafc !important; }
    .app-clear-btn { color: #cbd5e1 !important; background-color: #1e293b !important; border-color: #334155 !important; }
    .app-clear-btn:hover { background-color: #334155 !important; color: #ffffff !important; border-color: #475569 !important; }

    .sidebar-header-row h3 { color: #f8fafc !important; }
    #src_desc { color: #94a3b8 !important; }
    .sidebar-toggle-btn { color: #cbd5e1 !important; background-color: #1e293b !important; border-color: #334155 !important; }
    .sidebar-toggle-btn:hover { background-color: #334155 !important; color: #ffffff !important; border-color: #475569 !important; }
    .sources-box-wrapper { background-color: #0f172a !important; border-color: #334155 !important; }
    .source-item-card, .source-item-row { background-color: #1e293b !important; border-color: #334155 !important; }
    .source-item-card:hover, .source-item-row:hover { background-color: #243044 !important; border-color: #475569 !important; }
    .source-item-title { color: #f1f5f9 !important; }
    .source-item-meta { color: #94a3b8 !important; }
    .source-item-prev-btn { background-color: #1e293b !important; border-color: #475569 !important; color: #cbd5e1 !important; }
    .source-item-prev-btn:hover { background-color: #334155 !important; border-color: #64748b !important; color: #f8fafc !important; }
    .source-item-del-btn { color: #94a3b8 !important; background-color: #0f172a !important; border-color: #334155 !important; }
    .source-item-del-btn:hover { background-color: #450a0a !important; color: #fca5a5 !important; border-color: #dc2626 !important; }

    .source-tag-web { color: #93c5fd !important; background-color: #1e3a8a !important; border-color: #3b82f6 !important; }
    .source-tag-doc { color: #6ee7b7 !important; background-color: #064e3b !important; border-color: #10b981 !important; }

    .source-attachments-box { background-color: #141e30 !important; border-color: #334155 !important; }
    .attachment-item-row { border-bottom-color: #1e293b !important; }
    .attachment-item-row:hover { background-color: #1e293b !important; }
    .att-ext-badge { background-color: #312e81 !important; color: #c7d2fe !important; }
    .att-name { color: #cbd5e1 !important; text-decoration-color: #64748b !important; }
    .att-name:hover { color: #38bdf8 !important; text-decoration-color: #38bdf8 !important; }
    .lpb-tooltip { background-color: #0f172a !important; border-color: #334155 !important; color: #f1f5f9 !important; box-shadow: 0 16px 36px rgba(0, 0, 0, 0.6) !important; }
    .lpb-tooltip-head { color: #f8fafc !important; }
    .lpb-tooltip-meta { color: #94a3b8 !important; }
    .lpb-tooltip-tip { color: #93c5fd !important; border-top-color: #334155 !important; }
    .att-preview-btn { background-color: #1e293b !important; color: #cbd5e1 !important; border-color: #475569 !important; }
    .att-preview-btn:hover { background-color: #334155 !important; color: #ffffff !important; border-color: #64748b !important; }
    .att-download-btn { background-color: #1e293b !important; color: #60a5fa !important; border-color: #3b82f6 !important; }
    .att-download-btn:hover { background-color: #2563eb !important; color: #ffffff !important; border-color: #2563eb !important; }
    .att-downloaded-btn, .att-downloaded-btn:hover { background-color: #0f172a !important; color: #64748b !important; border-color: #334155 !important; cursor: default !important; opacity: 0.8 !important; }

    .chat-center-tabs { background-color: transparent !important; }
    .chat-center-tabs button[role="tab"], .gradio-container button[role="tab"] { color: #94a3b8 !important; border-color: transparent !important; background-color: transparent !important; }
    .chat-center-tabs button[role="tab"].selected, .gradio-container button[role="tab"].selected { color: #f8fafc !important; border-bottom: 2px solid #38bdf8 !important; background-color: transparent !important; }
    .memory-tab-panel { background-color: #111827 !important; border-color: #334155 !important; color: #f1f5f9 !important; }
    .memory-item-title { color: #f1f5f9 !important; }
    .memory-subtext { color: #cbd5e1 !important; }
    .memory-subtext-hint { color: #94a3b8 !important; }
    .memory-box-wrapper { background-color: #0f172a !important; border-color: #334155 !important; }

    .chat-main-area { background-color: #111827 !important; border-color: #334155 !important; }
    .adaptive-chat-input-bar { background-color: #1e293b !important; border-color: #334155 !important; box-shadow: 0 -2px 12px rgba(0, 0, 0, 0.4) !important; }
    .adaptive-chat-input-bar:focus-within { border-color: #38bdf8 !important; box-shadow: 0 -2px 10px rgba(56, 189, 248, 0.2) !important; }
    .chat-input-field textarea { color: #f8fafc !important; }
    button[aria-label*="like"]::after, button[title*="like"]::after, .like-btn::after { color: #94a3b8 !important; }
    button[aria-label*="like"]:hover::after, button[title*="like"]:hover::after, .like-btn:hover::after { color: #38bdf8 !important; }

    .console-right-column h3, .console-right-column strong { color: #f8fafc !important; }
    .skills-accordion > div[data-testid="accordion-content"], .console-right-column .gr-accordion > div[data-testid="accordion-content"] { background-color: #1e293b !important; border-color: #334155 !important; box-shadow: 0 12px 28px rgba(0, 0, 0, 0.5) !important; }
    .skills-scroll-container { background-color: #0f172a !important; border-color: #334155 !important; }
    .skill-item-card { background-color: #1e293b !important; border-color: #334155 !important; border-left: 3px solid #38bdf8 !important; }
    .skill-item-name { color: #f8fafc !important; }
    .skill-fn-badge { background-color: #1e3a8a !important; color: #93c5fd !important; border-color: #3b82f6 !important; }
    .skill-file-badge { color: #94a3b8 !important; }
    .skill-item-desc { color: #cbd5e1 !important; }
    .skill-params-container { background-color: #0f172a !important; border-color: #334155 !important; }
    .skill-section-label { color: #94a3b8 !important; }
    .skill-param-row { border-bottom-color: #334155 !important; }
    .skill-param-name code { color: #93c5fd !important; }
    .skill-param-type { color: #94a3b8 !important; }
    .skill-param-opt { background-color: #334155 !important; color: #cbd5e1 !important; }
    .skill-param-desc { color: #94a3b8 !important; }
    .skill-details-summary { color: #38bdf8 !important; }
    .skill-full-desc { background-color: #0f172a !important; border-color: #334155 !important; color: #cbd5e1 !important; }
    .skill-empty-note { color: #94a3b8 !important; }
    .compact-logs-box textarea { background-color: #0f172a !important; border-color: #334155 !important; color: #cbd5e1 !important; }

    #source_modal_dialog { background-color: #1e293b !important; border-color: #475569 !important; color: #f1f5f9 !important; box-shadow: 0 20px 50px rgba(0, 0, 0, 0.6) !important; }
    #source_modal_dialog h3, #source_modal_dialog h4, #source_modal_dialog p, #source_modal_dialog span, #source_modal_dialog label, #source_modal_dialog strong { color: #f8fafc !important; }
    .modal-header-row { border-bottom-color: #334155 !important; }
    .modal-x-btn { color: #94a3b8 !important; background-color: #0f172a !important; border-color: #334155 !important; }
    .modal-x-btn:hover { background-color: #334155 !important; color: #f8fafc !important; border-color: #64748b !important; }
    .modal-subtext, .modal-subtext span { color: #94a3b8 !important; }
    .modal-hint-box { color: #cbd5e1 !important; background-color: #0f172a !important; border: 1px solid #334155 !important; border-left: 3px solid #38bdf8 !important; }
    .modal-footer-row { border-top-color: #334155 !important; }
    #source_modal_dialog button[role="tab"] { color: #94a3b8 !important; border-color: transparent !important; background-color: transparent !important; }
    #source_modal_dialog button[role="tab"].selected { color: #f8fafc !important; border-bottom: 2px solid #38bdf8 !important; background-color: transparent !important; }
    #source_modal_dialog div[role="tablist"] { border-bottom: 1px solid #334155 !important; }
    #source_modal_dialog .file-preview-holder, #source_modal_dialog [data-testid="file-upload"], #source_modal_dialog .upload-container, #source_modal_dialog div[class*="upload"] { background-color: #0f172a !important; border-color: #334155 !important; color: #cbd5e1 !important; }
    #source_modal_dialog input, #source_modal_dialog textarea { background-color: #0f172a !important; border: 1px solid #334155 !important; color: #f8fafc !important; }
    #source_modal_dialog input::placeholder, #source_modal_dialog textarea::placeholder { color: #64748b !important; }
    .search-results-box { background-color: #0f172a !important; border-color: #334155 !important; }
    .search-result-item { background-color: #1e293b !important; border-color: #334155 !important; }
    .search-result-item:hover { background-color: #243044 !important; border-color: #38bdf8 !important; }
    .search-res-title { color: #f8fafc !important; }
    .search-res-url { color: #60a5fa !important; }
    .search-res-snippet { color: #94a3b8 !important; }
    .search-added-btn { background-color: #064e3b !important; color: #6ee7b7 !important; border-color: #059669 !important; }

    button.btn-primary, button.btn-primary-orange, .btn-primary, .btn-primary-orange, button.primary { background-color: #2563eb !important; border-color: #3b82f6 !important; color: #ffffff !important; }
    button.btn-primary:hover, button.btn-primary-orange:hover, .btn-primary:hover, .btn-primary-orange:hover, button.primary:hover { background-color: #1d4ed8 !important; border-color: #2563eb !important; }

    #att_preview_modal_dialog { background-color: #1e293b !important; border-color: #475569 !important; color: #f1f5f9 !important; box-shadow: 0 20px 50px rgba(0, 0, 0, 0.6) !important; }
    #att_preview_modal_dialog h3, #att_preview_modal_dialog p, #att_preview_modal_dialog span, #att_preview_modal_dialog strong { color: #f8fafc !important; }
    .att-modal-meta-box { background-color: #0f172a !important; border-color: #334155 !important; color: #cbd5e1 !important; }
    .att-external-link { color: #60a5fa !important; }
    .att-external-link:hover { color: #93c5fd !important; }
    .att-preview-content-box { background-color: #0f172a !important; border-color: #334155 !important; }
    .att-preview-body-text { color: #e2e8f0 !important; }
    .att-preview-empty-note { color: #94a3b8 !important; }

    .btn-neutral-gray, .btn-neutral-gray button { background-color: #0f172a !important; color: #cbd5e1 !important; border-color: #334155 !important; }
    .btn-neutral-gray:hover, .btn-neutral-gray button:hover { background-color: #334155 !important; color: #ffffff !important; border-color: #64748b !important; }

    .empty-state-notice { color: #cbd5e1 !important; }
    .empty-state-sub { color: #94a3b8 !important; }
}
"""

def get_ollama_status_text():
    h = check_ollama_status()
    st = "[在線]" if h["status_code"] == "READY" else ("[缺少模型]" if h["status_code"] == "MISSING_MODEL" else "[離線]")
    return f"- **LLM**: `{h['main_model']}`\n- **Embed**: `{h['embed_model']}`\n- **Ollama**: `{st}`"

custom_js = r"""
function() {
    // ── Cookie UID 持久化 ────────────────────────────────────────────────
    function lpbSetCookie(uid) {
        const maxAge = 365 * 24 * 60 * 60; // 1 年
        document.cookie = 'lpb_uid=' + uid + '; path=/; max-age=' + maxAge + '; SameSite=Lax';
    }

    function lpbGetCookie() {
        const match = document.cookie.match(/(?:^|;\s*)lpb_uid=([^;]+)/);
        return match ? match[1] : null;
    }

    // 確保 document.cookie 存在合法的 lpb_uid
    function ensureCookieUid() {
        const cookieUid = lpbGetCookie();
        if (!cookieUid || !/^user_[a-f0-9]{8}$/.test(cookieUid)) {
            const urlParams = new URLSearchParams(window.location.search);
            let uid = urlParams.get('uid');
            if (!uid || !/^user_[a-f0-9]{8}$/.test(uid)) {
                const hex = Array.from(crypto.getRandomValues(new Uint8Array(4)))
                    .map(b => b.toString(16).padStart(2, '0')).join('');
                uid = 'user_' + hex;
            }
            lpbSetCookie(uid);
            console.log('[LearnPythonBot] 訪客 Cookie 已初始化: ' + uid);
        }
    }
    ensureCookieUid();

    // 移除可能存在的既有 Cookie 橫幅元素
    const oldBanner = document.getElementById('lpb-cookie-banner');
    if (oldBanner) {
        oldBanner.remove();
    }

    // ── 按讚/不讚按鈕樣式調整 ────────────────────────────────────────────
    const updateButtons = () => {
        const likeBtns = document.querySelectorAll('button[aria-label*="like" i], button[title*="like" i], button[aria-label*="讚"], button[title*="讚"]');
        likeBtns.forEach(btn => {
            btn.setAttribute('aria-label', 'add');
            btn.setAttribute('title', '加入記憶庫');
        });
        const dislikeBtns = document.querySelectorAll('button[aria-label*="dislike" i], button[title*="dislike" i], button[aria-label*="不讚"], button[title*="不讚"]');
        dislikeBtns.forEach(btn => {
            btn.style.setProperty('display', 'none', 'important');
        });
    };
    updateButtons();
    const observer = new MutationObserver(updateButtons);
    observer.observe(document.body, { childList: true, subtree: true });

    // ── 深色模式類別同步 (Sync Dark Mode class to <html> and <body>) ────────
    const syncDarkMode = () => {
        const isSystemDark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
        const isGradioDark = document.body.classList.contains('dark') || 
                             document.documentElement.classList.contains('dark') ||
                             document.querySelector('.gradio-container.dark') !== null;
        if (isSystemDark || isGradioDark) {
            document.documentElement.classList.add('dark');
            document.body.classList.add('dark');
        } else {
            document.documentElement.classList.remove('dark');
            document.body.classList.remove('dark');
        }
    };
    syncDarkMode();
    if (window.matchMedia) {
        window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', syncDarkMode);
    }
    // ── 全域即時懸浮卡片 (Rich Hover Tooltip for Attachments & Sources) ─────
    let globalTooltip = document.getElementById('lpb-global-tooltip');
    if (!globalTooltip) {
        globalTooltip = document.createElement('div');
        globalTooltip.id = 'lpb-global-tooltip';
        globalTooltip.className = 'lpb-tooltip';
        document.body.appendChild(globalTooltip);
    }

    const showTooltip = (el, e) => {
        let titleEl = el.closest('.source-item-title, .att-name, [data-tooltip-title]') || el;
        if (titleEl.classList.contains('bulk-del-btn')) {
            globalTooltip.innerHTML = `
                <div class="lpb-tooltip-head">
                    <span class="lpb-tooltip-tag lpb-tag-att">[工具]</span>
                    <span class="lpb-tooltip-title">刪除選取項目</span>
                </div>
                <div class="lpb-tooltip-tip">將從知識庫中永久移除您勾選的來源與其關聯內容</div>
            `;
            globalTooltip.classList.add('lpb-tooltip-show');
            positionTooltip(e);
            return;
        }

        const title = titleEl.getAttribute('data-tooltip-title') || titleEl.getAttribute('title') || titleEl.innerText.trim();
        let type = titleEl.getAttribute('data-tooltip-type') || '';
        const cat = titleEl.getAttribute('data-tooltip-cat') || '';
        const ext = titleEl.getAttribute('data-tooltip-ext') || '';
        const url = titleEl.getAttribute('data-tooltip-url') || '';
        const meta = titleEl.getAttribute('data-tooltip-meta') || '';
        if (!title) return;

        // 若元素具有原生 title，暫存至 data-original-title，避免瀏覽器遲鈍原生提示與自訂卡片重疊
        if (titleEl.hasAttribute('title')) {
            titleEl.setAttribute('data-original-title', titleEl.getAttribute('title'));
            titleEl.removeAttribute('title');
        }

        if (!type) {
            const cardEl = titleEl.closest('.source-item-card, .source-item-row');
            if (cardEl) {
                const tagEl = cardEl.querySelector('.source-tag');
                if (tagEl) {
                    if (tagEl.textContent.includes('網路')) type = 'url';
                    else if (tagEl.textContent.includes('文件')) type = 'file';
                }
            }
        }

        let tagHtml = '';
        let tipHtml = '';
        let metaHtml = '';
        const catBadge = cat ? `<span class="lpb-tooltip-tag lpb-tag-cat">[${cat}]</span>` : '';

        if (type === 'url') {
            tagHtml = `<span class="lpb-tooltip-tag lpb-tag-web">[網路]</span>${catBadge}`;
            const urlHtml = url ? `<div class="lpb-tooltip-url">${url}</div>` : '';
            metaHtml = `<div class="lpb-tooltip-meta">${urlHtml}<span>${meta}</span></div>`;
            tipHtml = '<div class="lpb-tooltip-tip">提示：點擊右側「預覽」檢視已索引內容，點擊「-」移除此來源</div>';
        } else if (type === 'file') {
            tagHtml = `<span class="lpb-tooltip-tag lpb-tag-doc">[文件]</span>${catBadge}`;
            metaHtml = `<div class="lpb-tooltip-meta"><span>${meta}</span></div>`;
            tipHtml = '<div class="lpb-tooltip-tip">提示：點擊右側「預覽」檢視已索引內容，點擊「-」移除此來源</div>';
        } else {
            // 附件檔案
            const extBadge = ext ? `<span class="lpb-tooltip-ext">${ext} 格式</span>` : '';
            tagHtml = '<span class="lpb-tooltip-tag lpb-tag-att">[附件]</span>';
            metaHtml = `<div class="lpb-tooltip-meta">${extBadge} <span>點擊右側「預覽」或「+ 下載」</span></div>`;
            tipHtml = '<div class="lpb-tooltip-tip">提示：點擊「預覽」可檢視內容，點擊「+ 下載」可自動收錄至知識庫供 AI 參考</div>';
        }

        globalTooltip.innerHTML = `
            <div class="lpb-tooltip-head">
                ${tagHtml}
                <span class="lpb-tooltip-title">${title}</span>
            </div>
            ${metaHtml}
            ${tipHtml}
        `;
        globalTooltip.classList.add('lpb-tooltip-show');
        positionTooltip(e);
    };

    const hideTooltip = (el) => {
        if (el && el.hasAttribute('data-original-title')) {
            el.setAttribute('title', el.getAttribute('data-original-title'));
            el.removeAttribute('data-original-title');
        }
        if (globalTooltip) {
            globalTooltip.classList.remove('lpb-tooltip-show');
        }
    };

    const positionTooltip = (e) => {
        if (!globalTooltip || !globalTooltip.classList.contains('lpb-tooltip-show')) return;
        const offset = 14;
        let x = e.clientX + offset;
        let y = e.clientY + offset;
        const rect = globalTooltip.getBoundingClientRect();
        if (x + rect.width > window.innerWidth - 12) {
            x = e.clientX - rect.width - 10;
        }
        if (y + rect.height > window.innerHeight - 12) {
            y = e.clientY - rect.height - 10;
        }
        globalTooltip.style.left = Math.max(10, x) + 'px';
        globalTooltip.style.top = Math.max(10, y) + 'px';
    };

    document.addEventListener('mouseover', (e) => {
        const target = e.target.closest('.att-name, .source-item-title, [data-tooltip-title], .bulk-del-btn');
        if (target) {
            showTooltip(target, e);
        }
    });

    document.addEventListener('mousemove', (e) => {
        const target = e.target.closest('.att-name, .source-item-title, [data-tooltip-title], .bulk-del-btn');
        if (target) {
            positionTooltip(e);
        }
    });

    document.addEventListener('mouseout', (e) => {
        const target = e.target.closest('.att-name, .source-item-title, [data-tooltip-title], .bulk-del-btn');
        if (target) {
            hideTooltip(target);
        }
    });
}
"""



theme = gr.themes.Default(
    primary_hue="blue",
    secondary_hue="slate",
    neutral_hue="slate",
)

with gr.Blocks(title="LearnPythonBot") as demo:
    demo.css = custom_css
    demo.js = custom_js

    # 掛載 Cookie Middleware 確保伺服器端 HTTP 回應自動附帶 lpb_uid
    try:
        from core.security import UserCookieMiddleware
        if UserCookieMiddleware:
            demo.app.add_middleware(UserCookieMiddleware)
    except Exception as e:
        print(f"[app] 掛載 UserCookieMiddleware 失敗: {e}")

    uid_state = gr.State()
    sources_state = gr.State([])
    search_results_state = gr.State([])
    sidebar_open_state = gr.State(True)
    memory_state = gr.State([])

    # ── Modal: Attachment & Source Preview Dialog ──────────────────────────
    with gr.Column(elem_id="att_preview_modal_overlay", visible=False) as att_preview_modal:
        with gr.Column(elem_id="att_preview_modal_dialog"):
            with gr.Row(elem_classes=["modal-header-row"]):
                att_preview_title = gr.Markdown("### 內容預覽", scale=9)
                close_preview_x_btn = gr.Button("X", scale=1, min_width=32, variant="secondary", elem_classes=["modal-x-btn"])

            att_preview_meta = gr.Markdown("", elem_classes=["modal-subtext"])
            att_preview_content = gr.Markdown("", elem_classes=["att-preview-content-box"])

            preview_target_state = gr.State({})

            with gr.Row(elem_classes=["modal-footer-row"]):
                close_preview_btn = gr.Button("關閉", variant="secondary", scale=1, elem_classes=["btn-neutral-gray"])
                add_from_preview_btn = gr.Button("+ 下載並加入知識庫", variant="primary", scale=2, elem_classes=["btn-primary"])


    with gr.Row(elem_classes=["app-header-bar"]):
        gr.Markdown("# LearnPythonBot", elem_classes=["app-header-title"], scale=9)
        clear = gr.ClearButton(value="清除對話", min_width=110, elem_classes=["app-clear-btn"])

    with gr.Row(equal_height=False, elem_classes=["main-workspace-row"]):

        # ── LEFT: Knowledge Source Library (scale=3) ──────────────────────
        with gr.Column(scale=3, min_width=200, elem_classes=["sources-sidebar-col"]) as sidebar_col:
            with gr.Row(elem_classes=["sidebar-header-row"]):
                gr.Markdown("### 知識來源庫", scale=7)
                sidebar_toggle_btn = gr.Button("收起", scale=3, size="sm", variant="secondary", elem_classes=["sidebar-toggle-btn"])

            with gr.Column(visible=True, elem_classes=["sidebar-body-wrap"]) as sidebar_body:
                gr.Markdown(
                    "*獨立於長期記憶，專放外部文獻供 AI 參考。*",
                    elem_id="src_desc"
                )

                open_modal_btn = gr.Button(
                    "+ 新增資料 / 關鍵字搜尋",
                    size="sm",
                    variant="primary",
                    elem_classes=["btn-primary"]
                )

                selected_srcs_state = gr.State([])
                with gr.Row(elem_classes=["bulk-action-bar"], visible=True) as bulk_action_bar:
                    selected_count_text = gr.Markdown("", elem_classes=["bulk-count-text"])
                    
                    bulk_del_btn = gr.Button("🗑️", size="sm", variant="secondary", elem_classes=["bulk-del-btn"])
                    
                    def perform_bulk_del(u, sel):
                        try:
                            from core.source_manager import delete_source
                            for sid in sel:
                                delete_source(u, sid)
                            return list_sources(u), [], gr.update()
                        except Exception as e:
                            gr.Warning("刪除失敗，請稍後再試。")
                            return list_sources(u), [], gr.update()
                    
                    bulk_del_btn.click(
                        perform_bulk_del,
                        inputs=[uid_state, selected_srcs_state],
                        outputs=[sources_state, selected_srcs_state, bulk_action_bar]
                    )

                with gr.Column(elem_classes=["sources-box-wrapper"]):
                    @gr.render(inputs=[sources_state, uid_state])
                    def render_sources_list(sources, uid):
                        if not sources:
                            with gr.Group(elem_classes=["sources-empty-box"]):
                                gr.Markdown(
                                    "<div class='empty-state-notice'>"
                                    "<b>尚無知識來源</b><br><br>"
                                    "<span class='empty-state-sub'>點擊上方「+ 新增資料 / 關鍵字搜尋」<br>可上傳文獻檔案或透過關鍵字搜尋加入</span>"
                                    "</div>"
                                )
                            return

                        with gr.Column(elem_classes=["sources-items-list"]):
                            for s in sources:
                                sid = s["source_id"]
                                tag = "[網路]" if s["type"] == "url" else "[文件]"
                                tag_cls = "source-tag-web" if s["type"] == "url" else "source-tag-doc"
                                lbl = s["label"]
                                chunk_cnt = s.get("chunk_count", 0)
                                category = s.get("category", "") or ("網路資料" if s["type"] == "url" else "文件")
                                site_name = s.get("site_name", "")
                                path_or_url = s.get("path_or_url", "")
                                discovered = s.get("discovered_files", [])

                                escaped_lbl = html.escape(lbl, quote=True)
                                escaped_url = html.escape(path_or_url, quote=True)
                                escaped_cat = html.escape(category, quote=True)
                                escaped_site = html.escape(site_name, quote=True)
                                site_meta = f" · 來源：{escaped_site}" if (escaped_site and escaped_site != escaped_lbl) else ""

                                with gr.Column(elem_classes=["source-item-card"], min_width=0):
                                    with gr.Row(elem_classes=["source-item-header"]):
                                        with gr.Column(scale=8, min_width=0, elem_classes=["source-item-content"]):
                                            gr.Markdown(
                                                f"<div class='source-item-tags-row'><span class='source-tag {tag_cls}'>{tag}</span><span class='source-cat-badge'>{escaped_cat}</span></div>"
                                                f"<div class='source-item-title' data-tooltip-title='{escaped_lbl}' data-tooltip-type='{s['type']}' data-tooltip-cat='{escaped_cat}' data-tooltip-url='{escaped_url}' data-tooltip-meta='{chunk_cnt} 個切片段落 · 類別：{escaped_cat}{site_meta}'><span class='source-item-text'>{escaped_lbl}</span></div>"
                                                f"<div class='source-item-meta'>{chunk_cnt} 段落 · 類別：{escaped_cat}{site_meta}"
                                                + (f" · [附件] {len(discovered)} 個待下載" if discovered else "")
                                                + "</div>"
                                            )
                                        with gr.Row(scale=2, elem_classes=["source-item-actions"]):
                                            src_prev_btn = gr.Button("預覽", size="sm", elem_classes=["source-item-prev-btn"])
                                            cb = gr.Checkbox(label="", value=False, show_label=False, min_width=20, container=False, elem_classes=["source-select-cb"])

                                            def make_cb_toggle(target_sid=sid):
                                                def _toggle(is_checked, sel):
                                                    if is_checked:
                                                        if target_sid not in sel: sel.append(target_sid)
                                                    else:
                                                        if target_sid in sel: sel.remove(target_sid)
                                                    count = len(sel)
                                                    text = f"**已選取 {count} 項**" if count > 0 else ""
                                                    return sel, gr.update(value=text), gr.update()
                                                return _toggle

                                            cb.change(
                                                make_cb_toggle(),
                                                inputs=[cb, selected_srcs_state],
                                                outputs=[selected_srcs_state, selected_count_text, bulk_action_bar]
                                            )

                                    def make_preview_source(target_sid=sid, target_s=s):
                                        def _prev_src(u, l):
                                            from core.source_manager import get_source
                                            full_s = get_source(u, target_sid) or target_s
                                            lbl_name = full_s.get("label", "") or "來源文獻"
                                            url_val = full_s.get("path_or_url", "")
                                            cat_val = full_s.get("category", "") or category
                                            site_val = full_s.get("site_name", "") or site_name
                                            tag_val = "[網路]" if full_s.get("type") == "url" else "[文件]"
                                            tag_class = "source-tag-web" if full_s.get("type") == "url" else "source-tag-doc"
                                            chunks = full_s.get("chunks", [])
                                            escaped_name = html.escape(lbl_name, quote=True)
                                            escaped_url_val = html.escape(url_val, quote=True)
                                            escaped_cat_val = html.escape(cat_val, quote=True)
                                            escaped_site_val = html.escape(site_val, quote=True)

                                            title_md = f"### 來源內容預覽：{escaped_name}"
                                            meta_html = (
                                                f"<div class='att-modal-meta-box'>"
                                                f"<div><span class='source-tag {tag_class}'>{tag_val}</span> <span class='source-cat-badge'>{escaped_cat_val}</span> <b>來源名稱：</b>{escaped_name}</div>"
                                            )
                                            if site_val and site_val != lbl_name:
                                                meta_html += f"<div style='margin-top: 4px;'><b>來源站點/機構：</b>{escaped_site_val}</div>"
                                            if full_s.get("type") == "url":
                                                meta_html += (
                                                    f"<div style='margin-top: 4px;'><b>來源網址：</b>"
                                                    f"<a href='{escaped_url_val}' target='_blank' rel='noopener noreferrer' class='att-external-link'>{escaped_url_val} (開啟網頁)</a></div>"
                                                )
                                            meta_html += f"<div style='margin-top: 4px; color: #94a3b8;'>已建置索引：{len(chunks)} 個文本切片 · 類別：{escaped_cat_val}</div></div>"

                                            chunk_texts = [
                                                (c.get("text", "") if isinstance(c, dict) else str(c)).strip()
                                                for c in chunks
                                                if (c.get("text", "") if isinstance(c, dict) else str(c)).strip()
                                            ]

                                            summary_val = full_s.get("summary", "")
                                            content_md = ""
                                            if summary_val:
                                                content_md += f"<div class='att-preview-body-text'><b>✨ AI 內容摘要：</b><br><br>{html.escape(summary_val).replace(chr(10), '<br>')}</div>"
                                            else:
                                                content_md += "<div class='att-preview-empty-note'>尚無 AI 摘要內容。</div>"



                                            l += f"[{get_time_str()}] [來源預覽] 查看：{lbl_name}\n"
                                            return gr.update(visible=True), title_md, meta_html, content_md, {}, gr.update(visible=False), l
                                        return _prev_src

                                    src_prev_btn.click(
                                        make_preview_source(sid, s),
                                        inputs=[uid_state, logs_box],
                                        outputs=[att_preview_modal, att_preview_title, att_preview_meta, att_preview_content, preview_target_state, add_from_preview_btn, logs_box]
                                    )

                                    # ── 附件區塊：若有發現可下載附件則顯示 ──────────────────
                                    if discovered:
                                        with gr.Column(elem_classes=["source-attachments-box"], min_width=0):
                                            from core.source_manager import is_attachment_downloaded
                                            for att in discovered[:10]:  # 最多顯示 10 個
                                                raw_att_name = (att.get("text", "") or "").strip()
                                                if not raw_att_name:
                                                    from urllib.parse import urlparse, unquote
                                                    raw_att_name = unquote(urlparse(att.get("url", "")).path.split("/")[-1]) or "附件檔案"
                                                escaped_att_name = html.escape(raw_att_name, quote=True)
                                                att_ext  = att.get("ext", "").upper().lstrip(".")
                                                att_url  = att.get("url", "")

                                                is_att_dl = att.get("downloaded", False) or is_attachment_downloaded(uid, sid, att_url, raw_att_name)

                                                with gr.Row(elem_classes=["attachment-item-row"]):
                                                    gr.Markdown(
                                                        f"<span class='att-ext-badge'>{att_ext}</span>"
                                                        f"<span class='att-name' data-tooltip-title='{escaped_att_name}' data-tooltip-ext='{att_ext}'>{escaped_att_name}</span>",
                                                        elem_classes=["att-info"]
                                                    )
                                                    prev_btn = gr.Button(
                                                        "預覽", size="sm",
                                                        elem_classes=["att-preview-btn"]
                                                    )
                                                    if is_att_dl:
                                                        dl_btn = gr.Button(
                                                            "已下載", size="sm", interactive=False,
                                                            elem_classes=["att-download-btn", "att-downloaded-btn"]
                                                        )
                                                    else:
                                                        dl_btn = gr.Button(
                                                            "+ 下載", size="sm",
                                                            elem_classes=["att-download-btn"]
                                                        )

                                                    def make_preview_att(target_sid=sid, target_att=att, is_dl=is_att_dl):
                                                        def _prev(u, l):
                                                            from core.attachment_downloader import get_attachment_preview_text
                                                            from core.source_manager import is_attachment_downloaded
                                                            url = target_att.get("url", "")
                                                            raw_name = target_att.get("text", "") or "附件檔案"
                                                            ext_name = target_att.get("ext", "").upper().lstrip(".")
                                                            escaped_name = html.escape(raw_name, quote=True)
                                                            escaped_url = html.escape(url, quote=True)

                                                            title_md = f"### 附件預覽：{escaped_name}"
                                                            meta_md = (
                                                                f"<div class='att-modal-meta-box'>"
                                                                f"<div><span class='att-ext-badge'>{ext_name}</span> <b>檔案名稱：</b>{escaped_name}</div>"
                                                                f"<div style='margin-top: 4px;'><b>來源網址：</b><a href='{escaped_url}' target='_blank' rel='noopener noreferrer' class='att-external-link'>{escaped_url} ↗ (在新分頁開啟原始檔案)</a></div>"
                                                                f"</div>"
                                                            )
                                                            preview_text = get_attachment_preview_text(url, ext_name)
                                                            if not preview_text:
                                                                content_md = (
                                                                    "<div class='att-preview-empty-note'>"
                                                                    "未能直接擷取內文純文字預覽（可能為影像掃描檔、特殊格式或需授權）。<br><br>"
                                                                    f"建議點擊上方「<a href='{escaped_url}' target='_blank' rel='noopener noreferrer' class='att-external-link'>在新分頁開啟原始檔案</a>」直接於瀏覽器中檢視，或點擊下方按鈕加入知識庫。"
                                                                    "</div>"
                                                                )
                                                            elif preview_text.startswith("注意："):
                                                                content_md = f"<div class='att-preview-empty-note'>{html.escape(preview_text)}</div>"
                                                            else:
                                                                disp = preview_text[:2000]
                                                                if len(preview_text) > 2000:
                                                                    disp += "\n\n*(...因篇幅限制，以上僅顯示前 2000 字預覽；點擊下方「+ 下載並加入知識庫」可完整收錄並向量索引)*"
                                                                escaped_disp = html.escape(disp)
                                                                content_md = f"<div class='att-preview-body-text'>{escaped_disp}</div>"

                                                            target_data = {"uid": u, "parent_sid": target_sid, "att": target_att}
                                                            l += f"[{get_time_str()}] [附件預覽] 查看：{raw_name}\n"
                                                            
                                                            already_has = target_att.get("downloaded") or is_attachment_downloaded(u, target_sid, url, raw_name)
                                                            if already_has:
                                                                btn_update = gr.update(visible=True, value="已在知識庫", interactive=False)
                                                            else:
                                                                btn_update = gr.update(visible=True, value="+ 下載並加入知識庫", interactive=True)
                                                            return gr.update(visible=True), title_md, meta_md, content_md, target_data, btn_update, l
                                                        return _prev

                                                    def make_dl_att(target_sid=sid, target_att=att):
                                                        def _dl(u, l):
                                                            yield gr.update(value="處理中...", interactive=False, elem_classes=["att-download-btn", "att-downloaded-btn"]), gr.skip(), l + f"[{get_time_str()}] [系統] 正在下載並索引附件...\n"
                                                            from core.attachment_downloader import download_and_index_attachments

                                                            results = download_and_index_attachments(
                                                                uid=u,
                                                                parent_source_id=target_sid,
                                                                files=[target_att],
                                                                max_files=1
                                                            )
                                                            new_s = list_sources(u)
                                                            for r in results:
                                                                if r["status"] == "ok":
                                                                    gr.Info(f"已下載並索引：{r['filename']}")
                                                                    l += f"[{get_time_str()}] [附件] 已下載並索引：{r['filename']}\n"
                                                                else:
                                                                    gr.Warning(f"下載失敗：{r['message']}")
                                                                    l += f"[{get_time_str()}] [附件錯誤] {r['message']}\n"
                                                            yield gr.update(value="已下載", interactive=False, elem_classes=["att-download-btn", "att-downloaded-btn"]), new_s, l
                                                        return _dl

                                                    prev_btn.click(
                                                        make_preview_att(sid, att, is_att_dl),
                                                        inputs=[uid_state, logs_box],
                                                        outputs=[att_preview_modal, att_preview_title, att_preview_meta, att_preview_content, preview_target_state, add_from_preview_btn, logs_box]
                                                    )

                                                    if not is_att_dl:
                                                        dl_btn.click(
                                                            make_dl_att(sid, att),
                                                            inputs=[uid_state, logs_box],
                                                            outputs=[dl_btn, sources_state, logs_box]
                                                        )





        # ── CENTER: Tabs for Chat UI & Memory Management (scale=5) ────────
        with gr.Column(scale=5, elem_classes=["chat-center-column"]):
            with gr.Tabs(elem_classes=["chat-center-tabs"]):
                with gr.Tab("對話區", id="tab_chat"):
                    chatbot = gr.Chatbot(value=[], height=500, elem_classes=["chat-main-area"])
                    with gr.Row(elem_classes=["adaptive-chat-input-bar"]):
                        msg = gr.Textbox(
                            placeholder="輸入訊息... (Shift+Enter 換行，Enter 送出)",
                            show_label=False,
                            scale=9,
                            lines=1,
                            max_lines=7,
                            container=False,
                            elem_classes=["chat-input-field"]
                        )
                        submit_btn = gr.Button(value="送出", scale=1, min_width=72, elem_classes=["chat-input-submit-btn"])

                with gr.Tab("記憶管理區", id="tab_memory"):
                    with gr.Column(elem_classes=["memory-tab-panel"]):
                        gr.Markdown("### 長期記憶庫 (Long-term Memory)")
                        gr.Markdown(
                            "<div class='memory-subtext'>"
                            "在此檢視與管理 AI 對話中沉澱的重要個人筆記或偏好。<br>"
                            "<span class='memory-subtext-hint'>在對話區中點擊訊息下方的「+」按鈕即可沉澱至此處，或在右側點擊「-」移除記憶。</span>"
                            "</div>"
                        )
                        with gr.Column(elem_classes=["memory-box-wrapper"]):
                            @gr.render(inputs=[memory_state, uid_state])
                            def render_memory_list(memories, uid):
                                if not memories:
                                    with gr.Group(elem_classes=["sources-empty-box"]):
                                        gr.Markdown(
                                            "<div class='empty-state-notice'>"
                                            "<b>尚無長期記憶</b><br><br>"
                                            "<span class='empty-state-sub'>在對話區中點擊任何訊息下方的 [+] 按鈕<br>即可將對話重點加入記憶庫</span>"
                                            "</div>"
                                        )
                                    return

                                with gr.Column(elem_classes=["sources-items-list"]):
                                    for idx, m in enumerate(memories):
                                        mid = m.get("id", str(idx))
                                        content = m.get("content", "")
                                        escaped_content = html.escape(content, quote=True)
                                        with gr.Row(elem_classes=["source-item-row"]):
                                            with gr.Column(scale=9, min_width=0, elem_classes=["source-item-content"]):
                                                gr.Markdown(
                                                    f"<div class='memory-item-title' title='{escaped_content}'><span class='source-tag source-tag-doc'>[記憶 {idx+1}]</span><span class='source-item-text'>{escaped_content}</span></div>"
                                                )
                                            del_mem_btn = gr.Button("-", scale=1, min_width=20, size="sm", elem_classes=["source-item-del-btn"])

                                            def make_del_mem(target_id=mid):
                                                def _del_m(u, l):
                                                    from core.memory_manager import delete_active_memory, get_active_memory
                                                    delete_active_memory(u, target_id)
                                                    new_m = get_active_memory(u)
                                                    l += f"[{get_time_str()}] [記憶庫] 已刪除記憶項目：{target_id}\n"
                                                    return new_m, l
                                                return _del_m

                                            del_mem_btn.click(
                                                make_del_mem(mid),
                                                inputs=[uid_state, logs_box],
                                                outputs=[memory_state, logs_box]
                                            )

            clear.add([msg, chatbot])

        # ── RIGHT: Developer Console (scale=2) ────────────────────────────
        with gr.Column(scale=2, min_width=180, elem_classes=["console-right-column"]):
            gr.Markdown("### 開發者控制台")
            ollama_status_md = gr.Markdown(value=get_ollama_status_text())
            with gr.Accordion(label="AI 技能清單 (Skills)", open=False, elem_classes=["skills-accordion"]):
                gr.HTML(value=get_skills_html())
            gr.Markdown("**即時日誌**")
            logs_box = gr.Textbox(label="", show_label=False, lines=14, max_lines=26, autoscroll=True, interactive=False, elem_classes=["compact-logs-box"])

    # ── Modal: Integrated Add Source Dialog ───────────────────────────────
    with gr.Column(elem_id="source_modal_overlay", visible=False) as source_modal:
        with gr.Column(elem_id="source_modal_dialog"):
            with gr.Row(elem_classes=["modal-header-row"]):
                gr.Markdown("### 新增知識來源", scale=9)
                close_modal_x_btn = gr.Button("X", scale=1, min_width=32, variant="secondary", elem_classes=["modal-x-btn"])

            gr.Markdown(
                "*可上傳文獻檔案或透過關鍵字搜尋網路資料，將內容加入 AI 參考知識庫（供對話時檢索）：*",
                elem_classes=["modal-subtext"]
            )

            with gr.Tabs():
                with gr.Tab("檔案上傳"):
                    gr.Markdown(
                        "<div class='modal-hint-box'>"
                        "支援格式：PDF、TXT、CSV、DOCX、XLSX、XLS、JSON（單檔上限 10MB）"
                        "</div>"
                    )
                    modal_file = gr.File(
                        label="選擇或拖放檔案至此",
                        file_types=[".pdf", ".txt", ".csv", ".docx", ".xlsx", ".xls", ".json"],
                        file_count="single",
                        type="filepath"
                    )
                    modal_file_btn = gr.Button("確認加入檔案", variant="primary", elem_classes=["btn-primary"])

                with gr.Tab("關鍵字搜尋"):
                    gr.Markdown(
                        "<div class='modal-hint-box'>"
                        "輸入搜尋關鍵字（例如：台灣國慶、跟騷法、慈濟大學交通車、Python教學），系統將即時檢索並跳出最相關的 10 筆資料供您挑選加入。亦支援直接貼上特定網址。"
                        "</div>"
                    )
                    with gr.Row():
                        search_kw_input = gr.Textbox(
                            placeholder="輸入搜尋關鍵字（例如：台灣國慶、慈濟大學交通車、跟騷法...）或直接貼上網址",
                            show_label=False,
                            scale=8,
                            lines=1,
                            max_lines=1
                        )
                        search_kw_btn = gr.Button("搜尋最相關 10 筆", variant="primary", scale=2, elem_classes=["btn-primary"])

                    with gr.Column(elem_classes=["search-results-box"]):
                        @gr.render(inputs=[search_results_state, uid_state, sources_state])
                        def render_search_results(results, uid, sources):
                            if not results:
                                gr.Markdown(
                                    "<div class='empty-state-notice'>"
                                    "<b>尚未進行搜尋</b><br><br>"
                                    "<span class='empty-state-sub'>請在上方輸入關鍵字並點擊「搜尋最相關 10 筆」，系統將列出最相關的 10 筆資料供您挑選加入知識庫。</span>"
                                    "</div>"
                                )
                                return

                            from tools.web_search import canonicalize_url, canonical_title_key
                            added_urls = {canonicalize_url(s.get("path_or_url", "")) for s in (sources or []) if s.get("path_or_url")}
                            added_titles = {canonical_title_key(s.get("label", "")) for s in (sources or []) if s.get("label")}
                            gr.Markdown(f"**已檢索出最相關的 {len(results)} 筆資料，點擊「+ 加入知識庫」即可收錄：**")
                            with gr.Column(elem_classes=["search-results-list"]):
                                for i, r in enumerate(results):
                                    can_url = canonicalize_url(r.get("href", ""))
                                    can_title = canonical_title_key(r.get("title", ""))
                                    is_added = can_url in added_urls or (can_title and len(can_title) >= 4 and can_title in added_titles)
                                    from core.source_manager import normalize_source_meta
                                    meta = normalize_source_meta(
                                        r.get("href", ""),
                                        raw_title=r.get("title", ""),
                                        raw_snippet=r.get("body", "")
                                    )
                                    cat = meta["category"]
                                    site = meta["site_name"]
                                    clean_title = meta["clean_title"]
                                    clean_snip = meta["clean_snippet"]
                                    url_val = r.get("href", "")

                                    escaped_title = html.escape(clean_title, quote=True)
                                    escaped_url = html.escape(url_val, quote=True)
                                    escaped_site = html.escape(site, quote=True)
                                    escaped_snip = html.escape(clean_snip, quote=True)
                                    escaped_cat = html.escape(cat, quote=True)
                                    site_badge = f"<span class='search-res-site'>{escaped_site}</span>" if (escaped_site and escaped_site != escaped_title) else ""

                                    with gr.Row(elem_classes=["search-result-item"]):
                                        with gr.Column(scale=8, min_width=0, elem_classes=["search-result-info"]):
                                            gr.Markdown(
                                                f"<div class='search-res-header'>"
                                                f"<span class='search-res-idx'>#{i+1}</span>"
                                                f"<span class='source-cat-badge'>{escaped_cat}</span>"
                                                f"{site_badge}"
                                                f"<span class='search-res-title' data-tooltip-title='{escaped_title}' data-tooltip-type='url' data-tooltip-cat='{escaped_cat}' data-tooltip-url='{escaped_url}'>{escaped_title}</span>"
                                                f"</div>"
                                                f"<div class='search-res-url'><a href='{escaped_url}' target='_blank' rel='noopener noreferrer'>{escaped_url}</a></div>"
                                                f"<div class='search-res-snippet' title='{escaped_snip}'>{escaped_snip}</div>"
                                            )
                                        if is_added:
                                            gr.Button("已在知識庫", scale=2, size="sm", interactive=False, elem_classes=["search-added-btn"])
                                        else:
                                            add_btn = gr.Button("+ 加入知識庫", scale=2, size="sm", variant="primary", elem_classes=["search-add-btn"])

                                            def make_add(target_r=r, target_cat=cat, target_title=clean_title, target_snip=clean_snip):
                                                def _add(u, l):
                                                    from core.source_manager import add_source
                                                    res = add_source(u, target_r["href"], label=target_title, category=target_cat, snippet=target_snip)
                                                    new_s = list_sources(u)
                                                    if res.get("ok"):
                                                        gr.Info(f"已成功加入：[{target_cat}] {target_title}")
                                                        l += f"[{get_time_str()}] [來源庫] 已加入：[{target_cat}] {target_title} ({target_r['href']})\n"
                                                    else:
                                                        gr.Warning(f"加入失敗：{res.get('message')}")
                                                        l += f"[{get_time_str()}] [來源庫錯誤] {res.get('message')}\n"
                                                    return new_s, l
                                                return _add

                                            add_btn.click(
                                                make_add(r, cat, clean_title, clean_snip),
                                                inputs=[uid_state, logs_box],
                                                outputs=[sources_state, logs_box]
                                            )

            with gr.Row(elem_classes=["modal-footer-row"]):
                close_modal_btn = gr.Button("關閉", variant="secondary", scale=1, elem_classes=["btn-neutral-gray"])

    # ── Event wiring ──────────────────────────────────────────────────────
    def init_and_render(request: gr.Request):
        uid, log_txt = init_session(request)
        sources = list_sources(uid)
        from core.memory_manager import get_active_memory
        memories = get_active_memory(uid)
        h = check_ollama_status()
        t = get_time_str()
        if h["status_code"] == "READY":
            log_txt += f"[{t}] [系統連線] Ollama 服務正常 (主模型: {h['main_model']}, 向量: {h['embed_model']})\n"
        elif h["status_code"] == "MISSING_MODEL":
            log_txt += f"[{t}] [系統警告] Ollama 缺少模型！{h['summary']}\n"
        else:
            log_txt += f"[{t}] [系統警告] 未能連線至本地 Ollama 服務，請先啟動服務！\n"

        if sources:
            log_txt += f"[{t}] [知識庫] 已載入 {len(sources)} 筆個人知識來源\n"
        if memories:
            log_txt += f"[{t}] [記憶庫] 已載入 {len(memories)} 條使用者長期記憶\n"

        status_txt = get_ollama_status_text()
        return uid, log_txt, sources, memories, status_txt

    demo.load(fn=init_and_render, inputs=None, outputs=[uid_state, logs_box, sources_state, memory_state, ollama_status_md])


    # Chat events
    msg.submit(respond, inputs=[msg, chatbot, logs_box, uid_state], outputs=[msg, chatbot, logs_box])
    submit_btn.click(respond, inputs=[msg, chatbot, logs_box, uid_state], outputs=[msg, chatbot, logs_box])
    chatbot.like(handle_like, inputs=[uid_state, logs_box], outputs=[logs_box, memory_state])

    # Collapsible sidebar toggle event
    def toggle_sidebar(is_open):
        new_state = not is_open
        btn_txt = "收起" if new_state else "展開"
        return new_state, gr.update(visible=new_state), btn_txt

    sidebar_toggle_btn.click(
        toggle_sidebar,
        inputs=[sidebar_open_state],
        outputs=[sidebar_open_state, sidebar_body, sidebar_toggle_btn]
    )

    # Modal open & close events
    open_modal_btn.click(
        lambda: gr.update(visible=True),
        outputs=source_modal,
        js="function() { setTimeout(() => { const dlg = document.querySelector('#source_modal_dialog'); if (dlg) dlg.scrollTop = 0; }, 50); }"
    )
    close_modal_x_btn.click(lambda: gr.update(visible=False), outputs=source_modal)
    close_modal_btn.click(lambda: gr.update(visible=False), outputs=source_modal)

    # Modal file events: both upload and confirm button
    modal_file.upload(
        add_file_source_modal,
        inputs=[modal_file, uid_state, logs_box],
        outputs=[sources_state, logs_box, source_modal, modal_file]
    )
    modal_file_btn.click(
        add_file_source_modal,
        inputs=[modal_file, uid_state, logs_box],
        outputs=[sources_state, logs_box, source_modal, modal_file]
    )

    # Modal keyword search events
    search_last_kw_state = gr.State("")
    search_kw_btn.click(
        handle_keyword_search,
        inputs=[search_kw_input, search_last_kw_state, uid_state, logs_box],
        outputs=[search_results_state, logs_box, sources_state, search_last_kw_state]
    )
    search_kw_input.submit(
        handle_keyword_search,
        inputs=[search_kw_input, search_last_kw_state, uid_state, logs_box],
        outputs=[search_results_state, logs_box, sources_state, search_last_kw_state]
    )

    # ── Attachment preview modal events ───────────────────────────────────
    close_preview_x_btn.click(lambda: gr.update(visible=False), outputs=att_preview_modal)
    close_preview_btn.click(lambda: gr.update(visible=False), outputs=att_preview_modal)

    def _add_from_preview(target_data, u, l):
        if not target_data or not target_data.get("att"):
            yield gr.update(visible=False), list_sources(u), l, gr.update()
            return
            
        yield gr.skip(), gr.skip(), l + f"[{get_time_str()}] [系統] 正在下載並索引附件...\n", gr.update(value="處理中...", interactive=False)
        target_sid = target_data.get("parent_sid")
        target_att = target_data.get("att")
        from core.attachment_downloader import download_and_index_attachments
        results = download_and_index_attachments(
            uid=u,
            parent_source_id=target_sid,
            files=[target_att],
            max_files=1
        )
        new_s = list_sources(u)
        for r in results:
            if r["status"] == "ok":
                gr.Info(f"已下載並索引：{r['filename']}")
                l += f"[{get_time_str()}] [附件] 已下載並索引：{r['filename']}\n"
            else:
                gr.Warning(f"下載失敗：{r['message']}")
                l += f"[{get_time_str()}] [附件錯誤] {r['message']}\n"
        yield gr.update(visible=False), new_s, l, gr.update(value="+ 下載並加入知識庫", interactive=True)

    add_from_preview_btn.click(
        _add_from_preview,
        inputs=[preview_target_state, uid_state, logs_box],
        outputs=[att_preview_modal, sources_state, logs_box, add_from_preview_btn]
    )

if __name__ == "__main__":
    import threading
    is_reload_thread = False
    try:
        from gradio.cli.commands.reload import reload_thread
        is_reload_thread = getattr(reload_thread, "running_reload", False)
    except Exception:
        pass

    if threading.current_thread() is threading.main_thread() and not is_reload_thread:
        if not os.environ.get("_LPB_HEALTH_PRINTED"):
            os.environ["_LPB_HEALTH_PRINTED"] = "1"
            health = check_ollama_status()
            print(health["terminal_message"])

    demo.launch(server_name="127.0.0.1", share=False, theme=theme, css=custom_css, js=custom_js)



