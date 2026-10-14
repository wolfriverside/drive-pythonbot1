"""
tests/test_core_units.py

LearnPythonBot 核心模組單元測試。
覆蓋：
  - ConfigManager (mtime 快取、config 取得)
  - db_utils (read/write/update_json)
  - security (is_safe_local_path、validate_uid、mask_error)
  - embedding (chunk_text、cosine_similarity)
  - memory_manager (CRUD、上限保護)
  - source_manager (normalize_source_meta、index cache 失效)
  - bot_engine (_build_system_message、工具分派)

執行方式：
    pytest tests/test_core_units.py -v
"""

import os
import sys
import json
import tempfile
import shutil
import pytest

# 確保 project root 在 sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ─────────────────────────────────────────────────────────────
# ConfigManager
# ─────────────────────────────────────────────────────────────

class TestConfigManager:
    def test_get_returns_dict(self):
        from core.config_manager import config
        cfg = config.get()
        assert isinstance(cfg, dict)

    def test_model_name_property_default(self, tmp_path, monkeypatch):
        """若 config 檔案不存在，model_name 應回傳預設值 qwen3"""
        import core.config_manager as cm
        monkeypatch.setattr(cm, "_CONFIG_PATH", str(tmp_path / "nonexistent.json"))
        mgr = cm._ConfigManager()
        assert mgr.model_name == "qwen3"

    def test_mtime_cache(self, tmp_path, monkeypatch):
        """config 未變動時，第二次 get() 應命中快取（mtime 相同）"""
        import core.config_manager as cm
        cfg_path = tmp_path / "built_in_db.json"
        cfg_path.write_text(json.dumps({"model_name": "test_model"}), encoding="utf-8")
        monkeypatch.setattr(cm, "_CONFIG_PATH", str(cfg_path))

        mgr = cm._ConfigManager()
        result1 = mgr.get()
        mtime1 = mgr._mtime
        result2 = mgr.get()
        mtime2 = mgr._mtime

        assert result1["model_name"] == "test_model"
        assert mtime1 == mtime2  # 快取命中，mtime 未變
        assert result1 is result2  # 同一個 dict 物件


# ─────────────────────────────────────────────────────────────
# db_utils
# ─────────────────────────────────────────────────────────────

class TestDbUtils:
    def test_read_nonexistent_returns_default(self, tmp_path):
        from core.db_utils import read_json
        result = read_json(str(tmp_path / "no_file.json"), default={"a": 1})
        assert result == {"a": 1}

    def test_write_and_read_roundtrip(self, tmp_path):
        from core.db_utils import read_json, write_json
        path = str(tmp_path / "test.json")
        data = {"key": "value", "num": 42}
        ok = write_json(path, data)
        assert ok is True
        loaded = read_json(path)
        assert loaded == data

    def test_write_is_atomic(self, tmp_path):
        """寫入後應無殘留 .tmp 暫存檔"""
        from core.db_utils import write_json
        path = str(tmp_path / "atomic.json")
        write_json(path, {"x": 1})
        assert not os.path.exists(path + ".tmp")

    def test_update_json(self, tmp_path):
        from core.db_utils import write_json, update_json
        path = str(tmp_path / "u.json")
        write_json(path, {"count": 0})

        def increment(data):
            data["count"] += 1

        ok, result = update_json(path, increment)
        assert ok is True
        assert result["count"] == 1

    def test_read_invalid_json_returns_default(self, tmp_path):
        from core.db_utils import read_json
        bad_file = tmp_path / "bad.json"
        bad_file.write_text("{ invalid json }", encoding="utf-8")
        result = read_json(str(bad_file), default={"fallback": True})
        assert result == {"fallback": True}


# ─────────────────────────────────────────────────────────────
# security
# ─────────────────────────────────────────────────────────────

