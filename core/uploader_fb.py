import os
import time
import requests
from typing import Dict, Any, Optional
from core.config import Config
from core.logger import Logger

class FacebookUploader:
    """Tải video lên Facebook Fanpage qua Facebook Graph API."""

    @classmethod
    def upload(
        cls,
        video_path: str,
        title: str,
        description: str,
        thumbnail_path: Optional[str] = None,
        page_id: Optional[str] = None,
        access_token: Optional[str] = None,
        scheduled_publish_time: Optional[int] = None
    ) -> Dict[str, Any]:
        page_id = page_id or Config.FB_PAGE_ID
        access_token = access_token or Config.FB_ACCESS_TOKEN

        if not page_id or not access_token:
            Logger.warning("Chưa cấu hình Facebook Page ID hoặc Access Token. Đang chạy Dry-Run.", "FACEBOOK")
            return {
                "success": True,
                "simulated": True,
                "message": "Dry-run: Video đã hoàn tất nhưng chưa có Token Facebook."
            }

        if not os.path.exists(video_path):
            return {"success": False, "error": f"Không tìm thấy file: {video_path}"}

        # Tự động lấy Page Access Token nếu token hiện tại là User Token
        try:
            pt_res = requests.get(
                f"https://graph.facebook.com/v19.0/{page_id}?fields=access_token&access_token={access_token}",
                timeout=15
            )
            pt_data = pt_res.json()
            if pt_data.get("access_token"):
                access_token = pt_data["access_token"]
        except Exception:
            pass

        file_size = os.path.getsize(video_path)
        file_size_mb = round(file_size / (1024 * 1024), 1)

        try:
            Logger.info(f"Bắt đầu upload lên Facebook Page {page_id} ({file_size_mb} MB)...", "FACEBOOK")
            api_url = f"https://graph-video.facebook.com/v19.0/{page_id}/videos"

            # 1. Khởi tạo phiên upload (Start)
            start_res = requests.post(
                api_url,
                data={
                    "upload_phase": "start",
                    "file_size": file_size,
                    "access_token": access_token
                },
                timeout=60
            )
            start_data = start_res.json()
            if "upload_session_id" not in start_data:
                err_msg = start_data.get("error", {}).get("message", start_res.text)
                raise Exception(f"Không thể khởi tạo upload Facebook: {err_msg}")

            session_id = start_data["upload_session_id"]
            video_id = start_data.get("video_id")
            start_offset = int(start_data.get("start_offset", 0))
            end_offset = int(start_data.get("end_offset", file_size))

            # 2. Truyền dữ liệu chunk (Transfer)
            chunk_size = 10 * 1024 * 1024
            with open(video_path, "rb") as f:
                while start_offset < file_size:
                    f.seek(start_offset)
                    bytes_to_read = min(end_offset - start_offset, chunk_size)
                    chunk_data = f.read(bytes_to_read)

                    progress = round((start_offset / file_size) * 100, 1)
                    uploaded_mb = round(start_offset / (1024 * 1024), 1)
                    Logger.info(f"Đang tải lên Facebook: {progress}% ({uploaded_mb}/{file_size_mb} MB)...", "FACEBOOK")

                    transfer_res = requests.post(
                        api_url,
                        data={
                            "upload_phase": "transfer",
                            "upload_session_id": session_id,
                            "start_offset": start_offset,
                            "access_token": access_token
                        },
                        files={"video_file_chunk": chunk_data},
                        timeout=300
                    )
                    transfer_data = transfer_res.json()
                    if "start_offset" not in transfer_data:
                        err_msg = transfer_data.get("error", {}).get("message", transfer_res.text)
                        raise Exception(f"Lỗi tải chunk: {err_msg}")

                    new_start = int(transfer_data["start_offset"])
                    new_end = int(transfer_data.get("end_offset", min(new_start + chunk_size, file_size)))
                    if new_start == start_offset:
                        break
                    start_offset = new_start
                    end_offset = new_end

            # 3. Hoàn tất & Xuất bản (Finish)
            finish_payload = {
                "upload_phase": "finish",
                "upload_session_id": session_id,
                "title": title[:100],
                "description": description,
                "access_token": access_token
            }
            if scheduled_publish_time and scheduled_publish_time > time.time() + 600:
                finish_payload["published"] = False
                finish_payload["scheduled_publish_time"] = int(scheduled_publish_time)
            else:
                finish_payload["published"] = True

            finish_res = requests.post(api_url, data=finish_payload, timeout=120)
            finish_data = finish_res.json()

            if finish_data.get("success", False):
                final_video_id = video_id or session_id
                fb_url = f"https://www.facebook.com/watch/?v={final_video_id}"
                Logger.success(f"Upload Facebook thành công! Link: {fb_url}", "FACEBOOK")

                if thumbnail_path and os.path.exists(thumbnail_path):
                    cls.set_thumbnail(final_video_id, thumbnail_path, access_token)

                return {
                    "success": True,
                    "video_id": final_video_id,
                    "url": fb_url,
                    "message": f"Upload Facebook thành công! Link: {fb_url}"
                }
            else:
                err_msg = finish_data.get("error", {}).get("message", finish_res.text)
                raise Exception(f"Facebook từ chối xuất bản: {err_msg}")
        except Exception as e:
            Logger.error(f"Lỗi upload Facebook: {e}", "FACEBOOK")
            return {"success": False, "error": str(e)}

    @classmethod
    def set_thumbnail(cls, video_id: str, thumbnail_path: str, access_token: str) -> bool:
        try:
            url = f"https://graph-video.facebook.com/v19.0/{video_id}/thumbnails"
            with open(thumbnail_path, "rb") as tf:
                res = requests.post(
                    url,
                    files={"source": tf},
                    data={"access_token": access_token},
                    timeout=30
                )
            if res.json().get("success"):
                Logger.success(f"Cập nhật thumbnail Facebook thành công cho video {video_id}", "FACEBOOK")
                return True
        except Exception as e:
            Logger.warning(f"Không thể cập nhật thumbnail Facebook: {e}", "FACEBOOK")
        return False
