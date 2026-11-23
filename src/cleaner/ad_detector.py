import os
import re
import subprocess
from pathlib import Path
from typing import List, Tuple, Dict, Any, Optional
from config.settings import Settings

class AdDetector:
    """
    Lớp lọc quảng cáo cờ bạc thứ 2 bằng quét khung hình (Visual OCR/Pattern)
    kết hợp với skip_ranges được phát hiện bởi Gemini File API Multimodal.
    Phát hiện và cắt bỏ các đoạn quảng cáo nhà cái, website cá cược chèn vào video.
    """

    @classmethod
    def _extract_sample_frames(cls, video_path: str, output_dir: str, duration: float) -> List[Tuple[float, str]]:
        """Trích xuất các khung hình mẫu tại các mốc thời gian nhạy cảm (30s đầu, 30s cuối, giữa)."""
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        timestamps = []
        # 30s đầu (mỗi 3s lấy 1 frame)
        for t in range(1, min(30, int(duration)), 3):
            timestamps.append(float(t))
        # 30s cuối
        if duration > 45:
            for t in range(max(31, int(duration - 30)), int(duration), 4):
                timestamps.append(float(t))

        frames = []
        for ts in timestamps:
            frame_file = os.path.join(output_dir, f"frame_{int(ts):04d}.jpg")
            cmd = [
                "ffmpeg", "-nostdin", "-y", "-ss", str(ts),
                "-i", video_path,
                "-vframes", "1",
                "-q:v", "3",
                frame_file
            ]
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if os.path.exists(frame_file) and os.path.getsize(frame_file) > 1000:
                frames.append((ts, frame_file))
        return frames

    @classmethod
    def _run_ocr_on_frame(cls, frame_path: str) -> str:
        """Đọc văn bản từ khung hình bằng OCR (RapidOCR / Tesseract hoặc regex)."""
        # 1. Thử RapidOCR nếu có cài đặt
        try:
            from rapidocr_onnxruntime import RapidOCR
            engine = RapidOCR()
            result, _ = engine(frame_path)
            if result:
                return " ".join([line[1] for line in result])
        except Exception:
            pass

        # 2. Thử pytesseract nếu có
        try:
            import pytesseract
            from PIL import Image
            img = Image.open(frame_path)
            return pytesseract.image_to_string(img)
        except Exception:
            pass

        return ""

    @classmethod
    def detect_ad_ranges(
        cls,
        video_path: str,
        tmp_dir: str,
        external_skip_ranges: Optional[List[Dict[str, Any]]] = None
    ) -> List[Tuple[float, float]]:
        """
        Quét và phát hiện các khoảng thời gian bị chèn quảng cáo cờ bạc
        và kết hợp các khoảng skip_ranges từ Gemini Multimodal.
        """
        ad_ranges: List[Tuple[float, float]] = []

        # 1. Nạp external_skip_ranges từ Gemini (Opening, Ending, Middle Ads)
        if external_skip_ranges:
            for item in external_skip_ranges:
                st = float(item.get("start_sec", item.get("start", 0.0)))
                en = float(item.get("end_sec", item.get("end", 0.0)))
                if en > st + 0.5:
                    ad_ranges.append((max(0.0, st), en))

        # 2. Quét OCR bổ sung tìm từ khóa cờ bạc
        blacklist = Settings.load_blacklist()
        gambling_patterns = [re.compile(p, re.IGNORECASE) for p in blacklist.get("gambling_keywords", [])]

        dur_cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", video_path]
        try:
            res = subprocess.run(dur_cmd, capture_output=True, text=True)
            duration = float(res.stdout.strip() or 600.0)
        except Exception:
            duration = 600.0

        frames = cls._extract_sample_frames(video_path, os.path.join(tmp_dir, "ad_frames"), duration)
        ocr_timestamps = []

        for ts, frame_path in frames:
            text = cls._run_ocr_on_frame(frame_path)
            if text:
                for pattern in gambling_patterns:
                    if pattern.search(text):
                        print(f"[AdDetector] Phát hiện dấu hiệu cờ bạc qua OCR tại giây {ts:.1f}: '{text[:60]}...'")
                        ocr_timestamps.append(ts)
                        break

        # Gộp các timestamp OCR vào ad_ranges
        if ocr_timestamps:
            ocr_timestamps.sort()
            st = ocr_timestamps[0]
            en = ocr_timestamps[0]
            for t in ocr_timestamps[1:]:
                if t - en <= 6.0:
                    en = t
                else:
                    ad_ranges.append((max(0.0, st - 1.0), en + 2.0))
                    st = t
                    en = t
            ad_ranges.append((max(0.0, st - 1.0), en + 2.0))

        if not ad_ranges:
            return []

        # Hợp nhất các khoảng chồng lấn
        ad_ranges.sort(key=lambda x: x[0])
        merged = []
        cur_st, cur_en = ad_ranges[0]
        for st, en in ad_ranges[1:]:
            if st <= cur_en + 1.0:
                cur_en = max(cur_en, en)
            else:
                merged.append((round(cur_st, 2), round(cur_en, 2)))
                cur_st, cur_en = st, en
        merged.append((round(cur_st, 2), round(cur_en, 2)))
        return merged

    @classmethod
    def clean_video_ads(
        cls,
        video_path: str,
        output_path: str,
        tmp_dir: str,
        external_skip_ranges: Optional[List[Dict[str, Any]]] = None
    ) -> str:
        """
        Cắt bỏ hoàn toàn các đoạn quảng cáo đã phát hiện để thu được video sạch 100%.
        Nếu không có quảng cáo, trả về đường dẫn video gốc.
        """
        ad_ranges = cls.detect_ad_ranges(video_path, tmp_dir, external_skip_ranges=external_skip_ranges)
        if not ad_ranges:
            print("[AdDetector] Khung hình sạch, không phát hiện quảng cáo cờ bạc.")
            return video_path

        print(f"[AdDetector] Đang cắt bỏ {len(ad_ranges)} đoạn quảng cáo/credits: {ad_ranges}")

        dur_cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", video_path]
        res = subprocess.run(dur_cmd, capture_output=True, text=True)
        total_dur = float(res.stdout.strip()) if res.stdout.strip() else 600.0

        keep_segments = []
        curr = 0.0
        for ad_start, ad_end in ad_ranges:
            if ad_start > curr + 1.0:
                keep_segments.append((curr, ad_start))
            curr = max(curr, ad_end)
        if curr < total_dur - 1.0:
            keep_segments.append((curr, total_dur))

        if not keep_segments:
            return video_path

        cut_files = []
        concat_txt = os.path.join(tmp_dir, "clean_concat.txt")
        try:
            for idx, (k_start, k_end) in enumerate(keep_segments):
                part_f = os.path.join(tmp_dir, f"clean_part_{idx}.mp4")
                seg_dur = k_end - k_start
                cmd = [
                    "ffmpeg", "-nostdin", "-y", "-ss", str(k_start),
                    "-i", video_path,
                    "-t", str(seg_dur),
                    "-c", "copy",
                    part_f
                ]
                subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if os.path.exists(part_f) and os.path.getsize(part_f) > 50000:
                    cut_files.append(part_f)

            if not cut_files:
                return video_path

            with open(concat_txt, "w", encoding="utf-8") as f:
                for cf in cut_files:
                    f.write(f"file '{Path(cf).resolve().as_posix()}'\n")

            cmd_concat = [
                "ffmpeg", "-nostdin", "-y", "-f", "concat", "-safe", "0",
                "-i", concat_txt,
                "-c", "copy",
                output_path
            ]
            subprocess.run(cmd_concat, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if os.path.exists(output_path) and os.path.getsize(output_path) > 1000000:
                print(f"[AdDetector] Đã loại bỏ quảng cáo thành công. File sạch: {output_path}")
                return output_path
        finally:
            if os.path.exists(concat_txt):
                try: os.remove(concat_txt)
                except: pass
            for cf in cut_files:
                if os.path.exists(cf):
                    try: os.remove(cf)
                    except: pass

        return video_path
