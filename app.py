import os
import json
import uuid
import html
from datetime import datetime, timezone
import time
import gradio as gr
from core.bot_engine import BotEngine  # will be implemented later
from core.health_check import check_ollama_status

# ---------------------------------------------------------------------------
# Rate limiting for session creation
# ---------------------------------------------------------------------------
IP_CREATION_LOGS = {}
BLOCK_EXPIRY = {}

def check_rate_limit(request: gr.Request):
    ip = request.client.host if request and request.client else "unknown"
    now = time.time()
    
    # Check if IP is blocked
    if ip in BLOCK_EXPIRY:
        if now < BLOCK_EXPIRY[ip]:
            raise gr.Error("429 Too Many Requests: 您的連線頻率過高，暫時被封鎖。")
        else:
            del BLOCK_EXPIRY[ip]

    # Clean old logs (older than 1 second)
    if ip not in IP_CREATION_LOGS:
        IP_CREATION_LOGS[ip] = []
    
    IP_CREATION_LOGS[ip] = [t for t in IP_CREATION_LOGS[ip] if now - t <= 1.0]
    
    if len(IP_CREATION_LOGS[ip]) >= 10:
        BLOCK_EXPIRY[ip] = now + 60.0  # Block for 60 seconds
        raise gr.Error("429 Too Many Requests: 您的連線頻率過高，暫時被封鎖。")
        
    IP_CREATION_LOGS[ip].append(now)

# ---------------------------------------------------------------------------
# Helper: Initialise or load a user session based on URL query parameter `uid`
# ---------------------------------------------------------------------------
def init_session(request: gr.Request):
    """Return a unique user id and initial log."""
    check_rate_limit(request)
        
    uid = request.query_params.get("uid")
    if not uid:
        uid = f"user_{uuid.uuid4().hex[:8]}"
        # after creating the file we let the frontend reload with the new uid
        # by returning the uid – Gradio will later expose it via a hidden State.
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
        with open(user_path, "w", encoding="utf-8") as f:
            json.dump(init_data, f, ensure_ascii=False, indent=2)
            
    return uid, f"[{get_time_str()}] [系統] 伺服器啟動，等待操作...\n"

# ---------------------------------------------------------------------------
# Core response handling (streaming) – placeholder, real logic lives in BotEngine
# ---------------------------------------------------------------------------
bot = BotEngine()  # BotEngine must expose generate_response_stream

def get_time_str():
    return datetime.now().strftime("%H:%M:%S")

