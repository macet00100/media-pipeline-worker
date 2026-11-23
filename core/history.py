import os
import json
import subprocess
from datetime import datetime
from typing import Dict, Any, List, Optional
from core.config import STORAGE_DIR
from core.logger import Logger

HISTORY_FILE = os.path.join(STORAGE_DIR, "history.json")

class HistoryManager:
    """Quản lý danh sách các phim đã tải và xử lý để tránh làm lại."""

    @classmethod
    def _load_data(cls) -> Dict[str, Any]:
        if not os.path.exists(HISTORY_FILE):
            default_data = {"processed_ids": [], "items": {}}
            cls._save_data(default_data)
            return default_data
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if not isinstance(data, dict):
                    data = {}
                if "processed_ids" not in data:
                    data["processed_ids"] = []
                if "items" not in data:
                    data["items"] = {}
                return data
        except Exception as e:
            Logger.warning(f"Không thể đọc history: {e}", "HISTORY")
            return {"processed_ids": [], "items": {}}

    @classmethod
    def _save_data(cls, data: Dict[str, Any]):
        os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
        temp_file = HISTORY_FILE + ".tmp"
        try:
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(temp_file, HISTORY_FILE)
        except Exception as e:
            Logger.error(f"Lỗi lưu history: {e}", "HISTORY")
            if os.path.exists(temp_file):
                try: os.remove(temp_file)
                except: pass

    @classmethod
    def is_processed(cls, drama_id: str) -> bool:
        data = cls._load_data()
        return drama_id in set(data.get("processed_ids", []))

    @classmethod
    def get_processed_ids(cls) -> set:
        data = cls._load_data()
        return set(data.get("processed_ids", []))

    @classmethod
    def mark_processed(
        cls,
        drama_id: str,
        title: str,
        source: str,
        youtube_status: Optional[str] = None,
        facebook_status: Optional[str] = None
    ):
        data = cls._load_data()
        if drama_id not in data["processed_ids"]:
            data["processed_ids"].append(drama_id)
        data["items"][drama_id] = {
            "title": title,
            "source": source,
            "youtube": youtube_status,
            "facebook": facebook_status,
            "processed_at": datetime.now().isoformat()
        }
        cls._save_data(data)
        Logger.success(f"Đã lưu lịch sử phim [{title}] (ID: {drama_id})", "HISTORY")

    @classmethod
    def sync_to_git(cls, commit_message: str = "chore: sync history [skip ci]"):
        """Đồng bộ lịch sử lên GitHub (khi chạy trong GitHub Actions)."""
        try:
            subprocess.run(["git", "add", HISTORY_FILE], check=False)
            res = subprocess.run(["git", "diff", "--cached", "--quiet"], check=False)
            if res.returncode != 0:
                subprocess.run(["git", "commit", "-m", commit_message], check=False)
                subprocess.run(["git", "push"], check=False)
                Logger.info("Đã đồng bộ lịch sử lên GitHub.", "GIT")
        except Exception as e:
            Logger.warning(f"Không thể sync git: {e}", "GIT")