class TestSecurity:
    def test_validate_uid_valid(self):
        from core.security import validate_uid
        assert validate_uid("user_a1b2c3d4") is True
        assert validate_uid("user_deadbeef") is True

    def test_validate_uid_invalid(self):
        from core.security import validate_uid
        assert validate_uid("") is False
        assert validate_uid("admin") is False
        assert validate_uid("user_ZZZZZZZZ") is False  # 非 hex
        assert validate_uid("../etc/passwd") is False
        assert validate_uid("user_a1b2c3d4extra") is False  # 過長

    def test_is_safe_local_path_allows_http(self):
        from core.security import is_safe_local_path
        assert is_safe_local_path("https://example.com/file.pdf") is True

    def test_is_safe_local_path_blocks_traversal(self):
        from core.security import is_safe_local_path
        assert is_safe_local_path("../../etc/passwd") is False
        assert is_safe_local_path("../database/built_in_db.json") is False

    def test_is_safe_local_path_allows_uploads(self, tmp_path):
        """database/users/ 路徑下的檔案應被允許"""
        from core.security import is_safe_local_path, SAFE_UPLOAD_ROOT
        safe_path = str(SAFE_UPLOAD_ROOT / "user_abcd1234" / "uploads" / "test.pdf")
        # 即使檔案不存在，路徑本身應通過沙盒檢查
        assert is_safe_local_path(safe_path) is True

    def test_mask_error_permission(self):
        from core.security import mask_error
        result = mask_error(PermissionError("test"))
        assert "無權限" in result

    def test_mask_error_file_not_found(self):
        from core.security import mask_error
        result = mask_error(FileNotFoundError("test"))
        assert "找不到" in result

    def test_mask_error_generic(self):
        from core.security import mask_error
        result = mask_error(ValueError("unexpected"))
        assert "異常" in result or "系統" in result


# ─────────────────────────────────────────────────────────────
# embedding
# ─────────────────────────────────────────────────────────────

class TestEmbedding:
    def test_chunk_text_basic(self):
        from core.embedding import chunk_text
        text = "A" * 2000
        chunks = chunk_text(text, chunk_size=800, overlap=100, max_chunks=40)
        assert len(chunks) > 1
        assert all(len(c) <= 800 for c in chunks)

    def test_chunk_text_max_chunks(self):
        from core.embedding import chunk_text
        text = "B" * 10000
        chunks = chunk_text(text, chunk_size=100, overlap=0, max_chunks=5)
        assert len(chunks) <= 5

    def test_chunk_text_short_text(self):
        from core.embedding import chunk_text
        chunks = chunk_text("hello", chunk_size=800)
        assert chunks == ["hello"]

    def test_cosine_similarity_identical(self):
        from core.embedding import cosine_similarity
        v = [1.0, 2.0, 3.0]
        assert abs(cosine_similarity(v, v) - 1.0) < 1e-6

    def test_cosine_similarity_orthogonal(self):
        from core.embedding import cosine_similarity
        v1 = [1.0, 0.0]
        v2 = [0.0, 1.0]
        assert abs(cosine_similarity(v1, v2)) < 1e-6

    def test_cosine_similarity_empty(self):
        from core.embedding import cosine_similarity
        assert cosine_similarity([], [1.0]) == 0.0
        assert cosine_similarity([1.0], []) == 0.0

    def test_lru_cache_exists(self):
        """確認 LRU cache 裝飾器存在"""
        from core.embedding import _get_embedding_cached
        assert hasattr(_get_embedding_cached, "cache_info")

    def test_clear_cache(self):
        from core.embedding import clear_embedding_cache
        clear_embedding_cache()  # 不應拋出例外


# ─────────────────────────────────────────────────────────────
# memory_manager (使用臨時目錄模擬 database)
# ─────────────────────────────────────────────────────────────

