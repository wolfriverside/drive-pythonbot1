"""
core/db_utils.py

安全的 JSON 檔案讀寫工具。

解決問題：
  - 多 tab / 多使用者同時寫入 profile.json 的競態條件（race condition）
  - 提供統一的讀寫入口，避免各模組散落 open()/json.dump() 呼叫

使用 filelock 實現跨進程的檔案鎖定。
若 filelock 未安裝，自動降級為 threading.Lock（僅保護同進程並發）。
"""

import os
import json
import threading

# 嘗試使用 filelock（跨進程安全），若未安裝則降級為 threading.Lock
try:
    from filelock import FileLock, Timeout as FileLockTimeout
    _USE_FILELOCK = True
except ImportError:
    _USE_FILELOCK = False
    print("[db_utils] filelock 未安裝，使用 threading.Lock 作為降級方案。"
          "建議執行 pip install filelock 以獲得完整並發保護。")

# threading 層級鎖（按檔案路徑分開）
_thread_locks: dict[str, threading.Lock] = {}
_meta_lock = threading.Lock()


def _get_thread_lock(path: str) -> threading.Lock:
    """取得或建立對應 path 的 threading.Lock。"""
    with _meta_lock:
        if path not in _thread_locks:
            _thread_locks[path] = threading.Lock()
        return _thread_locks[path]


def read_json(path: str, default=None) -> dict:
    """
    安全讀取 JSON 檔案。
    若檔案不存在或解析失敗，回傳 default（預設 None）。
    """
    if not os.path.exists(path):
        return default if default is not None else {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[db_utils] 讀取 JSON 失敗 ({path}): {e}")
        return default if default is not None else {}


def write_json(path: str, data: dict, timeout: float = 5.0) -> bool:
    """
    安全寫入 JSON 檔案（附檔案鎖）。

    流程：
      1. 先寫入同目錄的 .tmp 暫存檔
      2. 取得鎖定後 atomic replace 正式檔案
      3. 釋放鎖定

    回傳 True 表示成功，False 表示失敗。
    """
    abs_path = os.path.abspath(path)
    tmp_path = abs_path + ".tmp"
    thread_lock = _get_thread_lock(abs_path)

    try:
        # 先在鎖外寫入暫存，減少持鎖時間
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        if _USE_FILELOCK:
            lock_path = abs_path + ".lock"
            file_lock = FileLock(lock_path, timeout=timeout)
            try:
                with file_lock:
                    with thread_lock:
                        os.replace(tmp_path, abs_path)
            except FileLockTimeout:
                print(f"[db_utils] 取得檔案鎖逾時 ({path})，使用 threading.Lock 降級寫入")
                with thread_lock:
                    os.replace(tmp_path, abs_path)
        else:
            with thread_lock:
                os.replace(tmp_path, abs_path)

        return True

    except Exception as e:
        print(f"[db_utils] 寫入 JSON 失敗 ({path}): {e}")
        # 清理殘留暫存
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        return False


def update_json(path: str, updater, default=None, timeout: float = 5.0) -> tuple[bool, dict]:
    """
    讀取 → 修改 → 安全寫入的原子操作。

    參數：
        path:    JSON 檔案路徑
        updater: Callable[[dict], None]，就地修改 data dict
        default: 若檔案不存在時的初始值
        timeout: 等待檔案鎖的最大秒數

    回傳 (success: bool, data: dict)
    """
    data = read_json(path, default=default if default is not None else {})
    updater(data)
    ok = write_json(path, data, timeout=timeout)
    return ok, data


__all__ = ["read_json", "write_json", "update_json"]
