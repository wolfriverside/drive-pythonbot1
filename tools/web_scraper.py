from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup
import re

def scrape_url(url: str) -> dict:
    """
    Visit a URL using Playwright, extract clean text and a list of all <a href> links.
    Returns:
        {"text": extracted_text, "links": [{"text": link_text, "url": link_url}, ...]}
    """
    result = {"text": "", "links": []}
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url, timeout=15000)
            # Wait for main content to load
            page.wait_for_load_state("domcontentloaded")
            html = page.content()
            browser.close()

        # Extract links BEFORE clean-up destroys <a> tags
        raw_soup = BeautifulSoup(html, "html.parser")
        for a in raw_soup.find_all('a', href=True):
            href = a['href']
            link_text = a.get_text(strip=True)
            if href.startswith('http') and link_text:
                result["links"].append({"text": link_text, "url": href})

        page_text, soup = extract_clean_text_and_tables(html, base_url=url)
        
        # Limit total text to avoid blowing up context window
        if len(page_text) > 15000:
            page_text = page_text[:15000] + "\n...(資料過長已截斷)..."
            
        result["text"] = page_text

    except Exception as e:
        print(f"Scraping error for {url}: {e}")
        
    return result

def extract_clean_text_and_tables(html: str, base_url: str = "") -> str:
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

    text = soup.get_text(separator=' ')
    text = re.sub(r'\s+', ' ', text).strip()
    
    if tables_md:
        text += "\n\n### 表格資料 ###\n" + "\n\n".join(tables_md)
        
    return text, soup

def to_traditional_chinese(text: str) -> str:
    try:
        from opencc import OpenCC
        return OpenCC('s2twp').convert(text)
    except Exception:
        return text

# Re-export form_search for backward compatibility
from tools.web_form_search import form_search