class TestMemoryManager:
    @pytest.fixture(autouse=True)
    def setup_temp_db(self, tmp_path, monkeypatch):
        """將 database/users/test_uid/ 重導向至臨時目錄"""
        self.uid = "user_testabcd"
        self.user_dir = tmp_path / "database" / "users" / self.uid
        self.user_dir.mkdir(parents=True)
        self.profile_path = self.user_dir / "profile.json"
        init_data = {
            "uid": self.uid,
            "active_memory": []
        }
        self.profile_path.write_text(json.dumps(init_data), encoding="utf-8")

        # monkeypatch os.path.join 路徑行為較複雜，改用 chdir
        monkeypatch.chdir(tmp_path)

    def test_get_active_memory_empty(self):
        from core.memory_manager import get_active_memory
        mems = get_active_memory(self.uid)
        assert mems == []

    def test_add_and_get_memory(self):
        from core.memory_manager import add_active_memory, get_active_memory
        result = add_active_memory(self.uid, "我喜歡 Python")
        assert "成功" in result
        mems = get_active_memory(self.uid)
        assert len(mems) == 1
        assert mems[0]["content"] == "我喜歡 Python"

    def test_delete_memory(self):
        from core.memory_manager import add_active_memory, get_active_memory, delete_active_memory
        add_active_memory(self.uid, "要刪除的記憶")
        mems = get_active_memory(self.uid)
        mem_id = mems[0]["id"]
        ok = delete_active_memory(self.uid, mem_id)
        assert ok is True
        assert get_active_memory(self.uid) == []

    def test_memory_limit_enforced(self, monkeypatch):
        """超過 memory_limit 應拒絕新增"""
        import core.memory_manager as mm_module

        class _FakeConfig:
            memory_limit = 2
            model_name = "qwen3"
            embed_model = "nomic-embed-text"
            system_prompt = ""
            constraints = []
            upload_whitelist = []
            upload_max_size_mb = 10
            def get(self): return {}

        # patch memory_manager 模組內部實際使用的 config binding
        monkeypatch.setattr(mm_module, "config", _FakeConfig())

        mm_module.add_active_memory(self.uid, "記憶 1")
        mm_module.add_active_memory(self.uid, "記憶 2")
        result = mm_module.add_active_memory(self.uid, "記憶 3")
        # 超限後不應回傳成功訊息
        assert "成功" not in result


# ─────────────────────────────────────────────────────────────
# source_manager — normalize_source_meta & index cache
# ─────────────────────────────────────────────────────────────

class TestSourceManager:
    def test_normalize_gov_url(self):
        from core.source_manager import normalize_source_meta
        meta = normalize_source_meta("https://stat.gov.tw/page1", raw_title="統計資料")
        assert meta["category"] == "政府統計"
        assert "中華民國統計資訊網" in meta["site_name"]

    def test_normalize_wikipedia(self):
        from core.source_manager import normalize_source_meta
        meta = normalize_source_meta("https://zh.wikipedia.org/wiki/Python", raw_title="Python")
        assert meta["category"] == "百科辭典"

    def test_normalize_pdf_local(self):
        from core.source_manager import normalize_source_meta
        meta = normalize_source_meta("/some/path/report.pdf")
        assert meta["category"] == "PDF 文件"
        assert meta["site_name"] == "本地文件"

    def test_normalize_excel_local(self):
        from core.source_manager import normalize_source_meta
        meta = normalize_source_meta("C:/Users/test/data.xlsx")
        assert "Excel" in meta["category"]

    def test_source_index_cache_invalidated_on_remove(self, tmp_path, monkeypatch):
        """remove_source 後快取應被清除"""
        import core.source_manager as sm
        uid = "user_abcd1234"

        # 預填快取
        sm._source_index_cache[uid] = {"chunks": [("label", "text", [0.1])], "mtime": 9999.0}
        assert uid in sm._source_index_cache

        # 模擬 sources dir 存在但為空
        sources_d = tmp_path / "database" / "users" / uid / "sources"
        sources_d.mkdir(parents=True)
        monkeypatch.chdir(tmp_path)

        sm.remove_source(uid, "nonexistent_source_id")
        assert uid not in sm._source_index_cache


# ─────────────────────────────────────────────────────────────
# bot_engine — _build_system_message 結構
# ─────────────────────────────────────────────────────────────

