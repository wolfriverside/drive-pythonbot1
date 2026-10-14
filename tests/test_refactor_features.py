"""
tests/test_refactor_features.py

針對重構方案兩大議題的單元測試：
1. Cookie UID 訪客持久化、uid 格式驗證、中介軟體、過期資料清理
2. HTML 原始存檔、網頁附件 (.pdf, .docx 等) 分析與提取、附件下載器與知識庫索引
"""

import os
import sys
import shutil
import tempfile
import unittest
from unittest.mock import patch, MagicMock

# Ensure project root is in sys.path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.security import validate_uid, UserCookieMiddleware
from core.web_content_analyzer import analyze_page, DOWNLOADABLE_EXTENSIONS
from core.attachment_downloader import _safe_filename, download_attachment
from core.cleanup_stale_users import cleanup_stale_users
from core.source_manager import _save_raw_html, get_raw_html, remove_source, list_sources, _sources_dir, _html_cache_dir

import app


class TestCookieUIDAndSecurity(unittest.TestCase):

    def test_validate_uid(self):
        """測試 UID 格式驗證與防路徑遍歷"""
        # 合法格式: user_ + 8位十六進位字元
        self.assertTrue(validate_uid("user_a1b2c3d4"))
        self.assertTrue(validate_uid("user_00000000"))
        self.assertTrue(validate_uid("user_ffffffff"))

        # 不合法格式
        self.assertFalse(validate_uid(""))
        self.assertFalse(validate_uid(None))
        self.assertFalse(validate_uid("user_123"))            # 太短
        self.assertFalse(validate_uid("user_g1h2i3j4"))       # 非十六進位
        self.assertFalse(validate_uid("../user_a1b2c3d4"))     # 路徑遍歷
        self.assertFalse(validate_uid("user_a1b2c3d4/test"))  # 斜線
        self.assertFalse(validate_uid("admin"))               # 任意名稱
        self.assertFalse(validate_uid("user_A1B2C3D4"))       # 大寫不符

    def test_init_session_from_cookie(self):
        """測試 init_session 從 Cookie 讀取 UID"""
        mock_req = MagicMock()
        mock_req.client.host = "127.0.0.1"
        mock_req.headers = {"cookie": "other=123; lpb_uid=user_12345678; foo=bar"}
        mock_req.cookies = {"lpb_uid": "user_12345678"}
        mock_req.query_params = {}

        uid, log_txt = app.init_session(mock_req)
        self.assertEqual(uid, "user_12345678")
        self.assertIn("伺服器啟動", log_txt)
        # 確保已建立使用者目錄
        user_dir = os.path.join("database", "users", "user_12345678")
        self.assertTrue(os.path.isdir(user_dir))
        # 清理測試目錄
        if os.path.isdir(user_dir):
            shutil.rmtree(user_dir)

    def test_init_session_fallback_query_params(self):
        """測試 Cookie 不存在時，降級至 URL query parameter（向後相容）"""
        mock_req = MagicMock()
        mock_req.client.host = "127.0.0.1"
        mock_req.headers = {}
        mock_req.cookies = {}
        mock_req.query_params = {"uid": "user_abcdef01"}

        uid, _ = app.init_session(mock_req)
        self.assertEqual(uid, "user_abcdef01")
        # 清理測試目錄
        user_dir = os.path.join("database", "users", "user_abcdef01")
        if os.path.isdir(user_dir):
            shutil.rmtree(user_dir)

    def test_init_session_invalid_uid_regenerates(self):
        """測試不合法的 UID 會被重置為全新合法 UID"""
        mock_req = MagicMock()
        mock_req.client.host = "127.0.0.1"
        mock_req.headers = {"cookie": "lpb_uid=../../etc/passwd"}
        mock_req.cookies = {"lpb_uid": "../../etc/passwd"}
        mock_req.query_params = {}

        uid, _ = app.init_session(mock_req)
        self.assertTrue(validate_uid(uid))
        self.assertNotEqual(uid, "../../etc/passwd")
        # 清理測試目錄
        user_dir = os.path.join("database", "users", uid)
        if os.path.isdir(user_dir):
            shutil.rmtree(user_dir)

    def test_cleanup_stale_users(self):
        """測試過期使用者目錄清理工具"""
        with tempfile.TemporaryDirectory() as temp_dir:
            # 建立一個測試使用者
            u1_dir = os.path.join(temp_dir, "user_11111111")
            os.makedirs(u1_dir)
            profile_path = os.path.join(u1_dir, "profile.json")
            import json
            # 建立一個 60 天前的 profile
            with open(profile_path, "w", encoding="utf-8") as f:
                json.dump({
                    "uid": "user_11111111",
                    "created_at": "2024-01-01T00:00:00+00:00",
                    "last_active_time": "2024-01-01T00:00:00+00:00"
                }, f)

            # 建立一個活躍使用者（當前時間）
            u2_dir = os.path.join(temp_dir, "user_22222222")
            os.makedirs(u2_dir)
            from datetime import datetime, timezone
            with open(os.path.join(u2_dir, "profile.json"), "w", encoding="utf-8") as f:
                json.dump({
                    "uid": "user_22222222",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "last_active_time": datetime.now(timezone.utc).isoformat()
                }, f)

            # 測試 dry_run
            res_dry = cleanup_stale_users(retention_days=30, dry_run=True, users_dir=temp_dir)
            self.assertEqual(res_dry["total_users"], 2)
            self.assertEqual(len(res_dry["stale_users"]), 1)
            self.assertEqual(res_dry["deleted_count"], 0)
            self.assertTrue(os.path.exists(u1_dir))

            # 測試實際刪除
            res = cleanup_stale_users(retention_days=30, dry_run=False, users_dir=temp_dir)
            self.assertEqual(res["deleted_count"], 1)
            self.assertFalse(os.path.exists(u1_dir))
            self.assertTrue(os.path.exists(u2_dir))


