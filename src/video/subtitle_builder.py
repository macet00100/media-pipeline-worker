import os
import re
from typing import List, Dict, Any, Optional, Tuple
from config.settings import Settings

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
    c1, c2 = clean_word_for_bond(w1), clean_word_for_bond(w2)
    if (c1, c2) in COMPOUND_PAIRS:
        return True
    if w1 and w2 and w1[0].isupper() and w2[0].isupper() and not w1.isupper() and not w2.isupper():
        return True
    return False

def split_words_naturally(words: List[str], min_w: int = 4, max_w: int = 6) -> List[List[str]]:
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

            cost = (take - 5) ** 2
            if next_idx < n and is_bonded_pair(words[next_idx - 1], words[next_idx]):
                cost += 500.0
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

class SubtitleBuilder:
    """
    Tạo phụ đề ASS (Advanced SubStation Alpha) tiếng Việt
    chuẩn hóa 4-6 từ/dòng, Arial 26pt, căn giữa dải kính mờ tại \\pos(360, 864).
    Khóa đồng bộ phụ đề tuyệt đối theo từng câu dựa trên mốc timestamp ffprobe thực tế.
    """

    @classmethod
    def _format_ass_time(cls, seconds: float) -> str:
        """Định dạng thời gian theo chuẩn ASS: H:MM:SS.cc"""
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = seconds % 60
        return f"{hours}:{minutes:02d}:{secs:05.2f}"

    @classmethod
    def _split_words_into_lines(cls, words: List[str], min_words: int = 4, max_words: int = 6) -> List[List[str]]:
        """
        Chia danh sách từ thành các cụm 4-6 từ/dòng tối ưu,
        bảo vệ từ ghép và tên nhân vật không bị cắt đôi.
        """
        return split_words_naturally(words, min_w=min_words, max_w=max_words)

    @classmethod
    def generate_ass_subtitles(
        cls,
        aligned_segments: List[Dict[str, Any]],
        output_ass_path: str
    ) -> str:
        """
        Sinh file phụ đề ASS tiếng Việt đồng bộ tuyệt đối theo mốc timestamp từng câu
        và chia dòng chuẩn 4-6 từ/dòng.
        """
        ass_header = (
            "[Script Info]\n"
            "Title: Cool Mèo Phim Subtitles\n"
            "ScriptType: v4.00+\n"
            "WrapStyle: 0\n"
            "PlayResX: 720\n"
            "PlayResY: 1280\n"
            "ScaledBorderAndShadow: yes\n\n"
            "[V4+ Styles]\n"
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
            "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
            "Alignment, MarginL, MarginR, MarginV, Encoding\n"
            "Style: Default,Arial,26,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,"
            "1,0,0,0,100,100,0,0,1,1.8,1,5,30,30,416,1\n\n"
            "[Events]\n"
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        )

        dialogues = []

        for seg in aligned_segments:
            seg_start = seg.get("start_sec", 0.0)
            sentence_timings = seg.get("sentence_timings")

            if sentence_timings and isinstance(sentence_timings, list) and len(sentence_timings) > 0:
                # 1. ĐỒNG BỘ TUYỆT ĐỐI THEO TỪNG CÂU VỚI TIMESTAMP FFPROBE
                for sent in sentence_timings:
                    sent_text = sent.get("text", "").strip()
                    if not sent_text:
                        continue
                    sent_rel_start = float(sent.get("start_sec", 0.0))
                    sent_rel_end = float(sent.get("end_sec", sent_rel_start))
                    sent_dur = float(sent.get("duration", max(0.01, sent_rel_end - sent_rel_start)))

                    abs_sent_start = seg_start + sent_rel_start
                    abs_sent_end = seg_start + sent_rel_end

                    words = sent_text.split()
                    lines = cls._split_words_into_lines(words, min_words=4, max_words=6)
                    total_w = len(words)
                    curr_time = abs_sent_start

                    for lw in lines:
                        w_cnt = len(lw)
                        line_dur = (w_cnt / total_w) * sent_dur if total_w > 0 else sent_dur
                        line_start = curr_time
                        line_end = min(abs_sent_end, line_start + line_dur)

                        start_str = cls._format_ass_time(line_start)
                        end_str = cls._format_ass_time(line_end)
                        line_text = " ".join(lw)

                        dialogue_line = (
                            f"Dialogue: 0,{start_str},{end_str},Default,,0,0,0,,"
                            f"{{\\pos(360,864)\\an5}}{line_text}"
                        )
                        dialogues.append(dialogue_line)
                        curr_time = line_end

            else:
                # Fallback: Chia theo câu và phân bổ thời lượng nếu chưa có sentence_timings
                script = seg.get("script", "").strip()
                actual_dur = seg.get("actual_tts_duration", seg.get("duration", 0.0))
                raw_sentences = [s.strip() for s in re.split(r"(?<=[.!?;\n])\s+", script) if s.strip()]
                if not raw_sentences:
                    raw_sentences = [script]

                total_all_words = max(1, len(script.split()))
                curr_sent_start = seg_start

                for sent_str in raw_sentences:
                    s_words = sent_str.split()
                    if not s_words:
                        continue
                    s_dur = (len(s_words) / total_all_words) * actual_dur
                    s_lines = cls._split_words_into_lines(s_words, min_words=4, max_words=6)
                    curr_line_start = curr_sent_start

                    for lw in s_lines:
                        line_dur = (len(lw) / len(s_words)) * s_dur
                        line_end = curr_line_start + line_dur

                        start_str = cls._format_ass_time(curr_line_start)
                        end_str = cls._format_ass_time(line_end)
                        line_text = " ".join(lw)

                        dialogue_line = (
                            f"Dialogue: 0,{start_str},{end_str},Default,,0,0,0,,"
                            f"{{\\pos(360,864)\\an5}}{line_text}"
                        )
                        dialogues.append(dialogue_line)
                        curr_line_start = line_end

                    curr_sent_start += s_dur

        with open(output_ass_path, "w", encoding="utf-8") as f:
            f.write(ass_header)
            f.write("\n".join(dialogues) + "\n")

        print(f"[SubtitleBuilder] Đã xuất file phụ đề ASS ({len(dialogues)} dòng): {output_ass_path}")
        return output_ass_path
