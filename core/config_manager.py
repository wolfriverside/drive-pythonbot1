"""
core/config_manager.py

ConfigManager — 統一設定管理單例（Singleton）。

消滅散落在 bot_engine、security、health_check、memory_manager、source_manager
各自獨立讀取 built_in_db.json 的問題。

特性：
  - 惰性讀取：首次 get() 才載入
  - mtime 快取：僅在 config 檔案實際變更時才重讀，平時 0 I/O
  - 執行緒安全：使用 threading.Lock 保護並發讀寫
  - 無外部依賴：純標準庫
"""

import os
import json
import threading

_CONFIG_PATH = os.path.join("database", "built_in_db.json")


class _ConfigManager:
    """內部單例類別，不應直接實例化。請使用模組層級的 `config` 物件。"""

    def __init__(self):
        self._config: dict = {}
        self._mtime: float = -1.0
        self._lock = threading.Lock()

    def get(self) -> dict:
        """
        取得設定字典。
        若 config 檔案自上次讀取後未變動，直接回傳快取；否則重新讀取。
        """
        try:
            current_mtime = os.path.getmtime(_CONFIG_PATH)
        except OSError:
            # 檔案不存在，回傳現有快取（或空 dict）
            return self._config

        with self._lock:
            if current_mtime != self._mtime:
                try:
                    with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
                        self._config = json.load(f)
                    self._mtime = current_mtime
                except Exception as e:
                    print(f"[ConfigManager] 無法讀取設定檔: {e}")
                    # 保留舊快取，避免設定歸零
            return self._config

    def reload(self) -> dict:
        """強制重新讀取 config，忽略 mtime 快取。"""
        with self._lock:
            self._mtime = -1.0
        return self.get()

    # ── 便捷屬性，常用設定直接取用 ──────────────────────────────────────

    @property
    def model_name(self) -> str:
        return self.get().get("model_name", "qwen3")

    @property
    def embed_model(self) -> str:
        return self.get().get("embed_model", "nomic-embed-text")

    @property
    def system_prompt(self) -> str:
        return self.get().get("system_prompt", "你是一個專業的 Python 教學助理。")

    @property
    def constraints(self) -> list:
        return self.get().get("constraints", [])

    @property
    def memory_limit(self) -> int:
        return self.get().get("memory_limit", 10)

    @property
    def upload_whitelist(self) -> list:
        return self.get().get("upload_whitelist", [".pdf", ".txt", ".csv", ".docx", ".json"])

    @property
    def upload_max_size_mb(self) -> int:
        return self.get().get("upload_max_size_mb", 10)


# ── 模組層級單例，全域共用 ───────────────────────────────────────────────
config = _ConfigManager()

__all__ = ["config"]
