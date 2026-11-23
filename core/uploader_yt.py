import os
import json
import base64
import requests
from typing import Dict, Any, Optional, List
from core.config import Config
from core.logger import Logger

class YouTubeUploader:
    """Tải video lên YouTube tự động qua YouTube Data API v3 với OAuth refresh token."""

    @staticmethod
    def get_valid_access_token(
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        refresh_token: Optional[str] = None
    ) -> Optional[str]:
        """Tự động gia hạn access token bằng refresh token trong Config/.env."""
        client_id = client_id or Config.YT_CLIENT_ID
        client_secret = client_secret or Config.YT_CLIENT_SECRET
        refresh_token = refresh_token or Config.YT_REFRESH_TOKEN

        if not refresh_token:
            return None

        # 1. Thử refresh qua OAuth token endpoint nếu có Client ID & Secret
        if client_id and client_secret:
            try:
                res = requests.post("https://oauth2.googleapis.com/token", data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token"
                }, timeout=15)
                if res.status_code == 200:
                    new_acc = res.json().get("access_token")
                    if new_acc:
                        Logger.success("Gia hạn YouTube Access Token qua Google OAuth thành công!", "YOUTUBE")
                        return new_acc
            except Exception as e:
                Logger.warning(f"Lỗi refresh Google OAuth: {e}", "YOUTUBE")

        # 2. Thử refresh qua Google Playground service
        try:
            res = requests.post("https://developers.google.com/oauthplayground/refreshAccessToken", json={
                "refresh_token": refresh_token
            }, timeout=15)
            if res.status_code == 200:
                data = res.json()
                resp_body = data.get("Response", {}).get("message-body", "")
                if resp_body:
                    try:
                        token_info = json.loads(resp_body)
                    except Exception:
                        token_info = json.loads(base64.b64decode(resp_body).decode('utf-8', errors='ignore'))
                    new_acc = token_info.get("access_token")
                    if new_acc:
                        Logger.success("Gia hạn YouTube Access Token từ Playground thành công!", "YOUTUBE")
                        return new_acc
        except Exception as e:
            Logger.warning(f"Lỗi refresh token từ Playground: {e}", "YOUTUBE")

        return None

    @classmethod
    def upload(
        cls,
        video_path: str,
        title: str,
        description: str,
        thumbnail_path: Optional[str] = None,
        tags: Optional[List[str]] = None,
        privacy_status: str = "public",
        category_id: str = "24"
    ) -> Dict[str, Any]:
        """Tải video lên YouTube qua resumable upload stream."""
        access_token = cls.get_valid_access_token()

        if not access_token:
            Logger.warning("Chưa cấu hình YouTube Access Token. Đang chạy chế độ Dry-Run (Test).", "YOUTUBE")
            return {
                "success": True,
                "simulated": True,
                "message": "Dry-run: Video đã tạo thành công nhưng chưa có token YouTube để tải lên."
            }

        if not os.path.exists(video_path):
            return {"success": False, "error": f"Không tìm thấy file: {video_path}"}

        try:
            headers = {"Authorization": f"Bearer {access_token}"}
            if not tags:
                tags = [
                    "review phim", "review phim hay", "tóm tắt phim", "phim ngắn trung quốc",
                    "phim trung quốc", "phim mới", "phim full bộ", "review phim vi kịch"
                ]

            init_url = "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status"
            metadata = {
                "snippet": {
                    "title": title[:100],
                    "description": description[:4500],
                    "tags": tags[:30],
                    "categoryId": category_id,
                    "defaultLanguage": "vi",
                    "defaultAudioLanguage": "vi"
                },
                "status": {
                    "privacyStatus": privacy_status
                }
            }

            init_res = requests.post(init_url, json=metadata, headers=headers)
            if init_res.status_code != 200:
                raise Exception(f"Không thể khởi tạo upload YouTube: {init_res.text}")

            upload_url = init_res.headers.get("Location")
            if not upload_url:
                raise Exception("Không nhận được upload Location URL từ YouTube API")

            Logger.info(f"Đang truyền video stream lên YouTube: {os.path.basename(video_path)}...", "YOUTUBE")
            with open(video_path, "rb") as vf:
                up_res = requests.put(upload_url, data=vf, headers={"Content-Type": "video/mp4"})

            if up_res.status_code not in [200, 201]:
                raise Exception(f"Lỗi khi gửi video stream: {up_res.text}")

            video_data = up_res.json()
            new_video_id = video_data.get("id")
            Logger.success(f"Upload YouTube thành công! Video ID: {new_video_id}", "YOUTUBE")

            # Cập nhật thumbnail nếu có
            if thumbnail_path and os.path.exists(thumbnail_path) and new_video_id:
                cls.set_thumbnail(new_video_id, thumbnail_path)

            return {
                "success": True,
                "video_id": new_video_id,
                "message": f"Upload thành công! Video ID: {new_video_id}"
            }
        except Exception as e:
            Logger.error(f"Lỗi upload YouTube: {e}", "YOUTUBE")
            return {"success": False, "error": str(e)}

    @classmethod
    def set_thumbnail(cls, video_id: str, thumbnail_path: str) -> bool:
        """Cài đặt thumbnail cho video."""
        access_token = cls.get_valid_access_token()
        if not access_token:
            return False
        try:
            url = f"https://www.googleapis.com/upload/youtube/v3/thumbnails/set?videoId={video_id}"
            with open(thumbnail_path, "rb") as tf:
                res = requests.post(
                    url,
                    data=tf,
                    headers={"Authorization": f"Bearer {access_token}", "Content-Type": "image/jpeg"},
                    timeout=30
                )
            if res.status_code in [200, 201]:
                Logger.success(f"Đã cập nhật thumbnail cho video {video_id}", "YOUTUBE")
                return True
        except Exception as e:
            Logger.warning(f"Lỗi cài đặt thumbnail: {e}", "YOUTUBE")
        return False
