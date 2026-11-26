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
    def is_youtube_auto_publish_enabled(cls) -> bool:
        """Kiểm tra cờ AUTO_PUBLISH_YOUTUBE (fallback về AUTO_PUBLISH nếu chưa đặt)."""
        val = os.getenv("AUTO_PUBLISH_YOUTUBE")
        if val is not None and val.strip():
            return val.strip().lower() in ("true", "1", "yes")
        if hasattr(Settings, "AUTO_PUBLISH_YOUTUBE"):
            return bool(Settings.AUTO_PUBLISH_YOUTUBE)
        gen = os.getenv("AUTO_PUBLISH", "false").strip().lower() in ("true", "1", "yes")
        return gen or bool(getattr(Settings, "AUTO_PUBLISH", False))

    @classmethod
    def is_facebook_auto_publish_enabled(cls) -> bool:
        """Kiểm tra cờ AUTO_PUBLISH_FACEBOOK (fallback về AUTO_PUBLISH nếu chưa đặt)."""
        val = os.getenv("AUTO_PUBLISH_FACEBOOK")
        if val is not None and val.strip():
            return val.strip().lower() in ("true", "1", "yes")
        if hasattr(Settings, "AUTO_PUBLISH_FACEBOOK"):
            return bool(Settings.AUTO_PUBLISH_FACEBOOK)
        gen = os.getenv("AUTO_PUBLISH", "false").strip().lower() in ("true", "1", "yes")
        return gen or bool(getattr(Settings, "AUTO_PUBLISH", False))

    @classmethod
    def is_auto_publish_enabled(cls) -> bool:
        """Kiểm tra nếu bất kỳ nền tảng nào (hoặc công tắc chung) được bật auto publish."""
        yt = cls.is_youtube_auto_publish_enabled()
        fb = cls.is_facebook_auto_publish_enabled()
        gen = os.getenv("AUTO_PUBLISH", "false").strip().lower() in ("true", "1", "yes") or bool(getattr(Settings, "AUTO_PUBLISH", False))
        return yt or fb or gen

    @classmethod
    def send_telegram_alert(cls, message: str) -> None:
        """Gửi cảnh báo khẩn cấp qua Telegram bot."""
        bot_token = os.getenv("TELEGRAM_BOT_TOKEN") or getattr(Settings, "TELEGRAM_BOT_TOKEN", None)
        chat_id = os.getenv("TELEGRAM_CHAT_ID") or getattr(Settings, "TELEGRAM_CHAT_ID", None)
        if not (bot_token and chat_id):
            return
        try:
            import requests
            requests.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json={"chat_id": chat_id, "text": message, "parse_mode": "Markdown"},
                timeout=10
            )
        except Exception:
            pass

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
        category_id: str = "24",
        skip_audit: bool = False
    ) -> Dict[str, Any]:
        """
        Quy trình upload YouTube 2 pha chuẩn:
        - Pha 1: Upload video ở trạng thái 'private', CHƯA gán publishAt.
        - Pha 2: Tự khám Content ID (bắt buộc ngâm tối thiểu 6 phút sau khi processed, chu kỳ poll 45s).
          + Nếu BỊ CHẶN: Tự động gọi youtube.videos().delete(id=video_id) xóa sạch khỏi kênh, báo Telegram và trả về copyright_blocked=True.
          + Nếu SẠCH BẢN QUYỀN: Gán lịch Giờ Vàng publishAt và cập nhật Custom Thumbnail 3 tầng.
        """
        from googleapiclient.http import MediaFileUpload

        clean_title, clean_desc, clean_tags = cls.sanitize_youtube_metadata(title, description, tags)

        if not publish_at_utc.endswith("Z"):
            raise ValueError(f"publish_at_utc bắt buộc phải là định dạng ISO-8601 UTC kết thúc bằng 'Z': {publish_at_utc}")

        youtube = cls.get_youtube_service()

        # PHA 1: Upload ở chế độ Private (CHƯA gán publishAt)
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
                "privacyStatus": "private",  # Khóa private, đợi tự khám Content ID
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

        print(f"[YouTube Upload] Bắt đầu Pha 1: Upload private video '{clean_title}'...")

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
        print(f"[YouTube Upload] ✅ Pha 1 hoàn tất! Video ID: {video_id} (Đang ở chế độ Private)")

        # PHA 2: TỰ KHÁM CONTENT ID VÀ TỰ XÓA NẾU BỊ CHẶN
        if not skip_audit:
            MIN_CONTENT_ID_SOAK_SEC = 360  # Bắt buộc ngâm tối thiểu 6 phút sau khi processed
            POLL_INTERVAL = 45  # Chu kỳ kiểm tra 45 giây/lần
            print(f"[YouTube Audit] Bắt đầu Pha 2: Tự khám Content ID cho video {video_id} (bắt buộc ngâm tối thiểu {MIN_CONTENT_ID_SOAK_SEC}s sau khi processed)...")
            
            total_elapsed = 0
            processed_at_elapsed = None
            soak_elapsed = 0
            is_blocked = False
            block_reason = ""
            max_total_wait = 1200  # Giới hạn an toàn tối đa 20 phút nếu YouTube processing quá lâu

            while True:
                time.sleep(POLL_INTERVAL)
                total_elapsed += POLL_INTERVAL

                try:
                    v_res = youtube.videos().list(
                        part="status,contentDetails",
                        id=video_id
                    ).execute()
                    items = v_res.get("items", [])
                    if not items:
                        print(f"[YouTube Audit] Cảnh báo: Không tìm thấy video {video_id} khi kiểm tra.")
                        continue

                    v_item = items[0]
                    v_status = v_item.get("status", {})
                    v_cd = v_item.get("contentDetails", {})
                    upload_status = v_status.get("uploadStatus", "")
                    rejection_reason = v_status.get("rejectionReason", "")
                    licensed_content = v_cd.get("licensedContent", False)
                    region_restriction = v_cd.get("regionRestriction", {})

                    blocked_regions = region_restriction.get("blocked", [])
                    allowed_regions = region_restriction.get("allowed", [])

                    if upload_status == "processed":
                        if processed_at_elapsed is None:
                            processed_at_elapsed = total_elapsed
                            print(f"[YouTube Audit] 🎯 Video đã 'processed' tại mốc {total_elapsed}s! Bắt đầu đếm ngược ngâm Content ID tối thiểu {MIN_CONTENT_ID_SOAK_SEC}s (6 phút)...")
                        soak_elapsed = total_elapsed - processed_at_elapsed
                        status_prefix = f"đã ngâm {soak_elapsed}s/{MIN_CONTENT_ID_SOAK_SEC}s"
                    else:
                        status_prefix = f"chờ processed (tổng {total_elapsed}s)"

                    print(
                        f"[YouTube Audit] ({total_elapsed}s | {status_prefix}) "
                        f"uploadStatus: '{upload_status}', licensedContent: {licensed_content}, "
                        f"rejectionReason: '{rejection_reason}', quốc gia bị chặn: {len(blocked_regions)}"
                    )

                    # 1. Bị từ chối do bản quyền
                    if upload_status in ("rejected", "failed") and rejection_reason == "copyright":
                        is_blocked = True
                        block_reason = f"Upload bị từ chối do vi phạm bản quyền: {rejection_reason}"
                        break

                    # 2. Bị chặn tại Việt Nam hoặc bị chặn diện rộng (>= 30 quốc gia)
                    if "VN" in blocked_regions or len(blocked_regions) >= 30:
                        is_blocked = True
                        block_reason = (
                            f"Chặn vùng lãnh thổ do bản quyền "
                            f"(số quốc gia bị chặn: {len(blocked_regions)}, chặn tại VN: {'VN' in blocked_regions})"
                        )
                        break

                    # 3. Chỉ cho phép danh sách quốc gia mà không có Việt Nam
                    if allowed_regions and "VN" not in allowed_regions:
                        is_blocked = True
                        block_reason = f"Chặn hiển thị tại Việt Nam (danh sách cho phép không có VN)"
                        break

                    # 4. Khi YouTube đã processed VÀ đã ngâm đủ MIN_CONTENT_ID_SOAK_SEC (6 phút)
                    if upload_status == "processed" and soak_elapsed >= MIN_CONTENT_ID_SOAK_SEC:
                        print(f"[YouTube Audit] ✅ Video đã ngâm đủ {MIN_CONTENT_ID_SOAK_SEC}s ({MIN_CONTENT_ID_SOAK_SEC//60} phút) sau khi processed và SẠCH BẢN QUYỀN 100%!")
                        break

                    if total_elapsed >= max_total_wait:
                        print(f"[YouTube Audit] ⚠️ Đã chạm ngưỡng thời gian tối đa {max_total_wait}s. Dừng chờ ngâm.")
                        break

                except Exception as audit_err:
                    print(f"[YouTube Audit] Cảnh báo lỗi truy vấn trạng thái: {audit_err}")

            # XỬ LÝ KHI BỊ CHẶN BẢN QUYỀN
            if is_blocked:
                print("=" * 70)
                print(f"[YouTube Audit] ❌ PHÁT HIỆN BỊ CHẶN BẢN QUYỀN: {block_reason}")
                print(f"[YouTube Audit] 🧹 Đang tự động XÓA SẠCH video {video_id} khỏi kênh...")
                print("=" * 70)
                try:
                    youtube.videos().delete(id=video_id).execute()
                    print(f"[YouTube Audit] ✅ ĐÃ XÓA VĨNH VIỄN video {video_id} khỏi kênh YouTube.")
                except Exception as del_err:
                    print(f"[YouTube Audit] ⚠️ Lỗi khi xóa video {video_id}: {del_err}")

                cls.send_telegram_alert(
                    f"🚨 *[Cool Mèo Phim] CẢNH BÁO BẢN QUYỀN YOUTUBE*\n\n"
                    f"🎬 *Video:* {clean_title}\n"
                    f"❌ *Lý do:* {block_reason}\n"
                    f"🧹 *Hành động:* Hệ thống đã tự động XÓA SẠCH video khỏi kênh và hủy bỏ gói."
                )

                return {
                    "success": False,
                    "copyright_blocked": True,
                    "video_id": video_id,
                    "reason": block_reason,
                    "platform": "youtube"
                }

        # PHA HOÀN TẤT: Video SẠCH BẢN QUYỀN -> Gán lịch Giờ Vàng & Set Custom Thumbnail
        print(f"[YouTube Schedule] Video SẠCH BẢN QUYỀN! Đang gán lịch công chiếu Giờ Vàng: {publish_at_utc}...")
        try:
            youtube.videos().update(
                part="snippet,status",
                body={
                    "id": video_id,
                    "snippet": {
                        "title": clean_title,
                        "description": clean_desc,
                        "tags": clean_tags,
                        "categoryId": category_id,
                        "defaultLanguage": "vi",
                        "defaultAudioLanguage": "vi"
                    },
                    "status": {
                        "privacyStatus": "private",  # Private chờ đến giờ công chiếu
                        "publishAt": publish_at_utc,
                        "selfDeclaredMadeForKids": False
                    }
                }
            ).execute()
            print(f"[YouTube Schedule] ✅ Đã gắn lịch công chiếu thành công ({publish_at_utc}).")
        except Exception as upd_err:
            print(f"[YouTube Schedule] ⚠️ Cảnh báo update status: {upd_err}")

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
            "copyright_blocked": False,
            "platform": "youtube",
            "video_id": video_id,
            "video_url": f"https://www.youtube.com/watch?v={video_id}",
            "publish_at_utc": publish_at_utc,
            "thumbnail_uploaded": thumb_uploaded,
            "title": clean_title
        }

    @classmethod
    def get_video_dimensions(cls, video_path: str) -> Tuple[int, int]:
        """Lấy chiều rộng và chiều cao của video bằng ffprobe (fallback 720x1280)."""
        import subprocess
        try:
            probe_cmd = [
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=width,height", "-of", "json", str(video_path)
            ]
            r_pr = subprocess.run(probe_cmd, capture_output=True, text=True, timeout=10)
            d_pr = json.loads(r_pr.stdout)
            for st in d_pr.get("streams", []):
                if "width" in st and "height" in st:
                    return int(st["width"]), int(st["height"])
        except Exception:
            pass
        return 720, 1280

    @classmethod
    def upload_to_facebook(
        cls,
        video_path: str,
        title: str,
        description: str,
        scheduled_publish_timestamp: int,
        thumbnail_path: Optional[str] = None,
        page_id: Optional[str] = None,
        access_token: Optional[str] = None,
        soak_seconds: Optional[int] = None
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

        # Tự động nhận diện hướng video (Dọc 9:16 hay Ngang 16:9) để gắn đúng tỷ lệ thumbnail Facebook
        v_w, v_h = cls.get_video_dimensions(str(v_path))
        is_vertical = v_h > v_w
        pkg_dir = Path(thumbnail_path).parent if thumbnail_path else v_path.parent

        chosen_thumb_path: Optional[Path] = None
        if is_vertical:
            # Video DỌC (Reels): BẮT BUỘC dùng thumbnail dọc (9:16, e.g. 720x1280). TUYỆT ĐỐI KHÔNG dùng thumbnail_horizontal.jpg!
            vert_candidates = [
                pkg_dir / "thumbnail.jpg",
                pkg_dir / "thumbnail_vertical.jpg",
                pkg_dir / "thumbnail_song_thu_vertical.jpg",
                pkg_dir / "thumbnail_tieu_thu_vertical.jpg"
            ]
            if thumbnail_path and Path(thumbnail_path).exists():
                tp = Path(thumbnail_path)
                if "horizontal" not in tp.name.lower():
                    try:
                        with Image.open(tp) as im:
                            if im.height >= im.width:
                                chosen_thumb_path = tp
                    except Exception:
                        chosen_thumb_path = tp
            if not chosen_thumb_path:
                for c in vert_candidates:
                    if c.exists():
                        chosen_thumb_path = c
                        break
        else:
            # Video NGANG (16:9): Dùng thumbnail ngang (16:9, e.g. 1280x720)
            horiz_candidates = [
                pkg_dir / "thumbnail_horizontal.jpg",
                pkg_dir / "thumbnail_song_thu_horizontal.jpg",
                pkg_dir / "thumbnail_tieu_thu_horizontal.jpg",
                pkg_dir / "thumbnail.jpg"
            ]
            if thumbnail_path and Path(thumbnail_path).exists():
                tp = Path(thumbnail_path)
                if "vertical" not in tp.name.lower():
                    try:
                        with Image.open(tp) as im:
                            if im.width >= im.height:
                                chosen_thumb_path = tp
                    except Exception:
                        chosen_thumb_path = tp
            if not chosen_thumb_path:
                for c in horiz_candidates:
                    if c.exists():
                        chosen_thumb_path = c
                        break

        # Upload custom thumbnail
        thumb_uploaded = False
        thumb_dims = ""
        if chosen_thumb_path and chosen_thumb_path.exists():
            t_w, t_h = 0, 0
            try:
                with Image.open(chosen_thumb_path) as im:
                    t_w, t_h = im.size
                thumb_dims = f"{t_w}x{t_h}"
            except Exception:
                thumb_dims = "unknown"

            orientation_label = "DỌC 9:16" if is_vertical else "NGANG 16:9"
            print(f"[Facebook Upload] 🖼️ Gắn custom thumbnail Facebook: {chosen_thumb_path.name} ({thumb_dims}), Video: {v_w}x{v_h} ({orientation_label})")

            try:
                thumb_url = f"https://graph-video.facebook.com/v20.0/{final_video_id}/thumbnails"
                with open(chosen_thumb_path, "rb") as tf:
                    t_res = requests.post(
                        thumb_url,
                        data={"access_token": token, "is_preferred": "true"},
                        files={"source": tf},
                        timeout=30
                    )
                    t_json = t_res.json()
                    if "error" not in t_json and (t_json.get("success") or "id" in t_json):
                        thumb_uploaded = True
                        print(f"[Facebook Upload] ✅ Đã cập nhật custom thumbnail thành công cho video {final_video_id} ({chosen_thumb_path.name} - {thumb_dims})")
                    else:
                        print(f"[Facebook Upload] ⚠️ Không thể đặt custom thumbnail Facebook: {t_json}")
            except Exception as te:
                print(f"[Facebook Upload] ⚠️ Cảnh báo thumbnail Facebook: {te}")

        # PHA NGÂM KIỂM TRA BẢN QUYỀN (SOAK CHECK 30S)
        soak_sec = soak_seconds if soak_seconds is not None else int(os.getenv("FB_SOAK_SECONDS", "30"))
        if soak_sec > 0:
            soak_res = cls.soak_and_verify_facebook_copyright(
                video_id=final_video_id,
                access_token=token,
                title=clean_title,
                soak_seconds=soak_sec,
                poll_interval=10
            )
            if soak_res.get("copyright_blocked"):
                return soak_res

        return {
            "success": True,
            "copyright_blocked": False,
            "platform": "facebook",
            "video_id": final_video_id,
            "video_url": f"https://www.facebook.com/watch/?v={final_video_id}",
            "scheduled_publish_time": scheduled_publish_timestamp,
            "thumbnail_uploaded": thumb_uploaded,
            "thumbnail_path": str(chosen_thumb_path) if chosen_thumb_path else None,
            "thumbnail_dimensions": thumb_dims,
            "is_vertical": is_vertical,
            "title": clean_title
        }

    @classmethod
    def verify_facebook_video_health(
        cls,
        video_id: str,
        access_token: str
    ) -> Tuple[bool, str]:
        """
        Kiểm tra độ an toàn của video trên Facebook:
        - Truy vấn Graph API v20.0 /{video_id}?fields=status,privacy,title
        - Phát hiện:
          + Lỗi bản quyền hoặc từ chối: status.video_status in ("error", "rejected", "copyright_violating", "blocked")
          + Lỗi xử lý: status.processing_phase.status == "error"
          + Lỗi công bố: status.publishing_phase.publish_status in ("failed", "rejected", "blocked")
          + Quyền xem bị giới hạn / bị gỡ: privacy rỗng/chặn hoặc Graph API trả về error
        Trả về: (is_healthy: bool, reason: str)
        """
        import requests
        try:
            url = f"https://graph.facebook.com/v20.0/{video_id}?fields=status,privacy,title&access_token={access_token}"
            res = requests.get(url, timeout=30)
            data = res.json()

            if "error" in data:
                err = data["error"]
                msg = err.get("message", "")
                code = err.get("code")
                if code in (100, 200, 10):
                    return False, f"Facebook API báo lỗi truy cập video (có thể đã bị gỡ hoặc hạn chế): {msg}"
                return False, f"Facebook API Error ({code}): {msg}"

            status_obj = data.get("status", {})
            v_status = str(status_obj.get("video_status", "")).lower()
            proc_status = str(status_obj.get("processing_phase", {}).get("status", "")).lower()
            pub_status = str(status_obj.get("publishing_phase", {}).get("publish_status", "")).lower()

            if v_status in ("error", "rejected", "copyright_violating", "blocked"):
                return False, f"Facebook video_status vi phạm/chặn: '{v_status}'"

            if proc_status == "error":
                return False, f"Facebook processing_phase gặp lỗi (error)"

            if pub_status in ("failed", "rejected", "blocked"):
                return False, f"Facebook publish_status bị chặn: '{pub_status}'"

            return True, "OK"
        except Exception as e:
            return False, f"Ngoại lệ khi kiểm tra Facebook video health: {e}"

    @classmethod
    def soak_and_verify_facebook_copyright(
        cls,
        video_id: str,
        access_token: str,
        title: str = "",
        soak_seconds: int = 30,
        poll_interval: int = 10
    ) -> Dict[str, Any]:
        """
        Ngâm kiểm tra bản quyền Facebook (Facebook Copyright Soak Check):
        - Mặc định ngâm 30 giây (có thể cấu hình qua FB_SOAK_SECONDS).
        - Định kỳ mỗi 10s kiểm tra trạng thái video qua Graph API.
        - NẾU PHÁT HIỆN BỊ CHẶN BẢN QUYỀN / TỪ CHỐI / LỖI TRUY CẬP:
          TỰ ĐỘNG GỌI DELETE https://graph.facebook.com/v20.0/{video_id} XÓA NGAY KHỎI FANPAGE.
          Bắn cảnh báo Telegram và trả về copyright_blocked=True.
        """
        import time
        import requests

        if soak_seconds <= 0:
            print(f"[Facebook Audit] FB_SOAK_SECONDS <= 0, bỏ qua ngâm kiểm tra.")
            return {"success": True, "copyright_blocked": False, "video_id": video_id}

        print("=" * 70)
        print(f"[Facebook Audit] 🛡️ Bắt đầu Ngâm Kiểm Tra Bản Quyền Facebook {soak_seconds}s ({soak_seconds//60} phút)...")
        print(f"   - Video ID: {video_id}")
        print(f"   - Tiêu đề: {title[:60]}")
        print("=" * 70)

        elapsed = 0
        is_blocked = False
        block_reason = ""

        while elapsed < soak_seconds:
            wait_step = min(poll_interval, soak_seconds - elapsed)
            time.sleep(wait_step)
            elapsed += wait_step

            healthy, reason = cls.verify_facebook_video_health(video_id, access_token)
            print(f"[Facebook Audit] ({elapsed}s/{soak_seconds}s) Health check: {'PASS' if healthy else 'FAIL'} - {reason}")

            if not healthy:
                r_lower = reason.lower()
                if any(k in r_lower for k in ("vi phạm", "bị chặn", "chặn", "copyright", "rejected", "error", "blocked")):
                    is_blocked = True
                    block_reason = reason
                    break

        if is_blocked:
            print("=" * 70)
            print(f"[Facebook Audit] ❌ PHÁT HIỆN BẢN QUYỀN / VI PHẠM FACEBOOK: {block_reason}")
            print(f"[Facebook Audit] 🧹 Đang tự động GỠ BỎ video {video_id} khỏi Fanpage ngay lập tức...")
            print("=" * 70)
            try:
                del_res = requests.delete(
                    f"https://graph.facebook.com/v20.0/{video_id}",
                    params={"access_token": access_token},
                    timeout=30
                )
                del_json = del_res.json()
                print(f"[Facebook Audit] Kết quả xóa video khỏi Fanpage: {del_json}")
            except Exception as del_err:
                print(f"[Facebook Audit] ⚠️ Lỗi khi gọi DELETE video {video_id}: {del_err}")

            cls.send_telegram_alert(
                f"🚨 *[Cool Mèo Phim] CẢNH BÁO BẢN QUYỀN FACEBOOK*\n\n"
                f"🎬 *Video:* {title}\n"
                f"❌ *Lý do:* {block_reason}\n"
                f"🧹 *Hành động:* Hệ thống đã tự động GỠ BỎ video khỏi Fanpage để bảo vệ kênh."
            )

            return {
                "success": False,
                "copyright_blocked": True,
                "video_id": video_id,
                "reason": block_reason,
                "platform": "facebook"
            }

        print(f"[Facebook Audit] ✅ Video đã ngâm đủ {soak_seconds}s ({soak_seconds//60} phút) trên Facebook và SẠCH BẢN QUYỀN 100%!")
        return {
            "success": True,
            "copyright_blocked": False,
            "video_id": video_id,
            "platform": "facebook"
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
        force_publish: bool = False,
        video_id: Optional[str] = None
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

        # 2. KIỂM TRA AUTO_PUBLISH THEO NỀN TẢNG
        auto_yt = force_publish or cls.is_youtube_auto_publish_enabled()
        auto_fb = force_publish or cls.is_facebook_auto_publish_enabled()
        auto_pub = auto_yt or auto_fb

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

        v_w, v_h = cls.get_video_dimensions(str(video_file))
        is_vertical = v_h > v_w

        # Tìm thumbnail phù hợp theo từng nền tảng:
        # YouTube: Luôn ưu tiên thumbnail ngang 16:9 cho chuẩn videos.insert
        thumb_yt_candidates = [
            pkg_dir / "thumbnail_horizontal.jpg",
            pkg_dir / "thumbnail_song_thu_horizontal.jpg",
            pkg_dir / "thumbnail_tieu_thu_horizontal.jpg",
            pkg_dir / "thumbnail.jpg"
        ]
        thumb_file_yt = next((c for c in thumb_yt_candidates if c.exists()), None)

        # Facebook: Video DỌC (Reels) BẮT BUỘC dùng thumbnail DỌC (9:16, e.g. 720x1280). Video NGANG dùng ngang 16:9.
        if is_vertical:
            thumb_fb_candidates = [
                pkg_dir / "thumbnail.jpg",
                pkg_dir / "thumbnail_vertical.jpg",
                pkg_dir / "thumbnail_song_thu_vertical.jpg",
                pkg_dir / "thumbnail_tieu_thu_vertical.jpg"
            ]
        else:
            thumb_fb_candidates = [
                pkg_dir / "thumbnail_horizontal.jpg",
                pkg_dir / "thumbnail_song_thu_horizontal.jpg",
                pkg_dir / "thumbnail_tieu_thu_horizontal.jpg",
                pkg_dir / "thumbnail.jpg"
            ]
        thumb_file_fb = next((c for c in thumb_fb_candidates if c.exists()), None)

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
        movie_id = video_id or seo_data.get("video_id") or seo_data.get("movie_id") or pkg_dir.name

        # Đọc Story Bible nếu có movie_id
        bible_file = pkg_dir / "story_bible.json"
        if bible_file.exists():
            try:
                with open(bible_file, "r", encoding="utf-8") as f:
                    sb = json.load(f)
                    if sb.get("movie_id") and not video_id:
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
                thumbnail_path=str(thumb_file_yt) if thumb_file_yt else None,
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

        # A. UPLOAD YOUTUBE (Chạy trước + Ngâm 6 phút kiểm duyệt Content ID)
        if auto_yt:
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
                        thumbnail_path=str(thumb_file_yt) if thumb_file_yt else None,
                        tags=tags
                    )
                except Exception as yt_err:
                    print(f"[Publisher] ❌ Lỗi upload YouTube: {yt_err}")
                    yt_result = {
                        "success": False,
                        "error": str(yt_err),
                        "platform": "youtube"
                    }

                # NẾU BỊ CHẶN BẢN QUYỀN: HỦY BỎ VÀ ĐÁNH DẤU COPYRIGHT_BLOCKED
                if yt_result.get("copyright_blocked"):
                    print(f"[Publisher] ⚠️ Video [{title}] bị chặn bản quyền YouTube. Đã tự động xóa video khỏi kênh.")
                    print(f"[Facebook Upload] 🛡️ KHÓA LIÊN HOÀN BẢO VỆ FANPAGE: YouTube bị chặn bản quyền Content ID -> TỰ ĐỘNG HỦY, KHÔNG ĐẨY VIDEO LÊN FACEBOOK (skipped_due_to_yt_copyright_block)!")
                    StateManager.mark_failed(movie_id, yt_result.get("reason", "copyright_blocked"), title)
                    result_payload = {
                        "movie_id": movie_id,
                        "title": title,
                        "package_dir": str(pkg_dir),
                        "qa_passed": True,
                        "copyright_blocked": True,
                        "overall_success": False,
                        "youtube": yt_result,
                        "facebook": {
                            "success": False,
                            "skipped": True,
                            "reason": "skipped_due_to_yt_copyright_block",
                            "platform": "facebook"
                        }
                    }
                    with open(publish_res_file, "w", encoding="utf-8") as f:
                        json.dump(result_payload, f, ensure_ascii=False, indent=2)
                    return result_payload
        else:
            print("[YouTube Upload] AUTO_PUBLISH_YOUTUBE=false: Bỏ qua upload YouTube.")
            yt_result = {"success": False, "skipped": True, "platform": "youtube"}

        # B. UPLOAD FACEBOOK (Chỉ chạy khi YouTube an toàn sạch sẽ hoặc khi tắt YouTube)
        if auto_fb:
            if auto_yt and yt_result.get("copyright_blocked"):
                print(f"[Facebook Upload] 🛡️ Bỏ qua Facebook do YouTube bị chặn bản quyền (skipped_due_to_yt_copyright_block): {title}")
                fb_result = {
                    "success": False,
                    "skipped": True,
                    "reason": "skipped_due_to_yt_copyright_block",
                    "platform": "facebook"
                }
            elif existing_result.get("facebook", {}).get("success"):
                print(f"[Facebook Upload] ✅ Video đã được upload lên Facebook trước đó (ID: {existing_result['facebook'].get('video_id')}). Bỏ qua upload trùng lặp.")
                fb_result = existing_result["facebook"]
            else:
                try:
                    fb_result = cls.upload_to_facebook(
                        video_path=str(video_file),
                        title=title,
                        description=description,
                        scheduled_publish_timestamp=scheduled_timestamp,
                        thumbnail_path=str(thumb_file_fb) if thumb_file_fb else None
                    )
                except Exception as fb_err:
                    print(f"[Publisher] ❌ Lỗi upload Facebook: {fb_err}")
                    fb_result = {
                        "success": False,
                        "error": str(fb_err),
                        "platform": "facebook"
                    }

                # NẾU FACEBOOK BỊ CHẶN BẢN QUYỀN
                if fb_result.get("copyright_blocked"):
                    print(f"[Publisher] ⚠️ Video [{title}] bị chặn bản quyền Facebook. Đã tự động gỡ video khỏi Fanpage.")
                    StateManager.mark_failed(movie_id, fb_result.get("reason", "copyright_blocked_facebook"), title)
        else:
            print("[Facebook Upload] AUTO_PUBLISH_FACEBOOK=false: Bỏ qua upload Facebook.")
            fb_result = {"success": False, "skipped": True, "platform": "facebook"}

        yt_ok = (not auto_yt) or (yt_result.get("success", False) and not yt_result.get("copyright_blocked", False))
        fb_ok = (not auto_fb) or (fb_result.get("success", False) and not fb_result.get("copyright_blocked", False))
        has_at_least_one = (auto_yt and yt_result.get("success", False)) or (auto_fb and fb_result.get("success", False))
        overall_success = yt_ok and fb_ok and has_at_least_one

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
            custom_scheduler=custom_scheduler,
            video_id=video_id
        )
