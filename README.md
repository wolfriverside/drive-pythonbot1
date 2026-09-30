# LearnPythonBot

一個具備深度推理、自主探索、工具行動（Function Calling）與多來源知識檢索能力的頂尖 AI Python 學習與在地生活智慧助理。

---

## 專案完整目錄架構 (Project Architecture)

```text
learnpythonbot/
├── app.py                          # Web 應用主程式（Gradio 現代化白底極簡介面）
├── requirements.txt                # Python 依賴套件清單
├── start.bat                       # Windows 一鍵環境健康檢查與自動啟動腳本
├── README.md                       # 專案完整說明文件
│
├── tools/                          # AI 技能工具庫與清單註冊表（Single Source of Truth）
│   ├── __init__.py                 # AI 技能清單權威註冊表（統一管理模型 Tool Schemas 與前端技能清單資訊）
│   ├── web_search.py               # 網路即時搜尋（DuckDuckGo TLS 偽裝 ＋ Bing 雙引擎，具垃圾廣告與辭典字義過濾）
│   ├── web_scraper.py              # 網頁全文深度爬取與結構化內容（表格 Markdown）提取
│   ├── web_form_search.py          # 網站查詢表單自動探索、填入關鍵字與多頁檢索
│   ├── file_reader.py              # 多格式文件讀取（支援 PDF、TXT、CSV、DOCX、JSON，支援遠端暫存解析）
│   └── suggest_add_source.py       # 知識來源收錄（自動切片、向量嵌入並持久化至個人知識庫）
│
├── core/                           # 系統核心服務與邏輯層
│   ├── bot_engine.py               # 對話推理引擎（動態 Prompt、工具調度、串流思考與 RAG 意圖融合）
│   ├── security.py                 # 智慧安全審查系統（防注入攻擊、放行合法公眾資訊）
│   ├── embedding.py                # 向量嵌入與語意相似度比對（nomic-embed-text）
│   ├── source_manager.py           # 知識來源庫管理（使用者上傳文獻、網路資料解析分塊與高信度 RAG 檢索）
│   ├── memory_manager.py           # 使用者個人長期記憶管理（點讚記錄與動態 Prompt 注入）
│   └── health_check.py             # Ollama 本機服務心跳與雙模型就緒狀態自動檢測
│
├── database/                       # 系統配置與使用者數據持久層
│   ├── built_in_db.json            # 系統核心配置（主模型名稱、決策流程圖、白名單約束）
│   ├── suspicious.log              # 惡意攻擊與可疑行為安全攔截日誌
│   └── users/                      # 使用者獨立資料夾（依 uid 隔離）
│       └── <uid>/
│           ├── profile.json        # 對話歷史紀錄與活躍時間
│           ├── sources/            # 使用者專屬知識來源分塊與向量索引檔案
│           └── uploads/            # 使用者上傳之原始文件存放區
│
├── cache/                          # 系統快取與暫存目錄
│   └── downloads/                  # 線上文獻暫存與即時解析目錄（檔案解析分塊完畢後自動清理）
│
└── tests/                          # 專案自動化測試套件
    └── test_system.py              # 系統核心、健康檢測、工具檔案與清單註冊表單元測試
```

---

## AI 技能工具與技能清單機制 (`tools/`)

系統秉持 **「單一真實來源 (Single Source of Truth)」** 原則，所有 AI 可自主調用的工具檔案與前端顯示的「AI 技能清單」均真實存在於 [`tools/`](tools/) 目錄中：

| 技能名稱 (Skill Name) | 實現檔案 | 職責與特性 |
| :--- | :--- | :--- |
| **`web_search`**<br>網路即時搜尋 | [`tools/web_search.py`](tools/web_search.py) | • 結合 DuckDuckGo (透過 `primp` 瀏覽器指紋偽裝繞過反爬) ＋ 台灣在地化 Bing 雙引擎。<br>• 內建在地實體錨定（如慈濟大學自動錨定花蓮）與日常飲食意圖重構。<br>• 嚴格過濾字典筆畫、股市同學會廣告與作業系統說明，杜絕搜尋失真。 |
| **`web_scraper`**<br>網頁內文擷取 | [`tools/web_scraper.py`](tools/web_scraper.py) | • 使用 Playwright 無頭瀏覽器抓取動態網頁。<br>• 自動將 HTML 表格轉為結構化 Markdown 表格輸出，保留完整文字與內部連結。 |
| **`web_form_search`**<br>站內搜尋表單 | [`tools/web_form_search.py`](tools/web_form_search.py) | • 針對學校、政府或機構的特定檢索頁面，智慧探索搜尋輸入框。<br>• 自動填入關鍵字並模擬點擊送出，支援至多 3 頁結果翻頁擷取。 |
| **`read_document`**<br>文獻閱讀與語意檢索 | [`tools/file_reader.py`](tools/file_reader.py) | • 支援 PDF、Word (DOCX)、純文字 (TXT)、CSV 與 JSON 等檔案讀取。<br>• 支援直接讀取線上檔案網址；若文件篇幅龐大，自動調用 Embedding 向量語意檢索篩選出關鍵段落。 |
| **`suggest_add_source`**<br>知識來源收錄 | [`tools/suggest_add_source.py`](tools/suggest_add_source.py) | • 當使用者要求收錄特定網址，或 AI 判定具有長期參考價值時調用。<br>• 自動調用爬蟲抓取、分塊、向量計算並建立索引至使用者的「知識來源庫」。 |

