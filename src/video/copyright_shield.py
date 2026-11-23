import os
from typing import Dict, Any, List
from config.settings import Settings

class CopyrightShield:
    """
    Lá chắn bản quyền và chống lỗi 'Nội dung sử dụng lại' (Reused Content)
    triển khai đầy đủ các kỹ thuật biến đổi fingerprint hợp lệ giữ nguyên độ nét.
    """

    @classmethod
    def get_visual_filters(cls) -> str:
        """
        Tạo chuỗi filter biến đổi chữ ký hình ảnh:
        1. Phóng nhẹ 1.03x và crop tâm về 720x1280.
        2. Tinh chỉnh màu sắc chuyên nghiệp (contrast, saturation, brightness) + unsharp làm trong hình.
        3. Thay đổi nhịp tốc độ 1.02x.
        """
        # Scale nhẹ 1.03x: 720 * 1.03 = 741.6 -> 742, 1280 * 1.03 = 1318.4 -> 1318
        zoom_w = int(Settings.VIDEO_WIDTH * 1.03)
        zoom_h = int(Settings.VIDEO_HEIGHT * 1.03)

        filters = [
            f"scale={zoom_w}:{zoom_h}:flags=lanczos",
            f"crop={Settings.VIDEO_WIDTH}:{Settings.VIDEO_HEIGHT}",
            "eq=contrast=1.03:brightness=0.01:saturation=1.06",
            "unsharp=3:3:0.3",
            "setpts=PTS/1.02"
        ]
        return ",".join(filters)

    @classmethod
    def get_audio_film_filter(cls) -> str:
        """
        Bộ lọc phá vỡ chữ ký âm thanh nhạc phim có bản quyền:
        Hạ âm lượng còn 10% kết hợp cắt dải tần highpass/lowpass giữ lại tiếng động tự nhiên.
        """
        return (
            f"volume={Settings.VOL_ORIGINAL_FILM},"
            "highpass=f=220,lowpass=f=3200,"
            "atempo=1.02"
        )

    @classmethod
    def get_metadata_args(cls, title: str) -> List[str]:
        """Tẩy sạch metadata cũ và gắn metadata mới của Cool Mèo Phim."""
        return [
            "-map_metadata", "-1",
            "-metadata", f"title=Review Phim - {title}",
            "-metadata", f"artist={Settings.CHANNEL_NAME}",
            "-metadata", f"author={Settings.CHANNEL_HANDLE}",
            "-metadata", "comment=Cool Meo Phim Original Commentary (Fair Use)",
            "-metadata", "copyright=Cool Meo Phim"
        ]
