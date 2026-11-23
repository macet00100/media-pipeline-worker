import os
import re
import math
import time
import json
import subprocess
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Dict, Any, Tuple, Optional
from config.settings import Settings, ASSETS_DIR, TMP_DIR
from src.audio.tts_engine import TTSEngine

# Từ điển các cụm từ ghép tiếng Việt và danh xưng thường gặp trong review phim
COMPOUND_PAIRS = {
    ('tiểu', 'thư'), ('tổng', 'tài'), ('hoàng', 'thượng'), ('hoàng', 'hậu'),
    ('hoàng', 'đế'), ('thái', 'tử'), ('vương', 'gia'), ('vương', 'phi'),
    ('công', 'chúa'), ('công', 'tử'), ('phu', 'nhân'), ('thiếu', 'gia'),
    ('gia', 'chủ'), ('đại', 'nhân'), ('tướng', 'quân'), ('nô', 'tỳ'),
    ('nha', 'hoàn'), ('thị', 'vệ'), ('mẫu', 'thân'), ('phụ', 'thân'),
    ('huynh', 'đệ'), ('tỷ', 'muội'), ('tuyệt', 'đối'), ('xuyên', 'không'),
    ('trùng', 'sinh'), ('tai', 'nạn'), ('hôn', 'mê'), ('tỉnh', 'dậy'),
    ('bất', 'ngờ'), ('kinh', 'hoàng'), ('nguy', 'hiểm'), ('sống', 'sót'),
    ('hãm', 'hại'), ('trả', 'thù'), ('phản', 'bội'), ('đau', 'đớn'),
    ('tuyệt', 'vọng'), ('biến', 'mất'), ('xuất', 'hiện'), ('thân', 'phận'),
    ('sự', 'thật'), ('hợp', 'đồng'), ('kết', 'hôn'), ('ly', 'hôn'),
    ('tập', 'đoàn'), ('tiểu', 'thuyết'), ('bảo', 'bọc'), ('bình', 'thường'),
    ('khán', 'giả'), ('long', 'thành'), ('mèo', 'phim'), ('tiếp', 'theo'),
    ('đón', 'xem'), ('đăng', 'ký'), ('thực', 'sự'), ('đặc', 'biệt'),
    ('hạnh', 'phúc'), ('tương', 'lai'), ('quá', 'khứ'), ('hiện', 'tại'),
    ('thiên', 'kim'), ('tướng', 'quốc'), ('đối', 'ngoại'), ('nữ', 'cường')
}

def clean_word_for_bond(w: str) -> str:
    return re.sub(r'[^\w\s]', '', w).lower()

def is_bonded_pair(w1: str, w2: str) -> bool:
    """Kiểm tra xem 2 từ liền kề có phải từ ghép hoặc tên riêng cần giữ liền dòng không."""
    c1, c2 = clean_word_for_bond(w1), clean_word_for_bond(w2)
    if (c1, c2) in COMPOUND_PAIRS:
        return True
    # Giữ liền tên riêng viết hoa 2 từ liên tiếp (Lâm Chiêu, Chiêu Hoa, Vũ Miên, Long Thành...)
    if w1 and w2 and w1[0].isupper() and w2[0].isupper() and not w1.isupper() and not w2.isupper():
        return True
    return False

def split_words_naturally(words: List[str], min_w: int = 4, max_w: int = 6) -> List[List[str]]:
    """
    Tách danh sách từ thành các cụm 4-6 từ tự nhiên, không ngắt đôi từ ghép hoặc tên nhân vật.
    Sử dụng quy hoạch động để tối ưu độ dài và tránh để lại từ mồ côi (1-2 từ).
    """
    n = len(words)
    if n <= max_w:
        return [words]

    memo = {}

    def solve(idx: int) -> Tuple[float, List[List[str]]]:
        if idx == n:
            return 0.0, []
        rem = n - idx
        if rem <= max_w:
            return 0.0, [words[idx:]]
        if idx in memo:
            return memo[idx]

        best_cost = float('inf')
        best_lines = []

        for take in range(min_w, max_w + 1):
            next_idx = idx + take
            if next_idx > n:
                break
            rem_after = n - next_idx
            if 0 < rem_after < min_w:
                continue

            # Ưu tiên các dòng 4-5 từ
            cost = (take - 5) ** 2
            # Phạt nặng nếu điểm ngắt chia đôi từ ghép hoặc tên riêng
            if next_idx < n and is_bonded_pair(words[next_idx - 1], words[next_idx]):
                cost += 500.0
            # Thưởng nhẹ nếu ngắt tại dấu ngắt nhịp (, ; :)
            if words[next_idx - 1].endswith((',', ';', ':')):
                cost -= 15.0

            future_cost, future_lines = solve(next_idx)
            total = cost + future_cost
            if total < best_cost:
                best_cost = total
                best_lines = [words[idx:next_idx]] + future_lines

        if best_cost == float('inf'):
            mid = (rem + 1) // 2
            take = max(min_w - 1, min(max_w + 1, mid))
            next_idx = idx + take
            _, future_lines = solve(next_idx)
            best_lines = [words[idx:next_idx]] + future_lines
            best_cost = 100.0

        memo[idx] = (best_cost, best_lines)
        return memo[idx]

    _, result = solve(0)
    return result if result else [words]

