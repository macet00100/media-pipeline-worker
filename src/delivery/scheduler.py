import os
import json
from pathlib import Path
from datetime import datetime, date, time, timedelta, timezone
from typing import Dict, Any, List, Optional, Tuple, Union

from config.settings import Settings, STATE_DIR

# Múi giờ Việt Nam GMT+7 (Asia/Ho_Chi_Minh)
VN_TIMEZONE = timezone(timedelta(hours=7))
UTC_TIMEZONE = timezone.utc


class VideoScheduler:
    """
    Hệ thống phân bổ lịch đăng 3 video/ngày vào các khung Giờ Vàng Việt Nam (GMT+7).
    - Giờ Vàng mặc định: 11:30, 17:30, 20:30 (GMT+7).
    - PRE_UPLOAD_BUFFER_HOURS = 6 (sản xuất và đưa vào hàng chờ hẹn giờ trước giờ công khai
      ít nhất 6 tiếng để YouTube/Facebook xử lý HD/4K và quét bản quyền xong).
    - Phân bổ nối tiếp vào khung giờ vàng hợp lệ gần nhất chưa có video chiếm chỗ,
      lưu trong state/schedule_calendar.json, tránh trùng 2 video cùng 1 khung giờ.
    """

    DEFAULT_GOLDEN_HOURS: str = "11:30,17:30,20:30"
    DEFAULT_BUFFER_HOURS: int = 6

    def __init__(
        self,
        calendar_path: Optional[Union[str, Path]] = None,
        golden_hours_str: Optional[str] = None,
        buffer_hours: Optional[int] = None
    ):
        if calendar_path:
            self.calendar_path = Path(calendar_path)
        else:
            self.calendar_path = getattr(Settings, "SCHEDULE_CALENDAR_FILE", STATE_DIR / "schedule_calendar.json")

        self.golden_hours_str = (
            golden_hours_str
            or getattr(Settings, "GOLDEN_HOURS_VN", None)
            or self.DEFAULT_GOLDEN_HOURS
        )
        self.buffer_hours = (
            buffer_hours
            if buffer_hours is not None
            else int(getattr(Settings, "PRE_UPLOAD_BUFFER_HOURS", self.DEFAULT_BUFFER_HOURS))
        )
        self.golden_hour_tuples = self.parse_golden_hours(self.golden_hours_str)

    @classmethod
    def parse_golden_hours(cls, hours_str: str) -> List[Tuple[int, int]]:
        """
        Chuyển chuỗi '11:30,17:30,20:30' thành danh sách tuple [(11, 30), (17, 30), (20, 30)].
        Được sắp xếp tăng dần theo thời gian trong ngày.
        """
        slots = []
        for item in str(hours_str).split(","):
            item = item.strip()
            if not item:
                continue
            try:
                parts = item.split(":")
                h = int(parts[0].strip())
                m = int(parts[1].strip())
                if 0 <= h < 24 and 0 <= m < 60:
                    slots.append((h, m))
            except Exception as e:
                print(f"[VideoScheduler] Cảnh báo định dạng giờ vàng không hợp lệ '{item}': {e}")
        slots.sort()
        if not slots:
            slots = [(11, 30), (17, 30), (20, 30)]
        return slots

    def load_calendar(self) -> Dict[str, Any]:
        """Tải lịch phát sóng từ state/schedule_calendar.json."""
        if self.calendar_path.exists():
            try:
                with open(self.calendar_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        if "slots" not in data:
                            data["slots"] = {}
                        if "scheduled_videos" not in data:
                            data["scheduled_videos"] = []
                        return data
            except Exception as e:
                print(f"[VideoScheduler] Lỗi đọc {self.calendar_path}: {e}. Khởi tạo lịch mới.")

        return {
            "version": "1.0",
            "timezone": "Asia/Ho_Chi_Minh (GMT+7)",
            "golden_hours_vn": [f"{h:02d}:{m:02d}" for h, m in self.golden_hour_tuples],
            "pre_upload_buffer_hours": self.buffer_hours,
            "slots": {},
            "scheduled_videos": [],
            "last_updated": datetime.now(VN_TIMEZONE).isoformat()
        }

    def save_calendar(self, data: Dict[str, Any]) -> None:
        """Lưu lịch phát sóng an toàn vào file state/schedule_calendar.json."""
        self.calendar_path.parent.mkdir(parents=True, exist_ok=True)
        data["last_updated"] = datetime.now(VN_TIMEZONE).isoformat()
        temp_path = self.calendar_path.with_suffix(".tmp")
        try:
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            temp_path.replace(self.calendar_path)
        except Exception as e:
            # Fallback ghi trực tiếp nếu replace gặp lỗi trên Windows
            with open(self.calendar_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except Exception:
                    pass

    def _normalize_time(self, dt: Optional[datetime]) -> datetime:
        """Đưa datetime về múi giờ VN (GMT+7)."""
        if dt is None:
            return datetime.now(VN_TIMEZONE)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=VN_TIMEZONE)
        return dt.astimezone(VN_TIMEZONE)

    def get_next_available_slot(
        self,
        from_time: Optional[datetime] = None,
        max_days_ahead: int = 90
    ) -> Dict[str, Any]:
        """
        Tìm khung giờ vàng hợp lệ tiếp theo chưa bị chiếm chỗ:
        - Phải cách thời điểm xét duyệt (from_time) ít nhất PRE_UPLOAD_BUFFER_HOURS (6 tiếng).
        - Không bị trùng với video nào đã xếp lịch trước đó.
        """
        now_vn = self._normalize_time(from_time)
        min_allowed_time = now_vn + timedelta(hours=self.buffer_hours)

        calendar = self.load_calendar()
        booked_slots = calendar.get("slots", {})

        current_date = min_allowed_time.date()
        for day_offset in range(max_days_ahead):
            check_date = current_date + timedelta(days=day_offset)
            for h, m in self.golden_hour_tuples:
                candidate_dt = datetime(
                    check_date.year, check_date.month, check_date.day,
                    h, m, 0, tzinfo=VN_TIMEZONE
                )

                # Kiểm tra điều kiện 1: Đủ buffer 6 tiếng
                if candidate_dt < min_allowed_time:
                    continue

                slot_key = candidate_dt.strftime("%Y-%m-%d %H:%M")

                # Kiểm tra điều kiện 2: Chưa có video chiếm chỗ
                slot_info = booked_slots.get(slot_key)
                if slot_info and slot_info.get("status") == "scheduled":
                    continue

                # Tìm thấy khung giờ vàng hợp lệ!
                utc_dt = candidate_dt.astimezone(UTC_TIMEZONE)
                publish_at_utc = utc_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
                scheduled_publish_timestamp = int(candidate_dt.timestamp())

                return {
                    "slot_key": slot_key,
                    "slot_vn": candidate_dt.isoformat(),
                    "publish_at_utc": publish_at_utc,
                    "scheduled_publish_timestamp": scheduled_publish_timestamp,
                    "slot_datetime_vn": candidate_dt,
                    "golden_hour": f"{h:02d}:{m:02d}",
                    "date": check_date.strftime("%Y-%m-%d"),
                    "buffer_hours": self.buffer_hours
                }

        raise RuntimeError(f"Không tìm thấy khung giờ vàng trống trong vòng {max_days_ahead} ngày tới!")

    def is_slot_available(self, slot_key_or_dt: Union[str, datetime]) -> bool:
        """Kiểm tra một khung giờ có còn trống hay không."""
        if isinstance(slot_key_or_dt, datetime):
            dt_vn = self._normalize_time(slot_key_or_dt)
            slot_key = dt_vn.strftime("%Y-%m-%d %H:%M")
        else:
            slot_key = str(slot_key_or_dt).strip()

        calendar = self.load_calendar()
        slot_info = calendar.get("slots", {}).get(slot_key)
        if not slot_info:
            return True
        return slot_info.get("status") != "scheduled"

    def schedule_video(
        self,
        video_id: str,
        title: str,
        package_dir: Optional[str] = None,
        from_time: Optional[datetime] = None,
        metadata: Optional[Dict[str, Any]] = None,
        force_new: bool = False
    ) -> Dict[str, Any]:
        """
        Phân bổ lịch phát sóng cho một video vào khung giờ vàng hợp lệ gần nhất.
        Lưu vào state/schedule_calendar.json để tránh trùng lặp.
        """
        calendar = self.load_calendar()
        now_vn = self._normalize_time(from_time)

        # Nếu video đã được xếp lịch trước đó và không yêu cầu xếp lại
        if not force_new:
            for s_key, s_data in calendar.get("slots", {}).items():
                if s_data.get("video_id") == str(video_id) and s_data.get("status") == "scheduled":
                    # Kiểm tra xem slot có còn hợp lệ (còn trong tương lai)
                    print(f"[VideoScheduler] Video '{video_id}' đã có lịch tại khung giờ: {s_key}")
                    return s_data

        # Tìm khung giờ vàng kế tiếp thỏa mãn buffer 6 tiếng và không bị trùng
        next_slot = self.get_next_available_slot(from_time=from_time)
        slot_key = next_slot["slot_key"]

        allocation_record = {
            "slot_key": slot_key,
            "slot_vn": next_slot["slot_vn"],
            "publish_at_utc": next_slot["publish_at_utc"],
            "scheduled_publish_timestamp": next_slot["scheduled_publish_timestamp"],
            "golden_hour": next_slot["golden_hour"],
            "date": next_slot["date"],
            "video_id": str(video_id),
            "title": str(title),
            "package_dir": str(package_dir) if package_dir else "",
            "allocated_at": now_vn.isoformat(),
            "buffer_hours": self.buffer_hours,
            "status": "scheduled",
            "metadata": metadata or {}
        }

        # Lưu vào calendar slots map
        calendar["slots"][slot_key] = allocation_record

        # Cập nhật danh sách scheduled_videos
        videos_list = calendar.get("scheduled_videos", [])
        # Xóa bản ghi cũ của video_id nếu có
        videos_list = [v for v in videos_list if v.get("video_id") != str(video_id)]
        videos_list.append(allocation_record)
        # Sắp xếp theo thứ tự thời gian đăng
        videos_list.sort(key=lambda x: x.get("scheduled_publish_timestamp", 0))
        calendar["scheduled_videos"] = videos_list

        self.save_calendar(calendar)

        print(
            f"[VideoScheduler] ✅ Đã phân bổ lịch cho '{title}' (ID: {video_id}):\n"
            f"   - Giờ Vàng VN: {next_slot['slot_vn']} ({next_slot['golden_hour']} GMT+7)\n"
            f"   - UTC (YouTube): {next_slot['publish_at_utc']}\n"
            f"   - Timestamp (Facebook): {next_slot['scheduled_publish_timestamp']}\n"
            f"   - Pre-upload Buffer: {self.buffer_hours} tiếng"
        )

        return allocation_record

    def release_slot(self, slot_key: str) -> bool:
        """Hủy/giải phóng một khung giờ khi video bị hủy hoặc lỗi."""
        calendar = self.load_calendar()
        if slot_key in calendar.get("slots", {}):
            calendar["slots"][slot_key]["status"] = "cancelled"
            calendar["slots"][slot_key]["cancelled_at"] = datetime.now(VN_TIMEZONE).isoformat()
            calendar["scheduled_videos"] = [
                v for v in calendar.get("scheduled_videos", [])
                if v.get("slot_key") != slot_key
            ]
            self.save_calendar(calendar)
            print(f"[VideoScheduler] Đã giải phóng slot {slot_key}.")
            return True
        return False

    def get_video_schedule(self, video_id: str) -> Optional[Dict[str, Any]]:
        """Lấy thông tin lịch đã phân bổ cho video_id."""
        calendar = self.load_calendar()
        for s_data in calendar.get("slots", {}).values():
            if s_data.get("video_id") == str(video_id) and s_data.get("status") == "scheduled":
                return s_data
        return None

    def list_upcoming_schedules(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Lấy danh sách các video đã xếp lịch trong tương lai."""
        calendar = self.load_calendar()
        return calendar.get("scheduled_videos", [])[:limit]

    def reset_calendar(self) -> None:
        """Đặt lại toàn bộ lịch phát sóng về trạng thái ban đầu."""
        initial_data = {
            "version": "1.0",
            "timezone": "Asia/Ho_Chi_Minh (GMT+7)",
            "golden_hours_vn": [f"{h:02d}:{m:02d}" for h, m in self.golden_hour_tuples],
            "pre_upload_buffer_hours": self.buffer_hours,
            "slots": {},
            "scheduled_videos": [],
            "last_updated": datetime.now(VN_TIMEZONE).isoformat()
        }
        self.save_calendar(initial_data)


# Singleton tiện ích cho toàn pipeline
scheduler = VideoScheduler()
Scheduler = VideoScheduler
