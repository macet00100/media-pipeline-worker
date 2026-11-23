import os
import subprocess
from pathlib import Path
from typing import Dict, Any, Optional
from config.settings import Settings, ASSETS_DIR
from src.video.copyright_shield import CopyrightShield

class VideoRenderer:
    r"""
    Trình kết xuất FFmpeg Filtergraph hợp nhất:
    - Scale chuẩn 720x1280 30fps SAR 1:1 yuv420p
    - Kính mờ thấu kính (Frosted Glass Blur) che sub Trung tại y=816..912
    - Watermark Mèo Thần Tài (120x120) tại tọa độ (32, 32)
    - Phụ đề ASS tiếng Việt Arial 26pt căn giữa \pos(360, 864)
    - Hòa âm: Phim gốc (0.10) + Giọng đọc (1.50) với normalize=0
    - Ghép nối Outro Mèo Thần Tài giữ nguyên 100% âm lượng gốc sống động
    """

    @classmethod
    def probe_media_file(cls, file_path: str) -> bool:
        """Kiểm tra tính hợp lệ của file media bằng ffprobe."""
        if not os.path.exists(file_path):
            return False
        cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", file_path]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            return bool(res.stdout.strip())
        except Exception:
            return False

    @classmethod
    def get_valid_bgm_track(cls) -> Optional[str]:
        """
        Tìm và kiểm tra file nhạc nền thật trong assets/bgm:
        - Chỉ cho phép trộn BGM khi có file nhạc thật.
        - Loại bỏ hoàn toàn file rỗng, dung lượng nhỏ (<20KB) hoặc file sinh nhân tạo từ sine/noise.
        """
        import re
        bgm_dir = ASSETS_DIR / "bgm"
        if not bgm_dir.exists():
            return None
        valid_exts = {".mp3", ".wav", ".m4a", ".aac", ".ogg"}
        for f in bgm_dir.iterdir():
            if f.is_file() and f.suffix.lower() in valid_exts and f.stat().st_size > 20000:
                # Kiểm tra RMS level để đảm bảo là nhạc thật, không phải sine/noise ngầm (-70dB)
                cmd = ["ffmpeg", "-i", str(f), "-af", "astats=metadata=1:reset=1", "-f", "null", "-"]
                try:
                    res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
                    m = re.search(r"RMS level dB:\s*([-\d.]+)", res.stderr)
                    if m and float(m.group(1)) > -50.0:
                        return str(f)
                except Exception:
                    continue
        return None

    @classmethod
    def prepare_normalized_outro(cls, outro_input: str, output_path: str) -> bool:
        """
        Chuẩn hóa assets/outtro.mp4 về đúng thông số kỹ thuật của thân phim:
        720x1280, 30fps, SAR 1:1, yuv420p, âm thanh AAC 48000Hz Stereo 100% volume.
        """
        if not os.path.exists(outro_input):
            print(f"[VideoRenderer] Cảnh báo: Không tìm thấy outro {outro_input}")
            return False

        cmd = [
            "ffmpeg", "-y",
            "-i", outro_input,
            "-vf", f"scale={Settings.VIDEO_WIDTH}:{Settings.VIDEO_HEIGHT}:force_original_aspect_ratio=increase,crop={Settings.VIDEO_WIDTH}:{Settings.VIDEO_HEIGHT},setsar=1,fps={Settings.VIDEO_FPS},format={Settings.VIDEO_PIX_FMT}",
            "-af", "aresample=48000:async=1,aformat=channel_layouts=stereo,volume=1.0",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
            "-c:a", "aac", "-b:a", "192k",
            output_path
        ]
        res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return res.returncode == 0 and os.path.exists(output_path)

    @classmethod
    def render_movie_body(
        cls,
        clean_movie_path: str,
        voice_track_path: str,
        ass_subtitles_path: str,
        output_body_path: str,
        tmp_dir: str
    ) -> bool:
        """Kết xuất phần thân phim hoàn chỉnh với toàn bộ hiệu ứng hình ảnh và âm thanh."""
        logo_path = str(ASSETS_DIR / "logo.png")
        fonts_dir = str(ASSETS_DIR / "fonts")
        bgm_path = cls.get_valid_bgm_track()
        has_bgm = bgm_path is not None and os.path.exists(bgm_path)

        # Thoát ký tự đường dẫn cho filter ass của FFmpeg
        safe_ass_path = ass_subtitles_path.replace("\\", "/").replace(":", "\\:")
        safe_fonts_dir = fonts_dir.replace("\\", "/").replace(":", "\\:")

        # 1. Video Filtergraph:
        # Scale/Crop 720x1280 -> Split kính mờ y=816..912 -> Logo 32,32 -> Subtitle ASS -> Copyright shield
        v_filters = (
            f"[0:v]scale={Settings.VIDEO_WIDTH}:{Settings.VIDEO_HEIGHT}:force_original_aspect_ratio=increase,"
            f"crop={Settings.VIDEO_WIDTH}:{Settings.VIDEO_HEIGHT},setsar=1,fps={Settings.VIDEO_FPS},format={Settings.VIDEO_PIX_FMT},"
            f"eq=contrast=1.03:brightness=0.01:saturation=1.06,unsharp=3:3:0.3[v_base];"
            f"[v_base]split=2[v_main][v_crop];"
            f"[v_crop]crop={Settings.FROSTED_GLASS_W}:{Settings.FROSTED_GLASS_H}:0:{Settings.FROSTED_GLASS_Y},"
            f"boxblur=12:2,"
            f"drawbox=x=0:y=0:w={Settings.FROSTED_GLASS_W}:h={Settings.FROSTED_GLASS_H}:color=white@0.12:t=fill,"
            f"drawbox=x=0:y=0:w={Settings.FROSTED_GLASS_W}:h=1:color=white@0.25:t=fill,"
            f"drawbox=x=0:y={Settings.FROSTED_GLASS_H - 1}:w={Settings.FROSTED_GLASS_W}:h=1:color=white@0.25:t=fill[v_glass];"
            f"[v_main][v_glass]overlay=0:{Settings.FROSTED_GLASS_Y}[v_masked];"
            f"[2:v]scale={Settings.LOGO_SIZE}:{Settings.LOGO_SIZE}:flags=lanczos[v_logo];"
            f"[v_masked][v_logo]overlay={Settings.LOGO_X}:{Settings.LOGO_Y}[v_logo_applied];"
            f"[v_logo_applied]ass='{safe_ass_path}':fontsdir='{safe_fonts_dir}'[v_out]"
        )

        # 2. Audio Filtergraph:
        # Phim gốc 10% + Giọng đọc 150% + normalize=0
        if has_bgm:
            a_filters = (
                f"[0:a]volume={Settings.VOL_ORIGINAL_FILM},highpass=f=220,lowpass=f=3200[a_film];"
                f"[1:a]volume={Settings.VOL_TTS_VOICE},alimiter=limit=0.95[a_voice];"
                f"[3:a]volume={Settings.VOL_BGM}[a_bgm];"
                f"[a_film][a_voice][a_bgm]amix=inputs=3:duration=first:dropout_transition=0:normalize=0,"
                f"aresample=48000:async=1,aformat=channel_layouts=stereo[a_out]"
            )
        else:
            a_filters = (
                f"[0:a]volume={Settings.VOL_ORIGINAL_FILM},highpass=f=220,lowpass=f=3200[a_film];"
                f"[1:a]volume={Settings.VOL_TTS_VOICE},alimiter=limit=0.95[a_voice];"
                f"[a_film][a_voice]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,"
                f"aresample=48000:async=1,aformat=channel_layouts=stereo[a_out]"
            )

        cmd = [
            "ffmpeg", "-y",
            "-i", clean_movie_path,
            "-i", voice_track_path,
            "-i", logo_path
        ]
        if has_bgm:
            cmd.extend(["-stream_loop", "-1", "-i", bgm_path])

        cmd.extend([
            "-filter_complex", f"{v_filters};{a_filters}",
            "-map", "[v_out]",
            "-map", "[a_out]",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
            "-c:a", "aac", "-b:a", "192k",
            "-shortest",
            output_body_path
        ])

        print(f"[VideoRenderer] Đang render thân phim: {output_body_path}...")
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        if res.returncode == 0 and os.path.exists(output_body_path) and os.path.getsize(output_body_path) > 50000:
            print("[VideoRenderer] Render thân phim thành công!")
            return True
        else:
            print(f"[VideoRenderer] Lỗi render thân phim: {res.stderr[-300:]}")
            return False

    @classmethod
    def concatenate_outro(
        cls,
        movie_body_path: str,
        outro_path: str,
        final_output_path: str,
        tmp_dir: str
    ) -> bool:
        """
        Ghép nối thân phim với Outro Mèo Thần Tài (100% âm lượng gốc).
        Sử dụng ffmpeg concat protocol siêu an toàn.
        """
        print("[VideoRenderer] Đang ghép Outro Mèo Thần Tài vào cuối phim...")
        normalized_outro = os.path.join(tmp_dir, "normalized_outro.mp4")
        if not cls.prepare_normalized_outro(outro_path, normalized_outro):
            # Nếu không có outro, dùng luôn body làm output
            print("[VideoRenderer] Bỏ qua ghép outro do không tìm thấy file.")
            os.replace(movie_body_path, final_output_path)
            return True

        concat_txt = os.path.join(tmp_dir, "final_concat_list.txt")
        with open(concat_txt, "w", encoding="utf-8") as f:
            f.write(f"file '{movie_body_path.replace(os.sep, '/')}'\n")
            f.write(f"file '{normalized_outro.replace(os.sep, '/')}'\n")

        cmd = [
            "ffmpeg", "-y", "-f", "concat", "-safe", "0",
            "-i", concat_txt,
            "-c", "copy",
            final_output_path
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if concat_txt and os.path.exists(concat_txt):
            try: os.remove(concat_txt)
            except: pass

        if res.returncode == 0 and os.path.exists(final_output_path):
            print(f"[VideoRenderer] Ghép outro hoàn tất! Video thành phẩm: {final_output_path}")
            return True
        else:
            print(f"[VideoRenderer] Lỗi ghép outro: {res.stderr[-200:]}")
            return False
