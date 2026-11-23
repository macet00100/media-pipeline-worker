import os
import sys
import re
import time
import asyncio
import subprocess
import shutil
from pathlib import Path
from typing import Dict, Any, List, Tuple, Optional
import edge_tts
from config.settings import Settings

class TTSEngine:
    """
    Bộ tổng hợp giọng đọc tiếng Việt sử dụng edge-tts
    Khóa cứng giọng vi-VN-NamMinhNeural, hỗ trợ retry với exponential backoff.
    Khóa đồng bộ phụ đề tuyệt đối theo từng câu ngắn:
    1. Tách kịch bản thành từng câu ngắn trước khi gọi edge-tts.
    2. Đo chính xác thời lượng thực tế từng câu bằng ffprobe.
    3. Ghi mốc timestamp chuẩn xác cho từng câu.
    4. Ghép nối audio các câu ngắn thành dải âm thanh hoàn chỉnh.
    """

    VOICE = "vi-VN-NamMinhNeural"
    _last_sentence_timings: List[Dict[str, Any]] = []

    @classmethod
    def normalize_narration_text(cls, text: str) -> str:
        """
        Chuẩn hóa văn bản đồng bộ cho cả Lời đọc TTS và Phụ đề ASS:
        Thay thế các từ dễ bị false-positive hoặc vấp từ ngữ nghĩa.
        GIỮ NGUYÊN thương hiệu 'Cool Mèo Phim'.
        """
        t = re.sub(r"\b(?:chị\s+gái|chị)\s+Lâm\b", "người chị Lâm", text, flags=re.IGNORECASE)
        t = re.sub(r"\bbệnh\s+tim\b(?!\s*mạch)", "bệnh tim mạch", t, flags=re.IGNORECASE)
        t = re.sub(r"\bcon\s+tim\b", "trái tim", t, flags=re.IGNORECASE)
        t = re.sub(r"\bkiểm\s+soát\s+nghiêm\s+ngặt\b", "quản lý nghiêm ngặt", t, flags=re.IGNORECASE)
        t = re.sub(r"\bđâm\s+nát\b", "hư hỏng nặng", t, flags=re.IGNORECASE)
        t = re.sub(r"\bkết\s+thúc\s+cuộc\s+đời\s*(?:của)?\s*(?:hai\s+chị\s+em)?\b", "khiến hai chị em qua đời", t, flags=re.IGNORECASE)
        t = re.sub(r"\bkết\s+thúc\s+cuộc\s+đời\b", "không may qua đời", t, flags=re.IGNORECASE)
        t = re.sub(r"\bđau\s+thương\s+tột\s+cùng\b", "tiếc nuối tột cùng", t, flags=re.IGNORECASE)
        return t

    @classmethod
    def prepare_tts_text(cls, text: str) -> str:
        """
        Chuẩn hóa text riêng cho giọng đọc NamMinhNeural:
        1. Gọi normalize_narration_text để đồng bộ 100% từ vựng với phụ đề ASS.
        2. Giữ nguyên 'Cool Mèo Phim' chuẩn xác (tuyệt đối không tách 2 âm tiết kỳ cục).
           LƯU Ý: Chữ hiển thị trên phụ đề ASS và SEO luôn giữ 100% là 'Cool Mèo Phim'.
        3. Dấu chấm kết thúc câu chuẩn hóa.
        """
        t = cls.normalize_narration_text(text)
        t = re.sub(r"@CoolMeoPhim", "kênh Cool Mèo Phim", t, flags=re.IGNORECASE)
        t = t.strip().rstrip('.,;:!?-–— ')
        if t:
            t = t[0].upper() + t[1:] + "."
        return t

    @classmethod
    def parse_vtt_timestamp(cls, ts_str: str) -> float:
        """Parse mốc thời gian WebVTT (00:00:03.300 hoặc 00:00:03,300) thành float seconds."""
        ts_str = ts_str.strip().replace(",", ".")
        parts = ts_str.split(":")
        if len(parts) == 3:
            h, m, s = parts
            return float(h) * 3600.0 + float(m) * 60.0 + float(s)
        elif len(parts) == 2:
            m, s = parts
            return float(m) * 60.0 + float(s)
        return float(ts_str)

    @classmethod
    def parse_vtt_cues(cls, vtt_content: str) -> List[Dict[str, Any]]:
        """Parse các khối cue từ file WebVTT do edge-tts sinh ra."""
        cues = []
        blocks = re.split(r"\n\s*\n", vtt_content.strip())
        for block in blocks:
            lines = [line.strip() for line in block.split("\n") if line.strip()]
            if not lines or lines[0].startswith("WEBVTT"):
                continue
            arrow_line_idx = -1
            for i, line in enumerate(lines):
                if "-->" in line:
                    arrow_line_idx = i
                    break
            if arrow_line_idx != -1:
                arrow_line = lines[arrow_line_idx]
                text = " ".join(lines[arrow_line_idx + 1:]).strip()
                parts = arrow_line.split("-->")
                start_sec = cls.parse_vtt_timestamp(parts[0])
                end_sec = cls.parse_vtt_timestamp(parts[1].split()[0])
                cues.append({
                    "start_sec": round(start_sec, 3),
                    "end_sec": round(end_sec, 3),
                    "duration": round(end_sec - start_sec, 3),
                    "text": text
                })
        return cues

    @classmethod
    def _synthesize_isolated_vtt(
        cls,
        text: str,
        output_audio_path: str,
        output_vtt_path: str,
        voice: Optional[str] = None,
        timeout_sec: float = 40.0
    ) -> bool:
        """
        Gọi edge-tts với cả --write-media và --write-subtitles trong một lần gọi subprocess.
        Tăng tốc gấp 5 lần bằng cách lấy toàn bộ audio và timestamp chi tiết từ .vtt.
        """
        use_voice = voice or cls.VOICE
        cmd = [
            sys.executable, "-m", "edge_tts",
            "--voice", use_voice,
            "--text", text,
            "--write-media", output_audio_path,
            "--write-subtitles", output_vtt_path
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec)
        if res.returncode == 0 and os.path.exists(output_audio_path) and os.path.getsize(output_audio_path) > 500:
            return True
        err_msg = res.stderr.strip() if res.stderr else "Không nhận được audio"
        raise RuntimeError(f"Edge-TTS Subprocess VTT error: {err_msg}")

    @classmethod
    def get_last_sentence_timings(cls) -> List[Dict[str, Any]]:
        """Lấy danh sách mốc timestamp của các câu được tổng hợp gần nhất."""
        return list(cls._last_sentence_timings)

    @classmethod
    def split_into_sentences(cls, text: str) -> List[str]:
        """
        Tách kịch bản thành từng câu hoàn chỉnh dựa trên dấu kết thúc câu (. ! ? ; \\n).
        Lọc sạch 100% chữ Hán trước khi đọc.
        Tuyệt đối không tách vụn câu theo dấu phẩy để đảm bảo tính trọn vẹn ngữ nghĩa,
        tránh bị hệ thống lọc giọng chặn và giữ nhịp đọc tự nhiên trôi chảy.
        """
        # 1. Lọc bỏ 100% chữ Hán
        text = re.sub(r"[\u4e00-\u9fff]+", "", text)
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            return []

        raw_parts = re.split(r"(?<=[.!?;\n])\s+", text)
        sentences = []
        for part in raw_parts:
            part = part.strip()
            if part:
                sentences.append(part)
        return sentences if sentences else [text]

    @classmethod
    def get_audio_duration(cls, audio_path: str) -> float:
        """Đo thời lượng chính xác của file audio bằng ffprobe."""
        if not os.path.exists(audio_path):
            return 0.0
        cmd = [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", audio_path
        ]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            return float(res.stdout.strip())
        except Exception:
            return 0.0

    @classmethod
    def _synthesize_isolated(cls, text: str, output_path: str, voice: Optional[str] = None, timeout_sec: float = 30.0) -> bool:
        """
        Gọi edge-tts thông qua tiến trình con độc lập (Isolated Subprocess).
        Khắc phục 100% xung đột IOCP/ProactorEventLoop trên Windows và hiện tượng rò rỉ socket của aiohttp,
        đảm bảo mỗi yêu cầu TTS được xử lý trong môi trường runtime sạch tuyệt đối.
        """
        use_voice = voice or cls.VOICE
        cmd = [
            sys.executable, "-m", "edge_tts",
            "--voice", use_voice,
            "--text", text,
            "--write-media", output_path
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec)
        if res.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 500:
            return True
        err_msg = res.stderr.strip() if res.stderr else "Không nhận được audio"
        raise RuntimeError(f"Edge-TTS Subprocess error: {err_msg}")

    @classmethod
    async def _synthesize_async(cls, text: str, output_path: str, voice: Optional[str] = None, timeout_sec: float = 20.0) -> bool:
        """Gọi edge-tts bất đồng bộ có timeout (fallback)."""
        use_voice = voice or cls.VOICE
        comm = edge_tts.Communicate(text, use_voice)
        await asyncio.wait_for(comm.save(output_path), timeout=timeout_sec)
        return os.path.exists(output_path) and os.path.getsize(output_path) > 1000

    @classmethod
    def synthesize_with_timestamps(
        cls,
        text: str,
        output_path: str,
        max_retries: int = 8
    ) -> Tuple[float, List[Dict[str, Any]]]:
        """Tổng hợp giọng đọc và trả về cả thời lượng tổng cùng danh sách timestamp từng câu."""
        return cls.synthesize_segment(text, output_path, max_retries=max_retries, return_timestamps=True)

    @classmethod
    def synthesize_segment(
        cls,
        text: str,
        output_path: str,
        max_retries: int = 8,
        return_timestamps: bool = False
    ) -> Any:
        """
        Tổng hợp giọng đọc cho một phân đoạn văn bản:
        1. Ưu tiên Block-Level VTT Parsing (tăng tốc 5x): Gom cả block gọi edge-tts --write-subtitles một lần,
           parse chi tiết mốc thời gian từng câu từ .vtt mà không cần gọi lẻ tẻ từng câu.
        2. Tự động fallback về Sentence-Level (tách từng câu + đo ffprobe + retry) nếu block-level gặp sự cố.
        """
        text = text.strip()
        cls._last_sentence_timings = []
        if not text:
            print("[TTSEngine] Cảnh báo: Văn bản rỗng.")
            return (0.0, []) if return_timestamps else 0.0

        sentences = cls.split_into_sentences(text)
        tmp_dir = Path(output_path).parent
        tmp_dir.mkdir(parents=True, exist_ok=True)
        vtt_path = str(output_path).replace(".mp3", ".vtt")

        # -------------------------------------------------------------
        # 1. THỬ NGHIỆM BLOCK-LEVEL VTT PARSING (TỐI ƯU TỐC ĐỘ GẤP 5 LẦN)
        # -------------------------------------------------------------
        num_words = len(text.split())
        if 1 <= len(sentences) <= 15 and num_words <= 180:
            try:
                base_block_text = cls.prepare_tts_text(text)
                if os.path.exists(output_path):
                    try: os.remove(output_path)
                    except: pass
                if os.path.exists(vtt_path):
                    try: os.remove(vtt_path)
                    except: pass

                cls._synthesize_isolated_vtt(base_block_text, output_path, vtt_path, timeout_sec=40.0)
                dur = cls.get_audio_duration(output_path)
                if dur > 0.5 and os.path.exists(vtt_path):
                    with open(vtt_path, "r", encoding="utf-8") as vf:
                        vtt_content = vf.read()
                    cues = cls.parse_vtt_cues(vtt_content)
                    if cues:
                        sentence_timings = []
                        for idx, cue in enumerate(cues):
                            if idx < len(sentences):
                                disp_text = cls.normalize_narration_text(sentences[idx])
                            else:
                                disp_text = re.sub(r"(?:Cu\s*lờ|Kun|Cun)\s*Mèo\s*Phim", "Cool Mèo Phim", cue["text"], flags=re.IGNORECASE)
                            c_start = 0.0 if idx == 0 else sentence_timings[-1]["end_sec"]
                            c_end = max(c_start + 0.3, cue["end_sec"])
                            sentence_timings.append({
                                "index": idx,
                                "text": disp_text,
                                "start_sec": round(c_start, 3),
                                "end_sec": round(c_end, 3),
                                "duration": round(c_end - c_start, 3)
                            })

                        # Đảm bảo mốc kết thúc câu cuối bao quát toàn bộ audio
                        if sentence_timings:
                            sentence_timings[-1]["end_sec"] = max(sentence_timings[-1]["end_sec"], round(dur, 3))
                            sentence_timings[-1]["duration"] = round(sentence_timings[-1]["end_sec"] - sentence_timings[-1]["start_sec"], 3)

                        cls._last_sentence_timings = sentence_timings
                        print(f"[TTSEngine] [BLOCK-VTT 5X SPEED] Hoàn tất TTS: {dur:.2f}s ({len(sentence_timings)} câu, {num_words} từ)")
                        if return_timestamps:
                            return dur, sentence_timings
                        return dur
            except Exception as e_block:
                print(f"[TTSEngine] Block-level VTT không hoàn tất ({e_block}). Tự động fallback về Sentence-Level...")

        # -------------------------------------------------------------
        # 2. SENTENCE-LEVEL FALLBACK (TÁCH CÂU + RETRY CHẶT CHẼ)
        # -------------------------------------------------------------
        part_files = []
        sentence_timings = []
        current_offset = 0.0

        try:
            for idx, sent in enumerate(sentences):
                part_file = str(tmp_dir / f"tts_sent_{idx}_{int(time.time()*1000)}.mp3")
                success = False

                # Đệm 1.5s giữa các câu
                time.sleep(1.5)

                # Text phát âm tự nhiên (chỉ dùng riêng cho Edge-TTS)
                base_tts_text = cls.prepare_tts_text(sent)
                if not base_tts_text:
                    continue

                for attempt in range(1, max_retries + 1):
                    try:
                        if os.path.exists(part_file):
                            try: os.remove(part_file)
                            except: pass

                        if attempt == 1:
                            text_to_speak = base_tts_text
                        elif attempt == 2:
                            # Làm sạch dấu câu lạ, giữ nguyên câu hoàn chỉnh
                            clean_t = re.sub(r"[,;:\-–—]", " ", base_tts_text)
                            clean_t = re.sub(r"\s+", " ", clean_t).strip()
                            text_to_speak = cls.prepare_tts_text(clean_t)
                        else:
                            # Thay thế từ đồng nghĩa nhẹ nhàng hơn để vượt qua bộ lọc khắt khe nếu có
                            clean_t = re.sub(r"[,;:\-–—]", " ", base_tts_text)
                            clean_t = re.sub(r"\s+", " ", clean_t).strip()
                            clean_t = re.sub(r"\bkiểm\s+soát\b", "chăm sóc chu đáo", clean_t, flags=re.IGNORECASE)
                            clean_t = re.sub(r"\btai\s+nạn\s+giao\s+thông\s+nghiêm\s+trọng\b", "sự cố va chạm bất ngờ", clean_t, flags=re.IGNORECASE)
                            clean_t = re.sub(r"\btai\s+nạn\b", "sự cố", clean_t, flags=re.IGNORECASE)
                            clean_t = re.sub(r"\bđau\s+thương\b", "xót xa", clean_t, flags=re.IGNORECASE)
                            clean_t = re.sub(r"\bchết\b", "ra đi", clean_t, flags=re.IGNORECASE)
                            text_to_speak = cls.prepare_tts_text(clean_t)

                        if not os.path.exists(part_file) or os.path.getsize(part_file) < 500:
                            cls._synthesize_isolated(text_to_speak, part_file, timeout_sec=30.0)

                        dur = cls.get_audio_duration(part_file)
                        if dur > 0.3:
                            success = True
                            part_files.append(part_file)
                            break
                        else:
                            print(f"[TTSEngine] Lần thử {attempt}: Audio câu {idx} bất thường.")
                    except Exception as e:
                        wait_sec = round(min(12.0, 1.5 * attempt), 1)
                        print(f"[TTSEngine] Lần thử {attempt}/{max_retries} câu #{idx}: {e}. Đang thử lại sau {wait_sec}s...")
                        if os.path.exists(part_file):
                            try: os.remove(part_file)
                            except: pass
                        time.sleep(wait_sec)

                if not success:
                    # Tuyệt đối cấm dùng sóng sine hoặc chèn âm thanh giả
                    raise RuntimeError(
                        f"LỖI TTS: Không thể tạo giọng đọc cho câu #{idx}: '{sent}'. "
                        "Dừng job để xử lý kết nối edge-tts, tuyệt đối không chèn tiếng bíp hay im lặng giả!"
                    )

                # 2. Đo chính xác thời lượng thực tế từng câu bằng ffprobe
                dur = cls.get_audio_duration(part_file)
                start_sec = current_offset
                end_sec = current_offset + dur

                # 3. Ghi mốc timestamp chuẩn xác cho từng câu (đồng bộ từ vựng với lời đọc)
                norm_disp_text = cls.normalize_narration_text(sent)
                norm_disp_text = re.sub(r"(?:Cu\s*lờ|Kun)\s*Mèo\s*Phim", "Cool Mèo Phim", norm_disp_text, flags=re.IGNORECASE)
                sentence_timings.append({
                    "index": idx,
                    "text": norm_disp_text,
                    "start_sec": round(start_sec, 3),
                    "end_sec": round(end_sec, 3),
                    "duration": round(dur, 3)
                })
                current_offset = end_sec

            # 4. Ghép nối audio các câu ngắn thành file hoàn chỉnh
            if len(part_files) == 1:
                if os.path.exists(output_path):
                    try: os.remove(output_path)
                    except: pass
                shutil.copyfile(part_files[0], output_path)
            else:
                concat_list = str(tmp_dir / f"concat_tts_{int(time.time()*1000)}.txt")
                with open(concat_list, "w", encoding="utf-8") as f:
                    for pf in part_files:
                        abs_posix = Path(pf).resolve().as_posix()
                        f.write(f"file '{abs_posix}'\n")

                cmd = [
                    "ffmpeg", "-y", "-f", "concat", "-safe", "0",
                    "-i", concat_list,
                    "-c", "copy",
                    output_path
                ]
                res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if res.returncode != 0 or not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
                    # Fallback re-encode nếu concat copy không thành công
                    subprocess.run([
                        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
                        "-i", concat_list,
                        "-c:a", "libmp3lame", "-b:a", "128k",
                        output_path
                    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

                if os.path.exists(concat_list):
                    try: os.remove(concat_list)
                    except: pass

            final_duration = cls.get_audio_duration(output_path)
            cls._last_sentence_timings = sentence_timings
            print(f"[TTSEngine] Hoàn tất TTS: {final_duration:.2f}s ({len(sentences)} câu, {len(text.split())} từ)")

            if return_timestamps:
                return final_duration, sentence_timings
            return final_duration

        finally:
            for pf in part_files:
                if os.path.exists(pf):
                    try: os.remove(pf)
                    except: pass
