import base64
import re
import requests
from bs4 import BeautifulSoup
from urllib.parse import urlparse, parse_qs, unquote
import urllib.parse

def decode_bing_url(url: str) -> str:
    """Decodes Bing redirect tracking links (bing.com/ck/a) into the original destination URL."""
    if "bing.com/ck/a" in url:
        try:
            parsed = urlparse(url)
            params = parse_qs(parsed.query)
            if "u" in params:
                u_val = params["u"][0]
                if u_val.startswith("a1"):
                    raw = u_val[2:]
                    raw += "=" * (-len(raw) % 4)
                    return base64.urlsafe_b64decode(raw).decode("utf-8", errors="ignore")
        except Exception:
            pass
    return url

def is_dictionary_result(title: str, href: str, query: str) -> bool:
    """Filters out dictionary definitions, character strokes, or language learning portals."""
    if any(k in query for k in ["字典", "辭典", "注音", "拼音", "筆畫", "部首", "意思", "字義"]):
        return False
    dict_patterns = [
        "dict.", "cidian", "zidian", "stroke-order", "chinesewords",
        "18dao.net", "zdic.net", "baike.baidu.com", "hanyuguoxue.com",
        "variants.moe.edu.tw", "dacidian"
    ]
    if any(p in href.lower() for p in dict_patterns):
        return True
    if re.search(r"(漢字|注音|部首|筆畫|筆順|辭典|字典|異體字|讀音|釋義)", title):
        return True
    if "global.baidu.com" in href or "baidu.com" in href:
        if "百度" not in query:
            return True
    return False

def is_spam_or_irrelevant(title: str, href: str, query: str) -> bool:
    """Filters out unrelated stock forum posts, OS help, or shopping spam unless queried."""
    # Finance/stock forum spam
    finance_keywords = ["股票", "股價", "股市", "大盤", "基金", "期貨", "台積電", "009", "2330", "投資", "除息", "財經"]
    if not any(k in query for k in finance_keywords):
        if any(d in href for d in ["cmoney.tw/forum", "goodinfo.tw", "cnyes.com", "wantgoo.com", "moneydj.com"]):
            return True
        if re.search(r"(\(\d{4}\)|\d{4}\s*今日股價|股市爆料同學會|走勢與討論)", title):
            return True
    # Windows/OS help spam
    if not any(k in query.lower() for k in ["windows", "檔案總管", "微軟", "作業系統", "wifi", "熱點"]):
        if "support.microsoft.com" in href and any(w in title for w in ["檔案總管", "行動熱點"]):
            return True
    return False

def to_traditional_chinese(text: str) -> str:
    try:
        from opencc import OpenCC
        return OpenCC('s2twp').convert(text)
    except Exception:
        return text

