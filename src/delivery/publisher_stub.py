import os
import json
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, Optional, List, Tuple

from config.settings import Settings
from src.delivery.scheduler import VideoScheduler, scheduler, VN_TIMEZONE, UTC_TIMEZONE


class PublisherStub:
    """
    Module phụ trách đăng tải và hẹn giờ xuất bản YouTube / Facebook.
    QUY TẮC BẢO VỆ TUYỆT ĐỐI:
    - KHÓA CỨNG AUTO_PUBLISH = false: Tuyệt đối không gọi upload API!
    - Lập trình sẵn logic hẹn giờ:
      + YouTube Data API v3: privacyStatus='private' + status.publishAt (ISO-8601 UTC).
      + Facebook Graph API: published=false + scheduled_publish_time (Unix epoch timestamp).
    - Tự động phân bổ vào Giờ Vàng VN và ghi thông tin vào schedule_plan.json trong Gói Xét Duyệt 3 phần.
    """

    # BẮT BUỘC KHÓA CỨNG Ở TẦNG CODE: Tuyệt đối không tự động upload
    AUTO_PUBLISH: bool = False

    @classmethod
    def build_youtube_scheduled_payload(
        cls,
        title: str,
        description: str,
        publish_at_utc: str,
        tags: Optional[List[str]] = None,
        category_id: str = "24"
    ) -> Dict[str, Any]:
        """
        Lập trình sẵn cấu trúc request YouTube Data API v3 (videos.insert).
        Yêu cầu kỹ thuật của YouTube:
        - privacyStatus BẮT BUỘC là 'private' khi đặt publishAt.
        - publishAt định dạng ISO-8601 UTC kết thúc bằng 'Z' (vd: 2026-11-22T10:30:00Z).
        """
        if not tags:
            tags = [
                "review phim",
                "Cool Mèo Phim",
                "@CoolMeoPhim",
                "tóm tắt phim",
                "phim ngắn trung quốc",
                "phim hay",
                "phim mới",
                "phim full bộ"
            ]

        return {
            "api": "YouTube Data API v3 (videos.insert)",
            "endpoint": "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status",
            "method": "POST",
            "headers": {
                "Authorization": "Bearer {YT_ACCESS_TOKEN}",
                "Content-Type": "application/json; charset=UTF-8"
            },
            "snippet": {
                "title": title[:100],
                "description": description[:4500],
                "tags": tags[:30],
                "categoryId": category_id,
                "defaultLanguage": "vi",
                "defaultAudioLanguage": "vi"
            },
            "status": {
                "privacyStatus": "private",  # Khóa riêng tư trước giờ công chiếu
                "publishAt": publish_at_utc,  # Thời điểm công chiếu ISO-8601 UTC
                "selfDeclaredMadeForKids": False
            }
        }

    @classmethod
    def build_facebook_scheduled_payload(
        cls,
        title: str,
        description: str,
        scheduled_publish_time: int,
        page_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Lập trình sẵn cấu trúc request Facebook Graph API (/{page-id}/videos).
        Yêu cầu kỹ thuật của Meta Graph API:
        - published BẮT BUỘC là False.
        - scheduled_publish_time là Unix timestamp (giây), phải cách hiện tại ít nhất 10 phút.
        """
        target_page_id = page_id or Settings.FB_PAGE_ID or "{FB_PAGE_ID}"
        return {
            "api": "Facebook Graph API (POST /{page-id}/videos)",
            "endpoint": f"https://graph-video.facebook.com/v19.0/{target_page_id}/videos",
            "method": "POST",
            "headers": {
                "Authorization": "Bearer {FB_PAGE_ACCESS_TOKEN}"
            },
            "params": {
                "upload_phase": "finish",
                "title": title[:100],
                "description": description,
                "published": False,  # Bắt buộc false khi đặt lịch
                "scheduled_publish_time": int(scheduled_publish_time)  # Epoch timestamp (s)
            }
        }

    @classmethod
    def generate_schedule_plan(
        cls,
        video_path: str,
        title: str,
        description: str,
        package_dir: Optional[str] = None,
        thumbnail_path: Optional[str] = None,
        tags: Optional[List[str]] = None,
        video_id: Optional[str] = None,
        from_time: Optional[datetime] = None,
        custom_scheduler: Optional[VideoScheduler] = None
    ) -> Tuple[str, Dict[str, Any]]:
        """
        Tạo kế hoạch phát sóng Giờ Vàng và xuất file schedule_plan.json vào Gói Xét Duyệt.
        """
        sched = custom_scheduler or scheduler

        # Xác định thư mục Gói Xét Duyệt
        if package_dir:
            target_dir = Path(package_dir)
        else:
            target_dir = Path(video_path).resolve().parent
        target_dir.mkdir(parents=True, exist_ok=True)

        vid_id = str(video_id or target_dir.name or Path(video_path).stem)

        # Phân bổ khung giờ vàng hợp lệ tiếp theo (buffer 6 tiếng, không trùng lặp)
        slot_info = sched.schedule_video(
            video_id=vid_id,
            title=title,
            package_dir=str(target_dir),
            from_time=from_time
        )

        publish_at_utc = slot_info["publish_at_utc"]
        scheduled_timestamp = slot_info["scheduled_publish_timestamp"]

        # Lập trình sẵn payloads cho YouTube và Facebook
        yt_payload = cls.build_youtube_scheduled_payload(
            title=title,
            description=description,
            publish_at_utc=publish_at_utc,
            tags=tags
        )

        fb_payload = cls.build_facebook_scheduled_payload(
            title=title,
            description=description,
            scheduled_publish_time=scheduled_timestamp
        )

        plan_data = {
            "schema_version": "1.0",
            "channel": {
                "name": Settings.CHANNEL_NAME,
                "handle": Settings.CHANNEL_HANDLE
            },
            "video_info": {
                "video_id": vid_id,
                "title": title,
                "video_path": str(video_path),
                "package_dir": str(target_dir),
                "thumbnail_path": str(thumbnail_path) if thumbnail_path else ""
            },
            "schedule_timing": {
                "golden_hour_vn": slot_info["golden_hour"],
                "slot_vn_iso": slot_info["slot_vn"],
                "publish_at_utc_iso": publish_at_utc,
                "scheduled_publish_timestamp": scheduled_timestamp,
                "date": slot_info["date"],
                "pre_upload_buffer_hours": slot_info.get("buffer_hours", 6),
                "timezone": "Asia/Ho_Chi_Minh (GMT+7)"
            },
            "safety_policy": {
                "auto_publish_enabled": False,
                "lock_status": "HARDLOCKED_AUTO_PUBLISH_FALSE",
                "explanation": (
                    "Chế độ Auto-Publish được KHÓA CỨNG ở tầng code. "
                    "Toàn bộ video được đưa vào hàng chờ duyệt 3 phần, tuyệt đối không tự động upload."
                )
            },
            "api_stubs": {
                "youtube_data_api_v3": yt_payload,
                "facebook_graph_api": fb_payload
            },
            "manual_publish_instructions": [
                "1. Tải video và thumbnail từ Gói Xét Duyệt.",
                f"2. Đăng tải lên YouTube Studio dạng 'Riêng tư' (Private) và hẹn giờ công khai vào đúng: {slot_info['slot_vn']} ({publish_at_utc} UTC).",
                f"3. Đăng tải lên Meta Business Suite (Facebook Fanpage) và hẹn giờ đăng vào đúng: {slot_info['slot_vn']}.",
                "4. Khi mở khóa production, gọi API với payload đã lập trình sẵn trong file này."
            ],
            "created_at": datetime.now(VN_TIMEZONE).isoformat()
        }

        plan_file = target_dir / "schedule_plan.json"
        with open(plan_file, "w", encoding="utf-8") as f:
            json.dump(plan_data, f, ensure_ascii=False, indent=2)

        return str(plan_file), plan_data

    @classmethod
    def publish_video(
        cls,
        video_path: str,
        title: str,
        description: str,
        thumbnail_path: Optional[str] = None,
        tags: Optional[List[str]] = None,
        package_dir: Optional[str] = None,
        video_id: Optional[str] = None,
        from_time: Optional[datetime] = None,
        custom_scheduler: Optional[VideoScheduler] = None
    ) -> Dict[str, Any]:
        """
        Khóa cứng không cho phép tự động upload khi chưa được duyệt.
        Ghi toàn bộ thông tin giờ vàng và payload hẹn giờ vào schedule_plan.json.
        """
        # KHÓA CỨNG: Tuyệt đối không gọi upload API
        if not Settings.AUTO_PUBLISH or not cls.AUTO_PUBLISH:
            plan_path, plan_data = cls.generate_schedule_plan(
                video_path=video_path,
                title=title,
                description=description,
                package_dir=package_dir,
                thumbnail_path=thumbnail_path,
                tags=tags,
                video_id=video_id,
                from_time=from_time,
                custom_scheduler=custom_scheduler
            )

            print(
                "[SAFETY LOCK] AUTO_PUBLISH=false KHÓA CỨNG: Tuyệt đối không gọi upload API.\n"
                f"[PublisherStub] Đã lập kế hoạch giờ vàng và ghi vào: {plan_path}"
            )

            return {
                "published": False,
                "locked": True,
                "safety_lock": "HARDLOCKED_AUTO_PUBLISH_FALSE",
                "message": (
                    "[SAFETY LOCK] Tính năng đăng tải tự động KHÓA CỨNG (AUTO_PUBLISH=false). "
                    "Chỉ xuất Gói Xét Duyệt kèm schedule_plan.json."
                ),
                "schedule_plan_path": plan_path,
                "schedule_plan": plan_data,
                "golden_hour_vn": plan_data["schedule_timing"]["slot_vn_iso"],
                "publish_at_utc": plan_data["schedule_timing"]["publish_at_utc_iso"],
                "scheduled_publish_timestamp": plan_data["schedule_timing"]["scheduled_publish_timestamp"]
            }

        # KHỐI PHÒNG THỦ: Trong mọi tình huống bình thường, luồng bên dưới không bao giờ được kích hoạt
        raise PermissionError(
            "[SAFETY LOCK VIOLATION] AUTO_PUBLISH bị khóa cứng ở tầng kiến trúc. Không được phép gọi API upload!"
        )