### 技能清單註冊表 (`tools/__init__.py`)
- **雙向同步**：定義權威清單 `AI_SKILLS_REGISTRY`，同時透過 `get_all_tool_schemas()` 提供給模型作為 Function Calling 工具定義，以及透過 `get_skills_display_info()` 提供給前端 Web 介面動態渲染。
- **免重複配置**：新增或修改任何工具時，僅需在 `tools/` 資料夾下擴充，前端與模型端即自動同步，避免硬編碼不一致問題。

---

## 核心系統運作機制 (`core/`)

1. **高信度 RAG 檢索防污染 (`core/source_manager.py`)**：
   - 設定相似度過濾門檻（`min_score = 0.65`）。
   - 當使用者提出日常問題（如打招呼、問附近美食）時，不符合門檻的歷史文獻不會被強行檢索注入，杜絕無關文件偏誤污染 System Prompt。
2. **語意意圖融合與上下文重構 (`core/bot_engine.py`)**：
   - 工具呼叫時主動融合歷史對話實體（例如先前提到「慈濟大學」，接著問「下午有什麼吃的」，自動重構為「花蓮 慈濟大學 下午茶 美食」），不再直接送出口語代詞。
3. **向量嵌入與比對 (`core/embedding.py`)**：
   - 使用 Ollama 本地運行的 `nomic-embed-text` 模型，以 768 維密集向量進行文字分塊與餘弦相似度計算。
4. **Ollama 連線健康檢查 (`core/health_check.py`)**：
   - 即時檢測 Ollama 服務埠號是否連通、主模型（`qwen3`）與嵌入模型（`nomic-embed-text`）是否已就緒。
5. **智慧安全審查 (`core/security.py`)**：
   - 防止 Prompt Injection 提示詞注入、SQL 惡意語法與系統命令執行，維護對話與系統安全。

---

## 暫存快取管理 (`cache/downloads/`)

- **功能定義**：專供 [`tools/file_reader.py`](tools/file_reader.py) 與 [`core/source_manager.py`](core/source_manager.py) 進行遠端線上檔案（如 PDF、DOCX）解析時的臨時暫存區。
- **安全與生命週期**：
  1. 當使用者新增線上檔案網址時，檔案暫存至此目錄。
  2. 提取內文文字並計算向量完成後，程式於 `finally` 區塊中**自動執行物理刪除**。
  3. 平時該目錄保持乾淨狀態（內含 `README.txt` 以維持 Git 目錄完整）。

---

## 快速啟動指南 (Getting Started)

### 步驟 1：安裝與啟動 Ollama
1. 前往 [Ollama 官方網站 (https://ollama.com)](https://ollama.com) 下載並安裝。
2. 啟動 Ollama 服務，並在終端機拉取所需模型：
   ```bash
   ollama pull qwen3
   ollama pull nomic-embed-text
   ```

### 步驟 2：安裝 Python 依賴套件
建議使用 Python 3.10 以上環境：
```bash
pip install -r requirements.txt
playwright install chromium
```

### 步驟 3：啟動系統
- **Windows 使用者**：直接雙擊執行根目錄下的 [`start.bat`](start.bat)，將自動執行環境檢查、依賴驗證、Ollama 模型狀態檢測並開啟應用程式。
- **跨平台命令列啟動**：
  ```bash
  python app.py
  ```
啟動後使用瀏覽器開啟 `http://127.0.0.1:7860/` 即可進入使用介面。

---

## 現代化介面亮點 (UI Features)

1. **白底黑灰極簡設計**：全介面以高雅簡約的黑白灰三色為主軸，輔以暖橘色作為操作亮點。
2. **向上覆蓋自適應輸入欄**：
   - 預設固定高度於對話框底部，送出按鈕常駐右側。
   - 支援 `Shift + Enter` 多行輸入，隨輸入字數增加平滑**向上遮蔽對話框展開**（上限 180px），超出後啟動內部滾輪。
3. **AI 技能清單懸浮覆蓋層**：
   - 點擊展開時以浮動圖層（Floating Overlay）向上浮現，直接覆蓋於底下的日誌之上，**不擠壓整體頁面高度排版**。
4. **知識來源庫智慧單行與懸浮 Tooltip**：
   - 來源卡片維持單行簡潔佈局，檔名過長時自動以省略號截斷，滑鼠懸浮時呈現完整檔名 Tooltip。
5. **即時日誌自適應展示**：
   - 提供 100px ~ 240px 彈性自適應高度，並具備即時串流輸出追蹤。

---

## 執行自動化測試

本專案提供標準單元測試套件，涵蓋設定讀取、Ollama 連線健康檢查、AI 技能註冊表與實體檔案對齊、安全審查及 UI 元件生成：

```bash
python -m unittest tests/test_system.py
```
全部 7 項測試將自動執行並輸出驗證結果。
