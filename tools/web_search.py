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

def is_language_mismatch(title: str, href: str, body: str, query: str) -> bool:
    """Detects foreign language mismatch (e.g. Japanese results when querying Chinese)."""
    has_japanese_query = bool(re.search(r'[\u3040-\u309f\u30a0-\u30ff]', query))
    if not has_japanese_query:
        p = urlparse(href)
        host = p.netloc.lower()
        if host.endswith(".jp") or "ja.wikipedia.org" in host or host.endswith(".kr") or "ko.wikipedia.org" in host:
            return True
        combined = (title or "") + " " + (body or "")
        # Japanese Hiragana or Katakana count
        kana_count = len(re.findall(r'[\u3040-\u309f\u30a0-\u30ff]', combined))
        if kana_count >= 2:
            return True
    return False

def is_spam_or_irrelevant(title: str, href: str, query: str, body: str = "") -> bool:
    """Filters out commercial shopping, SEO travel clickbait, forum gossip, and unrelated spam."""
    q_lower = query.lower()

    # 1. Travel / tourism SEO clickbait filter
    travel_kws = ["景點", "旅遊", "自由行", "住宿", "飯店", "必吃", "美食", "餐廳", "門票", "伴手禮", "機票", "玩", "吃", "行程"]
    if not any(k in q_lower for k in travel_kws):
        if re.search(r"(必去景點|自由行攻略|住宿推薦|必吃美食|行程規劃|景點推薦|優惠門票|包車旅遊|必去|打卡景點|伴手禮|一日遊|必買)", title):
            return True
        if re.search(r"【\d{4}.*(景點|旅遊|自由行|住宿|美食).*】", title):
            return True

    # 2. Shopping & e-commerce spam filter
    shop_kws = ["買", "購物", "價格", "特價", "商城", "比價", "二手", "代購", "拍賣"]
    if not any(k in q_lower for k in shop_kws):
        shop_domains = ["shopee.tw", "momo.com.tw", "pchome.com.tw", "rakuten.com.tw", "coupang.com", "ruten.com.tw", "carousell.", "findprice."]
        if any(d in href.lower() for d in shop_domains):
            return True
        if re.search(r"(熱銷推薦|限時下殺|免運優惠|折價券|比價網|促銷特價|線上購買|分期零利率)", title):
            return True

    # 3. Finance/stock forum spam
    finance_keywords = ["股票", "股價", "股市", "大盤", "基金", "期貨", "台積電", "009", "2330", "投資", "除息", "財經"]
    if not any(k in q_lower for k in finance_keywords):
        if any(d in href.lower() for d in ["cmoney.tw/forum", "goodinfo.tw", "cnyes.com", "wantgoo.com", "moneydj.com"]):
            return True
        if re.search(r"(\(\d{4}\)|\d{4}\s*今日股價|股市爆料同學會|走勢與討論)", title):
            return True

    # 4. Windows/OS help spam
    if not any(k in q_lower for k in ["windows", "檔案總管", "微軟", "作業系統", "wifi", "熱點"]):
        if "support.microsoft.com" in href.lower() and any(w in title for w in ["檔案總管", "行動熱點"]):
            return True

    # 5. Low-quality scraper / adult / gambling spam
    if re.search(r"(娛樂城|博弈|線上百家樂|六合彩|色情|成人影片|番號|高清線上看|免費看片)", title):
        return True

    return False

def calculate_relevance_score(query: str, title: str, href: str, body: str) -> float:
    """Calculates semantic relevance score between user query and search result."""
    q_norm = normalize_chinese_variants(to_traditional_chinese(query)).strip()
    t_norm = normalize_chinese_variants(to_traditional_chinese(title)).strip()
    b_norm = normalize_chinese_variants(to_traditional_chinese(body)).strip()
    h_norm = href.lower()

    score = 0.0

    # 1. Exact query match in title
    if q_norm.lower() in t_norm.lower():
        score += 50.0
    elif any(token in t_norm for token in q_norm.split() if len(token) >= 2):
        score += 30.0

    # 2. Token overlap analysis
    subtokens = set()
    cleaned_q = re.sub(r"[\s\-_/\\()（）\[\]【】\.,:;!！?？]+", " ", q_norm)
    for part in cleaned_q.split():
        if len(part) >= 2:
            subtokens.add(part)
        if len(part) > 2:
            for i in range(len(part) - 1):
                subtokens.add(part[i:i+2])

    matched_tokens = 0
    total_tokens = len(subtokens) if subtokens else 1
    for tok in subtokens:
        if tok in t_norm:
            score += 15.0
            matched_tokens += 1
        elif tok in b_norm:
            score += 5.0
            matched_tokens += 0.5

    coverage = matched_tokens / max(1, total_tokens)
    score += coverage * 25.0

    # 3. Domain authority bonus
    if any(gov in h_norm for gov in [".gov.tw", "dgbas.gov.tw", "stat.gov.tw", "ris.gov.tw", "moi.gov.tw", "mac.gov.tw"]):
        score += 20.0
    elif ".edu.tw" in h_norm or "ac.uk" in h_norm:
        score += 15.0
    elif "wikipedia.org" in h_norm:
        score += 12.0
    elif any(media in h_norm for media in ["cna.com.tw", "bbc.com", "udn.com", "ltn.com.tw", "storm.mg"]):
        score += 10.0

    # 4. Penalty for very low token match on complex queries
    if len(q_norm) >= 4 and coverage < 0.2:
        score -= 40.0

    return score