@dataclass
class OrientationProfile:
    """Hồ sơ định dạng video (Dọc hoặc Ngang)."""
    name: str
    width: int
    height: int
    fps: int
    frosted_glass_x: int
    frosted_glass_y: int
    frosted_glass_w: int
    frosted_glass_h: int
    frosted_glass_center_y: int
    sub_font: str
    sub_fontsize: int
    sub_pos: str
    sub_words_per_line_min: int
    sub_words_per_line_max: int
    logo_size: int
    logo_x: int
    logo_y: int
    is_vertical: bool

VERTICAL_PROFILE = OrientationProfile(
    name="vertical_720x1280",
    width=720,
    height=1280,
    fps=30,
    frosted_glass_x=0,
    frosted_glass_y=816,
    frosted_glass_w=720,
    frosted_glass_h=96,
    frosted_glass_center_y=864,
    sub_font="Arial",
    sub_fontsize=26,
    sub_pos=r"\pos(360,864)",
    sub_words_per_line_min=4,
    sub_words_per_line_max=6,
    logo_size=120,
    logo_x=32,
    logo_y=32,
    is_vertical=True
)

HORIZONTAL_PROFILE = OrientationProfile(
    name="horizontal_1280x720",
    width=1280,
    height=720,
    fps=30,
    frosted_glass_x=0,
    frosted_glass_y=556,
    frosted_glass_w=1280,
    frosted_glass_h=116,
    frosted_glass_center_y=614,
    sub_font="Arial",
    sub_fontsize=28,
    sub_pos=r"\pos(640,614)",
    sub_words_per_line_min=4,
    sub_words_per_line_max=6,
    logo_size=110,
    logo_x=32,
    logo_y=32,
    is_vertical=False
)

class CompressionPlanner:
    """
    Bộ lập kế hoạch nén thông minh:
    - Đoản kịch dọc (gốc 2-3h): nén còn 40%-50% (45-90p trọn bộ).
    - Phim bộ dài (gốc 15-30h): nén còn 15%-25% (2-3.5h trọn bộ, tự chia Part nếu > 3.5h = 12600s).
    - Cắt sạch thời lượng chết: nhạc đầu/cuối tập, cảnh đi lại chậm, nhìn nhau im lặng.
    """

    MAX_PART_DURATION_SEC: float = 3.5 * 3600.0  # 12600s = 3.5h

    @classmethod
    def calculate_plan(
        cls,
        raw_duration_sec: float,
        drama_type: str = "short_drama"
    ) -> Dict[str, Any]:
        """
        Tính toán mục tiêu thời lượng nén và số Part phân chia.
        """
        raw_hours = raw_duration_sec / 3600.0

        if drama_type == "long_series" or raw_hours > 5.0:
            # Phim bộ dài: nén còn 15% - 25% (mặc định chọn 20%)
            min_ratio, max_ratio, target_ratio = 0.15, 0.25, 0.20
            target_duration = raw_duration_sec * target_ratio

            # Nếu > 3.5 giờ (12600s), tự động chia Part
            if target_duration > cls.MAX_PART_DURATION_SEC:
                num_parts = math.ceil(target_duration / cls.MAX_PART_DURATION_SEC)
                part_duration = target_duration / num_parts
            else:
                num_parts = 1
                part_duration = target_duration

            target_min_sec = raw_duration_sec * min_ratio
            target_max_sec = raw_duration_sec * max_ratio
            desc = f"Phim bộ dài ({raw_hours:.1f}h gốc) -> Nén {target_ratio*100:.0f}% còn {target_duration/3600:.2f}h chia {num_parts} Part"

        else:
            # Đoản kịch dọc: nén còn 40% - 50% (mặc định chọn 45%)
            min_ratio, max_ratio, target_ratio = 0.40, 0.50, 0.45
            target_duration = raw_duration_sec * target_ratio

            # Đảm bảo phạm vi 45p (2700s) - 90p (5400s) nếu phim gốc 2-3h
            if raw_duration_sec >= 7200.0:
                target_duration = max(2700.0, min(5400.0, target_duration))

            num_parts = 1
            part_duration = target_duration
            target_min_sec = raw_duration_sec * min_ratio
            target_max_sec = raw_duration_sec * max_ratio
            desc = f"Đoản kịch dọc ({raw_hours:.1f}h gốc) -> Nén {target_ratio*100:.0f}% còn {target_duration/60:.1f} phút trọn bộ"

        return {
            "drama_type": drama_type,
            "raw_duration_sec": raw_duration_sec,
            "target_ratio": target_ratio,
            "target_duration_sec": target_duration,
            "target_min_sec": target_min_sec,
            "target_max_sec": target_max_sec,
            "num_parts": num_parts,
            "part_duration_sec": part_duration,
            "description": desc
        }

    @classmethod
    def filter_dead_time(
        cls,
        clip_ranges: List[Dict[str, float]],
        episode_duration: float,
        intro_cut_sec: float = 35.0,
        outro_cut_sec: float = 45.0
    ) -> List[Dict[str, float]]:
        """
        Lọc bỏ thời lượng chết:
        - Cắt sạch nhạc intro đầu tập (0 .. intro_cut_sec)
        - Cắt sạch nhạc outro cuối tập (episode_duration - outro_cut_sec .. episode_duration)
        """
        valid_ranges = []
        active_start = intro_cut_sec
        active_end = max(active_start, episode_duration - outro_cut_sec)

        for r in clip_ranges:
            start = max(active_start, r.get("start", 0.0))
            end = min(active_end, r.get("end", episode_duration))
            if end - start >= 2.0:
                valid_ranges.append({
                    "start": round(start, 2),
                    "end": round(end, 2),
                    "duration": round(end - start, 2)
                })

        return valid_ranges

