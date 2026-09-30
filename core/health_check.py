import os
import json
import ollama

def get_configured_models():
    """從 database/built_in_db.json 讀取設定的模型名稱"""
    config_path = os.path.join("database", "built_in_db.json")
    main_model = "qwen3"
    embed_model = "nomic-embed-text"
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
                main_model = cfg.get("model_name", "qwen3")
        except Exception:
            pass
    return main_model, embed_model

def check_ollama_status() -> dict:
    """
    全面檢測 Ollama 服務狀態與必要模型下載情況。
    回傳字典包含狀態旗標、模型清單、友善提示與 HTML 視圖。
    """
    main_model, embed_model = get_configured_models()
    result = {
        "connected": False,
        "main_model": main_model,
        "has_main": False,
        "embed_model": embed_model,
        "has_embed": False,
        "installed_models": [],
        "status_code": "OFFLINE",  # OFFLINE | MISSING_MODEL | READY
        "summary": "",
        "action_guide": "",
        "terminal_message": "",
        "ui_html": ""
    }

    try:
        res = ollama.list()
        result["connected"] = True

        # 取得已安裝模型名稱清單
        installed = []
        raw_models = getattr(res, "models", []) or []
        for m in raw_models:
            name = getattr(m, "model", None) or getattr(m, "name", None)
            if name:
                installed.append(name)
        result["installed_models"] = installed

        # 比對主模型
        has_main = any(
            main_model == m or
            m.startswith(main_model + ":") or
            main_model.split(":")[0] == m.split(":")[0]
            for m in installed
        )
        result["has_main"] = has_main

        # 比對向量嵌入模型
        has_embed = any(
            embed_model in m or
            m.startswith(embed_model)
            for m in installed
        )
        result["has_embed"] = has_embed

        if has_main and has_embed:
            result["status_code"] = "READY"
            result["summary"] = "Ollama 服務運作正常，所有模型均已就緒"
            result["action_guide"] = ""
            result["terminal_message"] = (
                f"[系統健康檢查] 本機 Ollama 服務連線正常！\n"
                f"  - 主對話模型: {main_model} (已就緒)\n"
                f"  - 向量模型: {embed_model} (已就緒)"
            )
        else:
            result["status_code"] = "MISSING_MODEL"
            missing_items = []
            pull_commands = []
            if not has_main:
                missing_items.append(f"主模型 '{main_model}'")
                pull_commands.append(f"ollama pull {main_model}")
            if not has_embed:
                missing_items.append(f"嵌入模型 '{embed_model}'")
                pull_commands.append(f"ollama pull {embed_model}")

            result["summary"] = f"Ollama 服務在線，但缺少必要模型：{', '.join(missing_items)}"
            result["action_guide"] = "請在終端機執行：\n" + "\n".join(pull_commands)
            result["terminal_message"] = (
                f"[系統健康檢查警告] Ollama 服務已連線，但缺少必要模型！\n"
                f"  - 請在終端機中執行下列指令完成下載：\n" +
                "".join([f"    {cmd}\n" for cmd in pull_commands])
            )

    except Exception as e:
        result["connected"] = False
        result["status_code"] = "OFFLINE"
        result["summary"] = "無法連線至本機 Ollama 服務 (127.0.0.1:11434)"
        result["action_guide"] = (
            "1. 前往 https://ollama.com 下載並安裝 Ollama\n"
            "2. 啟動 Ollama 服務 (終端機執行 `ollama serve` 或開啟桌面常駐程式)\n"
            f"3. 下載必要模型：\n"
            f"   ollama pull {main_model}\n"
            f"   ollama pull {embed_model}"
        )
        result["terminal_message"] = (
            "========================================================================\n"
            "[系統健康檢查警告] 未能連線至本地 Ollama 服務 (http://127.0.0.1:11434)！\n"
            "在新環境部署時，請依照下列步驟進行：\n"
            "1. 下載並安裝 Ollama：https://ollama.com\n"
            "2. 啟動服務：在命令列執行 `ollama serve` 或啟動桌面常駐程式\n"
            f"3. 下載專案模型：\n"
            f"   ollama pull {main_model}\n"
            f"   ollama pull {embed_model}\n"
            "========================================================================"
        )

    result["ui_html"] = render_status_html(result)
    return result

def render_status_html(res: dict) -> str:
    """產生符合白底簡約 + 3 色系 (綠/橘/藍) 的 Ollama 狀態卡片 HTML"""
    status_code = res["status_code"]
    main_model = res["main_model"]
    embed_model = res["embed_model"]

    if status_code == "READY":
        return f"""
<div class="ollama-status-card status-ready">
  <div class="ollama-status-header">
    <span class="status-indicator-badge status-ready-badge">[在線]</span>
    <span class="ollama-title">Ollama 服務運作正常</span>
  </div>
  <div class="ollama-details">
    <div class="ollama-detail-line">主模型：<code>{main_model}</code> (就緒)</div>
    <div class="ollama-detail-line">向量模型：<code>{embed_model}</code> (就緒)</div>
  </div>
</div>
"""
    elif status_code == "MISSING_MODEL":
        missing_cmds = []
        if not res["has_main"]:
            missing_cmds.append(f"ollama pull {main_model}")
        if not res["has_embed"]:
            missing_cmds.append(f"ollama pull {embed_model}")
        cmds_str = "<br>".join([f"<code>{c}</code>" for c in missing_cmds])

        return f"""
<div class="ollama-status-card status-warning">
  <div class="ollama-status-header">
    <span class="status-indicator-badge status-warning-badge">[警告]</span>
    <span class="ollama-title">缺少必要模型</span>
  </div>
  <div class="ollama-guide">
    請在終端機執行下載：<br>
    {cmds_str}
  </div>
</div>
"""
    else:  # OFFLINE
        return f"""
<div class="ollama-status-card status-offline">
  <div class="ollama-status-header">
    <span class="status-indicator-badge status-offline-badge">[離線]</span>
    <span class="ollama-title">Ollama 服務未啟動</span>
  </div>
  <div class="ollama-guide">
    1. 安裝：<a href="https://ollama.com" target="_blank" style="color: #2563eb; text-decoration: underline;">ollama.com</a><br>
    2. 啟動：終端機執行 <code>ollama serve</code><br>
    3. 下載：<code>ollama pull {main_model}</code>
  </div>
</div>
"""