def generate_search_candidates(query: str) -> list:
    """
    Expands a raw query into prioritized, clean candidates to maximize search engine accuracy.
    Removes conversational prefixes/suffixes, maps legal terminology, converts life intents,
    and anchors Taiwanese locations properly.
    """
    q0 = to_traditional_chinese(query.strip())
    
    legal_map = {
        "跟騷": "跟蹤騷擾防制法",
        "跟蹤騷擾": "跟蹤騷擾防制法",
        "性騷擾": "性騷擾防治法",
        "性平": "性別平等工作法",
        "個資": "個人資料保護法",
        "勞基": "勞動基準法",
        "颱風假": "天然災害停止上班及上課作業辦法",
        "停班停課": "天然災害停止上班及上課作業辦法"
    }

    candidates = []

    # Handle site: search syntax fallback candidates
    if "site:" in q0:
        candidates.append(q0)
        m = re.search(r"site:([^\s]+)\s*(.*)", q0)
        if m:
            domain, term = m.group(1), m.group(2)
            candidates.append(f"{domain} {term}".strip())
            # Common domain-to-name expansion
            if "tcu.edu.tw" in domain:
                candidates.append(f"花蓮 慈濟大學 {term}".strip())
            elif "ntu.edu.tw" in domain:
                candidates.append(f"台灣大學 {term}".strip())
            elif "nthu.edu.tw" in domain:
                candidates.append(f"新竹 清華大學 {term}".strip())
            elif "moj.gov.tw" in domain:
                candidates.append(f"全國法規資料庫 {term}".strip())

    # Map core legal/government concepts if present
    for k, v in legal_map.items():
        if k in q0 and v not in candidates:
            candidates.append(v)

    # 1. Aggressively strip conversational prefixes (longest match first)
    prefix_pat = r"^(想請問(一下)?|請問(一下)?|請幫我(查詢?|找尋?|搜尋?|推薦|找|查|看)?(一下)?|幫我(查詢?|找尋?|搜尋?|推薦|找|查|看)?(一下)?|請(查詢?|找尋?|搜尋?|推薦|找|查|看)?(一下)?|查一下|找一下|看一下|我想知道|我想了解|我想吃|我想找|有沒有推薦|有沒有|給我|請給我|我在)\s*"
    c1 = re.sub(prefix_pat, "", q0).strip()

    # 2. Strip conversational question endings and filler suffixes
    suffix_pat = r"(是什麼(呢|啊)?|是甚麼(呢|啊)?|有哪些|怎麼辦|怎麼做|要怎麼做|要怎麼申請|該如何|如何辦理|的資料|的資訊|相關資料|相關資訊|資料|資訊|相關|官方|最新)+$"
    c2 = re.sub(suffix_pat, "", c1).strip()
    c2 = re.sub(r"[的之了]+$", "", c2).strip()

    # 3. Transform daily life / location / food intents
    c_intent = c2
    c_intent = re.sub(r"我附近", "附近", c_intent)
    c_intent = re.sub(r"下午有什麼吃的|下午吃什麼|下午吃的", "下午茶 點心 美食", c_intent)
    c_intent = re.sub(r"有什麼吃的|吃什麼|有什麼好吃的|推薦吃的", "美食 推薦 小吃", c_intent)
    c_intent = re.sub(r"有什麼好玩的|去哪玩|哪裡好玩", "景點 必去 推薦", c_intent)
    c_intent = re.sub(r"\s+", " ", c_intent).strip()

    # 4. Regional campus anchors
    campus_anchors = [
        ("慈濟大學", "花蓮 慈濟大學"),
        ("慈大", "花蓮 慈濟大學"),
        ("東華大學", "花蓮 東華大學"),
        ("清華大學", "新竹 清華大學"),
        ("清大", "新竹 清華大學"),
        ("交通大學", "新竹 交通大學"),
        ("陽明交大", "新竹 陽明交大"),
        ("成功大學", "台南 成功大學"),
        ("成大", "台南 成功大學"),
        ("中山大學", "高雄 中山大學"),
        ("中正大學", "嘉義 中正大學"),
    ]
    c_anchored = c_intent
    for name, full_anchor in campus_anchors:
        if name in c_anchored and full_anchor.split()[0] not in c_anchored:
            c_anchored = c_anchored.replace(name, full_anchor)
            break

    # 5. Clean search-spoiling modifiers
    c3 = re.sub(r"(官方|資訊|資料|相關|最新|校園|請問|遇到|要怎麼|如何)", " ", c_anchored)
    c3 = re.sub(r"\s+", " ", c3).strip()

    # Prioritize: high-intent anchored queries first, cleaned queries second, raw last!
    for cand in [c_anchored, c_intent, c3, c2, c1]:
        if cand and len(cand) >= 2 and cand not in candidates:
            candidates.append(cand)

    orig_q = query.strip()
    if orig_q not in candidates:
        candidates.append(orig_q)

    return candidates

