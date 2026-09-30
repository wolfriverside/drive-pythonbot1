import os
import json
import uuid
import tiktoken

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

def add_active_memory(uid: str, content: str) -> str:
    """
    Add a new active memory for the user. Returns a status message.
    """
    user_path = os.path.join("database", "users", uid, "profile.json")
    try:
        with open(user_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        return f"無法讀取使用者資料: {str(e)}"
    
    # Load constraints
    try:
        with open(os.path.join("database", "built_in_db.json"), "r", encoding="utf-8") as f:
            config = json.load(f)
    except:
        config = {}
    
    mem_limit = config.get("memory_limit", 10)
    
    if len(data.get("active_memory", [])) >= mem_limit:
        return f"長期記憶已達上限 ({mem_limit} 則)，請先刪除舊記憶。"
        
    mem_id = uuid.uuid4().hex[:6]
    memory_entry = {
        "id": mem_id,
        "content": content
    }
    data["active_memory"].append(memory_entry)
    
    with open(user_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        
    return "成功加入長期記憶。"

def get_active_memory(uid: str) -> list:
    """Returns the list of active memories."""
    user_path = os.path.join("database", "users", uid, "profile.json")
    try:
        with open(user_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data.get("active_memory", [])
    except Exception:
        return []

def delete_active_memory(uid: str, mem_id: str) -> bool:
    user_path = os.path.join("database", "users", uid, "profile.json")
    try:
        with open(user_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            
        data["active_memory"] = [m for m in data.get("active_memory", []) if m["id"] != mem_id]
        
        with open(user_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False
