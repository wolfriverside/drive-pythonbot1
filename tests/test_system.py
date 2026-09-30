"""
LearnPythonBot - 系統核心與工具功能整合測試套件
執行方式: python -m unittest tests/test_system.py 或 python tests/test_system.py
"""

import unittest
import os
import json
import sys

# Ensure project root is in sys.path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.health_check import check_ollama_status, get_configured_models
from core.bot_engine import BotEngine
from core.security import check_suspicious
from tools.file_reader import read_local_file


class TestLearnPythonBot(unittest.TestCase):

    def test_configured_models(self):
        """測試設定檔模型讀取"""
        main_model, embed_model = get_configured_models()
        self.assertTrue(isinstance(main_model, str) and len(main_model) > 0)
        self.assertEqual(embed_model, "nomic-embed-text")

    def test_health_check_structure(self):
        """測試 Ollama 健康檢測回傳結構"""
        status = check_ollama_status()
        self.assertIn("connected", status)
        self.assertIn("status_code", status)
        self.assertIn(status["status_code"], ["READY", "MISSING_MODEL", "OFFLINE"])
        self.assertIn("ui_html", status)
        self.assertIn("terminal_message", status)

    def test_bot_engine_tool_schemas(self):
        """測試 AI 工具清單與定義格式"""
        engine = BotEngine()
        tools = engine.get_tool_schemas()
        self.assertEqual(len(tools), 5)
        tool_names = [t["function"]["name"] for t in tools]
        self.assertIn("web_search", tool_names)
        self.assertIn("web_scraper", tool_names)
        self.assertIn("web_form_search", tool_names)
        self.assertIn("read_document", tool_names)
        self.assertIn("suggest_add_source", tool_names)


    def test_security_check_safe_input(self):
        """測試正常提問不會被安全審查誤擋"""
        uid = "test_user_safe"
        is_blocked = check_suspicious("我想學習 Python 的裝飾器", uid)
        self.assertFalse(is_blocked)

    def test_file_reader_local_not_found(self):
        """測試讀取不存在檔案時的友善錯誤處理"""
        res = read_local_file("non_existent_file_xyz.pdf")
        self.assertTrue("Error: 找不到本機檔案" in res)

    def test_skills_html_generation(self):
        """測試 AI 技能清單 HTML 結構生成"""
        from app import get_skills_html
        html_out = get_skills_html()
        self.assertIn("skills-scroll-container", html_out)
        self.assertIn("skill-item-card", html_out)

    def test_tools_directory_files_and_registry(self):
        """測試 tools/ 目錄下的所有技能工具檔案皆真實存在且可調用"""
        from tools import (
            AI_SKILLS_REGISTRY,
            get_all_tool_schemas,
            get_skills_display_info,
            search_web,
            scrape_url,
            form_search,
            read_local_file,
            suggest_and_add_source
        )
        self.assertEqual(len(AI_SKILLS_REGISTRY), 5)
        for skill in AI_SKILLS_REGISTRY:
            filepath = os.path.join(PROJECT_ROOT, skill["file"])
            self.assertTrue(os.path.exists(filepath), f"工具檔案不存在: {filepath}")
            self.assertTrue(callable(skill["handler"]), f"工具處理器不可調用: {skill['name']}")
        
        schemas = get_all_tool_schemas()
        self.assertEqual(len(schemas), 5)
        
        display_info = get_skills_display_info()
        self.assertEqual(len(display_info), 5)



if __name__ == "__main__":
    unittest.main()

