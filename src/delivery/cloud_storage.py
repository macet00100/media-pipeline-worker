import os
import shutil
import subprocess
from pathlib import Path
from typing import Dict, Any, Optional, List
from config.settings import OUTPUT_DIR, BASE_DIR

class CloudStorageManager:
    """
    Quản lý lưu trữ đám mây cho video xuất xưởng:
    - Cơ chế rclone stub cho các file dung lượng vượt ngưỡng 2GB (> 2GB).
    - Hỗ trợ cả Google Drive ('gdrive:') và Cloudflare R2 ('r2:').
    - Cơ chế TỰ PHỤC HỒI (Fail-Safe): Tự động fallback xuất thẳng vào thư mục output/
      nếu rclone chưa được cài đặt, chưa có remote cấu hình hoặc upload gặp sự cố.
    """

    # Ngưỡng kích hoạt lưu trữ đám mây: 2 GB (2,147,483,648 bytes)
    SIZE_THRESHOLD_BYTES: int = int(os.getenv("CLOUD_SIZE_THRESHOLD_BYTES", 2 * 1024 * 1024 * 1024))

    # Cấu hình remote
    RCLONE_REMOTE: str = os.getenv("RCLONE_REMOTE", "").strip()
    RCLONE_GDRIVE_REMOTE: str = os.getenv("RCLONE_GDRIVE_REMOTE", "gdrive:CoolMeoPhim").strip()
    RCLONE_R2_REMOTE: str = os.getenv("RCLONE_R2_REMOTE", "r2:coolmeophim").strip()

    @classmethod
    def is_rclone_installed(cls) -> bool:
        """Kiểm tra sự tồn tại của tiện ích dòng lệnh rclone."""
        rclone_path = shutil.which("rclone")
        if not rclone_path:
            return False
        try:
            res = subprocess.run(["rclone", "version"], capture_output=True, text=True, timeout=5)
            return res.returncode == 0
        except Exception:
            return False

    @classmethod
    def get_configured_remotes(cls) -> List[str]:
        """Lấy danh sách các remote đã cấu hình trong rclone."""
        if not cls.is_rclone_installed():
            return []
        try:
            res = subprocess.run(["rclone", "listremotes"], capture_output=True, text=True, timeout=5)
            if res.returncode == 0:
                return [r.strip() for r in res.stdout.strip().splitlines() if r.strip()]
        except Exception:
            pass
        return []

    @classmethod
    def get_active_remote(cls) -> Optional[str]:
        """Xác định remote phù hợp nhất (ưu tiên biến môi trường hoặc tìm remote có sẵn)."""
        if cls.RCLONE_REMOTE:
            return cls.RCLONE_REMOTE

        remotes = cls.get_configured_remotes()
        # Ưu tiên Google Drive hoặc R2
        for r in remotes:
            if "gdrive" in r.lower():
                return r
        for r in remotes:
            if "r2" in r.lower():
                return r

        return remotes[0] if remotes else None

    @classmethod
    def handle_video_delivery(
        cls,
        source_file_path: str,
        drama_slug: Optional[str] = None,
        custom_remote: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Xử lý bàn giao file video thành phẩm:
        - Nếu file > 2GB và có rclone: Đẩy lên Cloud Storage (Google Drive / Cloudflare R2).
        - Nếu file <= 2GB hoặc rclone chưa khả dụng: Xuất thẳng vào output/.
        """
        if not os.path.exists(source_file_path):
            raise FileNotFoundError(f"File nguồn không tồn tại: {source_file_path}")

        file_size = os.path.getsize(source_file_path)
        file_size_mb = file_size / (1024 * 1024)
        file_name = Path(source_file_path).name

        print(f"[CloudStorage] Kiểm tra file: {file_name} ({file_size_mb:.2f} MB)")

        # 1. TRƯỜNG HỢP FILE > 2GB: THỬ NGHIỆM ĐẨY LÊN CLOUD BẰNG RCLONE
        if file_size > cls.SIZE_THRESHOLD_BYTES:
            print(f"[CloudStorage] Phát hiện file vượt ngưỡng 2GB (> 2GB). Kích hoạt quy trình Cloud Storage...")
            active_remote = custom_remote or cls.get_active_remote()

            if cls.is_rclone_installed() and active_remote:
                remote_dest = f"{active_remote}/{drama_slug or 'general'}/{file_name}".replace("//", "/")
                print(f"[CloudStorage] Đang tải lên qua rclone -> {remote_dest}...")
                try:
                    cmd = [
                        "rclone", "copyto",
                        source_file_path,
                        remote_dest,
                        "--transfers", "4",
                        "--retries", "3",
                        "--low-level-retries", "10"
                    ]
                    res = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
                    if res.returncode == 0:
                        print(f"[CloudStorage] Tải lên Cloud thành công: {remote_dest}")
                        return {
                            "status": "cloud_uploaded",
                            "is_cloud": True,
                            "remote": active_remote,
                            "remote_path": remote_dest,
                            "size_bytes": file_size,
                            "size_mb": file_size_mb,
                            "file_name": file_name
                        }
                    else:
                        print(f"[CloudStorage] Lỗi lệnh rclone ({res.stderr[:200]}). Kích hoạt Fallback.")
                except Exception as e:
                    print(f"[CloudStorage] Sự cố rclone ({e}). Kích hoạt Fallback.")
            else:
                reason = "rclone chưa cài đặt" if not cls.is_rclone_installed() else "chưa cấu hình remote"
                print(f"[CloudStorage] [RCLONE STUB] Không thể đẩy lên cloud ({reason}). Kích hoạt Fallback xuất thẳng output/.")

        # 2. FALLBACK TỰ PHỤC HỒI HOẶC FILE <= 2GB: XUẤT THẲNG RA THƯ MỤC OUTPUT/
        dest_dir = OUTPUT_DIR / (drama_slug or "")
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_file = dest_dir / file_name

        # Nếu source_file khác dest_file thì copy hoặc move
        if Path(source_file_path).resolve() != dest_file.resolve():
            try:
                shutil.copy2(source_file_path, dest_file)
                print(f"[CloudStorage] Đã xuất file thành công vào: {dest_file}")
            except Exception as e:
                print(f"[CloudStorage] Lỗi copy vào output: {e}")
                dest_file = Path(source_file_path)

        is_large_file = file_size > cls.SIZE_THRESHOLD_BYTES
        return {
            "status": "fallback_local" if is_large_file else "standard_local",
            "is_cloud": False,
            "output_path": str(dest_file),
            "size_bytes": file_size,
            "size_mb": file_size_mb,
            "file_name": file_name,
            "note": "Xuất thẳng thư mục output/ (Fallback an toàn)" if is_large_file else "Xuất thẳng thư mục output/"
        }
