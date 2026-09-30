import json
import os
import time
import ollama

class BotEngine:
    def __init__(self):
        # Load configs
        self.config_path = os.path.join("database", "built_in_db.json")
        self.config = self._load_config()
        self.model_name = self.config.get("model_name", "qwen3")
        self.system_prompt = self.config.get("system_prompt", "你是一個專業的 Python 教學助理。")

    def _load_config(self):
        if os.path.exists(self.config_path):
            with open(self.config_path, "r", encoding="utf-8") as f:
                return json.load(f)
        return {}

    def get_tool_schemas(self):
        """
        取得系統所有已註冊的 AI 技能工具規格（來源：tools/ 目錄真實工具與清單註冊表）。
        """
        from tools import get_all_tool_schemas
        return get_all_tool_schemas()


    def generate_response_stream(self, chat_history, uid=None):
        """
        Generator that yields (log_update, is_final, text_so_far)
        Connects to Ollama using the official python client and streams the response.
        Supports tool calling.
        """
        yield ("[System] 正在與 Qwen 3 模型連線中...", False, "")
        
        # Reload configs dynamically to reflect updates in built_in_db.json
        self.config = self._load_config()
        self.model_name = self.config.get("model_name", "qwen3")
        self.system_prompt = self.config.get("system_prompt", "你是一個專業的 Python 教學助理。")
        
        system_msg = self.system_prompt
        constraints = self.config.get("constraints", [])
        if constraints:
            system_msg += "\n\n【核心行為約束與安全規範】\n"
            for c in constraints:
                system_msg += f"- {c}\n"

        # Load active memory if uid provided
        if uid:
            from core.memory_manager import get_active_memory
            memories = get_active_memory(uid)
            if memories:
                system_msg += "\n\n[使用者長期記憶]\n"
                for m in memories:
                    system_msg += f"- {m['content']}\n"

        # Inject relevant knowledge source chunks based on latest user message
        if uid and chat_history:
            last_user_msg = ""
            for m in reversed(chat_history):
                if m.get("role") == "user":
                    last_user_msg = m.get("content", "")
                    break
            if last_user_msg:
                try:
                    from core.source_manager import retrieve_from_sources
                    source_ctx = retrieve_from_sources(uid, last_user_msg, top_k=3, min_score=0.65)
                    if source_ctx:
                        system_msg += (
                            f"\n\n【使用者個人知識庫資料（僅在使用者提問與此文獻直接相關時參考。若使用者是在問日常問題、所在地美食或生活資訊，請以使用者的即時問題為準，切勿強加文獻主題或被其侷限）】\n"
                            f"{source_ctx}"
                        )
                except Exception:
                    pass
        
        # Format messages for Ollama
        messages = [{"role": "system", "content": system_msg}]
        for msg in chat_history:
            messages.append({"role": msg["role"], "content": msg["content"]})
            
        current_text = ""
        try:
            max_iters = 5
            for _ in range(max_iters):
                response_stream = ollama.chat(
                    model=self.model_name,
                    messages=messages,
                    tools=self.get_tool_schemas(),
                    stream=True
                )
                
                tool_calls_collected = None
                msg_obj_collected = None
                step_thinking = ""
                
                for chunk in response_stream:
                    msg_obj = chunk.get("message", {})
                    
                    t = msg_obj.get("thinking") or ""
                    if t:
                        step_thinking += t
                    
                    if "tool_calls" in msg_obj and msg_obj["tool_calls"]:
                        tool_calls_collected = msg_obj["tool_calls"]
                        msg_obj_collected = msg_obj
                        # Stop streaming since it's a tool call
                        break 
                        
                    content = msg_obj.get("content", "")
                    if content:
                        current_text += content
                        yield (None, False, current_text)
                        
                if tool_calls_collected:
                    tool_call = tool_calls_collected[0]
                    tool_name = tool_call["function"]["name"]
                    args = tool_call["function"]["arguments"]
                    
                    if step_thinking.strip():
                        first_thought = step_thinking.strip().split("\n")[0][:120]
                        yield (f"[Thinking] {first_thought}...", False, current_text)
                    
                    yield (f"[System] AI 正在使用工具: {tool_name} (參數: {args})...", False, current_text)
                    
                    tool_result = "無結果"
                    if tool_name == "web_search":
                        from tools.web_search import search_web
                        res = search_web(args.get("query", ""))
                        tool_result = json.dumps(res, ensure_ascii=False)
                    elif tool_name == "web_scraper":
                        from tools.web_scraper import scrape_url
                        res = scrape_url(args.get("url", ""))
                        tool_result = json.dumps(res, ensure_ascii=False)
                    elif tool_name == "web_form_search":
                        from tools.web_form_search import form_search
                        res = form_search(args.get("url", ""), args.get("keyword", ""))
                        tool_result = json.dumps(res, ensure_ascii=False)
                    elif tool_name == "suggest_add_source":
                        from tools.suggest_add_source import suggest_and_add_source
                        res = suggest_and_add_source(
                            uid=uid,
                            url=args.get("url", ""),
                            reason=args.get("reason", ""),
                            label=args.get("label", "")
                        )
                        tool_result = json.dumps(res, ensure_ascii=False)

                    elif tool_name == "read_document":
                        from tools.file_reader import read_local_file
                        doc_path = args.get("filepath_or_url", "")
                        query = args.get("query", "").strip()
                        res = read_local_file(doc_path)
                        
                        if not res.startswith("Error:"):
                            # 如果指定了 query，或者文件過長 (>6000 字元) 進行向量檢索
                            if query:
                                from core.embedding import retrieve_relevant_chunks
                                yield (f"[System] 正在使用 Embedding (nomic-embed-text) 進行語意向量檢索 (主題: {query})...", False, current_text)
                                res = retrieve_relevant_chunks(query, res, top_k=4)
                            elif len(res) > 8000:
                                # 自動從聊天歷史中提取最近的使用者問題作為檢索目標
                                last_query = ""
                                for m in reversed(chat_history):
                                    if m.get("role") == "user":
                                        last_query = m.get("content", "")
                                        break
                                if last_query:
                                    from core.embedding import retrieve_relevant_chunks
                                    yield (f"[System] 文件較長 ({len(res)} 字)，自動以使用者提問啟用語意向量檢索...", False, current_text)
                                    res = retrieve_relevant_chunks(last_query, res, top_k=4)
                                else:
                                    res = res[:8000] + "\n...(文件過長已截斷)..."
                        tool_result = res
                    
                    yield (f"[System] 工具 {tool_name} 執行完畢，等待 AI 回應...", False, current_text)
                    
                    messages.append(msg_obj_collected)
                    messages.append({
                        "role": "tool",
                        "content": tool_result,
                        "name": tool_name
                    })
                    # Loop continues, model will be queried again with tool results
                else:
                    # Model finished sending regular text
                    break
                    
            # If after tool iterations the model has gathered info but hasn't output text yet, force final synthesis
            if not current_text.strip():
                yield ("[System] AI 正在彙整所有檢索資料產生最終回答...", False, current_text)
                final_stream = ollama.chat(
                    model=self.model_name,
                    messages=messages,
                    stream=True
                )
                for chunk in final_stream:
                    msg_obj = chunk.get("message", {})
                    content = msg_obj.get("content", "")
                    if content:
                        current_text += content
                        yield (None, False, current_text)

            yield ("[System] 回應生成完畢。", True, current_text)
            
        except Exception as e:
            err_str = str(e)
            if "Failed to connect" in err_str or "Connection refused" in err_str or "11434" in err_str or "ConnectError" in err_str:
                friendly_guidance = (
                    "### [系統提示] 無法連線至本地 Ollama 服務\n\n"
                    "系統目前未能連接到本地的 Ollama 服務 (`http://127.0.0.1:11434`)。\n"
                    "若您正在新環境中運行或尚未啟動 Ollama，請依照以下步驟操作：\n\n"
                    "1. **安裝 Ollama**：前往 [Ollama 官網 (https://ollama.com)](https://ollama.com) 下載並安裝軟體。\n"
                    "2. **啟動服務**：開啟 Ollama 應用程式，或於命令列執行 `ollama serve`。\n"
                    f"3. **下載模型**：請在終端機中執行下列指令完成下載：\n"
                    f"   ```bash\n"
                    f"   ollama pull {self.model_name}\n"
                    f"   ollama pull nomic-embed-text\n"
                    f"   ```\n\n"
                    "完成後請直接重新送出訊息或點擊右側「重新檢查連線」按鈕。"
                )
                yield ("[系統警告] Ollama 服務未啟動，無法進行對話與檢索。", True, friendly_guidance)
            elif "not found" in err_str.lower() and ("model" in err_str.lower() or "404" in err_str):
                friendly_guidance = (
                    f"### [系統提示] 本地尚未下載指定的 AI 模型 (`{self.model_name}`)\n\n"
                    f"本地 Ollama 服務雖然在線，但尚未下載名為 `{self.model_name}` 的模型。\n\n"
                    f"**解決方法**：\n"
                    f"請開啟終端機（Terminal / 命令提示字元），執行以下指令進行下載：\n"
                    f"```bash\n"
                    f"ollama pull {self.model_name}\n"
                    f"```\n\n"
                    f"下載完成後即可直接於此處繼續提問。"
                )
                yield (f"[系統警告] 找不到模型 {self.model_name}，請先執行 ollama pull。", True, friendly_guidance)
            else:
                error_msg = f"呼叫模型時發生錯誤: {err_str}"
                yield (f"[Error] {error_msg}", True, error_msg)
