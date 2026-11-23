import os
import json
import subprocess
from datetime import datetime
from typing import Dict, Any, List, Optional
from config.settings import STATE_DIR

DB_FILE = STATE_DIR / "processed_db.json"
QUEUE_FILE = STATE_DIR / "production_queue.json"

class StateManager:
    """Quản lý trạng thái phim đã xử lý, đang chờ duyệt hoặc lỗi để không bao giờ chạy trùng."""

    @classmethod
    def _load(cls) -> Dict[str, Any]:
        if not DB_FILE.exists():
            initial_data = {
                "processed_ids": [],
                "failed_ids": {},
                "pending_review": {},
                "completed": {},
                "last_updated": datetime.now().isoformat()
            }
            cls._save(initial_data)
            return initial_data
        try:
            with open(DB_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                data.setdefault("processed_ids", [])
                data.setdefault("failed_ids", {})
                data.setdefault("pending_review", {})
                data.setdefault("completed", {})
                return data
        except Exception as e:
            print(f"[STATE] Lỗi đọc state: {e}")
            return {"processed_ids": [], "failed_ids": {}, "pending_review": {}, "completed": {}}

    @classmethod
    def _save(cls, data: Dict[str, Any]):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        data["last_updated"] = datetime.now().isoformat()
        temp_file = DB_FILE.with_suffix(".tmp")
        try:
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            temp_file.replace(DB_FILE)
        except Exception as e:
            print(f"[STATE] Lỗi lưu state: {e}")
            if temp_file.exists():
                try: temp_file.unlink()
                except: pass

    @classmethod
    def is_processed(cls, movie_id: str) -> bool:
        data = cls._load()
        return (movie_id in data.get("processed_ids", []) or
                movie_id in data.get("pending_review", {}) or
                movie_id in data.get("completed", {}))

    @classmethod
    def is_failed(cls, movie_id: str) -> bool:
        data = cls._load()
        return movie_id in data.get("failed_ids", {})

    @classmethod
    def get_ignored_ids(cls) -> set:
        data = cls._load()
        return (set(data.get("processed_ids", [])) |
                set(data.get("pending_review", {}).keys()) |
                set(data.get("failed_ids", {}).keys()) |
                set(data.get("completed", {}).keys()))

    @classmethod
    def get_processed_titles(cls) -> set:
        """Lấy tập hợp tên phim đã có trong processed_db để lọc trùng theo tiêu đề."""
        data = cls._load()
        titles = set()
        for k, v in data.get("pending_review", {}).items():
            if isinstance(v, dict) and v.get("title"):
                titles.add(v["title"].strip().lower())
        for k, v in data.get("completed", {}).items():
            if isinstance(v, dict) and v.get("title"):
                titles.add(v["title"].strip().lower())
        for k, v in data.get("failed_ids", {}).items():
            if isinstance(v, dict) and v.get("title"):
                titles.add(v["title"].strip().lower())
        return titles

    @classmethod
    def load_production_queue(cls) -> List[Dict[str, Any]]:
        """Tải danh sách hàng chờ sản xuất từ state/production_queue.json."""
        if not QUEUE_FILE.exists():
            return []
        try:
            with open(QUEUE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return data
                elif isinstance(data, dict):
                    return data.get("queue", [])
        except Exception as e:
            print(f"[STATE] Lỗi đọc production_queue: {e}")
        return []

    @classmethod
    def save_production_queue(cls, queue: List[Dict[str, Any]]):
        """Lưu danh sách hàng chờ sản xuất vào state/production_queue.json."""
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        temp_file = QUEUE_FILE.with_suffix(".tmp")
        payload = {
            "queue": queue,
            "total_items": len(queue),
            "last_updated": datetime.now().isoformat()
        }
        try:
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            temp_file.replace(QUEUE_FILE)
            print(f"[STATE] Đã cập nhật hàng chờ sản xuất state/production_queue.json ({len(queue)} phim).")
        except Exception as e:
            print(f"[STATE] Lỗi lưu production_queue: {e}")
            if temp_file.exists():
                try: temp_file.unlink()
                except: pass

    @classmethod
    def get_queued_ids_and_titles(cls) -> tuple[set, set]:
        """Lấy danh sách ID và tên phim đã có trong hàng chờ sản xuất."""
        queue = cls.load_production_queue()
        q_ids = set()
        q_titles = set()
        for item in queue:
            if isinstance(item, dict):
                if item.get("id"):
                    q_ids.add(str(item["id"]))
                if item.get("title"):
                    q_titles.add(str(item["title"]).strip().lower())
        return q_ids, q_titles

    @classmethod
    def update_queue_status(cls, movie_id: str, status: str):
        """Cập nhật trạng thái của phim trong hàng chờ (ví dụ: 'processed', 'failed')."""
        queue = cls.load_production_queue()
        updated = False
        for item in queue:
            if isinstance(item, dict) and item.get("id") == movie_id:
                item["status"] = status
                updated = True
        if updated:
            cls.save_production_queue(queue)

    @classmethod
    def mark_pending_review(cls, movie_id: str, info: Dict[str, Any]):
        data = cls._load()
        if movie_id not in data["processed_ids"]:
            data["processed_ids"].append(movie_id)
        data["pending_review"][movie_id] = {
            **info,
            "created_at": datetime.now().isoformat()
        }
        cls._save(data)
        cls.update_queue_status(movie_id, "processed")
        print(f"[STATE] Đã chuyển phim [{info.get('title', movie_id)}] vào danh sách Chờ Xét Duyệt.")

    @classmethod
    def mark_completed(cls, movie_id: str, publish_info: Dict[str, Any]):
        data = cls._load()
        if movie_id not in data["processed_ids"]:
            data["processed_ids"].append(movie_id)
        data["completed"][movie_id] = {
            **publish_info,
            "completed_at": datetime.now().isoformat()
        }
        if movie_id in data.get("pending_review", {}):
            del data["pending_review"][movie_id]
        cls._save(data)
        cls.update_queue_status(movie_id, "completed")
        print(f"[STATE] Đã chuyển phim [{publish_info.get('title', movie_id)}] sang danh sách Hoàn Thành (Completed).")

    @classmethod
    def mark_failed(cls, movie_id: str, reason: str, title: str = ""):
        data = cls._load()
        data["failed_ids"][movie_id] = {
            "title": title,
            "reason": reason,
            "failed_at": datetime.now().isoformat()
        }
        cls._save(data)
        cls.update_queue_status(movie_id, "failed")
        print(f"[STATE] Đánh dấu phim lỗi [{movie_id}]: {reason}")

    @classmethod
    def sync_to_git(cls, commit_msg: str = "chore(state): update state files [skip ci]"):
        """Chỉ commit file JSON nhẹ processed_db.json, production_queue.json và schedule_calendar.json lên Git, tuyệt đối không commit media."""
        try:
            files_to_add = []
            if DB_FILE.exists():
                files_to_add.append(str(DB_FILE))
            if QUEUE_FILE.exists():
                files_to_add.append(str(QUEUE_FILE))
            cal_file = STATE_DIR / "schedule_calendar.json"
            if cal_file.exists():
                files_to_add.append(str(cal_file))
            if files_to_add:
                subprocess.run(["git", "add"] + files_to_add, check=False)
            res = subprocess.run(["git", "diff", "--cached", "--quiet"], check=False)
            if res.returncode != 0:
                subprocess.run(["git", "commit", "-m", commit_msg], check=False)
                subprocess.run(["git", "push"], check=False)
                print("[STATE] Đã đồng bộ processed_db.json, production_queue.json & schedule_calendar.json lên GitHub.")
        except Exception as e:
            print(f"[STATE] Bỏ qua sync git: {e}")
