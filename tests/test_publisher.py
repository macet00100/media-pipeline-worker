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

    def test_facebook_vertical_video_selects_vertical_thumbnail(self):
        """Video dọc (Reels) bắt buộc tự động chọn thumbnail dọc 720x1280 cho Facebook."""
        from PIL import Image

        video_file = self.package_dir / "final_review.mp4"
        video_file.touch()

        # Tạo 2 thumbnail: ngang (1280x720) và dọc (720x1280)
        h_thumb = self.package_dir / "thumbnail_horizontal.jpg"
        v_thumb = self.package_dir / "thumbnail.jpg"
        Image.new("RGB", (1280, 720), color="blue").save(h_thumb)
        Image.new("RGB", (720, 1280), color="red").save(v_thumb)

        mock_post = MagicMock()
        # Mock 3 phase Facebook upload
        mock_post.return_value.json.side_effect = [
            {"video_id": "fb_vid_123", "upload_session_id": "sess_123"},  # start
            {"success": True, "id": "fb_vid_123"},                         # finish
            {"success": True, "id": "thumb_123"}                           # thumbnail
        ]

        with patch("requests.post", mock_post), \
             patch.object(Publisher, "get_video_dimensions", return_value=(720, 1280)):
            # Truyền nhầm thumbnail_horizontal nhưng code phải tự sửa sang thumbnail.jpg
            res = Publisher.upload_to_facebook(
                video_path=str(video_file),
                title="Test Reels Title",
                description="Test Desc",
                scheduled_publish_timestamp=1795527000,
                thumbnail_path=str(h_thumb),
                page_id="test_page_123",
                access_token="test_token_xyz",
                soak_seconds=0
            )

            self.assertTrue(res["success"])
            self.assertTrue(res["is_vertical"])
            self.assertEqual(Path(res["thumbnail_path"]).name, "thumbnail.jpg")
            self.assertEqual(res["thumbnail_dimensions"], "720x1280")

    def test_facebook_soak_check_detects_copyright_and_deletes_video(self):
        """Kiểm tra Facebook Soak Check: Khi phát hiện vi phạm bản quyền -> Tự động gọi DELETE gỡ video ngay lập tức."""
        mock_get = MagicMock()
        mock_get.return_value.json.return_value = {
            "status": {
                "video_status": "copyright_violating",
                "processing_phase": {"status": "error"}
            }
        }

        mock_del = MagicMock()
        mock_del.return_value.json.return_value = {"success": True}

        with patch("requests.get", mock_get), \
             patch("requests.delete", mock_del), \
             patch.object(Publisher, "send_telegram_alert"):

            res = Publisher.soak_and_verify_facebook_copyright(
                video_id="fb_bad_vid_999",
                access_token="fake_token",
                title="Phim Vi Pham Ban Quyen",
                soak_seconds=2,
                poll_interval=1
            )

            self.assertTrue(res["copyright_blocked"])
            self.assertFalse(res["success"])
            self.assertEqual(res["video_id"], "fb_bad_vid_999")
            self.assertIn("vi phạm", res["reason"].lower())

            # Bắt buộc đã gọi DELETE lên đúng endpoint video
            mock_del.assert_called_once()
            called_url = mock_del.call_args[0][0]
            self.assertIn("fb_bad_vid_999", called_url)

    def test_facebook_soak_check_passes_when_healthy(self):
        """Kiểm tra Facebook Soak Check: Khi video an toàn sạch sẽ -> Kết quả thành công và không xóa."""
        mock_get = MagicMock()
        mock_get.return_value.json.return_value = {
            "status": {
                "video_status": "ready",
                "processing_phase": {"status": "complete"},
                "publishing_phase": {"status": "complete", "publish_status": "published"}
            }
        }

        mock_del = MagicMock()

        with patch("requests.get", mock_get), \
             patch("requests.delete", mock_del):

            res = Publisher.soak_and_verify_facebook_copyright(
                video_id="fb_good_vid_111",
                access_token="fake_token",
                title="Phim Sach Ban Quyen",
                soak_seconds=2,
                poll_interval=1
            )

            self.assertFalse(res["copyright_blocked"])
            self.assertTrue(res["success"])
            mock_del.assert_not_called()

    def test_youtube_copyright_blocked_skips_facebook(self):
        """Khóa liên hoàn: Khi YouTube bị chặn Content ID -> Tự hủy và bỏ qua Facebook hoàn toàn."""
        qa_file = self.package_dir / "qa_report.json"
        with open(qa_file, "w", encoding="utf-8") as f:
            json.dump({"passed": True, "score": 95}, f)

        video_file = self.package_dir / "final_review.mp4"
        video_file.touch()

        mock_yt_blocked = {
            "success": False,
            "copyright_blocked": True,
            "reason": "blocked_worldwide_by_kuaikan",
            "platform": "youtube"
        }

        mock_upload_fb = MagicMock()

        with patch.object(Publisher, "is_youtube_auto_publish_enabled", return_value=True), \
             patch.object(Publisher, "is_facebook_auto_publish_enabled", return_value=True), \
             patch.object(Publisher, "upload_to_youtube", return_value=mock_yt_blocked), \
             patch.object(Publisher, "upload_to_facebook", mock_upload_fb):

            res = Publisher.publish_package(
                package_dir=self.package_dir,
                custom_scheduler=self.scheduler,
                force_publish=True
            )

            # Đảm bảo KHÔNG BAO GIỜ gọi upload_to_facebook
            mock_upload_fb.assert_not_called()

            self.assertTrue(res["copyright_blocked"])
            self.assertFalse(res["overall_success"])
            self.assertTrue(res["facebook"]["skipped"])
            self.assertEqual(res["facebook"]["reason"], "skipped_due_to_yt_copyright_block")


if __name__ == "__main__":
    unittest.main()