def search_bing_raw(query: str, max_results: int = 5) -> list:
    """Executes search requests to Bing and parses organic results up to max_results."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
        "Cookie": "SRCHHPGUSR=ADLT=OFF&NRSLT=10; _EDGE_S=mkt=zh-tw"
    }
    results = []
    seen_links = set()
    for page in range(1, 4):
        first = (page - 1) * 10 + 1
        try:
            params = {"q": query, "cc": "TW", "setlang": "zh-Hant", "first": first}
            if page > 1:
                params["FORM"] = "PERE"
            resp = requests.get(
                "https://www.bing.com/search",
                params=params,
                headers=headers,
                timeout=10
            )
            if resp.status_code != 200:
                break

            soup = BeautifulSoup(resp.text, "html.parser")
            items = soup.find_all("li", class_="b_algo")
            if not items:
                break

            for li in items:
                h2 = li.find("h2")
                snippet = li.find("p") or li.find(class_="b_caption") or li.find(class_="b_snippet")
                if h2 and h2.find("a"):
                    a = h2.find("a")
                    title = a.get_text(strip=True)
                    link = decode_bing_url(a.get("href", ""))
                    body = snippet.get_text(strip=True) if snippet else ""
                    if (link and not link.startswith("javascript:") 
                            and not is_dictionary_result(title, link, query)
                            and not is_spam_or_irrelevant(title, link, query)):
                        if link not in seen_links:
                            seen_links.add(link)
                            results.append({"title": title, "href": link, "body": body})
                            if len(results) >= max_results:
                                break
            if len(results) >= max_results:
                break
        except Exception as e:
            print(f"Bing search error for query '{query}': {e}")
            break

    return results

def decode_ddg_url(url: str) -> str:
    """Decodes DuckDuckGo redirection URLs (//duckduckgo.com/l/?uddg=...) into destination URLs."""
    if "uddg=" in url:
        try:
            m = re.search(r"uddg=([^&]+)", url)
            if m:
                return urllib.parse.unquote(m.group(1))
        except Exception:
            pass
    return url

def search_ddg_raw(query: str, max_results: int = 5) -> list:
    """Executes search requests to DuckDuckGo HTML using primp (bypasses bot challenges)."""
    try:
        import primp
        client = primp.Client()
        r = client.post("https://html.duckduckgo.com/html/", data={"q": query})
        if r.status_code != 200:
            return []
        
        soup = BeautifulSoup(r.text, "html.parser")
        results = []
        seen_links = set()
        for res in soup.select("div.result__body"):
            a = res.find("a", class_="result__a")
            snippet = res.find("a", class_="result__snippet")
            if a and a.get("href"):
                link = decode_ddg_url(a["href"])
                title = a.get_text(strip=True)
                body = snippet.get_text(strip=True) if snippet else ""
                if (link and not link.startswith("javascript:")
                        and not is_dictionary_result(title, link, query)
                        and not is_spam_or_irrelevant(title, link, query)):
                    if link not in seen_links:
                        seen_links.add(link)
                        results.append({"title": title, "href": link, "body": body})
                        if len(results) >= max_results:
                            break
        return results
    except Exception as e:
        print(f"DDG search error for '{query}': {e}")
        return []

def search_bing(query: str, max_results: int = 5) -> list:
    """
    Smart multi-candidate Bing search.
    Returns organic results up to max_results across candidates.
    Trusts Bing's own relevance ranking rather than post-filtering.
    """
    candidates = generate_search_candidates(query)
    all_results = []
    seen = set()

    for cand in candidates:
        results = search_bing_raw(cand, max_results=max_results)
        for r in results:
            if r["href"] not in seen:
                seen.add(r["href"])
                all_results.append(r)
                if len(all_results) >= max_results:
                    return all_results
        if len(all_results) >= max_results:
            return all_results

    return all_results

def search_web(query: str, max_results: int = 5) -> list:
    """
    High-performance, reliable web search using Hybrid Search Engine:
    1. Primary: DuckDuckGo HTML via primp TLS-impersonation (bypasses bot challenges, extremely accurate)
    2. Fallback: Localized Bing search with candidate multi-query expansion & strict anti-spam filtering
    """
    candidates = generate_search_candidates(query)
    all_results = []
    seen = set()

    # Try Primary Engine (DDG via primp)
    for cand in candidates:
        results = search_ddg_raw(cand, max_results=max_results)
        for r in results:
            if r["href"] not in seen:
                seen.add(r["href"])
                all_results.append(r)
                if len(all_results) >= max_results:
                    return all_results
        if len(all_results) >= max_results:
            return all_results

    # Fallback to Bing
    for cand in candidates:
        results = search_bing_raw(cand, max_results=max_results)
        for r in results:
            if r["href"] not in seen:
                seen.add(r["href"])
                all_results.append(r)
                if len(all_results) >= max_results:
                    return all_results

    return all_results
