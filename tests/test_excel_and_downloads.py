import os
import unittest
import tempfile
import openpyxl
from tools.file_reader import read_local_file
from core.attachment_downloader import _safe_filename

# 【測試沙盒目錄】讓所有測試用的暫存檔案都在安全沙盒內，避免被路徑防禦機制擋截
_SANDBOX_TEST_DIR = os.path.join("database", "users", "test_sandbox", "uploads")
os.makedirs(_SANDBOX_TEST_DIR, exist_ok=True)

class TestExcelAndDownloadFeatures(unittest.TestCase):

    def test_xlsx_reading(self):
        """測試標準 .xlsx 檔案的讀取與表格轉換"""
        with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False, dir=_SANDBOX_TEST_DIR) as tf:
            tf_path = tf.name

        try:
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.title = "班車時刻表"
            ws.append(["站點", "抵達時間", "發車時間"])
            ws.append(["花蓮火車站", "08:10", "08:15"])
            ws.append(["慈濟大學", "08:30", "08:35"])
            wb.save(tf_path)
            wb.close()

            content = read_local_file(tf_path)
            self.assertIn("[工作表: 班車時刻表]", content)
            self.assertIn("花蓮火車站 | 08:10 | 08:15", content)
            self.assertIn("慈濟大學 | 08:30 | 08:35", content)
        finally:
            if os.path.exists(tf_path):
                os.remove(tf_path)

    def test_pseudo_xls_html_table(self):
        """測試政府/學校網站常見的『HTML 表格偽裝成 .xls』"""
        html_table = """
        <html>
        <head><meta charset="utf-8"></head>
        <body>
            <table>
                <tr><th>公車路線</th><th>起訖站</th><th>班距</th></tr>
                <tr><td>統聯301</td><td>花蓮-慈大</td><td>15分</td></tr>
                <tr><td>太魯閣客運</td><td>火車站-東華</td><td>20分</td></tr>
            </table>
        </body>
        </html>
        """
        with tempfile.NamedTemporaryFile(suffix=".xls", delete=False, mode="w",
                                         encoding="utf-8", dir=_SANDBOX_TEST_DIR) as tf:
            tf.write(html_table)
            tf_path = tf.name

        try:
            content = read_local_file(tf_path)
            self.assertNotIn("Error:", content)
            self.assertIn("公車路線 | 起訖站 | 班距", content)
            self.assertIn("統聯301 | 花蓮-慈大 | 15分", content)
        finally:
            if os.path.exists(tf_path):
                os.remove(tf_path)

    def test_pseudo_xls_xml_spreadsheet(self):
        """測試 XML Spreadsheet 2003 偽裝的 .xls"""
        xml_sheet = """<?xml version="1.0"?>
        <Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet">
            <Worksheet ss:Name="交通清冊">
                <Table>
                    <Row>
                        <Cell><Data ss:Type="String">車號</Data></Cell>
                        <Cell><Data ss:Type="String">司機</Data></Cell>
                    </Row>
                    <Row>
                        <Cell><Data ss:Type="String">ABC-123</Data></Cell>
                        <Cell><Data ss:Type="String">陳司機</Data></Cell>
                    </Row>
                </Table>
            </Worksheet>
        </Workbook>
        """
        with tempfile.NamedTemporaryFile(suffix=".xls", delete=False, mode="w",
                                         encoding="utf-8", dir=_SANDBOX_TEST_DIR) as tf:
            tf.write(xml_sheet)
            tf_path = tf.name

        try:
            content = read_local_file(tf_path)
            self.assertNotIn("Error:", content)
            self.assertIn("車號 | 司機", content)
            self.assertIn("ABC-123 | 陳司機", content)
        finally:
            if os.path.exists(tf_path):
                os.remove(tf_path)

    def test_cp950_big5_csv(self):
        """測試 Big5 / CP950 編碼的 CSV 檔案（台灣政府開源資料常見）"""
        cp950_content = "編號,項目名稱,說明\n1,慈濟大學交通車,每日營運行駛路線\n"
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False,
                                         mode="wb", dir=_SANDBOX_TEST_DIR) as tf:
            tf.write(cp950_content.encode("cp950"))
            tf_path = tf.name

        try:
            content = read_local_file(tf_path)
            self.assertNotIn("Error:", content)
            self.assertIn("慈濟大學交通車", content)
            self.assertIn("每日營運行駛路線", content)
        finally:
            if os.path.exists(tf_path):
                os.remove(tf_path)

    def test_safe_filename(self):
        """測試 Windows 安全檔名過濾與副檔名補正"""
        fn1 = _safe_filename("https://example.com/test.xls", "交通車:時刻表/2026?*", ".xls")
        self.assertNotIn(":", fn1)
        self.assertNotIn("/", fn1)
        self.assertNotIn("?", fn1)
        self.assertTrue(fn1.endswith(".xls"))

        # 測試結尾不能有點或空白
        fn2 = _safe_filename("https://example.com/data", "公告清冊. . .", ".pdf")
        self.assertTrue(fn2.endswith(".pdf"))
        self.assertNotIn(" .", fn2)

    def test_canonicalize_url_and_cross_language_dedup(self):
        """測試跨語系網址與簡繁標題標準化去重"""
        from tools.web_search import canonicalize_url, canonical_title_key

        # 維基百科各語系與編碼路徑標準化
        u1 = "https://zh.wikipedia.org/wiki/%E9%A6%99%E6%B8%AF%E5%9B%9E%E6%AD%B8"
        u2 = "https://zh.wikipedia.org/zh-cn/%E9%A6%99%E6%B8%AF%E5%9B%9E%E5%BD%92"
        u3 = "https://zh.wikipedia.org/zh-tw/香港回歸"
        u4 = "http://zh.m.wikipedia.org/zh-cn/香港回归?utm_source=test"

        self.assertEqual(canonicalize_url(u1), canonicalize_url(u2))
        self.assertEqual(canonicalize_url(u2), canonicalize_url(u3))
        self.assertEqual(canonicalize_url(u1), canonicalize_url(u4))

        # 簡繁標題與入口網站贅字去重
        t1 = "香港回归 - 维基百科，自由的百科全书"
        t2 = "香港回歸 - 維基百科，自由的百科全書"
        t3 = "香港主權移交25週年：令人難忘的十件大事 - BBC News 中文"
        t4 = "香港主权移交25周年：令人难忘的十件大事 - BBC 中文网"

        self.assertEqual(canonical_title_key(t1), canonical_title_key(t2))
        self.assertEqual(canonical_title_key(t3), canonical_title_key(t4))

    def test_attachment_download_tracking(self):
        """測試附件下載標記持久化與重複防護"""
        import json
        from core.source_manager import (
            _sources_dir,
            mark_attachment_downloaded,
            is_attachment_downloaded,
            list_sources
        )

        test_uid = "test_att_unit_user"
        test_sid = "test_parent_src"
        d = _sources_dir(test_uid)
        os.makedirs(d, exist_ok=True)
        src_path = os.path.join(d, f"{test_sid}.json")

        sample_source = {
            "source_id": test_sid,
            "type": "url",
            "label": "測試母網頁",
            "path_or_url": "https://example.com/page",
            "added_at": "2026-10-07T00:00:00Z",
            "summary": "測試摘要",
            "chunks": [],
            "discovered_files": [
                {
                    "url": "https://example.com/files/report.pdf",
                    "text": "統計研析報告",
                    "ext": ".pdf"
                }
            ]
        }
        with open(src_path, "w", encoding="utf-8") as f:
            json.dump(sample_source, f, ensure_ascii=False)

        try:
            # 初始狀態未下載
            self.assertFalse(is_attachment_downloaded(test_uid, test_sid, "https://example.com/files/report.pdf"))

            # 標記為已下載
            ok = mark_attachment_downloaded(test_uid, test_sid, "https://example.com/files/report.pdf", "child_123")
            self.assertTrue(ok)

            # 再次檢查應為已下載
            self.assertTrue(is_attachment_downloaded(test_uid, test_sid, "https://example.com/files/report.pdf"))
            # 以文字比對也應為已下載
            self.assertTrue(is_attachment_downloaded(test_uid, test_sid, "https://example.com/files/other.pdf", "統計研析報告"))

            # 檢查母來源資料庫中標記已成功持久化
            with open(src_path, "r", encoding="utf-8") as f:
                saved = json.load(f)
            self.assertTrue(saved["discovered_files"][0].get("downloaded"))
            self.assertEqual(saved["discovered_files"][0].get("downloaded_source_id"), "child_123")
        finally:
            import shutil
            user_dir = os.path.join("database", "users", test_uid)
            if os.path.exists(user_dir):
                shutil.rmtree(user_dir, ignore_errors=True)

    def test_search_relevance_and_noise_filtering(self):
        """測試搜尋相關性過濾器（排斥日文/異國語言混淆、旅遊購物垃圾、低相關度結果）"""
        from tools.web_search import (
            is_language_mismatch,
            is_spam_or_irrelevant,
            calculate_relevance_score
        )

        # 1. 測試非日文查詢時過濾日文網頁與假名內容
        self.assertTrue(is_language_mismatch("人口推計 - 統計局ホームページ", "https://www.stat.go.jp/data/jinsui/index.html", "國勢調査による人口を基に", "人口統計"))
        self.assertTrue(is_language_mismatch("日本の人口統計 - Wikipedia", "https://ja.wikipedia.org/wiki/test", "", "人口統計"))
        self.assertFalse(is_language_mismatch("中華民國 內政部戶政司 全球資訊網", "https://www.ris.gov.tw/app/portal/346", "人口統計資料", "人口統計"))

        # 2. 測試非旅遊查詢時過濾 SEO 旅遊行程與伴手禮點擊農場
        travel_blog = "【2026香港景點】九龍／中環／旺角／尖沙咀必去景點推薦"
        self.assertTrue(is_spam_or_irrelevant(travel_blog, "https://example.com/hongkong-travel", "香港移交"))
        self.assertTrue(is_spam_or_irrelevant("2026 最新折價券免運優惠熱銷推薦", "https://shopee.tw/test", "中華民國統計資訊網"))

        # 3. 測試語意相關性評分高低
        score_exact = calculate_relevance_score("香港移交後之爭議事件", "香港移交後之爭議事件 - 大陸委員會", "https://www.mac.gov.tw/report", "香港移交後爭議情勢研析")
        score_irrel = calculate_relevance_score("香港移交後之爭議事件", "香港旅遊飯店住宿推薦優惠", "https://hotel.com/hk", "訂房資訊優惠")
        self.assertGreater(score_exact, 100.0)
        self.assertLess(score_irrel, 10.0)


if __name__ == "__main__":
    unittest.main()

