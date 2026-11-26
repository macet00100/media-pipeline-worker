import os
from typing import Dict, Any, List
from config.settings import Settings


class CopyrightShield:
    """
    Lá chắn bản quyền chuyên trị Visual Content ID & Meta Rights Manager:
    1. Temporal Disruption:
       - Rút ngắn lát cắt 2.0s - 2.8s (tuyệt đối không quá 3.0s).
       - Nhảy cóc thời gian thật (Non-contiguous Jump-Cut): Lát cắt kế tiếp nhảy cóc tối thiểu
         +4.0s đến +8.0s trên dòng thời gian gốc để triệt tiêu hoàn toàn chuỗi khớp 13-30s.
       - Luân phiên tốc độ (1.16x lẻ / 1.22x chẵn) và zoom (1.08x lẻ / 1.18x chẵn).
    2. Spatial Disruption:
       - Lớp 1: hflip (lật gương ngang) + đổi tốc độ theo lát (setpts=PTS/speed).
       - Lớp 2: Kính mờ che sub Trung TRƯỚC KHI zoom (boxblur=22:3 + tint tối) trên tọa độ gốc.
       - Lớp 3: Khung lồng thật (True Inset Frame 80% ~ 78-82%), đặt chính giữa trên nền
                kính mờ chuyển động sâu (boxblur=32:4 + tối 50%), viền kép tương phản cao
                (viền ngoài tối đen black@0.70 độ dày 4px + viền trong sáng trắng white@0.45 độ dày 2px).
                Chỉnh màu điện ảnh (contrast 1.10, saturation 1.16, gamma 1.04) + nhiễu hạt phim (noise=5).
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
        zoom_ratio: float = 1.08,
        speed_factor: float = 1.18
    ) -> str:
        """
        Bộ lọc xử lý từng lát cắt hình ảnh:
        - Lật gương (hflip) + tăng tốc xen kẽ 1.15x - 1.25x (setpts=PTS/{speed_factor})
        - Che sub Trung TRƯỚC KHI zoom (boxblur=22:3 + tint đen 35%)
        - Phóng zoom_ratio (1.08x hoặc 1.18x) và crop tâm về đúng kích thước chuẩn.
        """
        zoom_w = round(width * zoom_ratio / 2) * 2
        zoom_h = round(height * zoom_ratio / 2) * 2

        filters = (
            f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},setsar=1,fps={Settings.VIDEO_FPS},"
            f"hflip,setpts=PTS/{speed_factor:.2f}[v_hf];"
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
        logo_y: int = 32,
        inset_ratio: float = 0.80
    ) -> str:
        """
        Bộ lọc ghép thân phim hoàn thiện (Lớp 3 + Lớp 4) chống Meta Rights Manager:
        - Lớp 3: Nền mờ động sâu (boxblur=32:4 + tối 50%) + Khung lồng thật 80% (78-82%) ở giữa.
                 Khung lồng có viền kép tương phản rõ nét: viền ngoài tối (black@0.70:t=4) + viền trong sáng (white@0.45:t=2).
                 Biến đổi điện ảnh (contrast 1.10, saturation 1.16, gamma 1.04) + nhiễu hạt phim (noise=alls=5:allf=t).
        - Lớp 4: Logo Mèo Thần Tài (32, 32) + Phụ đề ASS tiếng Việt xuôi chiều sắc nét.
        """
        inner_w = round(width * inset_ratio / 2) * 2
        inner_h = round(height * inset_ratio / 2) * 2
        inner_x = (width - inner_w) // 2
        inner_y = (height - inner_h) // 2

        filters = (
            f"[0:v]split=2[v_in_bg][v_in_fg];"
            f"[v_in_bg]scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},"
            f"boxblur=32:4,drawbox=x=0:y=0:w={width}:h={height}:color=black@0.50:t=fill[v_bg];"
            f"[v_in_fg]scale={inner_w}:{inner_h}:flags=lanczos,"
            f"eq=contrast=1.10:saturation=1.16:gamma=1.04,noise=alls=5:allf=t,"
            f"drawbox=x=0:y=0:w={inner_w}:h={inner_h}:color=black@0.70:t=4,"
            f"drawbox=x=4:y=4:w={inner_w-8}:h={inner_h-8}:color=white@0.45:t=2[v_fg];"
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
