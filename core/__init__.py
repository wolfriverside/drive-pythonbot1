"""
LearnPythonBot - Core Module
包含系統核心引擎、記憶管理、知識庫管理、安全審查、向量計算、網頁內容結構分析、附件下載與訪客管理。
"""

from core.config_manager import config
from core.db_utils import read_json, write_json, update_json
from core.bot_engine import BotEngine
from core.health_check import check_ollama_status, get_configured_models
from core.security import validate_uid, check_suspicious, UserCookieMiddleware, sanitize_for_log
from core.source_manager import add_source, remove_source, list_sources, get_source, normalize_source_meta
from core.memory_manager import get_active_memory, add_active_memory, delete_active_memory
from core.embedding import chunk_text, get_embedding, get_embeddings_batch, clear_embedding_cache
from core.web_content_analyzer import analyze_page, DOWNLOADABLE_EXTENSIONS
from core.attachment_downloader import download_attachment, download_and_index_attachments
from core.cleanup_stale_users import cleanup_stale_users

__all__ = [
    # Config & DB utilities
    "config",
    "read_json",
    "write_json",
    "update_json",
    # Engine
    "BotEngine",
    # Health
    "check_ollama_status",
    "get_configured_models",
    # Security
    "validate_uid",
    "check_suspicious",
    "UserCookieMiddleware",
    "sanitize_for_log",
    # Knowledge sources
    "add_source",
    "remove_source",
    "list_sources",
    "get_source",
    # Memory
    "get_active_memory",
    "add_active_memory",
    "delete_active_memory",
    # Embedding
    "chunk_text",
    "get_embedding",
    "get_embeddings_batch",
    "clear_embedding_cache",
    # Web & files
    "analyze_page",
    "DOWNLOADABLE_EXTENSIONS",
    "download_attachment",
    "download_and_index_attachments",
    "cleanup_stale_users",
]
