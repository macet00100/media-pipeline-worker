import os
import sys
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone, timedelta

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from src.delivery.publisher import Publisher
from src.delivery.scheduler import VideoScheduler, VN_TIMEZONE, UTC_TIMEZONE


class TestPublisher(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.package_dir = Path(self.temp_dir.name) / "test_package"
        self.package_dir.mkdir(parents=True, exist_ok=True)

        self.calendar_path = Path(self.temp_dir.name) / "schedule_calendar.json"
        self.scheduler = VideoScheduler(
            calendar_path=self.calendar_path,
            golden_hours_str="11:30,17:30,20:30",
            buffer_hours=6
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_sanitize_youtube_metadata(self):
        """Kiểm tra sanitize_youtube_metadata loại bỏ <>, giới hạn ký tự."""
        title = "Phim Hay <2026> " + "A" * 150
        desc = "Mô tả phim <hay nhất> " + "B" * 5000
        tags = ["<tag1>", "tag2", "tag3" * 20]

        clean_t, clean_d, clean_tags = Publisher.sanitize_youtube_metadata(title, desc, tags)

        self.assertLessEqual(len(clean_t), 95)
        self.assertNotIn("<", clean_t)
        self.assertNotIn(">", clean_t)

        self.assertLessEqual(len(clean_d), 4800)
        self.assertNotIn("<", clean_d)
        self.assertNotIn(">", clean_d)

        total_tag_len = sum(len(t) + 1 for t in clean_tags)
        self.assertLessEqual(total_tag_len, 450)
        for t in clean_tags:
            self.assertNotIn("<", t)
            self.assertNotIn(">", t)

    def test_qa_gate_blocking(self):
        """Kiểm tra Cổng QA Gate ngăn chặn tuyệt đối gói không đạt tiêu chuẩn."""
        qa_file = self.package_dir / "qa_report.json"
        with open(qa_file, "w", encoding="utf-8") as f:
            json.dump({"passed": False, "reason": "Silence too high"}, f)

        # Cố gắng publish sẽ bị ném PermissionError
        with self.assertRaises(PermissionError):
            Publisher.publish_package(
                package_dir=self.package_dir,
                custom_scheduler=self.scheduler,
                force_publish=True
            )

    def test_auto_publish_disabled_falls_back_to_stub(self):
        """Khi AUTO_PUBLISH=false, publish_package chỉ sinh schedule_plan.json."""
        qa_file = self.package_dir / "qa_report.json"
        with open(qa_file, "w", encoding="utf-8") as f:
            json.dump({"passed": True, "score": 100}, f)

        video_file = self.package_dir / "final_review.mp4"
        video_file.touch()

        with patch.object(Publisher, "is_auto_publish_enabled", return_value=False):
            res = Publisher.publish_package(
                package_dir=self.package_dir,
                custom_scheduler=self.scheduler,
                force_publish=False
            )

            self.assertFalse(res.get("published"))
            self.assertTrue(res.get("locked"))
            plan_file = self.package_dir / "schedule_plan.json"
            self.assertTrue(plan_file.exists())


if __name__ == "__main__":
    unittest.main()
