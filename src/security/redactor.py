import os
import re
import sys
import logging
from typing import List, Any

class SecretRedactingFilter(logging.Filter):
    """
    Bộ lọc tự động che kín mọi thông tin nhạy cảm (API Keys, Token, M3U8 URL)
    trong tất cả log xuất ra console và file trên Public Repository.
    """

    PATTERNS = [
        re.compile(r"AI" + r"za[0-9A-Za-z\-_]{25,50}"),                            # Google Gemini API key
        re.compile(r"EA" + r"A[0-9A-Za-z\-_]{20,}"),                               # Facebook Page/User Access Token
        re.compile(r"ya" + r"29\.[0-9A-Za-z\-_]+"),                                # Google OAuth Access Token
        re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{30,40}\b"),                         # Telegram Bot Token
        re.compile(r"bot\d+:[A-Za-z0-9_-]+", re.IGNORECASE),                      # Telegram bot endpoint
        re.compile(r"Bearer\s+[A-Za-z0-9._~+/-]+=*", re.IGNORECASE),             # Bearer Token
        re.compile(r"https?://[^\s\"']+\.m3u8[^\s\"']*", re.IGNORECASE),           # Direct M3U8 Stream URL
        re.compile(r"(?:https?://)?(?:cj\.lziapi\.com|api\.ffzyapi\.com)[^\s\"']*", re.IGNORECASE) # Direct MacCMS endpoints
    ]

    @classmethod
    def redact_text(cls, text: str) -> str:
        """Thay thế tất cả các chuỗi nhạy cảm bằng [REDACTED]."""
        if not isinstance(text, str):
            return text
        res = text
        for pat in cls.PATTERNS:
            res = pat.sub("[REDACTED]", res)

        # Che thêm các giá trị cấu hình từ biến môi trường nếu có
        for env_var in [
            "GEMINI_API_KEY", "LZ_API_URL", "FF_API_URL", "FB_PAGE_ID",
            "FB_ACCESS_TOKEN", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
            "YT_CLIENT_SECRET", "YT_REFRESH_TOKEN", "RCLONE_CONFIG_BASE64"
        ]:
            val = os.getenv(env_var, "").strip()
            if val and len(val) >= 4 and val in res:
                res = res.replace(val, "[REDACTED]")

        # Xử lý riêng danh sách GEMINI_API_KEYS (nhiều key phân cách bởi dấu phẩy)
        all_keys = os.getenv("GEMINI_API_KEYS", "")
        if all_keys:
            for k in all_keys.split(","):
                k_clean = k.strip()
                if k_clean and len(k_clean) >= 6 and k_clean in res:
                    res = res.replace(k_clean, "[REDACTED]")

        return res

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = self.redact_text(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: self.redact_text(str(v)) for k, v in record.args.items()}
            elif isinstance(record.args, tuple):
                record.args = tuple(self.redact_text(str(v)) for v in record.args)
        return True

class RedactingStream:
    """Wrapper cho stdout/stderr để tự động redact mọi lệnh print()."""
    def __init__(self, target_stream):
        self._target = target_stream

    def write(self, s: str) -> int:
        if s:
            s = SecretRedactingFilter.redact_text(s)
        return self._target.write(s)

    def flush(self):
        return self._target.flush()

    def isatty(self) -> bool:
        return getattr(self._target, "isatty", lambda: False)()

    def fileno(self) -> int:
        return getattr(self._target, "fileno", lambda: 1)()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._target, name)

_installed = False

def install_secret_redactor():
    """Gắn bộ lọc bảo mật vào root logger và chuẩn đầu ra stdout/stderr."""
    global _installed
    if _installed:
        return
    _installed = True

    # 1. Gắn vào logging root
    redacting_filter = SecretRedactingFilter()
    root_logger = logging.getLogger()
    root_logger.addFilter(redacting_filter)
    for handler in root_logger.handlers:
        handler.addFilter(redacting_filter)

    # 2. Gắn wrapper vào sys.stdout và sys.stderr để bảo vệ cả lệnh print()
    if not isinstance(sys.stdout, RedactingStream):
        sys.stdout = RedactingStream(sys.stdout)
    if not isinstance(sys.stderr, RedactingStream):
        sys.stderr = RedactingStream(sys.stderr)
