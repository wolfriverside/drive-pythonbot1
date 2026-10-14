import os
import uuid
import tiktoken

from core.config_manager import config
from core.db_utils import read_json, write_json, update_json


def get_token_count(text: str) -> int:
    """
    Estimate token count for a text string.
    We use cl100k_base (OpenAI) as a fast approximation for token limits.
    """
    try:
        enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text))
    except Exception:
        # Fallback approximation (1 char ~ 0.5 tokens for Chinese, roughly)
        return len(text)


def _user_profile_path(uid: str) -> str:
    return os.path.join("database", "users", uid, "profile.json")


def add_active_memory(uid: str, content: str) -> str:
    """
    Add a new active memory for the user. Returns a status message.
    """
    user_path = _user_profile_path(uid)
    if not os.path.exists(user_path):
        return "無法讀取使用者資料: 檔案不存在"

    mem_limit = config.memory_limit

    def _updater(data: dict):
        data.setdefault("active_memory", [])

    data = read_json(user_path)
    if data is None:
        return "無法讀取使用者資料: JSON 解析失敗"

    data.setdefault("active_memory", [])

    if len(data["active_memory"]) >= mem_limit:
        return f"長期記憶已達上限 ({mem_limit} 則)，請先刪除舊記憶。"

    mem_id = uuid.uuid4().hex[:6]
    memory_entry = {
        "id": mem_id,
        "content": content
    }
    data["active_memory"].append(memory_entry)

    ok = write_json(user_path, data)
    if not ok:
        return "寫入記憶失敗，請稍後再試。"

    return "成功加入長期記憶。"


def get_active_memory(uid: str) -> list:
    """Returns the list of active memories."""
    user_path = _user_profile_path(uid)
    data = read_json(user_path, default={})
    return data.get("active_memory", [])


def delete_active_memory(uid: str, mem_id: str) -> bool:
    user_path = _user_profile_path(uid)
    if not os.path.exists(user_path):
        return False

    def _updater(data: dict):
        data["active_memory"] = [
            m for m in data.get("active_memory", []) if m["id"] != mem_id
        ]

    ok, _ = update_json(user_path, _updater)
    return ok
