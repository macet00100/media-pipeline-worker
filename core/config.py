import os
from typing import Dict, Any

# Đường dẫn thư mục gốc dự án
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
ENV_FILE = os.path.join(BASE_DIR, ".env")
STORAGE_DIR = os.path.join(BASE_DIR, "storage")
TEMP_DIR = os.path.join(STORAGE_DIR, "temp")
ASSETS_DIR = os.path.join(STORAGE_DIR, "assets")
OUTPUT_DIR = os.path.join(STORAGE_DIR, "output")

def load_dotenv():
    """Tự động nạp các biến môi trường từ file .env nếu có."""
    if not os.path.exists(ENV_FILE):
        return
    try:
        with open(ENV_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, val = line.split("=", 1)
                    key = key.strip()
                    val = val.strip().strip("'\"")
                    if key and key not in os.environ:
                        os.environ[key] = val
    except Exception as e:
        print(f"[CONFIG] Không thể đọc file .env: {e}")

# Nạp .env ngay khi nạp module
load_dotenv()

# Tự động tạo các thư mục lưu trữ cần thiết
for d in [STORAGE_DIR, TEMP_DIR, ASSETS_DIR, OUTPUT_DIR]:
    os.makedirs(d, exist_ok=True)

class Config:
    """Quản lý tập trung các cấu hình và kết nối dịch vụ."""

    # AI Service
    GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()

    # YouTube API Credentials
    YT_CLIENT_ID = os.environ.get("YT_CLIENT_ID", "").strip()
    YT_CLIENT_SECRET = os.environ.get("YT_CLIENT_SECRET", "").strip()
    YT_REFRESH_TOKEN = os.environ.get("YT_REFRESH_TOKEN", "").strip()

    # Facebook Fanpage Credentials
    FB_PAGE_ID = os.environ.get("FB_PAGE_ID", "").strip()
    FB_ACCESS_TOKEN = os.environ.get("FB_ACCESS_TOKEN", "").strip()

    @classmethod
    def reload(cls):
        """Nạp lại các biến môi trường từ .env."""
        load_dotenv()
        cls.GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
        cls.YT_CLIENT_ID = os.environ.get("YT_CLIENT_ID", "").strip()
        cls.YT_CLIENT_SECRET = os.environ.get("YT_CLIENT_SECRET", "").strip()
        cls.YT_REFRESH_TOKEN = os.environ.get("YT_REFRESH_TOKEN", "").strip()
        cls.FB_PAGE_ID = os.environ.get("FB_PAGE_ID", "").strip()
        cls.FB_ACCESS_TOKEN = os.environ.get("FB_ACCESS_TOKEN", "").strip()

    @classmethod
    def get_status(cls) -> Dict[str, bool]:
        """Kiểm tra tình trạng các kết nối đã nạp."""
        return {
            "gemini_api_key": bool(cls.GEMINI_API_KEY),
            "youtube_configured": bool(cls.YT_CLIENT_ID and cls.YT_REFRESH_TOKEN),
            "facebook_configured": bool(cls.FB_PAGE_ID and cls.FB_ACCESS_TOKEN),
        }