def to_traditional_chinese(text: str) -> str:
    try:
        from opencc import OpenCC
        return OpenCC('s2twp').convert(text)
    except Exception:
        return text

def normalize_chinese_variants(text: str) -> str:
    """Normalizes common Chinese character variants across Taiwan/HK/PRC."""
    t = to_traditional_chinese(text or "")
    variants = [
        ("迴", "回"),
        ("臺", "台"),
        ("裏", "裡"),
        ("鑒", "鑑"),
        ("沈", "沉"),
        ("羣", "群"),
        ("滙", "匯"),
        ("綫", "線"),
        ("鍊", "鏈"),
    ]
    for old, new in variants:
        t = t.replace(old, new)
    return t

def canonicalize_url(url: str) -> str:
    """
    Normalizes a URL to a canonical representation for cross-language deduplication:
    - Normalizes scheme (http -> https)
    - Strips 'www.' and mobile prefixes (e.g. 'm.', 'zh.m.')
    - Decodes percent-encoded characters (unquote) and converts to Traditional Chinese
    - Normalizes language paths for Wikipedia, BBC, NYTimes, DW, RFI, etc.
    - Strips tracking / analytics / language variant query parameters
    - Removes trailing slashes and fragment identifiers
    """
    if not url:
        return ""
    try:
        from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode, unquote
        p = urlparse(url.strip())
        scheme = "https" if p.scheme in ("http", "https") else p.scheme
        netloc = p.netloc.lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]
        
        # Mobile Wikipedia / site normalization
        netloc = re.sub(r"^([a-z0-9\-]+)\.m\.", r"\1.", netloc)
        if netloc.endswith("m.wikipedia.org"):
            netloc = "zh.wikipedia.org"
        if "wikipedia.org" in netloc and (netloc.startswith("zh-yue.") or netloc.startswith("zh-classical.")):
            netloc = "zh.wikipedia.org"

        raw_path = unquote(p.path).rstrip("/")
        # Path language normalization
        if "wikipedia.org" in netloc:
            raw_path = re.sub(r"^/(zh-cn|zh-tw|zh-hk|zh-sg|zh-mo|zh-hans|zh-hant|zh|wiki)/", "/zh-tw/", raw_path)
        elif "bbc.com" in netloc or "bbc.co.uk" in netloc:
            raw_path = re.sub(r"/zhongwen/(simp|trad)/", "/zhongwen/trad/", raw_path)
        elif "nytimes.com" in netloc:
            raw_path = re.sub(r"/(zh-hans|zh-hant)/", "/zh-hant/", raw_path)
        elif "rfi.fr" in netloc:
            raw_path = re.sub(r"^/(cn|tw)/", "/tw/", raw_path)
        
        path = normalize_chinese_variants(raw_path)

        filtered_params = []
        for k, v in parse_qsl(p.query):
            k_lower = k.lower()
            if k_lower.startswith("utm_") or k_lower in ("fbclid", "ref", "source", "from", "spm", "variant", "lang", "setlang", "form", "cvid"):
                continue
            v_norm = normalize_chinese_variants(unquote(v))
            filtered_params.append((k_lower, v_norm))

        filtered_params.sort(key=lambda x: x[0])
        query = urlencode(filtered_params)
        return urlunparse((scheme, netloc, path, "", query, ""))
    except Exception:
        return url.strip().rstrip("/")

