from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup
import re
from core.web_content_analyzer import extract_clean_text_and_tables
from tools.web_search import to_traditional_chinese


def form_search(url: str, keyword: str, max_pages: int = 3) -> dict:
    """
    造訪指定網址，尋找搜尋輸入框並填入關鍵字，模擬送出並抓取結果。
    支援多頁翻頁（至多 max_pages 頁），並將表格轉化為 Markdown 格式輸出。

    參數:
        url: 指定的檢索網頁 URL (例如：https://admin.tcu.edu.tw/...)
        keyword: 要填入搜尋欄的關鍵字
        max_pages: 最大抓取頁數（預設 3 頁）
    回傳:
        {"text": extracted_text, "links": [{"text": link_text, "url": link_url}, ...], "pages_scraped": count}
    """
    keyword = to_traditional_chinese(keyword)
    result = {"text": "", "links": [], "pages_scraped": 0}
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url, timeout=15000)
            page.wait_for_load_state("domcontentloaded")

            # 智慧探索定位搜尋輸入欄
            search_input = None
            if page.locator('input[type="search"]').first.is_visible():
                search_input = page.locator('input[type="search"]').first

            if not search_input:
                for attr in ["name", "id", "placeholder", "class"]:
                    for term in [
                        "search", "keyword", "query", "q", "subj", "title",
                        "name", "名稱", "關鍵字"
                    ]:
                        loc = page.locator(
                            f'input[type="text"][{attr}*="{term}" i]').first
                        if loc.is_visible():
                            search_input = loc
                            break
                    if search_input:
                        break

            if not search_input:
                search_input = page.locator('input[type="text"]').first

            if search_input.is_visible():
                search_input.fill(keyword)
                search_input.press("Enter")

                # 嘗試點擊送出/搜尋按鈕以確保查詢觸發
                submit_btn = page.locator(
                    'input[type="button"][value*="搜尋" i], input[type="button"][value*="查詢" i], input[type="submit"], button[type="submit"], button:has-text("搜尋")'
                ).first
                if submit_btn.is_visible():
                    submit_btn.click()

                try:
                    page.wait_for_load_state("networkidle", timeout=10000)
                except Exception:
                    pass
                page.wait_for_timeout(2000)

            all_text = ""
            for i in range(max_pages):
                html = page.content()
                page_text, soup = extract_clean_text_and_tables(
                    html, base_url=url)
                all_text += f"\n\n--- 第 {i+1} 頁 ---\n" + page_text
                result["pages_scraped"] += 1

                if i == 0:
                    for a in soup.find_all("a", href=True):
                        href = a["href"]
                        link_text = a.get_text(strip=True)
                        if href.startswith("http") and link_text:
                            result["links"].append(
                                {"text": link_text, "url": href})

                # 偵測「下一頁」翻頁按鈕
                next_btn = page.locator(
                    'a:has-text("下一頁"), button:has-text("下一頁"), a:has-text("Next"), button:has-text("Next")'
                ).first
                if next_btn.is_visible():
                    next_btn.click()
                    try:
                        page.wait_for_load_state("networkidle", timeout=10000)
                    except Exception:
                        pass
                    page.wait_for_timeout(2000)
                else:
                    break

            browser.close()

            # 限制文字長度防止炸 context window
            if len(all_text) > 15000:
                all_text = all_text[:15000] + "\n...(資料過長已截斷)..."

            result["text"] = all_text.strip()

    except Exception as e:
        print(f"Form search error for {url}: {e}")

    return result
