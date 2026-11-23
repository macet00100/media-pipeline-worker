import os
import sys
import json
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timezone, timedelta

# Ensure repo root is on sys.path
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from config.settings import Settings
from src.delivery.scheduler import VideoScheduler, VN_TIMEZONE, UTC_TIMEZONE
from src.delivery.publisher_stub import PublisherStub


class TestVideoSchedulerAndPublisher(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.calendar_path = Path(self.temp_dir.name) / "schedule_calendar.json"
        self.scheduler = VideoScheduler(
            calendar_path=self.calendar_path,
            golden_hours_str="11:30,17:30,20:30",
            buffer_hours=6
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_golden_hours_parsing(self):
        """Kiểm tra phân tích chuỗi Giờ Vàng VN chuẩn xác."""
        hours = self.scheduler.parse_golden_hours("11:30,17:30,20:30")
        self.assertEqual(hours, [(11, 30), (17, 30), (20, 30)])

        # Test khoảng trắng và định dạng lộn xộn
        hours_messy = self.scheduler.parse_golden_hours(" 20:30 , 11:30,  17:30 ")
        self.assertEqual(hours_messy, [(11, 30), (17, 30), (20, 30)])

    def test_buffer_hours_enforcement(self):
        """
        Kiểm tra quy tắc PRE_UPLOAD_BUFFER_HOURS = 6 tiếng.
        """
        # Case A: Sản xuất lúc 08:00 AM (2026-11-22).
        # Buffer 6 tiếng -> Sớm nhất được công khai: 14:00 PM.
        # Khung 11:30 cùng ngày vi phạm buffer (chỉ cách 3.5 tiếng) -> BẮT BUỘC BỎ QUA.
        # Khung hợp lệ đầu tiên: 17:30 cùng ngày.
        t_0800 = datetime(2026, 11, 22, 8, 0, 0, tzinfo=VN_TIMEZONE)
        slot_a = self.scheduler.get_next_available_slot(from_time=t_0800)
        self.assertEqual(slot_a["golden_hour"], "17:30")
        self.assertEqual(slot_a["date"], "2026-11-22")
        self.assertEqual(slot_a["slot_key"], "2026-11-22 17:30")

        # Case B: Sản xuất lúc 11:31 AM (2026-11-22).
        # Buffer 6 tiếng -> Sớm nhất: 17:31 PM.
        # Khung 17:30 cùng ngày thiếu 1 phút -> BẮT BUỘC BỎ QUA.
        # Khung hợp lệ đầu tiên: 20:30 cùng ngày.
        t_1131 = datetime(2026, 11, 22, 11, 31, 0, tzinfo=VN_TIMEZONE)
        slot_b = self.scheduler.get_next_available_slot(from_time=t_1131)
        self.assertEqual(slot_b["golden_hour"], "20:30")
        self.assertEqual(slot_b["date"], "2026-11-22")

        # Case C: Sản xuất lúc 18:00 PM (2026-11-22).
        # Buffer 6 tiếng -> Sớm nhất: 00:00 AM ngày 2026-11-23.
        # Toàn bộ khung ngày 2026-11-22 không khả dụng.
        # Khung hợp lệ đầu tiên: 11:30 sáng hôm sau (2026-11-23).
        t_1800 = datetime(2026, 11, 22, 18, 0, 0, tzinfo=VN_TIMEZONE)
        slot_c = self.scheduler.get_next_available_slot(from_time=t_1800)
        self.assertEqual(slot_c["golden_hour"], "11:30")
        self.assertEqual(slot_c["date"], "2026-11-23")

    def test_sequential_collision_avoidance_3_videos_per_day(self):
        """
        Kiểm tra phân bổ nối tiếp 3 video/ngày vào các khung Giờ Vàng,
        tránh tuyệt đối việc trùng 2 video cùng 1 khung giờ.
        """
        base_time = datetime(2026, 11, 22, 8, 0, 0, tzinfo=VN_TIMEZONE)

        # Video 1
        v1 = self.scheduler.schedule_video(
            video_id="drama_001",
            title="Đoản Kịch Đại Chiến Tập 1",
            from_time=base_time
        )
        self.assertEqual(v1["slot_key"], "2026-11-22 17:30")

        # Video 2 (sản xuất tiếp nối cùng đợt)
        v2 = self.scheduler.schedule_video(
            video_id="drama_002",
            title="Đoản Kịch Đại Chiến Tập 2",
            from_time=base_time
        )
        self.assertEqual(v2["slot_key"], "2026-11-22 20:30")

        # Video 3 (sản xuất tiếp nối cùng đợt)
        v3 = self.scheduler.schedule_video(
            video_id="drama_003",
            title="Phim Dài Trọn Bộ Kỳ 1",
            from_time=base_time
        )
        self.assertEqual(v3["slot_key"], "2026-11-23 11:30")

        # Video 4 (sản xuất tiếp nối cùng đợt)
        v4 = self.scheduler.schedule_video(
            video_id="drama_004",
            title="Đoản Kịch Mới Tập 1",
            from_time=base_time
        )
        self.assertEqual(v4["slot_key"], "2026-11-23 17:30")

        # Xác nhận 4 video có 4 slot hoàn toàn khác biệt
        allocated_keys = [v1["slot_key"], v2["slot_key"], v3["slot_key"], v4["slot_key"]]
        self.assertEqual(len(allocated_keys), len(set(allocated_keys)), "Bị trùng khung giờ vàng!")

        # Kiểm tra file state/schedule_calendar.json
        self.assertTrue(self.calendar_path.exists())
        with open(self.calendar_path, "r", encoding="utf-8") as f:
            cal_data = json.load(f)

        self.assertIn("2026-11-22 17:30", cal_data["slots"])
        self.assertIn("2026-11-22 20:30", cal_data["slots"])
        self.assertIn("2026-11-23 11:30", cal_data["slots"])
        self.assertIn("2026-11-23 17:30", cal_data["slots"])
        self.assertEqual(len(cal_data["scheduled_videos"]), 4)

    def test_utc_and_timestamp_conversion(self):
        """
        Kiểm tra độ chính xác của định dạng UTC ISO-8601 (YouTube)
        và Unix Epoch Timestamp (Facebook).
        """
        t_base = datetime(2026, 11, 22, 8, 0, 0, tzinfo=VN_TIMEZONE)
        slot = self.scheduler.schedule_video(
            video_id="test_format",
            title="Test Format Phim",
            from_time=t_base
        )

        # Khung giờ VN: 2026-11-22T17:30:00+07:00
        self.assertEqual(slot["slot_vn"], "2026-11-22T17:30:00+07:00")

        # YouTube publishAt: UTC ISO-8601 kết thúc bằng 'Z'
        # 17:30 GMT+7 = 10:30 UTC
        self.assertEqual(slot["publish_at_utc"], "2026-11-22T10:30:00Z")

        # Facebook scheduled_publish_time: Unix timestamp tương đương
        expected_ts = int(datetime(2026, 11, 22, 10, 30, 0, tzinfo=timezone.utc).timestamp())
        self.assertEqual(slot["scheduled_publish_timestamp"], expected_ts)

    def test_publisher_stub_hardlock_and_schedule_plan(self):
        """
        Kiểm tra KHÓA CỨNG AUTO_PUBLISH=false:
        - Không được phép upload
        - Sinh đầy đủ file schedule_plan.json trong Gói Xét Duyệt 3 phần
        - Payload YouTube có privacyStatus='private' và status.publishAt ISO-8601 UTC
        - Payload Facebook có published=false và scheduled_publish_time
        """
        package_dir = Path(self.temp_dir.name) / "package_output_test"
        package_dir.mkdir(parents=True, exist_ok=True)
        video_file = package_dir / "final_review.mp4"
        video_file.touch()
        thumb_file = package_dir / "thumbnail.jpg"
        thumb_file.touch()

        # Gọi PublisherStub với mock scheduler
        sim_time = datetime(2026, 11, 22, 9, 0, 0, tzinfo=VN_TIMEZONE)
        res = PublisherStub.publish_video(
            video_path=str(video_file),
            title="Đoản Kịch Tổng Tài Hào Môn Tập 1",
            description="Review trọn bộ phim tổng tài hào môn @CoolMeoPhim",
            thumbnail_path=str(thumb_file),
            package_dir=str(package_dir),
            video_id="drama_tongtai_01",
            from_time=sim_time,
            custom_scheduler=self.scheduler
        )

        # 1. Kiểm tra an toàn: Lock được kích hoạt, upload bị chặn
        self.assertFalse(res["published"])
        self.assertTrue(res["locked"])
        self.assertEqual(res["safety_lock"], "HARDLOCKED_AUTO_PUBLISH_FALSE")

        # 2. Kiểm tra file schedule_plan.json được ghi đúng thư mục
        plan_path = package_dir / "schedule_plan.json"
        self.assertTrue(plan_path.exists())

        with open(plan_path, "r", encoding="utf-8") as f:
            plan_data = json.load(f)

        # 3. Kiểm tra thông tin kênh và an toàn
        self.assertEqual(plan_data["channel"]["name"], Settings.CHANNEL_NAME)
        self.assertEqual(plan_data["channel"]["handle"], Settings.CHANNEL_HANDLE)
        self.assertFalse(plan_data["safety_policy"]["auto_publish_enabled"])

        # 4. Kiểm tra YouTube payload
        yt_req = plan_data["api_stubs"]["youtube_data_api_v3"]
        self.assertEqual(yt_req["status"]["privacyStatus"], "private")
        self.assertTrue(yt_req["status"]["publishAt"].endswith("Z"))
        self.assertEqual(yt_req["status"]["publishAt"], "2026-11-22T10:30:00Z")
        self.assertIn("Cool Mèo Phim", yt_req["snippet"]["tags"])

        # 5. Kiểm tra Facebook payload
        fb_req = plan_data["api_stubs"]["facebook_graph_api"]
        self.assertFalse(fb_req["params"]["published"])
        self.assertIsInstance(fb_req["params"]["scheduled_publish_time"], int)
        self.assertGreater(fb_req["params"]["scheduled_publish_time"], 0)

    def test_release_slot_functionality(self):
        """Kiểm tra chức năng hủy/giải phóng slot khi có nhu cầu điều phối lại."""
        sim_time = datetime(2026, 11, 22, 8, 0, 0, tzinfo=VN_TIMEZONE)
        slot = self.scheduler.schedule_video(
            video_id="video_to_cancel",
            title="Video Bị Hủy",
            from_time=sim_time
        )
        slot_key = slot["slot_key"]

        # Ban đầu slot không còn trống
        self.assertFalse(self.scheduler.is_slot_available(slot_key))

        # Giải phóng slot
        success = self.scheduler.release_slot(slot_key)
        self.assertTrue(success)

        # Bây giờ slot lại trống
        self.assertTrue(self.scheduler.is_slot_available(slot_key))


if __name__ == "__main__":
    unittest.main()