def canonical_title_key(title: str) -> str:
    """
    Normalizes title to a unified Traditional Chinese canonical key for cross-language deduplication:
    - Converts Simplified Chinese to Traditional Chinese
    - Normalizes Chinese character variants
    - Strips leading numbering (e.g. '1. ', '01 - ')
    - Strips common media/portal/encyclopedia site suffixes and branding
    - Strips all punctuation, whitespace, brackets, symbols
    """
    t = normalize_chinese_variants(title or "")
    t = re.sub(r"^[\d#]+[\.\、\s\-]+", "", t).strip()
    t = re.sub(r"[\s\-_|—–·]+(維基百科[，,\s]*自由的百科全書|維基百科[，,\s]*自由嘅百科全書|維基百科|自由的百科全書|自由嘅百科全書|bbc\s*news\s*中文|bbc\s*中文[網网]?|yahoo\s*新聞|newtalk\s*新聞|中時新聞網|聯合新聞網|自由時報|中央社\s*cna|ettoday\s*新聞雲|風傳媒|tvbs\s*新聞網|中華民國統計資訊網|百度百科|官方網站|首頁).*$", "", t, flags=re.IGNORECASE)
    t = re.sub(r"[\s\-_·|—–/\\()（）\[\]【】\.,:;!！?？'\"“”‘’`~]+", "", t.lower())
    return t

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
                            and not is_language_mismatch(title, link, body, query)
                            and not is_spam_or_irrelevant(title, link, query, body=body)):
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
        r = client.post("https://html.duckduckgo.com/html/", data={"q": query, "kl": "tw-tz"})
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
                        and not is_language_mismatch(title, link, body, query)
                        and not is_spam_or_irrelevant(title, link, query, body=body)):
                    if link not in seen_links:
                        seen_links.add(link)
                        results.append({"title": title, "href": link, "body": body})
                        if len(results) >= max_results:
                            break
        return results
    except Exception as e:
        print(f"DDG search error for '{query}': {e}")
        return []

def _filter_and_score_result(r: dict, query: str, seen_urls: set, seen_titles: set) -> dict | None:
    """Filters out irrelevant/spam results and assigns semantic relevance score for ranking."""
    r_title = to_traditional_chinese(r.get("title", ""))
    r_body = to_traditional_chinese(r.get("body", ""))
    r_href = r.get("href", "")
    r["title"] = r_title
    r["body"] = r_body

    # 1. 字典/字義網站過濾
    if is_dictionary_result(r_title, r_href, query):
        return None

    # 2. 異國語言不相符（例如搜尋中文卻傳回日文/韓文官網）
    if is_language_mismatch(r_title, r_href, r_body, query):
        return None

    # 3. 商業購物、旅遊景點點擊農場、論壇灌水等無關垃圾過濾
    if is_spam_or_irrelevant(r_title, r_href, query, body=r_body):
        return None

    # 4. 關鍵字語意關聯性門檻評分
    rel_score = calculate_relevance_score(query, r_title, r_href, r_body)
    if rel_score < 10.0:
        return None

    # 5. 跨語系、跨網址標準化去重
    can_url = canonicalize_url(r_href)
    can_title = canonical_title_key(r_title)

    if can_url in seen_urls:
        return None
    if can_title and len(can_title) >= 4 and can_title in seen_titles:
        return None

    seen_urls.add(can_url)
    if can_title and len(can_title) >= 4:
        seen_titles.add(can_title)

    r["_score"] = rel_score
    return r

def search_bing(query: str, max_results: int = 5) -> list:
    """
    Smart multi-candidate Bing search with strict cross-lingual and canonical URL deduplication.
    """
    candidates = generate_search_candidates(query)
    all_results = []
    seen_urls = set()
    seen_titles = set()

    for cand in candidates:
        results = search_bing_raw(cand, max_results=max_results)
        for r in results:
            item = _filter_and_score_result(r, query, seen_urls, seen_titles)
            if item:
                all_results.append(item)
                if len(all_results) >= max_results:
                    break
        if len(all_results) >= max_results:
            break

    all_results.sort(key=lambda x: x.get("_score", 0), reverse=True)
    return all_results[:max_results]

def search_web(query: str, max_results: int = 5) -> list:
    """
    High-performance, reliable web search using Hybrid Cascading Search Engine:
    1. Primary: DuckDuckGo HTML via primp TLS-impersonation (bypasses bot challenges, extremely accurate)
    2. Fallback / Augment: Localized Bing search with candidate multi-query expansion
    Strictly filters out foreign language mismatch, travel/shopping SEO spam, and ranks by semantic relevance.
    """
    candidates = generate_search_candidates(query)
    all_results = []
    seen_urls = set()
    seen_titles = set()

    # 1. 優先由 DuckDuckGo 檢索
    for cand in candidates:
        results = search_ddg_raw(cand, max_results=max_results)
        for r in results:
            item = _filter_and_score_result(r, query, seen_urls, seen_titles)
            if item:
                all_results.append(item)
                if len(all_results) >= max_results:
                    break
        if len(all_results) >= max_results:
            break

    # 2. 若有效相關結果不足（例如 DDG 回傳異國語言或垃圾被濾除），自動向 Bing 串接補充
    if len(all_results) < max_results:
        for cand in candidates:
            results = search_bing_raw(cand, max_results=max_results)
            for r in results:
                item = _filter_and_score_result(r, query, seen_urls, seen_titles)
                if item:
                    all_results.append(item)
                    if len(all_results) >= max_results:
                        break
            if len(all_results) >= max_results:
                break

    # 依語意關聯性評分降序排列，確保最精準、最權威結果置頂
    all_results.sort(key=lambda x: x.get("_score", 0), reverse=True)
    return all_results[:max_results]
