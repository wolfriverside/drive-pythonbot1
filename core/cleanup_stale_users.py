"""
core/cleanup_stale_users.py

定期清理過期或長期未活躍的訪客資料夾，防止 database/users/ 目錄無上限增長。
可作為獨立腳本排程執行（如 cron 或 Windows 工作排程器）。
"""

import os
import shutil
import json
import argparse
from datetime import datetime, timezone, timedelta


def cleanup_stale_users(retention_days: int = 30, dry_run: bool = False, users_dir: str = "database/users") -> dict:
    """
    掃描使用者目錄，清理超過 retention_days 未活躍的使用者。

    回傳:
        {
            "total_users": int,
            "stale_users": list[str],
            "deleted_count": int,
            "freed_bytes": int,
            "dry_run": bool
        }
    """
    if not os.path.isdir(users_dir):
        return {
            "total_users": 0,
            "stale_users": [],
            "deleted_count": 0,
            "freed_bytes": 0,
            "dry_run": dry_run,
        }

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=retention_days)

    total_users = 0
    stale_users = []
    freed_bytes = 0

    for item in os.listdir(users_dir):
        user_path = os.path.join(users_dir, item)
        if not os.path.isdir(user_path):
            continue

        total_users += 1
        profile_path = os.path.join(user_path, "profile.json")
        last_active = None

        if os.path.isfile(profile_path):
            try:
                with open(profile_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                active_str = data.get("last_active_time") or data.get("created_at")
                if active_str:
                    last_active = datetime.fromisoformat(active_str)
                    if last_active.tzinfo is None:
                        last_active = last_active.replace(tzinfo=timezone.utc)
            except Exception:
                pass

        # 若無 profile.json 或讀取失敗，使用目錄修改時間作為 fallback
        if not last_active:
            mtime = os.path.getmtime(user_path)
            last_active = datetime.fromtimestamp(mtime, tz=timezone.utc)

        if last_active < cutoff:
            stale_users.append(item)
            # 計算欲釋放的大小
            for root, _, files in os.walk(user_path):
                for f in files:
                    try:
                        freed_bytes += os.path.getsize(os.path.join(root, f))
                    except Exception:
                        pass

            if not dry_run:
                try:
                    shutil.rmtree(user_path)
                except Exception as e:
                    print(f"[cleanup] 刪除 {item} 失敗: {e}")

    return {
        "total_users": total_users,
        "stale_users": stale_users,
        "deleted_count": len(stale_users) if not dry_run else 0,
        "freed_bytes": freed_bytes,
        "dry_run": dry_run,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="清理過期訪客資料")
    parser.add_argument("--days", type=int, default=30, help="保留天數 (預設: 30)")
    parser.add_argument("--dry-run", action="store_true", help="模擬執行，不實際刪除檔案")
    parser.add_argument("--dir", type=str, default="database/users", help="使用者資料目錄")
    args = parser.parse_args()

    print(f"[清理排程] 開始檢查超過 {args.days} 天未活躍的訪客資料... (dry_run={args.dry_run})")
    result = cleanup_stale_users(retention_days=args.days, dry_run=args.dry_run, users_dir=args.dir)
    print(f"總訪客數: {result['total_users']}")
    print(f"過期訪客數: {len(result['stale_users'])}")
    if args.dry_run:
        print(f"預計釋放空間: {result['freed_bytes'] / 1024:.2f} KB (未實際刪除)")
    else:
        print(f"已刪除訪客數: {result['deleted_count']}")
        print(f"已釋放空間: {result['freed_bytes'] / 1024:.2f} KB")
