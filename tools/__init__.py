"""
LearnPythonBot - Tools & Skills Registry
本模組為系統所有 AI 技能 (Skills) 與調用工具 (Tools) 的核心註冊表與單一真實來源 (Single Source of Truth)。
所有 AI 可自主調用的工具檔案均真實存放於 tools/ 目錄中。
"""

import json
from typing import List, Dict, Any, Callable

# 匯入 tools 資料夾下所有真實存在的工具函式
from tools.web_search import search_web
from tools.web_scraper import scrape_url
from tools.web_form_search import form_search
from tools.file_reader import read_local_file
from tools.suggest_add_source import suggest_and_add_source



# ── AI 技能清單權威定義 (AI Skills Registry) ──
AI_SKILLS_REGISTRY: List[Dict[str, Any]] = [
    {
        "name": "web_search",
        "friendly_name": "網路即時搜尋 (web_search)",
        "file": "tools/web_search.py",
        "short_desc": "搜尋網路上的最新資訊、地理位置、店家美食、法規或生活實用資訊。",
        "description": (
            "搜尋網路上的最新資訊、地理位置、店家美食、法規或生活實用資訊。\n"
            "【重要搜尋原則】：\n"
            "1. 嚴禁直接將整句口語或第一人稱代詞（例如『幫我找一下 我附近下午有什麼吃的』或『我在慈濟大學附近』）傳入 query！\n"
            "2. 必須融合先前對話的上下文實體（例如使用者前面說了『我在慈濟大學附近』，後續問『下午有什麼吃的』，你必須在 query 中組合出『花蓮 慈濟大學 下午茶 美食』進行搜尋）！\n"
            "3. 關鍵字必須精簡為 2~4 個核心實體與目標詞彙（例如『花蓮 慈濟大學 附近 美食』）。\n"
            "4. 若針對特定機關/學校網站檢索，請在 query 中加入 'site:網址'。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "重構後的精準繁體搜尋關鍵字（例如：花蓮 慈濟大學 附近 美食）"
                }
            },
            "required": ["query"]
        },
        "handler": search_web
    },
    {
        "name": "web_scraper",
        "friendly_name": "網頁深度擷取 (web_scraper)",
        "file": "tools/web_scraper.py",
        "short_desc": "讀取指定網址的內容，提取純文字、表格與内部超連結，並自動識別頁面上的 PDF/DOCX 等可下載附件連結。",
        "description": (
            "深度讀取指定網址的完整內容，提取純文字、表格，並解析頁面上所有的超連結。\n"
            "會自動分類出可下載附件（PDF、DOCX、XLSX 等）與其他頁面連結。\n"
            "回傳結果中的 downloadable_files 欄位列出所有發現的附件，\n"
            "可進一步使用 read_document 工具讀取附件內容。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "要抓取的網頁 URL"
                }
            },
            "required": ["url"]
        },
        "handler": scrape_url
    },
    {
        "name": "web_form_search",
        "friendly_name": "站內搜尋表單 (web_form_search)",
        "file": "tools/web_form_search.py",
        "short_desc": "自動造訪指定檢索網頁，填寫搜尋關鍵字並送出以獲取站內查詢結果。",
        "description": "當使用者指定要到「某個特定的檢索網頁」填寫關鍵字進行站內搜尋時使用。會自動填寫網頁上的搜尋欄並送出。",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "指定的檢索網頁 URL (例如：https://admin.tcu.edu.tw/...)"
                },
                "keyword": {
                    "type": "string",
                    "description": "要填入搜尋欄的關鍵字"
                }
            },
            "required": ["url", "keyword"]
        },
        "handler": form_search
    },
    {
        "name": "read_document",
        "friendly_name": "文獻閱讀與語意檢索 (read_document)",
        "file": "tools/file_reader.py",
        "short_desc": "讀取本地或遠端多格式檔案 (PDF/TXT/DOCX/CSV)，並結合 Embedding 語意向量抽取重點。",
        "description": "讀取本地檔案或網址檔案（如 PDF, TXT, DOCX, CSV）的內文。若文件龐大或想查詢特定主題（如『保險給付標準』、『退費辦法』），可在 query 填寫問題，系統會自動使用 Embedding 語意向量檢索抽出最關鍵的內文段落。",
        "parameters": {
            "type": "object",
            "properties": {
                "filepath_or_url": {
                    "type": "string",
                    "description": "要讀取的檔案絕對路徑或網址 (如 https://...)"
                },
                "query": {
                    "type": "string",
                    "description": "選填。要在此文件中查詢的主題或關鍵問題，系統將以向量嵌入技術比對出最相關的章節段落"
                }
            },
            "required": ["filepath_or_url"]
        },
        "handler": read_local_file
    },
    {
        "name": "suggest_add_source",
        "friendly_name": "知識來源收錄 (suggest_add_source)",
        "file": "tools/suggest_add_source.py",
        "short_desc": "將網址或文獻自動下載、切片分塊並建立向量索引加入使用者的長期知識來源庫。",
        "description": "當使用者要求將某個網址或主題資料加入知識庫，或者你認為某個網址/文件對使用者有長期參考價值時調用此工具。系統會自動下載、切片並建立索引至使用者的「知識來源庫」。",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "要加入知識來源庫的公開網址（http/https）或本地檔案路徑"
                },
                "reason": {
                    "type": "string",
                    "description": "加入知識庫的理由或內容簡述"
                },
                "label": {
                    "type": "string",
                    "description": "選填。知識來源的自訂標題名稱"
                }
            },
            "required": ["url", "reason"]
        },
        "handler": suggest_and_add_source
    }
]


def get_all_tool_schemas() -> List[Dict[str, Any]]:
    """
    提供給 BotEngine 與 Ollama Function Calling 的標準工具清單。
    格式符合 OpenAI / Ollama tools 定義。
    """
    schemas = []
    for skill in AI_SKILLS_REGISTRY:
        schemas.append({
            "type": "function",
            "function": {
                "name": skill["name"],
                "description": skill["description"],
                "parameters": skill["parameters"]
            }
        })
    return schemas


def get_skills_display_info() -> List[Dict[str, Any]]:
    """
    提供給前端 UI (app.py) 渲染「AI 技能清單 (Skills)」摺疊面板的結構化資訊。
    包含名稱、原名、檔案路徑、簡介、詳細規範與參數詳細說明。
    """
    display_list = []
    for skill in AI_SKILLS_REGISTRY:
        params_props = skill.get("parameters", {}).get("properties", {})
        required_list = skill.get("parameters", {}).get("required", [])
        params_list = []
        for p_name, p_info in params_props.items():
            params_list.append({
                "name": p_name,
                "type": p_info.get("type", "string"),
                "required": p_name in required_list,
                "desc": p_info.get("description", "")
            })

        display_list.append({
            "name": skill["friendly_name"],
            "raw_name": skill["name"],
            "file": skill["file"],
            "desc": skill["short_desc"],
            "full_desc": skill.get("description", ""),
            "parameters": params_list
        })
    return display_list



def get_skill_by_name(name: str) -> Dict[str, Any]:
    """根據技能名稱查詢對應的技能定義"""
    for skill in AI_SKILLS_REGISTRY:
        if skill["name"] == name:
            return skill
    return {}


__all__ = [
    "AI_SKILLS_REGISTRY",
    "get_all_tool_schemas",
    "get_skills_display_info",
    "get_skill_by_name",
    "search_web",
    "scrape_url",
    "form_search",
    "read_local_file",
    "suggest_and_add_source",
]