from core.security import check_suspicious

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
        
    with open(user_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    
    data["chat_log"].append({"role": "user", "content": message})
    data["last_active_time"] = datetime.now(timezone.utc).isoformat()
    logs += f"[{get_time_str()}] [User] {message}\n"
    
    # We yield the pristine data["chat_log"] directly to Gradio UI
    yield "", data["chat_log"], logs

    # Perform security check before querying AI
    is_suspicious = check_suspicious(message, uid)
    if is_suspicious:
        bot_response = "很抱歉，您的輸入似乎違反了系統規範，或者與學習 Python 無關。我無法為您解答這個問題。"
        data["chat_log"].append({"role": "assistant", "content": bot_response})
        with open(user_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        logs += f"[{get_time_str()}] [System] 已攔截可疑輸入並記錄至背景。\n"
        yield "", data["chat_log"], logs
        return

    # Streaming from bot
    data["chat_log"].append({"role": "assistant", "content": ""})
    bot_response = ""
    # pass data["chat_log"] up to the last user message
    for log_update, is_final, text in bot.generate_response_stream(data["chat_log"][:-1], uid):
        if log_update:
            logs += f"{log_update}\n"
            
        if text:
            data["chat_log"][-1]["content"] = text
            bot_response = text
            
        yield "", data["chat_log"], logs

    with open(user_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    yield "", data["chat_log"], logs

# ---------------------------------------------------------------------------
# File upload handling – respects whitelist & size limit from built_in_db.json
# ---------------------------------------------------------------------------
with open(os.path.join("database", "built_in_db.json"), "r", encoding="utf-8") as f:
    built_in_cfg = json.load(f)

WHITELIST = built_in_cfg.get("upload_whitelist", [".pdf", ".txt", ".csv", ".docx", ".json"])
MAX_SIZE_MB = built_in_cfg.get("upload_max_size_mb", 10)

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
    except Exception as e:
        pass

    # Save under user specific folder
    uploads_dir = os.path.join("database", "users", uid, "uploads")
    os.makedirs(uploads_dir, exist_ok=True)
    dest_path = os.path.join(uploads_dir, filename)
    with open(src_path, "rb") as s, open(dest_path, "wb") as d:
        d.write(s.read())

    # Record in user profile
    user_path = os.path.join("database", "users", uid, "profile.json")
    if os.path.exists(user_path):
        try:
            with open(user_path, "r", encoding="utf-8") as f:
                udata = json.load(f)
            if dest_path not in udata.get("uploaded_files", []):
                udata.setdefault("uploaded_files", []).append(dest_path)
            udata["last_active_time"] = datetime.now(timezone.utc).isoformat()
            with open(user_path, "w", encoding="utf-8") as f:
                json.dump(udata, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    logs += f"[{get_time_str()}] [來源庫] 正在索引檔案：{filename}\n"

    try:
        result = add_source(uid, dest_path, label=filename)
        logs += f"[{get_time_str()}] [來源庫] {result['message']}\n"
        if result.get("ok"):
            gr.Info(f"已成功加入檔案：{filename}")
        else:
            gr.Warning(result.get("message", "檔案索引失敗"))
    except Exception as e:
        logs += f"[{get_time_str()}] [來源庫錯誤] {e}\n"
        gr.Warning(f"檔案處理失敗：{e}")

    new_sources = list_sources(uid)
    # Do NOT auto exit modal! Keep it open and clear file input
    return new_sources, logs, gr.update(visible=True), None


def handle_keyword_search(kw: str, uid: str, logs: str):
    """Search web for keywords or add direct URL, returning top 10 results."""
    q = (kw or "").strip()
    if not q:
        gr.Warning("請輸入搜尋關鍵字或網址。")
        return [], logs, list_sources(uid)

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
        return [], logs, new_s

    logs += f"[{get_time_str()}] [搜尋] 正在檢索關鍵字：{q}...\n"
    from tools.web_search import search_web
    results = search_web(q, max_results=10)
    if not results:
        gr.Warning("未找到相關網頁，請嘗試更換關鍵字。")
        logs += f"[{get_time_str()}] [搜尋] 關鍵字「{q}」未找到相關結果。\n"
    else:
        logs += f"[{get_time_str()}] [搜尋] 找到 {len(results)} 筆最相關結果。\n"
    return results, logs, list_sources(uid)


def get_skills_html():
    """Generates a clean, intuitive scrollable list of skills currently held by the AI directly from tools/."""
    from tools import get_skills_display_info
    skills = get_skills_display_info()
    cards = []
    for s in (skills or []):
        name = s.get("name", "skill")
        desc = s.get("desc", "")
        cards.append(
            f'<div class="skill-item-card">'
            f'<div class="skill-item-name">{name}</div>'
            f'<div class="skill-item-desc">{desc}</div>'
            f'</div>'
        )
    if not cards:
        cards.append('<div style="color: #888; font-size: 11px; padding: 6px;">尚無可用技能</div>')
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

/* ── Primary Orange Action Buttons (Auxiliary Color 1: 暖橘色) ── */
.btn-primary-orange,
.btn-primary-orange button,
button.primary {
    background-color: #ea580c !important;
    color: #ffffff !important;
    border: 1px solid #ea580c !important;
    font-weight: 600 !important;
    transition: background-color 0.15s ease, border-color 0.15s ease !important;
}

.btn-primary-orange:hover,
.btn-primary-orange button:hover,
button.primary:hover {
    background-color: #c2410c !important;
    border-color: #c2410c !important;
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

/* ── Sources Library List Container Box (淺色柔和邊框) ── */
.sources-box-wrapper {
    border: 1px solid #e5e7eb !important;
    background-color: #fafbfc !important;
    padding: 8px !important;
    min-height: 380px !important;
    max-height: 520px !important;
    overflow-y: auto !important;
}

.sources-empty-box {
    display: flex !important;
    flex-direction: column !important;
    align-items: center !important;
    justify-content: center !important;
    min-height: 240px !important;
}

.sources-items-list {
    display: flex !important;
    flex-direction: column !important;
    gap: 6px !important;
    width: 100% !important;
}

.source-item-row {
    background-color: #ffffff !important;
    border: 1px solid #e5e7eb !important;
    padding: 7px 10px !important;
    display: flex !important;
    flex-direction: row !important;
    flex-wrap: nowrap !important; /* 絕對不換行，維持單行 */
    align-items: center !important;
    justify-content: space-between !important;
    gap: 8px !important;
    margin-bottom: 4px !important;
    width: 100% !important;
    box-sizing: border-box !important;
    transition: border-color 0.15s ease, background-color 0.15s ease !important;
}

.source-item-row:hover {
    border-color: #fed7aa !important; /* 柔和暖橘邊框提示 */
    background-color: #fffbf7 !important;
}

.source-item-content {
    flex: 1 1 0% !important;
    min-width: 0 !important; /* 關鍵：覆蓋 flex item 的 min-width: auto，使文字截斷生效 */
    width: 0 !important;
    overflow: hidden !important;
}

/* ── Auxiliary Colors: Blue for Web, Green for Doc ── */
.source-tag {
    font-size: 11px !important;
    font-weight: 700 !important;
    padding: 1px 5px !important;
    margin-right: 5px !important;
    display: inline-block !important;
    flex-shrink: 0 !important;
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
    font-size: 13px !important;
    font-weight: 600 !important;
    color: #1f2937 !important;
    white-space: nowrap !important;
    overflow: hidden !important;
    text-overflow: ellipsis !important;
    line-height: 1.3 !important;
    display: flex !important;
    align-items: center !important;
    width: 100% !important;
    cursor: pointer !important;
}

.source-item-text {
    white-space: nowrap !important;
    overflow: hidden !important;
    text-overflow: ellipsis !important;
    flex: 1 1 auto !important;
    min-width: 0 !important;
}

.source-item-meta {
    font-size: 11px !important;
    color: #6b7280 !important;
    margin-top: 3px !important;
    white-space: nowrap !important;
    overflow: hidden !important;
    text-overflow: ellipsis !important;
}

.source-item-del-btn {
    flex: 0 0 24px !important; /* 固定寬度 24px，永不縮減或被擠壓換行 */
    width: 24px !important;
    min-width: 24px !important;
    max-width: 24px !important;
    height: 24px !important;
    min-height: 24px !important;
    max-height: 24px !important;
    padding: 0 !important;
    font-size: 16px !important;
    font-weight: bold !important;
    line-height: 1 !important;
    color: #9ca3af !important;
    background-color: #f9fafb !important;
    border: 1px solid #e5e7eb !important;
    display: flex !important;
    align-items: center !important;
    justify-content: center !important;
    cursor: pointer !important;
    margin-left: auto !important; /* 靠右對齊維持單行 */
    transition: all 0.15s ease !important;
}

.source-item-del-btn:hover {
    background-color: #fee2e2 !important;
    color: #ef4444 !important;
    border-color: #fca5a5 !important;
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
    border-color: #ea580c !important; /* 焦點暖橘色光暈 */
    box-shadow: 0 -3px 12px rgba(234, 88, 12, 0.12) !important;
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
    background-color: #ea580c !important; /* 暖橘色送出按鈕在右側 */
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
    background-color: #c2410c !important;
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
    border: 1px solid #d1d5db !important; /* 淺灰色外框 */
    border-radius: 0px !important;
    width: 720px !important;
    max-width: 96vw !important;
    max-height: 90vh !important;
    overflow-y: auto !important;
    padding: 24px !important;
    box-shadow: 0 16px 40px rgba(0, 0, 0, 0.12) !important;
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
    color: #9a3412 !important;
    background-color: #fff7ed !important;
    border-left: 3px solid #ea580c !important; /* 暖橘指示邊線 */
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
    max-height: 380px !important;
    overflow-y: auto !important;
    margin-top: 10px !important;
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
    align-items: center !important;
    justify-content: space-between !important;
    gap: 12px !important;
    transition: border-color 0.15s ease !important;
}

.search-result-item:hover {
    border-color: #fed7aa !important;
}

.search-result-info {
    flex: 1 1 auto !important;
    overflow: hidden !important;
}

.search-res-title {
    font-size: 13px !important;
    font-weight: 700 !important;
    color: #111827 !important;
    margin-bottom: 2px !important;
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
    color: #6b7280 !important;
    line-height: 1.4 !important;
}

.search-add-btn {
    white-space: nowrap !important;
    font-size: 12px !important;
    font-weight: bold !important;
    min-width: 95px !important;
    height: 32px !important;
    background-color: #ea580c !important;
    color: #ffffff !important;
    border: 1px solid #ea580c !important;
}

.search-add-btn:hover {
    background-color: #c2410c !important;
    border-color: #c2410c !important;
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
    color: #ea580c !important;
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
    height: 100% !important;
    min-height: 520px !important;
    max-height: 535px !important;
    overflow-y: auto !important;
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
"""

def get_ollama_status_text():
    h = check_ollama_status()
    st = "[在線]" if h["status_code"] == "READY" else ("[缺少模型]" if h["status_code"] == "MISSING_MODEL" else "[離線]")
    return f"- **LLM**: `{h['main_model']}`\n- **Embed**: `{h['embed_model']}`\n- **Ollama**: `{st}`"

custom_js = """
function() {
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
}
"""

with gr.Blocks(title="LearnPythonBot") as demo:
    uid_state = gr.State()
    sources_state = gr.State([])
    search_results_state = gr.State([])
    sidebar_open_state = gr.State(True)
    memory_state = gr.State([])

    with gr.Row(elem_classes=["app-header-bar"]):
        gr.Markdown("# LearnPythonBot", elem_classes=["app-header-title"], scale=9)
        clear = gr.ClearButton(value="清除對話", min_width=110, elem_classes=["app-clear-btn"])

    with gr.Row(equal_height=False, elem_classes=["main-workspace-row"]):

        # ── LEFT: Knowledge Source Library (scale=2) ──────────────────────
        with gr.Column(scale=2, min_width=200, elem_classes=["sources-sidebar-col"]) as sidebar_col:
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
                    elem_classes=["btn-primary-orange"]
                )

                with gr.Column(elem_classes=["sources-box-wrapper"]):
                    @gr.render(inputs=[sources_state, uid_state])
                    def render_sources_list(sources, uid):
                        if not sources:
                            with gr.Group(elem_classes=["sources-empty-box"]):
                                gr.Markdown(
                                    "<div style='text-align: center; color: #888; padding: 32px 10px; font-size: 13px; line-height: 1.6;'>"
                                    "<b>尚無知識來源</b><br><br>"
                                    "<span style='font-size: 11px; color: #999;'>點擊上方「+ 新增資料 / 關鍵字搜尋」<br>可上傳文獻檔案或透過關鍵字搜尋加入</span>"
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
                                stype = "網路資料" if s["type"] == "url" else "文件"

                                escaped_lbl = html.escape(lbl, quote=True)
                                with gr.Row(elem_classes=["source-item-row"]):
                                    with gr.Column(scale=9, min_width=0, elem_classes=["source-item-content"]):
                                        gr.Markdown(
                                            f"<div class='source-item-title' title='{escaped_lbl}'><span class='source-tag {tag_cls}'>{tag}</span><span class='source-item-text'>{escaped_lbl}</span></div>"
                                            f"<div class='source-item-meta' title='{escaped_lbl}'>{chunk_cnt} 段落 · {stype}</div>"
                                        )
                                    del_btn = gr.Button("-", scale=1, min_width=24, elem_classes=["source-item-del-btn"])

                                    def make_del(target_id=sid):
                                        def _del(u, l):
                                            remove_source(u, target_id)
                                            new_s = list_sources(u)
                                            l += f"[{get_time_str()}] [來源庫] 已移除來源：{target_id}\n"
                                            return new_s, l
                                        return _del

                                    del_btn.click(
                                        make_del(sid),
                                        inputs=[uid_state, logs_box],
                                        outputs=[sources_state, logs_box]
                                    )

        # ── CENTER: Tabs for Chat UI & Memory Management (scale=6) ────────
        with gr.Column(scale=6, elem_classes=["chat-center-column"]):
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
                            "<span style='font-size: 12px; color: #6b7280;'>在對話區中點擊訊息下方的「+」按鈕即可沉澱至此處，或在右側點擊「-」移除記憶。</span>"
                            "</div>"
                        )
                        with gr.Column(elem_classes=["memory-box-wrapper"]):
                            @gr.render(inputs=[memory_state, uid_state])
                            def render_memory_list(memories, uid):
                                if not memories:
                                    with gr.Group(elem_classes=["sources-empty-box"]):
                                        gr.Markdown(
                                            "<div style='text-align: center; color: #888; padding: 48px 10px; font-size: 13px; line-height: 1.6;'>"
                                            "<b>尚無長期記憶</b><br><br>"
                                            "<span style='font-size: 11px; color: #999;'>在對話區中點擊任何訊息下方的 [+] 按鈕<br>即可將對話重點加入記憶庫</span>"
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
                                                    f"<div class='source-item-title' title='{escaped_content}'><span class='source-tag source-tag-doc'>[記憶 {idx+1}]</span><span class='source-item-text'>{escaped_content}</span></div>"
                                                )
                                            del_mem_btn = gr.Button("-", scale=1, min_width=24, elem_classes=["source-item-del-btn"])

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
                        "支援格式：PDF、TXT、CSV、DOCX、JSON（單檔上限 10MB）"
                        "</div>"
                    )
                    modal_file = gr.File(
                        label="選擇或拖放檔案至此",
                        file_types=[".pdf", ".txt", ".csv", ".docx", ".json"],
                        file_count="single",
                        type="filepath"
                    )
                    modal_file_btn = gr.Button("確認加入檔案", elem_classes=["btn-primary-orange"])

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
                            lines=1
                        )
                        search_kw_btn = gr.Button("搜尋最相關 10 筆", scale=2, elem_classes=["btn-primary-orange"])

                    with gr.Column(elem_classes=["search-results-box"]):
                        @gr.render(inputs=[search_results_state, uid_state, sources_state])
                        def render_search_results(results, uid, sources):
                            if not results:
                                gr.Markdown(
                                    "<div style='text-align: center; color: #888; padding: 28px 10px; font-size: 13px; line-height: 1.6;'>"
                                    "<b>尚未進行搜尋</b><br><br>"
                                    "<span style='font-size: 11px; color: #999;'>請在上方輸入關鍵字並點擊「搜尋最相關 10 筆」，系統將列出最相關的 10 筆資料供您挑選加入知識庫。</span>"
                                    "</div>"
                                )
                                return

                            added_urls = {s.get("path_or_url") for s in (sources or [])}
                            gr.Markdown(f"**已檢索出最相關的 {len(results)} 筆資料，點擊「+ 加入知識庫」即可收錄：**")
                            with gr.Column(elem_classes=["search-results-list"]):
                                for i, r in enumerate(results):
                                    is_added = r.get("href") in added_urls
                                    with gr.Row(elem_classes=["search-result-item"]):
                                        with gr.Column(scale=8, elem_classes=["search-result-info"]):
                                            gr.Markdown(
                                                f"<div class='search-res-title'>{i+1}. {r['title']}</div>"
                                                f"<div class='search-res-url'>{r['href']}</div>"
                                                f"<div class='search-res-snippet'>{r.get('body', '')}</div>"
                                            )
                                        if is_added:
                                            gr.Button("已在知識庫", scale=2, size="sm", interactive=False, elem_classes=["search-added-btn"])
                                        else:
                                            add_btn = gr.Button("+ 加入知識庫", scale=2, size="sm", variant="primary", elem_classes=["search-add-btn"])

                                            def make_add(target_r=r):
                                                def _add(u, l):
                                                    from core.source_manager import add_source
                                                    res = add_source(u, target_r["href"], label=target_r["title"])
                                                    new_s = list_sources(u)
                                                    if res.get("ok"):
                                                        gr.Info(f"已成功加入：{target_r['title']}")
                                                        l += f"[{get_time_str()}] [來源庫] 已加入：{target_r['title']} ({target_r['href']})\n"
                                                    else:
                                                        gr.Warning(f"加入失敗：{res.get('message')}")
                                                        l += f"[{get_time_str()}] [來源庫錯誤] {res.get('message')}\n"
                                                    return new_s, l
                                                return _add

                                            add_btn.click(
                                                make_add(r),
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
            log_txt += f"[{t}] [系統警告] 未能連線至 Ollama (127.0.0.1:11434)，請先啟動服務！\n"
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
    open_modal_btn.click(lambda: gr.update(visible=True), outputs=source_modal)
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
    search_kw_btn.click(
        handle_keyword_search,
        inputs=[search_kw_input, uid_state, logs_box],
        outputs=[search_results_state, logs_box, sources_state]
    )
    search_kw_input.submit(
        handle_keyword_search,
        inputs=[search_kw_input, uid_state, logs_box],
        outputs=[search_results_state, logs_box, sources_state]
    )

if __name__ == "__main__":
    health = check_ollama_status()
    print(health["terminal_message"])
    demo.launch(server_name="127.0.0.1", share=False, css=custom_css, js=custom_js)



