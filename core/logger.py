import sys
from datetime import datetime

class Logger:
    """Hệ thống ghi log đồng nhất, rõ ràng cho toàn bộ quy trình."""

    COLORS = {
        "INFO": "\033[94m",       # Xanh lam
        "SUCCESS": "\033[92m",    # Xanh lá
        "WARNING": "\033[93m",    # Vàng
        "ERROR": "\033[91m",      # Đỏ
        "RESET": "\033[0m"
    }

    @classmethod
    def log(cls, message: str, level: str = "INFO", tag: str = "SYSTEM"):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        color = cls.COLORS.get(level.upper(), "")
        reset = cls.COLORS["RESET"]
        prefix = f"[{timestamp}] [{level.upper()}] [{tag}]"
        print(f"{color}{prefix} {message}{reset}", flush=True)

    @classmethod
    def info(cls, message: str, tag: str = "SYSTEM"):
        cls.log(message, "INFO", tag)

    @classmethod
    def success(cls, message: str, tag: str = "SYSTEM"):
        cls.log(message, "SUCCESS", tag)

    @classmethod
    def warning(cls, message: str, tag: str = "SYSTEM"):
        cls.log(message, "WARNING", tag)

    @classmethod
    def error(cls, message: str, tag: str = "SYSTEM"):
        cls.log(message, "ERROR", tag)