class TestWebContentAnalyzerAndStorage(unittest.TestCase):

    def test_analyze_page_finds_attachments(self):
        """測試 HTML 分析器能正確提取 PDF, DOCX 等可下載附件與頁面連結"""
        sample_html = """
        <!DOCTYPE html>
        <html>
        <head><title>測試法規公告</title></head>
        <body>
            <h1>跟蹤騷擾防制法公告</h1>
            <p>本法規全文及附件如下：</p>
            <ul>
                <li><a href="/downloads/law_full.pdf">法規全文 PDF 下載</a></li>
                <li><a href="https://example.com/docs/notice.docx">施行細則草案 DOCX</a></li>
                <li><a href="/data/stats.xlsx">統計附表 Excel</a></li>
                <li><a href="/news/page2.html">相關新聞報導</a></li>
                <li><a href="javascript:void(0)">無效按鈕</a></li>
                <li><a href="#section2">錨點連結</a></li>
            </ul>
        </body>
        </html>
        """
        base_url = "https://law.moj.gov.tw/News/"
        result = analyze_page(sample_html, base_url)

        self.assertIn("跟蹤騷擾防制法公告", result["text"])

        # 檢驗下載附件
        files = result["downloadable_files"]
        self.assertEqual(len(files), 3)

        pdf_item = next((f for f in files if f["ext"] == ".pdf"), None)
        self.assertIsNotNone(pdf_item)
        self.assertEqual(pdf_item["text"], "法規全文 PDF 下載")
        self.assertEqual(pdf_item["url"], "https://law.moj.gov.tw/downloads/law_full.pdf")

        docx_item = next((f for f in files if f["ext"] == ".docx"), None)
        self.assertIsNotNone(docx_item)
        self.assertEqual(docx_item["url"], "https://example.com/docs/notice.docx")

        xlsx_item = next((f for f in files if f["ext"] == ".xlsx"), None)
        self.assertIsNotNone(xlsx_item)
        self.assertEqual(xlsx_item["url"], "https://law.moj.gov.tw/data/stats.xlsx")

        # 檢驗頁面連結
        page_links = result["page_links"]
        self.assertEqual(len(page_links), 1)
        self.assertEqual(page_links[0]["url"], "https://law.moj.gov.tw/news/page2.html")

    def test_safe_filename(self):
        """測試下載檔案檔名清理與安全防護"""
        name1 = _safe_filename("https://example.com/test.pdf", "跟騷法/全文:第1版", ".pdf")
        self.assertTrue(name1.endswith(".pdf"))
        self.assertNotIn("/", name1)
        self.assertNotIn(":", name1)

        name2 = _safe_filename("https://example.com/path/document.docx", "", ".docx")
        self.assertEqual(name2, "document.docx")

    def test_raw_html_cache_and_cleanup(self):
        """測試原始 HTML 儲存、讀取與來源刪除時同步清理"""
        test_uid = "user_99999999"
        test_sid = "test_src_01"
        test_html = "<html><body><h1>Original Cached HTML</h1></body></html>"

        # 1. 存檔
        _save_raw_html(test_uid, test_sid, test_html)

        # 2. 讀取
        cached = get_raw_html(test_uid, test_sid)
        self.assertEqual(cached, test_html)

        # 3. 建立 fake source json 供 remove_source 測試
        src_path = os.path.join(_sources_dir(test_uid), f"{test_sid}.json")
        with open(src_path, "w", encoding="utf-8") as f:
            f.write("{}")

        # 4. 刪除來源
        removed = remove_source(test_uid, test_sid)
        self.assertTrue(removed)

        # 5. 確認關聯的 HTML 快取已被清理
        html_file = os.path.join(_html_cache_dir(test_uid), f"{test_sid}.html")
        self.assertFalse(os.path.exists(html_file))

        # 清理使用者資料夾
        shutil.rmtree(os.path.join("database", "users", test_uid), ignore_errors=True)

    def test_standardized_source_format_and_category(self):
        """測試資料庫固定格式規整、資料類別判定與導覽雜訊清洗"""
        from core.source_manager import normalize_source_meta, add_source, list_sources

        # 1. 政府統計資料
        m1 = normalize_source_meta(
            "https://www.stat.gov.tw/cl.aspx?n=2959",
            raw_title="8. 中華民國統計資訊網-人口",
            raw_snippet="地址：100214臺北市中正區廣州街2號 電話總機： (02)2380-3400 服務電話： (02)2380-3536"
        )
        self.assertEqual(m1["category"], "政府統計")
        self.assertEqual(m1["site_name"], "中華民國統計資訊網")
        self.assertEqual(m1["clean_title"], "中華民國統計資訊網-人口")

        # 2. 開放數據平台
        m2 = normalize_source_meta(
            "https://stats.tw/zh-tw/population",
            raw_title="7. 人口 · Taiwan Stats 台灣統計",
            raw_snippet="台灣戶籍登記人口 — 長期走向自然負成長..."
        )
        self.assertEqual(m2["category"], "統計數據")
        self.assertEqual(m2["site_name"], "Taiwan Stats 台灣統計")
        self.assertEqual(m2["clean_title"], "人口 · Taiwan Stats 台灣統計")

        # 3. 導覽雜訊過濾
        m3 = normalize_source_meta(
            "https://www.stat.gov.tw/Statistics.aspx?n=2960&CaN=362",
            raw_title="9. 人口靜態統計 - 中華民國統計資訊網",
            raw_snippet="回上一頁 回最上面 行政院主計總處影音 統計網rss"
        )
        self.assertEqual(m3["category"], "政府統計")
        self.assertNotIn("回上一頁", m3["clean_snippet"])
        self.assertNotIn("回最上面", m3["clean_snippet"])

        # 4. 本地檔案分類測試
        m_pdf = normalize_source_meta("reports/summary.pdf", raw_title="summary.pdf")
        self.assertEqual(m_pdf["category"], "PDF 文件")
        m_xls = normalize_source_meta("data/stats.xlsx", raw_title="stats.xlsx")
        self.assertEqual(m_xls["category"], "Excel 表格")


if __name__ == "__main__":
    unittest.main()
