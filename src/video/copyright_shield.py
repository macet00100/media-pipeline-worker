import os
from typing import Dict, Any, List
from config.settings import Settings


class CopyrightShield:
    """
    Lá chắn bản quyền chuyên trị Visual Content ID & Audio Fingerprint:
    1. Temporal Disruption:
       - Rút ngắn lát cắt 1.8s - 2.5s.
       - Nhảy cóc nhẹ +0.8s..+1.5s giữa 2 lát cắt liên tiếp trong phim gốc.
       - Luân phiên zoom theo lát cắt (1.06x lẻ / 1.15x chẵn).
    2. Spatial Disruption:
       - Lớp 1: hflip (lật gương ngang) + tăng tốc setpts=PTS/1.08.
       - Lớp 2: Kính mờ che sub Trung TRƯỚC KHI zoom (boxblur=22:3 + tint tối) trên tọa độ gốc.
       - Lớp 3: Zoom theo lát (1.06x / 1.15x), chỉnh màu điện ảnh (contrast 1.07, saturation 1.12, gamma 1.03)
                + nhiễu hạt phim (noise=alls=4:allf=t) -> thu gọn 86% kích thước đặt chính giữa trên nền
                kính mờ chuyển động (boxblur=28:3 + tối 45%), viền mảnh sáng tinh tế (white@0.25).
       - Lớp 4: Phụ đề ASS Tiếng Việt + Logo Mèo Thần Tài (đứng yên, xuôi chiều 100%).
    3. Audio Disruption:
       - Tắt 100% âm thanh phim gốc (volume=0.0). Chỉ dùng 100% giọng đọc TTS + BGM.
    """

    @classmethod
    def get_cut_filter(
        cls,
        width: int,
        height: int,
        frosted_glass_y: int,
        frosted_glass_h: int,
        frosted_glass_w: int,
        zoom_ratio: float = 1.06
    ) -> str:
        """
        Bộ lọc xử lý từng lát cắt hình ảnh:
        - Lật gương (hflip) + tăng tốc 1.08x (setpts=PTS/1.08)
        - Che sub Trung TRƯỚC KHI zoom (boxblur=22:3 + tint đen 35%)
        - Phóng zoom_ratio (1.06x hoặc 1.15x) và crop tâm về đúng kích thước chuẩn.
        """
        zoom_w = round(width * zoom_ratio / 2) * 2
        zoom_h = round(height * zoom_ratio / 2) * 2

        filters = (
            f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},setsar=1,fps={Settings.VIDEO_FPS},"
            f"hflip,setpts=PTS/1.08[v_hf];"
            f"[v_hf]split=2[v_base][v_sub];"
            f"[v_sub]crop={frosted_glass_w}:{frosted_glass_h}:0:{frosted_glass_y},"
            f"boxblur=22:3,"
            f"drawbox=x=0:y=0:w={frosted_glass_w}:h={frosted_glass_h}:color=black@0.35:t=fill[v_glass];"
            f"[v_base][v_glass]overlay=0:{frosted_glass_y}[v_masked];"
            f"[v_masked]scale={zoom_w}:{zoom_h}:flags=lanczos,"
            f"crop={width}:{height}[v_out]"
        )
        return filters

    @classmethod
    def get_body_render_filter(
        cls,
        width: int,
        height: int,
        safe_ass_path: str,
        safe_fonts_dir: str,
        logo_size: int = 120,
        logo_x: int = 32,
        logo_y: int = 32
    ) -> str:
        """
        Bộ lọc ghép thân phim hoàn thiện (Lớp 3 + Lớp 4):
        - Lớp 3: Nền mờ động (boxblur=28:3 + tối 45%) + Khung 86% ở giữa (chỉnh màu, nhiễu hạt mịn, viền trắng mờ).
        - Lớp 4: Logo Mèo Thần Tài (32, 32) + Phụ đề ASS tiếng Việt xuôi chiều sắc nét.
        """
        inner_w = round(width * 0.86 / 2) * 2
        inner_h = round(height * 0.86 / 2) * 2
        inner_x = (width - inner_w) // 2
        inner_y = (height - inner_h) // 2

        filters = (
            f"[0:v]split=2[v_in_bg][v_in_fg];"
            f"[v_in_bg]scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},"
            f"boxblur=28:3,drawbox=x=0:y=0:w={width}:h={height}:color=black@0.45:t=fill[v_bg];"
            f"[v_in_fg]scale={inner_w}:{inner_h}:flags=lanczos,"
            f"eq=contrast=1.07:saturation=1.12:gamma=1.03,noise=alls=4:allf=t,"
            f"drawbox=x=0:y=0:w={inner_w}:h={inner_h}:color=white@0.25:t=2[v_fg];"
            f"[v_bg][v_fg]overlay={inner_x}:{inner_y}[v_shielded];"
            f"[2:v]scale={logo_size}:{logo_size}:flags=lanczos[v_logo];"
            f"[v_shielded][v_logo]overlay={logo_x}:{logo_y}:shortest=1[v_with_logo];"
            f"[v_with_logo]ass='{safe_ass_path}':fontsdir='{safe_fonts_dir}'[v_out]"
        )
        return filters

    @classmethod
    def get_audio_film_filter(cls) -> str:
        """Tắt hoàn toàn 100% âm thanh phim gốc."""
        return "volume=0.0"

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
