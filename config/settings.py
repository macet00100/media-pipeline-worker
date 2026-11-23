import os
import json
from pathlib import Path
from typing import List, Dict, Any, Optional

# Base Directories
BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = BASE_DIR / "config"
ASSETS_DIR = BASE_DIR / "assets"
STATE_DIR = BASE_DIR / "state"
OUTPUT_DIR = BASE_DIR / "output"
TMP_DIR = BASE_DIR / "tmp"

# Create directories
for directory in [STATE_DIR, OUTPUT_DIR, TMP_DIR, ASSETS_DIR / "bgm", ASSETS_DIR / "fonts"]:
    directory.mkdir(parents=True, exist_ok=True)

def _load_env_file():
    """Load key-values from .env if present."""
    env_path = BASE_DIR / ".env"
    if env_path.exists():
        try:
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        k = k.strip()
                        v = v.strip().strip("'\"")
                        if k and k not in os.environ:
                            os.environ[k] = v
        except Exception as e:
            print(f"[SETTINGS] Warning loading .env: {e}")

_load_env_file()

class Settings:
    """Hệ thống thiết lập tập trung cho pipeline sản xuất video Cool Mèo Phim."""

    # 1. Pipeline Control & Automation Lock
    # Mặc định là False nếu không cấu hình; bật bằng AUTO_PUBLISH=true trong .env hoặc GitHub Variables
    AUTO_PUBLISH: bool = os.getenv("AUTO_PUBLISH", "false").strip().lower() in ("true", "1", "yes")
    # Công tắc tách rời cho từng nền tảng (fallback về AUTO_PUBLISH nếu chưa định nghĩa riêng)
    AUTO_PUBLISH_YOUTUBE: bool = (
        os.getenv("AUTO_PUBLISH_YOUTUBE", "").strip().lower() in ("true", "1", "yes")
        if os.getenv("AUTO_PUBLISH_YOUTUBE") is not None
        else AUTO_PUBLISH
    )
    AUTO_PUBLISH_FACEBOOK: bool = (
        os.getenv("AUTO_PUBLISH_FACEBOOK", "").strip().lower() in ("true", "1", "yes")
        if os.getenv("AUTO_PUBLISH_FACEBOOK") is not None
        else AUTO_PUBLISH
    )

    # Golden Hours & Video Scheduling (GMT+7)
    GOLDEN_HOURS_VN: str = os.getenv("GOLDEN_HOURS_VN", "11:30,17:30,20:30")
    PRE_UPLOAD_BUFFER_HOURS: int = int(os.getenv("PRE_UPLOAD_BUFFER_HOURS", "6"))
    SCHEDULE_CALENDAR_FILE: Path = STATE_DIR / "schedule_calendar.json"

    MAX_VIDEOS_PER_RUN: int = int(os.getenv("MAX_VIDEOS_PER_RUN", "1"))
    JOB_TIMEOUT_MINUTES: int = int(os.getenv("JOB_TIMEOUT_MINUTES", "45"))

    # Daily Curation Targets & Mix Ratio (Bộ lọc & Chấm điểm Phim Hay)
    DAILY_TARGET_VIDEOS: int = int(os.getenv("DAILY_TARGET_VIDEOS", "3"))
    DAILY_MIX_SHORT: int = int(os.getenv("DAILY_MIX_SHORT", "2"))
    DAILY_MIX_LONG: int = int(os.getenv("DAILY_MIX_LONG", "1"))
    CURATION_REPORT_FILE: Path = BASE_DIR / "daily_curation_report.json"
    PRODUCTION_QUEUE_FILE: Path = STATE_DIR / "production_queue.json"

    # 2. Source APIs (Lượng Tử & Phi Phàm) - Bắt buộc nạp từ biến môi trường
    LZ_API_URL: str = os.getenv("LZ_API_URL", "")
    FF_API_URL: str = os.getenv("FF_API_URL", "")
    HTTP_PROXY: Optional[str] = os.getenv("HTTP_PROXY") or os.getenv("HTTPS_PROXY") or None

    # 3. Gemini Multimodal / Structured LLM
    _gemini_env = os.getenv("GEMINI_API_KEYS") or os.getenv("GEMINI_API_KEY") or ""
    GEMINI_API_KEYS: List[str] = [k.strip() for k in _gemini_env.split(",") if k.strip()]

    # 4. Brand & Channels
    CHANNEL_HANDLE: str = "@CoolMeoPhim"
    CHANNEL_NAME: str = "Cool Mèo Phim"

    # 5. Audio Settings
    TTS_VOICE: str = "vi-VN-NamMinhNeural"  # KHÓA CỨNG: Tuyệt đối không đổi giọng
    SPEECH_WORDS_PER_SEC: float = 2.95      # Định mức từ chuẩn
    TARGET_AUDIO_COVERAGE_MIN: float = 0.85 # Độ phủ tối thiểu 85%
    TARGET_AUDIO_COVERAGE_MAX: float = 0.95 # Độ phủ tối đa 95%
    MAX_ALLOWABLE_SILENCE_SEC: float = 3.5  # Khoảng lặng chết tối đa cho phép

    # 6. Video Specifications (Chuẩn hóa 720x1280 30fps)
    VIDEO_WIDTH: int = 720
    VIDEO_HEIGHT: int = 1280
    VIDEO_FPS: int = 30
    VIDEO_PIX_FMT: str = "yuv420p"

    # Frosted Glass Subtitle Mask Coordinates
    FROSTED_GLASS_X: int = 0
    FROSTED_GLASS_Y: int = 816
    FROSTED_GLASS_W: int = 720
    FROSTED_GLASS_H: int = 96
    FROSTED_GLASS_CENTER_Y: int = 864

    # Watermark Logo
    LOGO_SIZE: int = 120
    LOGO_X: int = 32
    LOGO_Y: int = 32

    # Volume balance (Tắt 100% âm thanh phim gốc để chống Audio Content ID)
    VOL_ORIGINAL_FILM: float = 0.0
    VOL_TTS_VOICE: float = 1.50
    VOL_BGM: float = 0.06
    VOL_OUTRO: float = 1.00  # 100% âm thanh sống động gốc của outtro

    # 7. Credentials for Publisher (YouTube / Facebook)
    YT_CLIENT_ID: str = os.getenv("YT_CLIENT_ID", "")
    YT_CLIENT_SECRET: str = os.getenv("YT_CLIENT_SECRET", "")
    YT_REFRESH_TOKEN: str = os.getenv("YT_REFRESH_TOKEN", "")
    FB_PAGE_ID: str = os.getenv("FB_PAGE_ID", "")
    FB_ACCESS_TOKEN: str = os.getenv("FB_PAGE_ACCESS_TOKEN") or os.getenv("FB_ACCESS_TOKEN", "")
    FB_PAGE_ACCESS_TOKEN: str = os.getenv("FB_PAGE_ACCESS_TOKEN") or os.getenv("FB_ACCESS_TOKEN", "")

    # 8. Telegram Notification for Review Package
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")

    @classmethod
    def load_blacklist(cls) -> Dict[str, Any]:
        """Tải danh sách đen từ file config/blacklist.json."""
        bl_file = CONFIG_DIR / "blacklist.json"
        if bl_file.exists():
            try:
                with open(bl_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                print(f"[SETTINGS] Lỗi tải blacklist.json: {e}")
        return {"gambling_keywords": [], "forbidden_channel_names": [], "copyright_high_risk": []}

    @classmethod
    def get_font_path(cls) -> str:
        """Lấy đường dẫn font chữ có hỗ trợ tiếng Việt đầy đủ."""
        # Ưu tiên font trong assets/fonts
        for name in ["Arial.ttf", "font.ttf", "LiberationSans-Bold.ttf", "LiberationSans-Regular.ttf"]:
            f_path = ASSETS_DIR / "fonts" / name
            if f_path.exists():
                return str(f_path)
        # Hệ điều hành Windows
        win_arial = Path("C:/Windows/Fonts/arial.ttf")
        if win_arial.exists():
            return str(win_arial)
        # Hệ điều hành Linux / Ubuntu
        linux_liberation = Path("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf")
        if linux_liberation.exists():
            return str(linux_liberation)
        return "Arial"

# Tự động kích hoạt bộ lọc bảo mật Redactor che kín Secrets & URLs
try:
    from src.security.redactor import install_secret_redactor
    install_secret_redactor()
except Exception:
    pass
