import re
import requests
import urllib3
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

WAF_BLOCK_KEYWORDS = [
    "Web Page Blocked",
    "Attack ID:",
    "Access Denied",
    "The URL you requested has been blocked",
    "403 Forbidden",
    "Cloudflare Ray ID",
]

DEFAULT_BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
}

def is_waf_blocked(html: str) -> bool:
    """Detects whether HTML is a WAF / firewall block page rather than actual site content."""
    if not html:
        return True
    return any(kw.lower() in html.lower() for kw in WAF_BLOCK_KEYWORDS)

def scrape_url(url: str) -> dict:
    """
    Visit a URL using a hybrid scraper (requests with SSL fallback + stealth Playwright fallback).
    Prevents government sites (e.g., stat.gov.tw) from blocking headless browsers or failing SSL.
    Returns:
        {
            "text":     extracted_text,
            "links":    [{"text": link_text, "url": link_url}, ...],
            "raw_html": raw HTML string (for downstream attachment discovery)
        }
    """
    result = {"text": "", "links": [], "raw_html": ""}
    html = ""

    # 1. 優先嘗試 requests 高速讀取（避開多數台灣政府網站對 Playwright Headless 機器人的 WAF 阻擋）
    try:
        resp = requests.get(
            url,
            headers=DEFAULT_BROWSER_HEADERS,
            verify=False,
            timeout=12,
            allow_redirects=True
        )
        if resp.status_code == 200:
            resp.encoding = resp.apparent_encoding or "utf-8"
            candidate_html = resp.text
            if not is_waf_blocked(candidate_html) and len(candidate_html.strip()) > 300:
                html = candidate_html
    except Exception:
        pass

    # 2. 若 requests 未取得有效內容或需要客戶端 JS 渲染，備援切換至 Playwright
    if not html:
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(
                    headless=True,
                    args=["--disable-blink-features=AutomationControlled", "--no-sandbox"]
                )
                page = browser.new_page(
                    user_agent=DEFAULT_BROWSER_HEADERS["User-Agent"],
                    locale="zh-TW"
                )
                page.goto(url, timeout=15000)
                page.wait_for_load_state("domcontentloaded")
                pw_html = page.content()
                browser.close()

                if not is_waf_blocked(pw_html):
                    html = pw_html
        except Exception as e:
            print(f"[web_scraper] Playwright scraping error for {url}: {e}")

    if not html or is_waf_blocked(html):
        # 若仍被阻擋或無內容，嘗試從 URL 或標頭保留基本資訊
        return result

    # 保留原始 HTML 供下游分析使用
    result["raw_html"] = html

    # Extract links BEFORE clean-up destroys <a> tags
    try:
        raw_soup = BeautifulSoup(html, "html.parser")
        for a in raw_soup.find_all('a', href=True):
            href = a['href']
            link_text = a.get_text(strip=True)
            if href.startswith('http') and link_text:
                result["links"].append({"text": link_text, "url": href})
    except Exception:
        pass

    from core.web_content_analyzer import extract_clean_text_and_tables
    page_text, soup = extract_clean_text_and_tables(html, base_url=url)
    
    # Limit total text to avoid blowing up context window
    if len(page_text) > 15000:
        page_text = page_text[:15000] + "\n...(資料過長已截斷)..."
        
    result["text"] = page_text
    return result

# Re-export form_search for backward compatibility
from tools.web_form_search import form_search