class TestBotEngine:
    def test_instantiation(self):
        from core.bot_engine import BotEngine
        bot = BotEngine()
        assert bot is not None

    def test_model_name_delegates_to_config(self, monkeypatch):
        """bot.model_name 應透過 config 單例取得模型名稱"""
        import core.bot_engine as be_module
        import core.config_manager as cm

        # monkeypatch 實際被 BotEngine 引用的 config 物件
        class _FakeConfig:
            model_name = "test_model_xyz"
            system_prompt = "你好"
            constraints = []
            memory_limit = 10
            embed_model = "nomic"
            upload_whitelist = []
            upload_max_size_mb = 10
            def get(self): return {}

        fake = _FakeConfig()
        monkeypatch.setattr(cm, "config", fake)
        monkeypatch.setattr(be_module, "config", fake)

        bot = be_module.BotEngine()
        assert bot.model_name == "test_model_xyz"

    def test_get_tool_schemas_returns_list(self):
        from core.bot_engine import BotEngine
        bot = BotEngine()
        schemas = bot.get_tool_schemas()
        assert isinstance(schemas, list)
        assert len(schemas) > 0
        for s in schemas:
            assert "type" in s
            assert s["type"] == "function"
            assert "function" in s

    def test_format_tool_call_log_sanitizes_paths(self):
        """工具調用日誌不應暴露實體本機路徑或內部端口"""
        from core.bot_engine import BotEngine
        bot = BotEngine()
        # 模擬 read_document 包含本機路徑
        log_msg = bot._format_tool_call_log("read_document", {
            "filepath_or_url": "database/users/user_12345678/uploads/secret_doc.pdf",
            "query": "機密內容"
        })
        assert "database" not in log_msg
        assert "user_12345678" not in log_msg
        assert "secret_doc.pdf" in log_msg
        assert "機密內容" in log_msg

        # 模擬 web_search
        ws_msg = bot._format_tool_call_log("web_search", {"query": "Python 迴圈"})
        assert "Python 迴圈" in ws_msg

        # 模擬 web_scraper 含本地端口嘗試
        scraper_msg = bot._format_tool_call_log("web_scraper", {"url": "http://127.0.0.1:11434/api"})
        assert "127.0.0.1" not in scraper_msg
        assert "本地服務" in scraper_msg


# ─────────────────────────────────────────────────────────────
# security — sanitize_for_log 與健康檢查模型解包
# ─────────────────────────────────────────────────────────────

class TestLogSanitizationAndModels:
    def test_sanitize_local_addresses(self):
        from core.security import sanitize_for_log
        raw = "連線失敗: http://127.0.0.1:11434/api 或 localhost:8000"
        clean = sanitize_for_log(raw)
        assert "127.0.0.1" not in clean
        assert "localhost" not in clean
        assert "本地服務" in clean

    def test_sanitize_windows_and_db_paths(self):
        from core.security import sanitize_for_log
        win_path = r"讀取檔案 C:\Users\strig\learnpythonbot\database\users\user_01\uploads\test.pdf 失敗"
        clean = sanitize_for_log(win_path)
        assert "C:" not in clean
        assert "strig" not in clean
        assert "test.pdf" in clean

        db_path = "database/users/user_abc12345/uploads/handbook.pdf"
        clean_db = sanitize_for_log(db_path)
        assert "user_abc12345" not in clean_db
        assert "handbook.pdf" in clean_db

    def test_sanitize_preserves_normal_content(self):
        from core.security import sanitize_for_log
        normal = "使用者詢問：請推薦花蓮美食，參考 https://www.google.com"
        clean = sanitize_for_log(normal)
        assert clean == normal

    def test_configured_models_dict_and_unpacking(self):
        from core.health_check import get_configured_models
        models = get_configured_models()
        # 支援字典讀取
        assert isinstance(models, dict)
        assert "main_model" in models
        assert "embed_model" in models
        # 支援 tuple 解包
        main, embed = get_configured_models()
        assert main == models["main_model"]
        assert embed == models["embed_model"]

