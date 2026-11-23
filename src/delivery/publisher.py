import os
import sys
import json
import time
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, Optional, List, Tuple, Union
from PIL import Image

from config.settings import Settings, STATE_DIR
from src.delivery.scheduler import VideoScheduler, scheduler, VN_TIMEZONE, UTC_TIMEZONE
from src.state.state_manager import StateManager


class Publisher:
    """
    Module phụ trách đăng tải và hẹn giờ xuất bản chính thức lên YouTube Data API v3
    và Facebook Graph API v20.0.
    
    Quy tắc an toàn:
    1. Cổng QA Gate: Chỉ đăng khi qa_report["passed"] == True.
    2. Khóa AUTO_PUBLISH: Chỉ kích hoạt upload thật khi Settings.AUTO_PUBLISH == True.
    3. Hẹn giờ Giờ Vàng: publish_at luôn >= now + 2h và <= 6 tháng.
    4. Cách ly lỗi: YouTube và Facebook chạy độc lập trong try...except riêng biệt.
    """

    @classmethod
    def is_auto_publish_enabled(cls) -> bool:
        """Kiểm tra cờ AUTO_PUBLISH từ Settings hoặc os.environ."""
        val = os.getenv("AUTO_PUBLISH", "false")
        if val.strip().lower() in ("true", "1", "yes"):
            return True
        return bool(getattr(Settings, "AUTO_PUBLISH", False))

    @staticmethod
    def sanitize_youtube_metadata(
        title: str,
        description: str,
        tags: Optional[List[str]] = None
    ) -> Tuple[str, str, List[str]]:
        """
        Làm sạch metadata theo tiêu chuẩn nghiêm ngặt của YouTube:
        - Title: Tối đa 95 ký tự, loại bỏ hoàn toàn các ký tự cấm '<' và '>'.
        - Description: Tối đa 4800 ký tự, loại bỏ '<' và '>'.
        - Tags: Tổng độ dài các tag không vượt quá 450 ký tự.
        """
        # Clean title
        clean_title = (title or "").replace("<", "").replace(">", "").strip()
        if len(clean_title) > 95:
            clean_title = clean_title[:92].rstrip() + "..."
        if not clean_title:
            clean_title = "Video Phim Hay Mới Nhất"

        # Clean description
        clean_desc = (description or "").replace("<", "").replace(">", "").strip()
        if len(clean_desc) > 4800:
            clean_desc = clean_desc[:4797].rstrip() + "..."

        # Default tags if not provided
        default_tags = [
            "review phim",
            "Cool Mèo Phim",
            "@CoolMeoPhim",
            "tóm tắt phim",
            "phim ngắn trung quốc",
            "phim hay",
            "phim mới",
            "phim full bộ"
        ]
        raw_tags = tags if tags else default_tags

        clean_tags = []
        total_len = 0
        for tag in raw_tags:
            tag_clean = str(tag).replace("<", "").replace(">", "").strip()
            if not tag_clean:
                continue
            if total_len + len(tag_clean) + 1 <= 450:
                clean_tags.append(tag_clean)
                total_len += len(tag_clean) + 1
            else:
                break

        return clean_title, clean_desc, clean_tags

    @staticmethod
    def get_youtube_service():
        """Khởi tạo Google YouTube Data API v3 Client từ Refresh Token."""
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
        import google.auth.transport.requests

        client_id = os.getenv("YT_CLIENT_ID") or Settings.YT_CLIENT_ID
        client_secret = os.getenv("YT_CLIENT_SECRET") or Settings.YT_CLIENT_SECRET
        refresh_token = os.getenv("YT_REFRESH_TOKEN") or Settings.YT_REFRESH_TOKEN

        if not (client_id and client_secret and refresh_token):
            raise ValueError("Thiếu thông tin xác thực YouTube (YT_CLIENT_ID, YT_CLIENT_SECRET, YT_REFRESH_TOKEN).")

        creds = Credentials(
            token=None,
            refresh_token=refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=client_id,
            client_secret=client_secret
        )
        req = google.auth.transport.requests.Request()
        creds.refresh(req)
        return build("youtube", "v3", credentials=creds)

    @classmethod
    def compress_thumbnail_if_needed(cls, thumbnail_path: Union[str, Path], max_bytes: int = 1900000) -> str:
        """
        Đảm bảo file ảnh thumbnail luôn < 1.9MB (giới hạn YouTube là 2MB).
        Nếu file vượt quá, nén lại bằng JPEG với quality giảm dần.
        """
        thumb_p = Path(thumbnail_path)
        if not thumb_p.exists():
            return str(thumbnail_path)

        file_size = thumb_p.stat().st_size
        if file_size <= max_bytes:
            return str(thumb_p)

        print(f"[Publisher] Dung lượng thumbnail ({file_size} bytes) vượt ngưỡng {max_bytes} bytes. Tiến hành tối ưu dung lượng...")
        out_p = thumb_p.parent / f"{thumb_p.stem}_compressed.jpg"

        with Image.open(thumb_p) as img:
            rgb_img = img.convert("RGB")
            for q in [90, 82, 75, 68]:
                rgb_img.save(out_p, "JPEG", quality=q, optimize=True)
                if out_p.stat().st_size <= max_bytes:
                    print(f"[Publisher] Thumbnail đã nén thành công (quality={q}, size={out_p.stat().st_size} bytes): {out_p.name}")
                    return str(out_p)

        return str(out_p)

    @classmethod
    def upload_to_youtube(
        cls,
        video_path: str,
        title: str,
        description: str,
        publish_at_utc: str,
        thumbnail_path: Optional[str] = None,
        tags: Optional[List[str]] = None,
        category_id: str = "24"
    ) -> Dict[str, Any]:
        """
        Upload video lên YouTube Data API v3 dạng Resumable Upload (chunks 10MB)
        và hẹn giờ phát sóng công khai (privacyStatus='private' + publishAt).
        """
        from googleapiclient.http import MediaFileUpload

        clean_title, clean_desc, clean_tags = cls.sanitize_youtube_metadata(title, description, tags)

        if not publish_at_utc.endswith("Z"):
            raise ValueError(f"publish_at_utc bắt buộc phải là định dạng ISO-8601 UTC kết thúc bằng 'Z': {publish_at_utc}")

        youtube = cls.get_youtube_service()

        body = {
            "snippet": {
                "title": clean_title,
                "description": clean_desc,
                "tags": clean_tags,
                "categoryId": category_id,
                "defaultLanguage": "vi",
                "defaultAudioLanguage": "vi"
            },
            "status": {
                "privacyStatus": "private",  # Bắt buộc private khi đặt lịch
                "publishAt": publish_at_utc,
                "selfDeclaredMadeForKids": False
            }
        }

        # Resumable upload chunk 10MB
        chunk_size = 10 * 1024 * 1024
        media = MediaFileUpload(
            video_path,
            mimetype="video/mp4",
            chunksize=chunk_size,
            resumable=True
        )

        request = youtube.videos().insert(
            part="snippet,status",
            body=body,
            media_body=media
        )

        print(f"[YouTube Upload] Bắt đầu upload video '{clean_title}'...")
        print(f"   - Lịch công chiếu UTC: {publish_at_utc}")

        response = None
        retries = 0
        max_retries = 5

        while response is None:
            try:
                status, response = request.next_chunk()
                if status:
                    pct = int(status.progress() * 100)
                    print(f"[YouTube Upload] Tiến độ upload: {pct}%")
                retries = 0
            except Exception as e:
                retries += 1
                if retries > max_retries:
                    raise RuntimeError(f"YouTube Upload thất bại sau {max_retries} lần retry: {e}")
                backoff = min(2 ** retries, 60)
                print(f"[YouTube Upload] Lỗi chunk ({e}). Thử lại lần {retries}/{max_retries} sau {backoff}s...")
                time.sleep(backoff)

        video_id = response.get("id")
        print(f"[YouTube Upload] ✅ Upload thành công! Video ID: {video_id}")

        # Set custom thumbnail if provided
        thumb_uploaded = False
        if thumbnail_path and Path(thumbnail_path).exists():
            try:
                safe_thumb_path = cls.compress_thumbnail_if_needed(thumbnail_path)
                thumb_media = MediaFileUpload(safe_thumb_path, mimetype="image/jpeg")
                youtube.thumbnails().set(
                    videoId=video_id,
                    media_body=thumb_media
                ).execute()
                thumb_uploaded = True
                print(f"[YouTube Upload] ✅ Đã cập nhật custom thumbnail thành công cho video {video_id}")
            except Exception as te:
                print(f"[YouTube Upload] ⚠️ Cảnh báo: Không thể tải custom thumbnail ({te})")

        return {
            "success": True,
            "platform": "youtube",
            "video_id": video_id,
            "video_url": f"https://www.youtube.com/watch?v={video_id}",
            "publish_at_utc": publish_at_utc,
            "thumbnail_uploaded": thumb_uploaded,
            "title": clean_title
        }

    @classmethod
    def upload_to_facebook(
        cls,
        video_path: str,
        title: str,
        description: str,
        scheduled_publish_timestamp: int,
        thumbnail_path: Optional[str] = None,
        page_id: Optional[str] = None,
        access_token: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Upload video lên Facebook Page qua Graph API v20.0 dùng 3-phase Resumable Upload
        và hẹn giờ đăng (published=False + scheduled_publish_time).
        """
        import requests

        target_page_id = (
            page_id
            or os.getenv("FB_PAGE_ID")
            or getattr(Settings, "FB_PAGE_ID", None)
        )
        token = (
            access_token
            or os.getenv("FB_PAGE_ACCESS_TOKEN")
            or os.getenv("FB_ACCESS_TOKEN")
            or getattr(Settings, "FB_PAGE_ACCESS_TOKEN", None)
            or getattr(Settings, "FB_ACCESS_TOKEN", None)
        )

        if not (target_page_id and token):
            raise ValueError("Thiếu thông tin xác thực Facebook (FB_PAGE_ID, FB_PAGE_ACCESS_TOKEN / FB_ACCESS_TOKEN).")

        v_path = Path(video_path)
        if not v_path.exists():
            raise FileNotFoundError(f"Không tìm thấy file video: {video_path}")

        file_size = v_path.stat().st_size
        base_url = f"https://graph-video.facebook.com/v20.0/{target_page_id}/videos"

        print(f"[Facebook Upload] Bắt đầu upload video '{title[:60]}' (size: {file_size} bytes)...")
        print(f"   - Lịch đăng timestamp: {scheduled_publish_timestamp}")

        # PHASE 1: START
        start_payload = {
            "access_token": token,
            "upload_phase": "start",
            "file_size": file_size
        }
        res_start = requests.post(base_url, data=start_payload, timeout=30)
        start_data = res_start.json()

        if "error" in start_data:
            raise RuntimeError(f"Facebook Graph API Start Error: {start_data['error'].get('message')}")

        upload_session_id = start_data.get("upload_session_id")
        video_id = start_data.get("video_id")
        start_offset = int(start_data.get("start_offset", 0))
        end_offset = int(start_data.get("end_offset", file_size))

        if not upload_session_id:
            raise RuntimeError(f"Facebook Graph API không trả về upload_session_id: {start_data}")

        # PHASE 2: TRANSFER
        with open(v_path, "rb") as vf:
            while start_offset < file_size:
                chunk_len = end_offset - start_offset
                vf.seek(start_offset)
                chunk_bytes = vf.read(chunk_len)

                transfer_payload = {
                    "access_token": token,
                    "upload_phase": "transfer",
                    "upload_session_id": upload_session_id,
                    "start_offset": start_offset
                }
                files = {
                    "video_file_chunk": ("chunk.mp4", chunk_bytes, "application/octet-stream")
                }

                # Retry up to 5 times for transfer
                transferred = False
                for attempt in range(5):
                    try:
                        res_transfer = requests.post(
                            base_url,
                            data=transfer_payload,
                            files=files,
                            timeout=180
                        )
                        t_data = res_transfer.json()
                        if "error" in t_data:
                            raise RuntimeError(f"Transfer error: {t_data['error'].get('message')}")
                        start_offset = int(t_data.get("start_offset", file_size))
                        end_offset = int(t_data.get("end_offset", file_size))
                        pct = min(100, int((start_offset / file_size) * 100))
                        print(f"[Facebook Upload] Tiến độ transfer: {pct}% ({start_offset}/{file_size} bytes)")
                        transferred = True
                        break
                    except Exception as ex:
                        if attempt == 4:
                            raise ex
                        backoff = min(2 ** (attempt + 1), 30)
                        print(f"[Facebook Upload] Lỗi chunk ({ex}), thử lại lần {attempt+1}/5 sau {backoff}s...")
                        time.sleep(backoff)

                if not transferred:
                    raise RuntimeError("Facebook chunk transfer thất bại.")

        # PHASE 3: FINISH
        clean_title = (title or "").strip()[:100]
        clean_desc = (description or "").strip()[:4500]

        finish_payload = {
            "access_token": token,
            "upload_phase": "finish",
            "upload_session_id": upload_session_id,
            "title": clean_title,
            "description": clean_desc,
            "published": False,  # Bắt buộc False khi hẹn giờ
            "scheduled_publish_time": int(scheduled_publish_timestamp)
        }

        res_finish = requests.post(base_url, data=finish_payload, timeout=60)
        finish_data = res_finish.json()

        if "error" in finish_data:
            raise RuntimeError(f"Facebook Graph API Finish Error: {finish_data['error'].get('message')}")

        final_video_id = finish_data.get("video_id") or finish_data.get("id") or video_id
        print(f"[Facebook Upload] ✅ Upload thành công! Video ID: {final_video_id}")

        # Upload custom thumbnail
        thumb_uploaded = False
        if thumbnail_path and Path(thumbnail_path).exists():
            try:
                thumb_url = f"https://graph-video.facebook.com/v20.0/{final_video_id}/thumbnails"
                with open(thumbnail_path, "rb") as tf:
                    t_res = requests.post(
                        thumb_url,
                        data={"access_token": token, "is_preferred": "true"},
                        files={"source": tf},
                        timeout=30
                    )
                    t_json = t_res.json()
                    if "error" not in t_json and (t_json.get("success") or "id" in t_json):
                        thumb_uploaded = True
                        print(f"[Facebook Upload] ✅ Đã cập nhật custom thumbnail thành công cho video {final_video_id}")
                    else:
                        print(f"[Facebook Upload] ⚠️ Không thể đặt custom thumbnail Facebook: {t_json}")
            except Exception as te:
                print(f"[Facebook Upload] ⚠️ Cảnh báo thumbnail Facebook: {te}")

        return {
            "success": True,
            "platform": "facebook",
            "video_id": final_video_id,
            "video_url": f"https://www.facebook.com/watch/?v={final_video_id}",
            "scheduled_publish_time": scheduled_publish_timestamp,
            "thumbnail_uploaded": thumb_uploaded,
            "title": clean_title
        }

    @classmethod
    def recalculate_golden_hour_slot(
        cls,
        movie_id: str,
        title: str,
        package_dir: str,
        custom_scheduler: Optional[VideoScheduler] = None,
        min_buffer_hours: int = 2
    ) -> Dict[str, Any]:
        """
        Tính toán lại Giờ Vàng đảm bảo:
        - Cách hiện tại ít nhất min_buffer_hours (mặc định 2 tiếng, hoặc 6 tiếng theo cấu hình).
        - Không trùng với slot đã book khác.
        - Nằm trong giới hạn tương lai hợp lệ của YouTube và Facebook (<= 6 tháng).
        """
        sched = custom_scheduler or scheduler
        now_vn = datetime.now(VN_TIMEZONE)

        # Kiểm tra slot tiếp theo thỏa mãn buffer
        # Nếu buffer của scheduler đang là 6 tiếng thì tự động thỏa mãn >= 2 tiếng
        effective_buffer = max(min_buffer_hours, sched.buffer_hours)
        min_allowed_time = now_vn + timedelta(hours=effective_buffer)

        # Sử dụng scheduler để phân bổ slot
        slot_info = sched.schedule_video(
            video_id=movie_id,
            title=title,
            package_dir=package_dir,
            from_time=now_vn,
            force_new=False
        )

        # Kiểm tra xem slot vừa lấy có cách hiện tại >= min_buffer_hours không
        slot_dt = datetime.fromtimestamp(slot_info["scheduled_publish_timestamp"], tz=VN_TIMEZONE)
        if slot_dt < min_allowed_time:
            print(f"[Publisher] Slot cũ ({slot_info.get('slot_key')}) quá gần hiện tại (< {effective_buffer}h). Đang phân bổ slot mới...")
            slot_info = sched.schedule_video(
                video_id=movie_id,
                title=title,
                package_dir=package_dir,
                from_time=min_allowed_time,
                force_new=True
            )

        return slot_info

    @classmethod
    def publish_package(
        cls,
        package_dir: Union[str, Path],
        custom_scheduler: Optional[VideoScheduler] = None,
        force_publish: bool = False
    ) -> Dict[str, Any]:
        """
        Đăng tải gói video chuẩn Gói Xét Duyệt 3 phần:
        1. Kiểm tra 100% Cổng QA Gate (qa_report.json -> passed == True).
        2. Kiểm tra cờ AUTO_PUBLISH.
        3. Tự động tính toán lại Golden Hour.
        4. Đăng tải độc lập lên YouTube và Facebook.
        5. Ghi kết quả vào publish_result.json và cập nhật state/processed_db.json.
        """
        pkg_dir = Path(package_dir).resolve()
        if not pkg_dir.exists():
            raise FileNotFoundError(f"Không tìm thấy thư mục gói: {package_dir}")

        # 1. CỔNG QA GATE BẮT BUỘC
        qa_file = pkg_dir / "qa_report.json"
        if not qa_file.exists():
            raise PermissionError(f"[QA GATE BLOCKED] Không tìm thấy qa_report.json trong {pkg_dir}. Nghiêm cấm upload!")

        with open(qa_file, "r", encoding="utf-8") as f:
            qa_report = json.load(f)

        if not qa_report.get("passed", False):
            raise PermissionError(
                f"[QA GATE BLOCKED] Gói xét duyệt không vượt qua cổng QA (passed={qa_report.get('passed')}). Nghiêm cấm upload!"
            )

        # 2. KIỂM TRA AUTO_PUBLISH
        auto_pub = force_publish or cls.is_auto_publish_enabled()

        # Tìm file video
        video_candidates = [
            pkg_dir / "final_review.mp4",
            pkg_dir / "video_1.mp4"
        ]
        video_file = None
        for cand in video_candidates:
            if cand.exists():
                video_file = cand
                break
        if not video_file:
            raise FileNotFoundError(f"Không tìm thấy file video hoàn chỉnh trong {pkg_dir}")

        # Tìm thumbnail (ưu tiên bản ngang 16:9 cho YouTube/Facebook video)
        thumb_candidates = [
            pkg_dir / "thumbnail_horizontal.jpg",
            pkg_dir / "thumbnail.jpg",
            pkg_dir / "thumbnail_song_thu_horizontal.jpg",
            pkg_dir / "thumbnail_tieu_thu_horizontal.jpg"
        ]
        thumb_file = None
        for cand in thumb_candidates:
            if cand.exists():
                thumb_file = cand
                break

        # Đọc SEO metadata
        seo_file = pkg_dir / "seo_metadata.json"
        seo_data = {}
        if seo_file.exists():
            try:
                with open(seo_file, "r", encoding="utf-8") as f:
                    seo_data = json.load(f)
            except Exception:
                pass

        # Lấy title & description
        title = seo_data.get("title") or pkg_dir.name
        description = seo_data.get("description") or f"Review trọn bộ phim {title} @CoolMeoPhim"
        tags = seo_data.get("tags") or []

        # Tên/ID phim
        movie_id = pkg_dir.name

        # Đọc Story Bible nếu có movie_id
        bible_file = pkg_dir / "story_bible.json"
        if bible_file.exists():
            try:
                with open(bible_file, "r", encoding="utf-8") as f:
                    sb = json.load(f)
                    if sb.get("movie_id"):
                        movie_id = sb["movie_id"]
                    if sb.get("vietnamese_title") and not seo_data.get("title"):
                        title = sb["vietnamese_title"]
            except Exception:
                pass

        # 3. TÍNH TOÁN LẠI KHUNG GIỜ VÀNG
        slot_info = cls.recalculate_golden_hour_slot(
            movie_id=movie_id,
            title=title,
            package_dir=str(pkg_dir),
            custom_scheduler=custom_scheduler,
            min_buffer_hours=2
        )

        publish_at_utc = slot_info["publish_at_utc"]
        scheduled_timestamp = slot_info["scheduled_publish_timestamp"]

        # Nếu AUTO_PUBLISH == False: chỉ lập lịch stub, không gọi API upload
        if not auto_pub:
            from src.delivery.publisher_stub import PublisherStub
            plan_path, plan_data = PublisherStub.generate_schedule_plan(
                video_path=str(video_file),
                title=title,
                description=description,
                package_dir=str(pkg_dir),
                thumbnail_path=str(thumb_file) if thumb_file else None,
                tags=tags,
                video_id=movie_id,
                custom_scheduler=custom_scheduler
            )
            return {
                "published": False,
                "locked": True,
                "safety_lock": "AUTO_PUBLISH_FALSE",
                "message": "AUTO_PUBLISH=false: Chỉ xuất Gói Xét Duyệt và file schedule_plan.json.",
                "schedule_plan_path": plan_path,
                "schedule_plan": plan_data
            }

        # 4. TIẾN HÀNH UPLOAD THẬT ĐỘC LẬP YOUTUBE & FACEBOOK
        print("=" * 70)
        print(f"🚀 KÍCH HOẠT HỆ THỐNG ĐĂNG TẢI TỰ ĐỘNG CHO: {title}")
        print(f"   - Giờ Vàng VN: {slot_info['slot_vn']} ({slot_info['golden_hour']} GMT+7)")
        print(f"   - YouTube UTC: {publish_at_utc}")
        print(f"   - Facebook Timestamp: {scheduled_timestamp}")
        print("=" * 70)

        yt_result: Dict[str, Any] = {"success": False}
        fb_result: Dict[str, Any] = {"success": False}

        # Đọc publish_result.json hiện có để tránh upload trùng lặp nếu 1 nền tảng đã thành công
        existing_result = {}
        publish_res_file = pkg_dir / "publish_result.json"
        if publish_res_file.exists():
            try:
                with open(publish_res_file, "r", encoding="utf-8") as rf:
                    existing_result = json.load(rf)
            except Exception:
                pass

        # A. UPLOAD YOUTUBE
        if existing_result.get("youtube", {}).get("success"):
            print(f"[YouTube Upload] ✅ Video đã được upload trước đó (ID: {existing_result['youtube'].get('video_id')}). Bỏ qua upload trùng lặp.")
            yt_result = existing_result["youtube"]
        else:
            try:
                yt_result = cls.upload_to_youtube(
                    video_path=str(video_file),
                    title=title,
                    description=description,
                    publish_at_utc=publish_at_utc,
                    thumbnail_path=str(thumb_file) if thumb_file else None,
                    tags=tags
                )
            except Exception as yt_err:
                print(f"[Publisher] ❌ Lỗi upload YouTube: {yt_err}")
                yt_result = {
                    "success": False,
                    "error": str(yt_err),
                    "platform": "youtube"
                }

        # B. UPLOAD FACEBOOK
        if existing_result.get("facebook", {}).get("success"):
            print(f"[Facebook Upload] ✅ Video đã được upload lên Facebook trước đó (ID: {existing_result['facebook'].get('video_id')}). Bỏ qua upload trùng lặp.")
            fb_result = existing_result["facebook"]
        else:
            try:
                fb_result = cls.upload_to_facebook(
                    video_path=str(video_file),
                    title=title,
                    description=description,
                    scheduled_publish_timestamp=scheduled_timestamp,
                    thumbnail_path=str(thumb_file) if thumb_file else None
                )
            except Exception as fb_err:
                print(f"[Publisher] ❌ Lỗi upload Facebook: {fb_err}")
                fb_result = {
                    "success": False,
                    "error": str(fb_err),
                    "platform": "facebook"
                }

        overall_success = yt_result.get("success", False) or fb_result.get("success", False)

        # 5. GHI KẾT QUẢ VÀO publish_result.json VÀ CẬP NHẬT STATE
        result_payload = {
            "movie_id": movie_id,
            "title": title,
            "package_dir": str(pkg_dir),
            "qa_passed": True,
            "auto_publish": True,
            "overall_success": overall_success,
            "published_at": datetime.now(VN_TIMEZONE).isoformat(),
            "schedule_slot": {
                "slot_vn": slot_info["slot_vn"],
                "golden_hour": slot_info["golden_hour"],
                "publish_at_utc": publish_at_utc,
                "scheduled_timestamp": scheduled_timestamp
            },
            "youtube": yt_result,
            "facebook": fb_result
        }

        publish_res_file = pkg_dir / "publish_result.json"
        with open(publish_res_file, "w", encoding="utf-8") as f:
            json.dump(result_payload, f, ensure_ascii=False, indent=2)

        # Cập nhật State DB
        if overall_success:
            StateManager.mark_completed(movie_id, {
                "title": title,
                "package_path": str(pkg_dir),
                "schedule_slot": slot_info["slot_vn"],
                "golden_hour": slot_info["golden_hour"],
                "youtube": yt_result,
                "facebook": fb_result
            })

        return result_payload

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
        Giao diện gọi tương thích hoàn toàn với main.py và PublisherStub.
        """
        pkg_d = package_dir or str(Path(video_path).resolve().parent)
        
        # Kiểm tra nếu AUTO_PUBLISH tắt thì trả về schedule plan stub
        if not cls.is_auto_publish_enabled():
            from src.delivery.publisher_stub import PublisherStub
            return PublisherStub.publish_video(
                video_path=video_path,
                title=title,
                description=description,
                thumbnail_path=thumbnail_path,
                tags=tags,
                package_dir=package_dir,
                video_id=video_id,
                from_time=from_time,
                custom_scheduler=custom_scheduler
            )

        # Khi AUTO_PUBLISH bật -> Chạy qua Cổng QA Gate và publish
        return cls.publish_package(
            package_dir=pkg_d,
            custom_scheduler=custom_scheduler
        )
