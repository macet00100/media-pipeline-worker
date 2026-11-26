import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from src.state.state_manager import StateManager
from src.delivery.publisher import Publisher
from src.delivery.scheduler import VideoScheduler


class TestCopyrightBlockState(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.test_dir = Path(self.temp_dir.name)
        self.state_dir = self.test_dir / "state"
        self.state_dir.mkdir(parents=True, exist_ok=True)

        self.db_path = self.state_dir / "processed_db.json"
        self.queue_path = self.state_dir / "production_queue.json"

        # Initialize mock state files
        with open(self.db_path, "w", encoding="utf-8") as f:
            json.dump({
                "processed_ids": [],
                "failed_ids": {},
                "copyright_blocked_ids": {},
                "pending_review": {},
                "completed": {}
            }, f)

        with open(self.queue_path, "w", encoding="utf-8") as f:
            json.dump({
                "queue": [
                    {"id": "lz_12345", "title": "Test Drama", "status": "queued"}
                ]
            }, f)

        # Patch StateManager paths
        self.patch_db = patch("src.state.state_manager.DB_FILE", self.db_path)
        self.patch_queue = patch("src.state.state_manager.QUEUE_FILE", self.queue_path)
        self.patch_db.start()
        self.patch_queue.start()

    def tearDown(self):
        self.patch_db.stop()
        self.patch_queue.stop()
        self.temp_dir.cleanup()

    def test_mark_copyright_blocked_persists_in_both_lists_and_updates_queue(self):
        """Kiểm tra StateManager khi đánh dấu phim dính bản quyền."""
        movie_id = "lz_12345"
        reason = "Chặn vùng lãnh thổ do bản quyền YouTube Content ID"
        title = "Test Drama"

        StateManager.mark_copyright_blocked(movie_id, reason, title)

        self.assertTrue(StateManager.is_copyright_blocked(movie_id))
        self.assertTrue(StateManager.is_failed(movie_id))
        self.assertIn(movie_id, StateManager.get_ignored_ids())

        # Kiểm tra file processed_db.json
        with open(self.db_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertIn(movie_id, data["copyright_blocked_ids"])
        self.assertIn(movie_id, data["failed_ids"])
        self.assertEqual(data["copyright_blocked_ids"][movie_id]["title"], title)

        # Kiểm tra file production_queue.json
        with open(self.queue_path, "r", encoding="utf-8") as f:
            q_data = json.load(f)
        item = next(x for x in q_data["queue"] if x["id"] == movie_id)
        self.assertEqual(item["status"], "copyright_blocked")

    def test_publisher_uses_explicit_video_id_on_copyright_block(self):
        """Publisher.publish_package phải dùng video_id chỉ định thay vì tên thư mục."""
        pkg_dir = self.test_dir / "random_folder_name_20261126_123456"
        pkg_dir.mkdir()

        # QA passed
        qa_file = pkg_dir / "qa_report.json"
        with open(qa_file, "w", encoding="utf-8") as f:
            json.dump({"passed": True}, f)

        # Dummy video
        video_file = pkg_dir / "final_review.mp4"
        video_file.touch()

        mock_yt_blocked = {
            "success": False,
            "copyright_blocked": True,
            "reason": "blocked_by_content_id",
            "platform": "youtube"
        }

        real_movie_id = "lz_168036"

        with patch.object(Publisher, "is_youtube_auto_publish_enabled", return_value=True), \
             patch.object(Publisher, "upload_to_youtube", return_value=mock_yt_blocked), \
             patch.object(Publisher, "recalculate_golden_hour_slot", return_value={
                 "slot_vn": "11:30", "golden_hour": "11:30", "publish_at_utc": "2026-11-27T04:30:00Z",
                 "scheduled_publish_timestamp": 1795753800
             }):

            res = Publisher.publish_package(
                package_dir=pkg_dir,
                force_publish=True,
                video_id=real_movie_id
            )

            self.assertTrue(res["copyright_blocked"])
            self.assertEqual(res["movie_id"], real_movie_id)

            # Phim thực sự phải nằm trong copyright_blocked_ids
            self.assertTrue(StateManager.is_copyright_blocked(real_movie_id))
            self.assertIn(real_movie_id, StateManager.get_ignored_ids())
            # Tên thư mục không được phép xuất hiện trong DB
            self.assertFalse(StateManager.is_copyright_blocked(pkg_dir.name))


if __name__ == "__main__":
    unittest.main()
