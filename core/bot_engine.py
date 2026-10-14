import os
import re
import time
import json
import inspect
import ollama

from core.config_manager import config
from core.security import sanitize_for_log


class BotEngine:
    def __init__(self):
        # 設定透過 ConfigManager 動態取得，不需要在 __init__ 讀取一次
        pass

    @property
    def model_name(self) -> str:
        return config.model_name

    @property
    def system_prompt(self) -> str:
        return config.system_prompt

    def get_tool_schemas(self):
        """
        取得系統所有已註冊的 AI 技能工具規格（來源：tools/ 目錄真實工具與清單註冊表）。
        """
        from tools import get_all_tool_schemas
        return get_all_tool_schemas()

    def _format_tool_call_log(self, tool_name: str, args: dict) -> str:
        """產出精簡、具體且安全的工具調用日誌，絕不洩漏實體磁碟路徑或內部服務端口。"""
        if tool_name == "web_search":
            q = (args.get("query") or "").strip()
            return f"[AI 工具] 調用 web_search（搜尋關鍵字：「{q[:40]}」）"
        elif tool_name == "web_scraper":
            url = sanitize_for_log(args.get("url") or "")
            return f"[AI 工具] 調用 web_scraper（抓取目標：「{url[:60]}」）"
        elif tool_name == "read_document":
            doc = args.get("filepath_or_url") or ""
            doc_name = os.path.basename(doc) if doc else "文獻檔案"
            q = (args.get("query") or "").strip()
            detail = f"，檢索主題：「{q[:30]}」" if q else ""
            return f"[AI 工具] 調用 read_document（讀取「{doc_name}」{detail}）"
        elif tool_name == "calculator":
            expr = (args.get("expression") or "").strip()
            return f"[AI 工具] 調用 calculator（計算算式：「{expr[:40]}」）"
        elif tool_name == "form_search":
            kw = (args.get("keyword") or "").strip()
            return f"[AI 工具] 調用 form_search（檢索：「{kw[:40]}」）"
        elif tool_name == "suggest_and_add_source":
            lbl = args.get("label") or args.get("url") or ""
            return f"[AI 工具] 調用 suggest_and_add_source（收錄來源：「{sanitize_for_log(lbl)[:40]}」）"
        return f"[AI 工具] 調用工具：{tool_name}..."

    def _dispatch_tool(self, tool_name: str, args: dict, uid: str, chat_history: list):
        """
        Registry-based 工具分派器。
        取代原本的巨型 if-elif，透過 AI_SKILLS_REGISTRY 動態查找 handler。

        特殊工具（read_document）有額外的 security + embedding 前處理，
        由 _handle_read_document() 單獨管理。
        """
        from tools import get_skill_by_name

        if tool_name == "read_document":
            return self._handle_read_document(args, uid, chat_history)

        if tool_name == "web_scraper":
            return self._handle_web_scraper(args)

        skill = get_skill_by_name(tool_name)
        if not skill or "handler" not in skill:
            return f"不支援的工具：{tool_name}"

        handler = skill["handler"]
        sig = inspect.signature(handler)
        params = sig.parameters

        # 自動注入 uid（若 handler 支援）
        call_kwargs = dict(args)
        if "uid" in params and "uid" not in call_kwargs:
            call_kwargs["uid"] = uid

        try:
            result = handler(**call_kwargs)
            return json.dumps(result, ensure_ascii=False) if isinstance(result, (dict, list)) else str(result)
        except Exception as e:
            return f"工具 {tool_name} 執行失敗：{e}"

    def _handle_web_scraper(self, args: dict) -> str:
        """
        web_scraper 的特殊處理：執行後還需分析頁面中的可下載附件連結。
        回傳 (tool_result_str, downloadable_count) tuple。
        注意：此方法回傳 tuple，讓 generate_response_stream 可拿到 attach count。
        """
        from tools.web_scraper import scrape_url
        res = scrape_url(args.get("url", ""))
        raw_html = res.pop("raw_html", "")  # 取出後移除，不送給 LLM
        downloadable_count = 0
        if raw_html:
            try:
                from core.web_content_analyzer import analyze_page
                analysis = analyze_page(raw_html, args.get("url", ""))
                if analysis["downloadable_files"]:
                    res["downloadable_files"] = analysis["downloadable_files"]
                    downloadable_count = len(analysis["downloadable_files"])
            except Exception as analyze_err:
                print(f"[bot_engine] analyze_page error: {analyze_err}")
        return json.dumps(res, ensure_ascii=False), downloadable_count

    def _handle_read_document(self, args: dict, uid: str, chat_history: list):
        """
        read_document 的特殊處理：
        【防禦一】沙盒白名單攔截
        【防禦三】錯誤遮蔽
        + 向量語意檢索（若 query 有指定或文件超過 8000 字元）。

        回傳 (tool_result_str, status_msg) tuple；status_msg 供 log 顯示，可為 None。
        """
        from tools.file_reader import read_local_file
        from core.security import is_safe_local_path, mask_error

        doc_path = args.get("filepath_or_url", "")
        query = args.get("query", "").strip()
        status_msg = None

        if not is_safe_local_path(doc_path):
            return "錯誤：無權限讀取指定路徑之檔案。", None

        try:
            res = read_local_file(doc_path)
        except PermissionError as e:
            return mask_error(e, context="bot_engine/read_document/permission"), None
        except FileNotFoundError as e:
            return mask_error(e, context="bot_engine/read_document/notfound"), None
        except Exception as e:
            return mask_error(e, context="bot_engine/read_document"), None

        if not res.startswith("錯誤：") and not res.startswith("Error:"):
            if query:
                from core.embedding import retrieve_relevant_chunks
                status_msg = f"[向量檢索] nomic-embed-text 語意檢索中（主題：「{query[:30]}」）..."
                res = retrieve_relevant_chunks(query, res, top_k=4)
            elif len(res) > 8000:
                last_query = ""
                for m in reversed(chat_history):
                    if m.get("role") == "user":
                        last_query = m.get("content", "")
                        break
                if last_query:
                    from core.embedding import retrieve_relevant_chunks
                    status_msg = f"[向量檢索] 文件長度 {len(res)} 字，啟用向量檢索比對使用者提問..."
                    res = retrieve_relevant_chunks(last_query, res, top_k=4)
                else:
                    res = res[:8000] + "\n...(文件過長已截斷)..."

        return res, status_msg

    def _build_system_message(self, uid: str, chat_history: list, info: dict = None) -> str:
        """組裝完整的 system prompt（包含約束、記憶、知識庫 chunks）。"""
        system_msg = config.system_prompt
        constraints = config.constraints
        if constraints:
            system_msg += "\n\n【核心行為約束與安全規範】\n"
            for c in constraints:
                system_msg += f"- {c}\n"

        # Load active memory if uid provided
        if uid:
            from core.memory_manager import get_active_memory
            memories = get_active_memory(uid)
            if memories:
                if info is not None:
                    info["memory_count"] = len(memories)
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
                        if info is not None:
                            labels = list(dict.fromkeys(re.findall(r"來源：(.*?)(?:（|$)", source_ctx)))
                            info["sources"] = [os.path.basename(lbl.strip()) for lbl in labels]
                        system_msg += (
                            f"\n\n【使用者個人知識庫資料（僅在使用者提問與此文獻直接相關時參考。若使用者是在問日常問題、所在地美食或生活資訊，請以使用者的即時問題為準，切勿強加文獻主題或被其侷限）】\n"
                            f"{source_ctx}"
                        )
                except Exception:
                    pass

        return system_msg

    def generate_response_stream(self, chat_history, uid=None):
        """
        Generator that yields (log_update, is_final, text_so_far)
        Connects to Ollama using the official python client and streams the response.
        Supports tool calling via Registry-based dispatcher.
        """
        start_time = time.time()
        yield (f"[模型] 連線本地模型：{self.model_name}", False, "")

        info = {}
        system_msg = self._build_system_message(uid, chat_history, info=info)

        if info.get("memory_count"):
            yield (f"[記憶庫] 已檢索並掛載 {info['memory_count']} 條使用者長期記憶", False, "")

        if info.get("sources"):
            src_str = "、".join(info["sources"][:2])
            suffix = f" 等 {len(info['sources'])} 筆" if len(info['sources']) > 2 else ""
            yield (f"[知識庫] 向量檢索命中相關文獻（{src_str}{suffix}）", False, "")

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
                        first_thought = step_thinking.strip().split("\n")[0][:100]
                        yield (f"[思考] {first_thought}...", False, current_text)

                    yield (self._format_tool_call_log(tool_name, args), False, current_text)

                    # ── 使用 Registry Dispatcher 執行工具 ──
                    if tool_name == "web_scraper":
                        tool_result, attach_count = self._handle_web_scraper(args)
                        attach_info = f"，發現 {attach_count} 個可下載附件" if attach_count > 0 else ""
                        yield (f"[AI 工具] web_scraper 執行完成（已取得網頁內文{attach_info}）", False, current_text)
                    elif tool_name == "read_document":
                        tool_result, pre_msg = self._handle_read_document(args, uid, chat_history)
                        if pre_msg:
                            yield (pre_msg, False, current_text)
                        doc_chars = len(tool_result) if isinstance(tool_result, str) else 0
                        yield (f"[AI 工具] read_document 執行完成（已擷取 {doc_chars} 字內容）", False, current_text)
                    elif tool_name == "web_search":
                        tool_result = self._dispatch_tool(tool_name, args, uid, chat_history)
                        res_count = ""
                        try:
                            parsed = json.loads(tool_result)
                            if isinstance(parsed, list):
                                res_count = f"（取得 {len(parsed)} 筆檢索結果）"
                        except Exception:
                            pass
                        yield (f"[AI 工具] web_search 檢索完成{res_count}，交由 AI 分析", False, current_text)
                    else:
                        tool_result = self._dispatch_tool(tool_name, args, uid, chat_history)
                        yield (f"[AI 工具] 工具 {tool_name} 執行完成，等待 AI 研判...", False, current_text)

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
                yield ("[模型] AI 正在彙整所有檢索資料產生最終回答...", False, current_text)
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

            elapsed = time.time() - start_time
            yield (f"[系統] 回應生成完畢（耗時 {elapsed:.1f} 秒，共 {len(current_text)} 字元）", True, current_text)

        except Exception as e:
            err_str = str(e)
            if "Failed to connect" in err_str or "Connection refused" in err_str or "11434" in err_str or "ConnectError" in err_str:
                friendly_guidance = (
                    "### [系統提示] 無法連線至本地 Ollama 服務\n\n"
                    "系統目前未能連接到本地的 Ollama 服務。\n"
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
                yield (f"[系統錯誤] {error_msg}", True, error_msg)
