"""
core/web_content_analyzer.py

爬取網頁後進行結構化分析，實現「不只是列印網頁」的目標：
1. 萃取純文字內容（沿用現有邏輯）
2. 解析並分類頁面上所有的超連結
3. 自動識別可下載附件 (PDF, DOCX, XLSX 等)
"""

import os
from urllib.parse import urlparse, urljoin
from bs4 import BeautifulSoup

# 視為可下載附件的副檔名集合
DOWNLOADABLE_EXTENSIONS = {
    '.pdf', '.docx', '.doc',
    '.xlsx', '.xls',
    '.pptx', '.ppt',
    '.csv',
    '.zip', '.rar', '.7z',
    '.odt', '.ods', '.odp',
}


def analyze_page(html: str, base_url: str) -> dict:
    """
    結構化分析一個 HTML 頁面的所有超連結並分類。

    參數:
        html:     完整的原始 HTML 字串
        base_url: 頁面的來源 URL（用於解析相對路徑）

    回傳:
        {
            "text":               str,   純文字內容（已去除 script/style）
            "downloadable_files": list,  可下載附件，每項為
                                         {"text": 連結文字, "url": 絕對URL, "ext": 副檔名}
            "page_links":         list,  其他 HTML 頁面連結，每項為
                                         {"text": 連結文字, "url": 絕對URL}
        }
    """
    soup = BeautifulSoup(html, "html.parser")
    downloadable = []
    page_links = []
    seen_urls = set()

    for a in soup.find_all('a', href=True):
        href = a['href'].strip()
        link_text = a.get_text(strip=True)

        # 跳過無效 / 非 HTTP 連結
        if not href or href.startswith(('javascript:', 'mailto:', 'tel:', '#')):
            continue

        # 解析相對 URL → 絕對 URL
        full_url = urljoin(base_url, href)

        # 僅處理 http/https
        parsed = urlparse(full_url)
        if parsed.scheme not in ('http', 'https'):
            continue

        # 去重
        if full_url in seen_urls:
            continue
        seen_urls.add(full_url)

        if not link_text:
            link_text = parsed.path.split('/')[-1] or "Link"

        # 從 URL path 提取副檔名（忽略 query string）
        url_path = parsed.path.lower()
        ext = os.path.splitext(url_path)[1]

        if ext in DOWNLOADABLE_EXTENSIONS:
            downloadable.append({
                "text": link_text,
                "url":  full_url,
                "ext":  ext,
            })
        else:
            page_links.append({
                "text": link_text,
                "url":  full_url,
            })

    clean_text, _ = extract_clean_text_and_tables(html, base_url=base_url)

    return {
        "text":               clean_text,
        "downloadable_files": downloadable,
        "page_links":         page_links,
    }


def extract_clean_text_and_tables(html: str, base_url: str = "") -> tuple[str, BeautifulSoup]:
    soup = BeautifulSoup(html, "html.parser")
    for script in soup(["script", "style", "nav", "footer", "header"]):
        script.decompose()

    # Convert all <a> tags to Markdown links so URLs aren't lost
    for a in soup.find_all('a', href=True):
        href = a['href']
        text = a.get_text(strip=True)
        if not text:
            text = "Link"
        # Handle relative URLs heuristically if base_url is provided
        if base_url and href.startswith('/'):
            from urllib.parse import urlparse
            parsed_uri = urlparse(base_url)
            domain = '{uri.scheme}://{uri.netloc}'.format(uri=parsed_uri)
            href = domain + href
        elif base_url and not href.startswith('http') and not href.startswith('javascript'):
            href = base_url.rstrip('/') + '/' + href
            
        a.replace_with(f"[{text}]({href})")

    # Extract tables as markdown
    tables_md = []
    for table in soup.find_all('table'):
        rows = []
        for tr in table.find_all('tr'):
            cells = [td.get_text(strip=True) for td in tr.find_all(['th', 'td'])]
            if cells:
                # remove newlines in cells
                cells = [c.replace('\n', ' ') for c in cells]
                rows.append(" | ".join(cells))
        if rows:
            header = rows[0]
            separator = " | ".join(["---"] * len(header.split(" | ")))
            tables_md.append("\n".join([header, separator] + rows[1:]))
        # Remove the table from soup so we don't duplicate text
        table.decompose()

    import re
    text = soup.get_text(separator=' ')
    text = re.sub(r'\s+', ' ', text).strip()
    
    if tables_md:
        text += "\n\n### 表格資料 ###\n" + "\n\n".join(tables_md)
        
    return text, soup
