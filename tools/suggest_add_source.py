from typing import Dict, Any


def suggest_and_add_source(uid: str, url: str, reason: str = "", label: str = "") -> Dict[str, Any]:
    """
    當使用者要求將某個網址或主題資料加入知識庫，或者 AI 自主判定某個網址/文件對使用者具有
    長期參考價值時調用此工具。系統會自動下載、切片並建立索引至使用者的「知識來源庫」。

    參數:
        uid: 使用者專屬識別碼
        url: 要加入知識來源庫的公開網址（http/https）或本地檔案路徑
        reason: 加入知識庫的理由或內容簡述
        label: 選填。知識來源的自訂標題名稱
    回傳:
        包含加入狀態與訊息的字典
    """
    from core.source_manager import add_source
    
    clean_label = label.strip() if label else ""
    if not clean_label and reason:
        clean_label = reason.strip()[:30]

    add_res = add_source(uid=uid, path_or_url=url, label=clean_label)
    
    if add_res.get("ok"):
        return {
            "status": "added",
            "message": f"已成功將「{url}」索引並加入知識來源庫！（{add_res.get('message')}）",
            "source_id": add_res.get("source_id"),
            "action": "ADD_SOURCE"
        }
    else:
        return {
            "status": "suggested",
            "message": f"建議加入：{url}（理由：{reason}），自動下載與索引狀態：{add_res.get('message')}",
            "suggested_url": url,
            "reason": reason,
            "action": "SUGGEST_ADD_SOURCE"
        }
