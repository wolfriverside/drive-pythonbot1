import os
import json
import csv
from pypdf import PdfReader
from docx import Document

def read_local_file(filepath: str) -> str:
    """
    Reads a local file or downloads an online file (PDF, TXT, CSV, DOCX, JSON) and extracts its content.
    Supports academic papers, journal articles, and government documents from URLs.
    """
    temp_file_path = None
    if filepath.startswith("http://") or filepath.startswith("https://"):
        import requests
        import tempfile
        from urllib.parse import urlparse, unquote

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml,application/pdf;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
        }

        try:
            try:
                response = requests.get(filepath, headers=headers, timeout=20)
            except requests.exceptions.SSLError:
                import urllib3
                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
                response = requests.get(filepath, headers=headers, verify=False, timeout=20)
            response.raise_for_status()

            # 解析副檔名（去除 query string）
            url_path = unquote(urlparse(filepath).path)
            ext = os.path.splitext(url_path)[1].lower()
            content_type = response.headers.get("Content-Type", "").lower()

            # 若為網頁 (HTML)，直接提取乾淨內文與表格，避免混入原始 HTML 標籤
            if "text/html" in content_type or (not ext and "application/pdf" not in content_type) or ext in [".html", ".htm", ".php", ".asp", ".aspx", ".jsp"]:
                try:
                    from tools.web_scraper import extract_clean_text_and_tables
                    response.encoding = response.apparent_encoding or "utf-8"
                    clean_text, _ = extract_clean_text_and_tables(response.text, base_url=filepath)
                    if clean_text and clean_text.strip():
                        return clean_text.strip()
                except Exception:
                    pass

            # 從 Content-Type 推斷副檔名
            if not ext or ext in [".php", ".asp", ".aspx", ".jsp", ".cgi"]:
                type_map = {
                    "application/pdf": ".pdf",
                    "application/msword": ".docx",
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
                    "text/plain": ".txt",
                    "text/csv": ".csv",
                    "application/json": ".json",
                }
                ext = type_map.get(content_type.split(";")[0].strip(), ".txt")

            cache_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cache", "downloads")
            os.makedirs(cache_dir, exist_ok=True)
            temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=ext, dir=cache_dir)
            temp_file.write(response.content)
            temp_file.close()
            temp_file_path = temp_file.name
            filepath = temp_file.name
        except Exception as e:
            return f"Error: 無法下載線上檔案 {filepath} - {e}"
    elif not os.path.exists(filepath):
        return f"Error: 找不到本機檔案 {filepath}"

    ext = os.path.splitext(filepath)[1].lower()
    text = ""

    try:
        if ext == ".txt":
            with open(filepath, "r", encoding="utf-8") as f:
                text = f.read()

        elif ext == ".pdf":
            reader = PdfReader(filepath)
            total_pages = len(reader.pages)
            for i, page in enumerate(reader.pages):
                page_text = page.extract_text()
                if page_text:
                    text += f"\n[第 {i+1} 頁]\n" + page_text.strip() + "\n"
            if not text.strip():
                return f"注意：此 PDF 文件共有 {total_pages} 頁，但疑似為純影像掃描檔，未包含純文字層可供提取。"

        elif ext == ".docx":
            doc = Document(filepath)
            for para in doc.paragraphs:
                if para.text.strip():
                    text += para.text.strip() + "\n"

        elif ext == ".csv":
            with open(filepath, "r", encoding="utf-8") as f:
                reader = csv.reader(f)
                for row in reader:
                    text += ", ".join(row) + "\n"

        elif ext == ".json":
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
                text = json.dumps(data, ensure_ascii=False, indent=2)

        else:
            return f"Error: 不支援的檔案格式 {ext}"

        return text.strip()
    except Exception as e:
        return f"Error: 讀取檔案時發生錯誤 {e}"
    finally:
        # Clean up temporary file if it was created from a URL
        if temp_file_path and os.path.exists(temp_file_path):
            try:
                os.remove(temp_file_path)
            except Exception:
                pass