class VoiceDrivenEditor:
    r"""
    Bộ dựng video theo Thuật toán Tiếng Dẫn Hình (Voice-Driven Editing):
    1. Nhận diện tỷ lệ khung hình (Orientation Profile) bằng ffprobe.
    2. Nhận narration_blocks (lời kể ngôi 3 + source_clip_ranges).
    3. Đo T_audio thực tế từng câu với edge-tts / ffprobe.
    4. Cắt ghép hình ảnh đúng chính xác T_audio giây từ source_clip_ranges,
       tuyệt đối không giữ một góc quay quá 6-7s (chia lát cắt động 3-6s).
    5. Render theo Dual Orientation Profile:
       - Profile Dọc: 720x1280 @ 30fps, Frosted Glass y=816..912, Sub Arial 26pt \pos(360,864), Logo 120x120.
       - Profile Ngang: 1280x720 @ 30fps toàn cảnh, Frosted Glass y=600..688, Sub Arial 28pt \pos(640,644), Logo 110x110,
         Outro dọc đặt giữa nền blurred pillarbox 1280x720 100% volume.
    """

    last_render_stats: Dict[str, Any] = {}

    @classmethod
    def probe_video(cls, video_path: str) -> Dict[str, Any]:
        """Dò thông số kỹ thuật và nhận diện tỷ lệ khung hình video bằng ffprobe."""
        if not os.path.exists(video_path):
            raise FileNotFoundError(f"Không tìm thấy file video: {video_path}")

        cmd = [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height,r_frame_rate,duration",
            "-of", "json",
            video_path
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if res.returncode != 0:
            raise RuntimeError(f"Lỗi ffprobe: {res.stderr}")

        data = json.loads(res.stdout)
        stream = data["streams"][0]
        width = int(stream["width"])
        height = int(stream["height"])

        # Tính duration nếu stream chưa có
        duration = float(stream.get("duration", 0.0))
        if duration <= 0:
            cmd_dur = [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                video_path
            ]
            r_dur = subprocess.run(cmd_dur, capture_output=True, text=True, timeout=10)
            duration = float(r_dur.stdout.strip() or 0.0)

        is_vertical = height > width
        profile = VERTICAL_PROFILE if is_vertical else HORIZONTAL_PROFILE

        return {
            "width": width,
            "height": height,
            "duration": duration,
            "aspect_ratio": width / height if height > 0 else 1.0,
            "is_vertical": is_vertical,
            "orientation": "vertical" if is_vertical else "horizontal",
            "profile": profile
        }

    @classmethod
    def slice_visual_to_audio(
        cls,
        t_audio: float,
        source_clip_ranges: List[Dict[str, float]],
        min_cut_sec: float = 1.8,
        max_cut_sec: float = 2.5
    ) -> List[Dict[str, Any]]:
        """
        Thuật toán Chia Lát Cắt Động (Dynamic Visual Slicing) - Chống Visual Content ID:
        - Rút ngắn bắt buộc thời lượng mỗi lát cắt xuống 1.8s - 2.5s (tuyệt đối không quá 2.5s).
        - Giữa 2 lát cắt liên tiếp nhảy cóc nhẹ +0.8s..+1.5s trong phim gốc để phá cấu trúc chuỗi tuyến tính.
        - Luân phiên tỷ lệ zoom theo lát cắt: Lát lẻ phóng 1.06x, Lát chẵn phóng 1.15x.
        """
        if t_audio <= 0:
            return []

        # 1. Làm sạch và chuẩn hóa ranges của block này
        valid_ranges = []
        for r in (source_clip_ranges or []):
            st = float(r.get("start", 0.0))
            en = float(r.get("end", 0.0))
            if en - st >= 1.0:
                valid_ranges.append({"start": st, "end": en, "len": en - st})

        if not valid_ranges:
            valid_ranges = [{"start": 35.0, "end": 90.0, "len": 55.0}]

        # 2. Tính toán danh sách thời lượng các lát cắt (1.8s - 2.5s)
        if t_audio <= max_cut_sec:
            slice_durations = [round(t_audio, 2)]
        else:
            k = max(2, math.ceil(t_audio / max_cut_sec))
            avg = t_audio / k
            while avg > max_cut_sec:
                k += 1
                avg = t_audio / k

            slice_durations = []
            rem = t_audio
            for i in range(k - 1):
                cur = round(avg, 2)
                slice_durations.append(cur)
                rem -= cur
            slice_durations.append(round(rem, 2))

        # 3. Ánh xạ các lát cắt vào các dải an toàn của block kèm nhảy cóc và luân phiên zoom
        visual_cuts = []
        range_idx = 0
        range_offset = 0.0

        for idx, dur in enumerate(slice_durations):
            cur_r = valid_ranges[range_idx % len(valid_ranges)]
            r_st = cur_r["start"]
            r_en = cur_r["end"]
            r_len = cur_r["len"]

            # Luân phiên zoom: lẻ phóng 1.06x, chẵn phóng 1.15x
            cut_zoom = 1.06 if (idx % 2 == 1) else 1.15

            # Nhảy cóc nhẹ +0.8s..+1.5s giữa các lát cắt liên tiếp
            jump_sec = 1.2 if idx > 0 else 0.0

            if r_len <= dur:
                cut_start = r_st
                cut_end = min(r_en, r_st + dur)
                actual_dur = round(cut_end - cut_start, 2)
                visual_cuts.append({
                    "start": round(cut_start, 2),
                    "end": round(cut_end, 2),
                    "duration": actual_dur,
                    "zoom": cut_zoom
                })
                rem_dur = round(dur - actual_dur, 2)
                range_idx += 1
                range_offset = 0.0
                if rem_dur > 0.1:
                    next_r = valid_ranges[range_idx % len(valid_ranges)]
                    n_st = next_r["start"]
                    n_en = next_r["end"]
                    sub_cut_end = min(n_en, n_st + rem_dur)
                    visual_cuts.append({
                        "start": round(n_st, 2),
                        "end": round(sub_cut_end, 2),
                        "duration": round(sub_cut_end - n_st, 2),
                        "zoom": cut_zoom
                    })
                    range_offset = rem_dur + 1.2
            else:
                cut_start = r_st + range_offset
                if cut_start + dur > r_en:
                    range_idx += 1
                    cur_r = valid_ranges[range_idx % len(valid_ranges)]
                    r_st = cur_r["start"]
                    r_en = cur_r["end"]
                    range_offset = 0.0
                    cut_start = r_st

                cut_end = min(r_en, cut_start + dur)
                visual_cuts.append({
                    "start": round(cut_start, 2),
                    "end": round(cut_end, 2),
                    "duration": round(cut_end - cut_start, 2),
                    "zoom": cut_zoom
                })
                range_offset += dur + jump_sec
                if range_offset + dur > cur_r["len"]:
                    range_idx += 1
                    range_offset = 0.0

        return visual_cuts

    @classmethod
    def generate_profile_subtitles_ass(
        cls,
        subtitles_data: List[Dict[str, Any]],
        output_ass_path: str,
        profile: OrientationProfile
    ):
        r"""
        Sinh file phụ đề ASS chuẩn hóa theo Profile:
        - Bắt buộc chia cứng 4-6 từ/dòng tự nhiên qua split_words_naturally, TUYỆT ĐỐI TỐI ĐA 1 DÒNG DUY NHẤT.
        - Chiều cao không bao giờ vượt quá dải kính mờ (h=116px ngang / h=96px dọc).
        - Hậu kiểm timestamp: block[i].end <= block[i+1].start (0% đè chữ).
        - Lọc sạch 100% ký tự chữ Hán.
        """
        def format_ass_time(seconds: float) -> str:
            h = int(seconds // 3600)
            m = int((seconds % 3600) // 60)
            s = int(seconds % 60)
            cs = int(round((seconds - int(seconds)) * 100))
            if cs >= 100:
                s += 1
                cs = 0
            return f"{h:01d}:{m:02d}:{s:02d}.{cs:02d}"

        # 1. Tách từng câu thành các chunk 4-6 từ tự nhiên với mốc thời gian riêng biệt
        w_min = profile.sub_words_per_line_min
        w_max = profile.sub_words_per_line_max

        raw_events = []
        for sub in subtitles_data:
            text = re.sub(r"[\u4e00-\u9fff]+", "", sub.get("text", "")).strip()
            text = re.sub(r"\s+", " ", text).strip()
            if not text:
                continue

            words = text.split()
            if not words:
                continue

            t_start = float(sub["start"])
            t_end = float(sub["end"])
            t_dur = max(0.4, t_end - t_start)

            # Chia từ thành các cụm 4-6 từ tự nhiên, không ngắt đôi từ ghép hoặc tên riêng
            chunks = split_words_naturally(words, w_min, w_max)

            total_words = len(words)
            cur_t = t_start
            for c in chunks:
                c_dur = (len(c) / total_words) * t_dur
                c_end = min(t_end, cur_t + c_dur)
                line_str = " ".join(c)
                raw_events.append({
                    "start": cur_t,
                    "end": c_end,
                    "text": line_str
                })
                cur_t = c_end

        # 2. Hậu kiểm timestamp: Đảm bảo 0% khả năng đè lên nhau
        raw_events.sort(key=lambda x: x["start"])
        clean_events = []
        for i in range(len(raw_events)):
            ev = dict(raw_events[i])
            if clean_events:
                prev = clean_events[-1]
                if prev["end"] > ev["start"]:
                    # Điều chỉnh kết thúc câu trước tránh chồng lấn
                    prev["end"] = max(prev["start"] + 0.2, ev["start"] - 0.02)
            if ev["end"] > ev["start"] + 0.1:
                clean_events.append(ev)

        # 3. Tạo header và events
        font_name = profile.sub_font
        fontsize = profile.sub_fontsize
        pos_tag = profile.sub_pos

        header = f"""[Script Info]
Title: Cool Mèo Phim Subtitles ({profile.name})
ScriptType: v4.00+
WrapStyle: 0
ScaledBorderAndShadow: yes
PlayResX: {profile.width}
PlayResY: {profile.height}

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: CoolStyle,{font_name},{fontsize},&H00FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0.5,0,1,2.0,1.0,5,20,20,20,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
        event_lines = []
        for ev in clean_events:
            s_str = format_ass_time(ev["start"])
            e_str = format_ass_time(ev["end"])
            text_field = f"{{{pos_tag}}}{ev['text']}"
            event_lines.append(f"Dialogue: 0,{s_str},{e_str},CoolStyle,,0,0,0,,{text_field}")

        content = header + "\n".join(event_lines) + "\n"
        with open(output_ass_path, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"[VoiceDrivenEditor] Đã tạo phụ đề ASS ({profile.name}) chuẩn 4-6 từ/dòng ({len(event_lines)} dòng): {output_ass_path}")

    @classmethod
    def prepare_outro_for_profile(
        cls,
        outro_input: str,
        output_path: str,
        profile: OrientationProfile
    ) -> bool:
        """
        Chuẩn hóa Outro Mèo Thần Tài (100% volume gốc):
        - Profile Dọc: scale=720:1280 @ 30fps stereo 48k.
        - Profile Ngang: Outro dọc đặt giữa nền blurred pillarbox 1280x720 100% volume!
        """
        if not os.path.exists(outro_input):
            print(f"[VoiceDrivenEditor] Cảnh báo: Không tìm thấy outro {outro_input}")
            return False

        if profile.is_vertical:
            # Profile Dọc
            vf = (
                f"scale={profile.width}:{profile.height}:force_original_aspect_ratio=increase,"
                f"crop={profile.width}:{profile.height},setsar=1,fps={profile.fps},"
                f"format={Settings.VIDEO_PIX_FMT}"
            )
            cmd = [
                "ffmpeg", "-y",
                "-i", outro_input,
                "-vf", vf,
                "-af", "aresample=48000:async=1,aformat=channel_layouts=stereo,volume=1.0",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
                "-c:a", "aac", "-b:a", "192k",
                output_path
            ]
        else:
            # Profile Ngang: Outro dọc đặt ở trung tâm nền blurred pillarbox 1280x720
            vf = (
                f"[0:v]scale={profile.width}:{profile.height}:force_original_aspect_ratio=increase,"
                f"crop={profile.width}:{profile.height},boxblur=20:5,setsar=1,fps={profile.fps}[bg];"
                f"[0:v]scale=-2:{profile.height}:force_original_aspect_ratio=decrease,setsar=1,fps={profile.fps}[fg];"
                f"[bg][fg]overlay=(W-w)/2:0:shortest=1,format={Settings.VIDEO_PIX_FMT}[v_out]"
            )
            cmd = [
                "ffmpeg", "-y",
                "-i", outro_input,
                "-filter_complex", vf,
                "-map", "[v_out]",
                "-map", "0:a",
                "-af", "aresample=48000:async=1,aformat=channel_layouts=stereo,volume=1.0",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
                "-c:a", "aac", "-b:a", "192k",
                output_path
            ]

        res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return res.returncode == 0 and os.path.exists(output_path)

    @classmethod
    def render_voice_driven_video(
        cls,
        source_video: str,
        narration_blocks: List[Dict[str, Any]],
        output_video_path: str,
        tmp_dir: str,
        drama_type: str = "short_drama",
        include_outro: bool = True
    ) -> bool:
        """
        Quy trình Dựng Tiếng Dẫn Hình toàn diện:
        1. Nhận diện tỷ lệ bằng ffprobe -> Khởi tạo Profile (Dọc hoặc Ngang).
        2. Tổng hợp giọng đọc TTS và đo thời lượng T_audio từng câu.
        3. Cắt lát hình ảnh 3-6s từ source_clip_ranges khớp từng T_audio.
        4. Render video áp dụng kính mờ Frosted Glass + Logo + Subtitle ASS theo đúng Profile.
        5. Ghép Outro 100% volume gốc.
        """
        tmp_path = Path(tmp_dir)
        tmp_path.mkdir(parents=True, exist_ok=True)

        # 1. Nhận diện tỷ lệ
        probe_info = cls.probe_video(source_video)
        profile: OrientationProfile = probe_info["profile"]
        print(f"[VoiceDrivenEditor] Kích hoạt hồ sơ: {profile.name} ({probe_info['width']}x{probe_info['height']})")

        # 2. Xử lý kịch bản, TTS và Lát cắt hình ảnh
        processed_blocks = []
        all_cuts = []
        all_subtitles = []
        current_timeline = 0.0
        total_voice_duration = 0.0

        valid_blocks = [b for b in narration_blocks if b.get("script", "").strip()]
        num_valid = len(valid_blocks)
        pause_dur = 1.2 if num_valid > 1 else 0.0
        buffer_pad_sec = 0.6

        for idx, block in enumerate(valid_blocks):
            script_text = block.get("script", "").strip()
            clip_ranges = block.get("source_clip_ranges", [])
            is_first = (idx == 0)
            is_last = (idx == num_valid - 1)

            # Nghỉ thở tự nhiên 1.2s giữa các khối kịch bản (trừ khối đầu tiên)
            b_pause = 0.0 if is_first else pause_dur
            # Khoảng đệm 0.6s cuối thân phim để câu cuối vang trọn vẹn trước Outro (chỉ khối cuối)
            b_pad = buffer_pad_sec if is_last else 0.0

            # 1. Tạo audio giọng đọc cho block kèm timestamps từng câu
            time.sleep(1.5)
            block_audio_file = str(tmp_path / f"voice_block_{idx}.mp3")
            t_audio, sent_timings = TTSEngine.synthesize_with_timestamps(script_text, block_audio_file)
            if t_audio <= 0:
                raise RuntimeError(f"LỖI TTS: Không thể tạo audio cho khối #{idx}: '{script_text[:40]}...'. Dừng job để sửa kết nối, tuyệt đối không dùng audio giả!")

            total_voice_duration += t_audio

            # 2. Xử lý audio pause đệm giữa các cảnh (nếu có)
            if b_pause > 0:
                pause_mp3 = str(tmp_path / f"voice_pause_{idx}.mp3")
                subprocess.run([
                    "ffmpeg", "-nostdin", "-y", "-f", "lavfi",
                    "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
                    "-t", str(b_pause),
                    pause_mp3
                ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                processed_blocks.append({
                    "block_id": f"pause_{idx}",
                    "script": "",
                    "t_audio": b_pause,
                    "audio_file": pause_mp3,
                    "cuts": []
                })

            processed_blocks.append({
                "block_id": idx,
                "script": script_text,
                "t_audio": t_audio,
                "audio_file": block_audio_file,
                "cuts": []
            })

            # Nếu là khối cuối, thêm đệm âm thanh 0.6s trước Outro
            if b_pad > 0:
                pad_mp3 = str(tmp_path / "voice_pad_final.mp3")
                subprocess.run([
                    "ffmpeg", "-nostdin", "-y", "-f", "lavfi",
                    "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
                    "-t", str(b_pad),
                    pad_mp3
                ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                processed_blocks.append({
                    "block_id": "pad_final",
                    "script": "",
                    "t_audio": b_pad,
                    "audio_file": pad_mp3,
                    "cuts": []
                })

            # 3. Tính tổng thời lượng hình ảnh cho khối này (khớp chính xác thời lượng audio)
            t_visual_needed = round(b_pause + t_audio + b_pad, 2)

            # Cắt lát hình ảnh 3-6s TỪ CHÍNH source_clip_ranges CỦA KHỐI NÀY
            cuts = cls.slice_visual_to_audio(t_visual_needed, clip_ranges)
            all_cuts.extend(cuts)

            # 4. Tạo dữ liệu phụ đề: Bắt đầu sau khoảng pause đệm
            sub_start_base = current_timeline + b_pause
            if sent_timings:
                for st in sent_timings:
                    all_subtitles.append({
                        "start": sub_start_base + st["start_sec"],
                        "end": sub_start_base + st["end_sec"],
                        "text": st["text"]
                    })
            else:
                all_subtitles.append({
                    "start": sub_start_base,
                    "end": sub_start_base + t_audio,
                    "text": script_text
                })

            current_timeline += t_visual_needed

        cls.last_render_stats = {
            "total_voice_duration": total_voice_duration,
            "timeline_duration": current_timeline
        }

        if not processed_blocks:
            print("[VoiceDrivenEditor] Không có khối nội dung nào để dựng.")
            return False

        # 3. Ghép nối toàn bộ audio giọng đọc thành 1 file duy nhất
        concat_audio_txt = str(tmp_path / "audio_concat.txt")
        full_voice_path = str(tmp_path / "full_voice_track.mp3")
        with open(concat_audio_txt, "w", encoding="utf-8") as f:
            for b in processed_blocks:
                safe_p = Path(b["audio_file"]).resolve().as_posix()
                f.write(f"file '{safe_p}'\n")

        subprocess.run([
            "ffmpeg", "-nostdin", "-y", "-f", "concat", "-safe", "0",
            "-i", concat_audio_txt, "-c", "copy", full_voice_path
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        # 4. Cắt và nối các lát cắt hình ảnh từ phim gốc với Lá Chắn Bản Quyền Lớp 1 & Lớp 2
        from src.video.copyright_shield import CopyrightShield

        sliced_video_clips = []
        for c_idx, cut in enumerate(all_cuts):
            clip_out = str(tmp_path / f"cut_{c_idx}.mp4")
            c_start = cut["start"]
            c_dur = cut["duration"]
            c_zoom = cut.get("zoom", 1.06 if (c_idx % 2 == 1) else 1.15)

            # Lớp 1 (hflip + speed 1.08x) + Lớp 2 (kính mờ che sub Trung TRƯỚC zoom) + Zoom lát
            cut_filter = CopyrightShield.get_cut_filter(
                width=profile.width,
                height=profile.height,
                frosted_glass_y=profile.frosted_glass_y,
                frosted_glass_h=profile.frosted_glass_h,
                frosted_glass_w=profile.frosted_glass_w,
                zoom_ratio=c_zoom
            )

            cmd_cut = [
                "ffmpeg", "-nostdin", "-y",
                "-ss", str(c_start),
                "-t", str(c_dur),
                "-i", source_video,
                "-filter_complex", cut_filter,
                "-map", "[v_out]",
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "22",
                "-an",
                clip_out
            ]
            subprocess.run(cmd_cut, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if os.path.exists(clip_out) and os.path.getsize(clip_out) > 5000:
                sliced_video_clips.append(clip_out)

        # Ghép các lát cắt hình ảnh
        raw_stitched_video = str(tmp_path / "raw_visual_stitched.mp4")
        if sliced_video_clips:
            v_concat_txt = str(tmp_path / "visual_concat.txt")
            with open(v_concat_txt, "w", encoding="utf-8") as f:
                for v in sliced_video_clips:
                    safe_vp = Path(v).resolve().as_posix()
                    f.write(f"file '{safe_vp}'\n")
            res_v = subprocess.run([
                "ffmpeg", "-nostdin", "-y", "-f", "concat", "-safe", "0",
                "-i", v_concat_txt, "-c", "copy", raw_stitched_video
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if res_v.returncode != 0 or not os.path.exists(raw_stitched_video) or os.path.getsize(raw_stitched_video) == 0:
                subprocess.run([
                    "ffmpeg", "-nostdin", "-y", "-f", "concat", "-safe", "0",
                    "-i", v_concat_txt, "-c:v", "libx264", "-preset", "ultrafast", "-crf", "22", "-an", raw_stitched_video
                ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            # Fallback lấy đoạn đầu source_video
            subprocess.run([
                "ffmpeg", "-nostdin", "-y", "-t", str(current_timeline), "-i", source_video,
                "-c", "copy", raw_stitched_video
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        # 5. Tạo file phụ đề ASS chuẩn theo Profile
        ass_sub_path = str(tmp_path / "subtitles_profile.ass")
        cls.generate_profile_subtitles_ass(all_subtitles, ass_sub_path, profile)

        # 6. Render thân phim hoàn chỉnh với Lá Chắn Bản Quyền Lớp 3 & Lớp 4
        movie_body_path = str(tmp_path / "movie_body_rendered.mp4")
        logo_path = str(ASSETS_DIR / "logo.png")
        fonts_dir = str(ASSETS_DIR / "fonts")
        bgm_path = str(ASSETS_DIR / "bgm" / "ambient_bgm.mp3")
        has_bgm = os.path.exists(bgm_path)

        safe_ass = Path(ass_sub_path).resolve().as_posix().replace(":", "\\:")
        safe_fonts = Path(fonts_dir).resolve().as_posix().replace(":", "\\:")

        # FILTERGRAPH 4 LỚP: Nền mờ 28px + Khung 86% ở giữa + Viền sáng + Logo + Subtitle ASS
        v_filters = CopyrightShield.get_body_render_filter(
            width=profile.width,
            height=profile.height,
            safe_ass_path=safe_ass,
            safe_fonts_dir=safe_fonts,
            logo_size=profile.logo_size,
            logo_x=profile.logo_x,
            logo_y=profile.logo_y
        )

        # Hòa âm: Giọng đọc 150% + BGM 6% (nếu có)
        if has_bgm:
            a_filters = (
                f"[1:a]volume={Settings.VOL_TTS_VOICE},alimiter=limit=0.95[a_voice];"
                f"[3:a]volume={Settings.VOL_BGM}[a_bgm];"
                f"[a_voice][a_bgm]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,"
                f"aresample=48000:async=1,aformat=channel_layouts=stereo[a_out]"
            )
        else:
            a_filters = (
                f"[1:a]volume={Settings.VOL_TTS_VOICE},alimiter=limit=0.95,aresample=48000:async=1,"
                f"aformat=channel_layouts=stereo[a_out]"
            )

        cmd_render = [
            "ffmpeg", "-nostdin", "-y",
            "-i", raw_stitched_video,
            "-i", full_voice_path,
            "-loop", "1", "-i", logo_path
        ]
        if has_bgm:
            cmd_render.extend(["-stream_loop", "-1", "-i", bgm_path])

        cmd_render.extend([
            "-filter_complex", f"{v_filters};{a_filters}",
            "-map", "[v_out]",
            "-map", "[a_out]",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
            "-c:a", "aac", "-b:a", "192k",
            "-shortest",
            movie_body_path
        ])
        res_r = subprocess.run(cmd_render, capture_output=True, text=True, timeout=1800)
        if res_r.returncode != 0 or not os.path.exists(movie_body_path):
            print(f"[VoiceDrivenEditor] Lỗi render thân phim: {res_r.stderr[-300:]}")
            return False

        # Nếu không yêu cầu outro (ví dụ checkpoint từng tập), xuất trực tiếp thân phim
        if not include_outro:
            if os.path.exists(output_video_path):
                try: os.remove(output_video_path)
                except: pass
            os.replace(movie_body_path, output_video_path)
            print(f"[VoiceDrivenEditor] Hoàn tất render thân phim checkpoint (không outro): {output_video_path}")
            return True

        # 7. Ghép nối Outro Mèo Thần Tài (100% volume gốc)
        outro_source = str(ASSETS_DIR / "outtro.mp4")
        normalized_outro = str(tmp_path / f"normalized_outro_{profile.name}.mp4")
        if cls.prepare_outro_for_profile(outro_source, normalized_outro, profile):
            final_concat_txt = str(tmp_path / "final_outro_concat.txt")
            with open(final_concat_txt, "w", encoding="utf-8") as f:
                f.write(f"file '{Path(movie_body_path).resolve().as_posix()}'\n")
                f.write(f"file '{Path(normalized_outro).resolve().as_posix()}'\n")

            cmd_concat = [
                "ffmpeg", "-nostdin", "-y", "-f", "concat", "-safe", "0",
                "-i", final_concat_txt,
                "-c", "copy",
                output_video_path
            ]
            res_c = subprocess.run(cmd_concat, capture_output=True, text=True, timeout=300)
            if res_c.returncode != 0 or not os.path.exists(output_video_path) or os.path.getsize(output_video_path) == 0:
                print("[VoiceDrivenEditor] Thử lại ghép outro bằng re-encode...")
                cmd_reencode = [
                    "ffmpeg", "-nostdin", "-y", "-f", "concat", "-safe", "0",
                    "-i", final_concat_txt,
                    "-c:v", "libx264", "-preset", "ultrafast", "-crf", "22",
                    "-c:a", "aac", "-b:a", "192k",
                    output_video_path
                ]
                res_c = subprocess.run(cmd_reencode, capture_output=True, text=True, timeout=600)

            if res_c.returncode == 0 and os.path.exists(output_video_path) and os.path.getsize(output_video_path) > 0:
                print(f"[VoiceDrivenEditor] Hoàn tất render video Tiếng Dẫn Hình ({profile.name}): {output_video_path}")
                return True

        # Nếu không có outro hoặc lỗi outro, dùng luôn body
        os.replace(movie_body_path, output_video_path)
        print(f"[VoiceDrivenEditor] Hoàn tất render thân phim (không outro): {output_video_path}")
        return True
