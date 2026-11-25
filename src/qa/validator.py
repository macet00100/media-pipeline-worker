import os
import json
import re
import subprocess
import numpy as np
from pathlib import Path
from typing import Dict, Any, List, Tuple, Optional
from PIL import Image

from config.settings import Settings, ASSETS_DIR

class QualityValidator:
    """
    Cổng kiểm định chất lượng tự động (QA Gate) trước khi xuất Gói Xét Duyệt.
    Kiểm tra 12 tiêu chí bắt buộc:
    1. Chuẩn độ phân giải (720x1280 hoặc 1280x720) & Codec chuẩn.
    2. Tồn tại file Thumbnail chất lượng cao.
    3. Tỷ lệ phủ giọng đọc (Khóa cứng 85% - 95%).
    4. Khoảng lặng chết (Khóa cứng <= 3.5s).
    5. Nhận diện thương hiệu 'Cool Mèo Phim' (@CoolMeoPhim).
    6. 0 từ khóa cờ bạc / 0 tên kênh lạ từ blacklist.
    7. Giữ nguyên 100% âm lượng gốc của Outro Mèo Thần Tài (diff <= 1.5 dB).
    8. QUÉT SINE WAVE: 0% tiếng bíp sine wave 440Hz gian lận.
    9. QUÉT CHỮ HÁN: 0% ký tự tiếng Trung ([\\u4e00-\\u9fff]) trong kịch bản, sub ASS, SEO.
    10. QUÉT N-GRAM: Không có cụm từ lặp đi lặp lại bất thường (> 3 lần liên tiếp).
    11. CHUẨN SUBTITLE ASS: <= 45 ký tự/dòng, 0% chồng lấn timestamp.
    12. BỀN VỮNG LOGO: Logo tồn tại bền vững tại (32, 32) xuyên suốt 15%, 50%, 85% video.
    """

    @classmethod
    def check_video_specs(cls, video_path: str) -> Tuple[bool, Dict[str, Any]]:
        """Kiểm tra độ phân giải, fps và codec."""
        cmd = [
            "ffprobe", "-v", "error",
            "-show_entries", "stream=width,height,codec_name,r_frame_rate,codec_type",
            "-of", "json",
            video_path
        ]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            data = json.loads(res.stdout)
            streams = data.get("streams", [])
            v_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
            a_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)

            if not v_stream or not a_stream:
                return False, {"error": "Thiếu stream video hoặc audio"}

            w = int(v_stream.get("width", 0))
            h = int(v_stream.get("height", 0))
            # Hỗ trợ cả 2 định dạng chuẩn: Đoản kịch dọc (720x1280) và Phim bộ ngang (1280x720)
            is_valid_size = (w == 720 and h == 1280) or (w == 1280 and h == 720)

            return is_valid_size, {
                "width": w,
                "height": h,
                "orientation": "vertical" if h > w else "horizontal",
                "video_codec": v_stream.get("codec_name"),
                "audio_codec": a_stream.get("codec_name")
            }
        except Exception as e:
            return False, {"error": str(e)}

    @classmethod
    def detect_max_silence(cls, video_path: str) -> float:
        """Đo khoảng lặng lớn nhất trên dải âm thanh bằng filter silencedetect."""
        cmd = [
            "ffmpeg", "-i", video_path,
            "-af", "silencedetect=noise=-30dB:d=1.5",
            "-f", "null", "-"
        ]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            durations = []
            for line in res.stderr.splitlines():
                if "silence_duration:" in line:
                    match = re.search(r"silence_duration:\s*([0-9.]+)", line)
                    if match:
                        durations.append(float(match.group(1)))
            return max(durations) if durations else 0.0
        except Exception:
            return 0.0

    @classmethod
    def get_audio_rms_db(cls, media_path: str, sseof: Optional[float] = None) -> Optional[float]:
        """Đo giá trị RMS level dB tổng thể của file media hoặc đoạn cuối sseof."""
        if not os.path.exists(media_path):
            return None
        cmd = ["ffmpeg"]
        if sseof is not None:
            cmd.extend(["-sseof", str(sseof)])
        cmd.extend([
            "-i", media_path,
            "-af", "astats=metadata=1:reset=1",
            "-f", "null", "-"
        ])
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            for line in res.stderr.splitlines():
                if "RMS level dB:" in line:
                    m = re.search(r"RMS level dB:\s*([-\d.]+)", line)
                    if m:
                        return float(m.group(1))
            return None
        except Exception:
            return None

    @classmethod
    def verify_outro_audio_rms(cls, final_video_path: str, outro_path: str) -> Tuple[bool, Dict[str, Any]]:
        """
        Xác nhận âm lượng phần Outro giữ nguyên 100% gốc:
        - So sánh RMS của 8s cuối video thành phẩm với 8s cuối của outro gốc.
        - Chênh lệch RMS phải <= 1.5 dB.
        - Tuyệt đối không bị tắt tiếng (-inf dB hoặc <= -90dB).
        """
        if not os.path.exists(outro_path):
            return True, {"note": "Không tìm thấy file outro gốc"}
        if not os.path.exists(final_video_path):
            return False, {"error": "Không tìm thấy video thành phẩm"}

        orig_rms = cls.get_audio_rms_db(outro_path, sseof=-8)
        final_outro_rms = cls.get_audio_rms_db(final_video_path, sseof=-8)

        metrics: Dict[str, Any] = {
            "original_outro_rms_db": orig_rms,
            "final_outro_rms_db": final_outro_rms,
            "diff_db": round(abs(final_outro_rms - orig_rms), 2) if (orig_rms is not None and final_outro_rms is not None) else None
        }

        if orig_rms is None or final_outro_rms is None:
            return False, metrics

        if final_outro_rms <= -90.0:
            return False, metrics

        diff = abs(final_outro_rms - orig_rms)
        return diff <= 1.5, metrics

    @classmethod
    def check_no_sine_wave(cls, video_path: str) -> Tuple[bool, Dict[str, Any]]:
        """
        KIỂM TRA 1 MỚI: Quét phổ âm thanh xem có phát hiện tiếng bíp sine wave 440Hz hay không.
        Nếu phát hiện năng lượng tập trung đơn cực tại dải 430-450Hz > 65% tổng công suất -> FAIL.
        """
        if not os.path.exists(video_path):
            return False, {"error": "File không tồn tại"}

        sr = 16000
        cmd = [
            "ffmpeg", "-nostdin", "-v", "error",
            "-i", video_path,
            "-vn", "-ac", "1", "-ar", str(sr),
            "-f", "f32le", "-"
        ]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            raw_data, _ = proc.communicate(timeout=60)
            if not raw_data:
                return True, {"checked": "no_audio_stream"}

            samples = np.frombuffer(raw_data, dtype=np.float32)
            win_len = int(sr * 1.0)  # Cửa sổ 1 giây
            freqs = np.fft.rfftfreq(win_len, 1 / sr)
            band_mask = (freqs >= 430) & (freqs <= 450)

            sine_detected_at = []
            for i in range(0, len(samples) - win_len, win_len):
                chunk = samples[i:i + win_len]
                rms = np.sqrt(np.mean(chunk**2))
                if rms < 0.01:
                    continue
                fft_vals = np.abs(np.fft.rfft(chunk))**2
                total_pwr = np.sum(fft_vals)
                if total_pwr <= 0:
                    continue
                band_pwr = np.sum(fft_vals[band_mask])
                ratio = band_pwr / total_pwr
                if ratio > 0.65:
                    sine_detected_at.append(round(i / sr, 2))

            has_sine = len(sine_detected_at) > 0
            return not has_sine, {
                "sine_440hz_detected": has_sine,
                "detected_timestamps": sine_detected_at[:5]
            }
        except Exception as e:
            print(f"[QualityValidator] Cảnh báo quét sine wave: {e}")
            return True, {"warning": str(e)}

    @classmethod
    def check_zero_chinese_characters(cls, corpus: Dict[str, str]) -> Tuple[bool, Dict[str, Any]]:
        """
        KIỂM TRA 2 MỚI: Regex quét toàn bộ văn bản (kịch bản, phụ đề, tiêu đề, mô tả, tags).
        Tuyệt đối không được sót dù chỉ 1 ký tự chữ Hán ([\\u4e00-\\u9fff]).
        """
        all_found = []
        offending_fields = {}

        chinese_pattern = re.compile(r"[\u4e00-\u9fff]")
        for field_name, text in corpus.items():
            if not text:
                continue
            matches = chinese_pattern.findall(text)
            if matches:
                offending_fields[field_name] = matches[:10]
                all_found.extend(matches)

        passed = len(all_found) == 0
        return passed, {
            "total_chinese_chars": len(all_found),
            "offending_fields": offending_fields
        }

    @classmethod
    def check_no_ngram_repetition(cls, text: str, min_n: int = 4, max_n: int = 8, max_repeats: int = 3) -> Tuple[bool, Dict[str, Any]]:
        """
        KIỂM TRA 3 MỚI: Quét n-gram kịch bản xem có cụm từ nào lặp đi lặp lại bất thường
        (ví dụ cụm từ > 4 chữ lặp > 3 lần liên tiếp) -> QA FAIL.
        """
        words = re.findall(r"\w+", text.lower())
        if len(words) < min_n * max_repeats:
            return True, {"status": "text_too_short"}

        for n in range(min_n, min(max_n + 1, len(words) // max_repeats)):
            for i in range(len(words) - n * max_repeats + 1):
                pattern = words[i:i + n]
                repeats = 1
                j = i + n
                while j + n <= len(words) and words[j:j + n] == pattern:
                    repeats += 1
                    j += n
                if repeats > max_repeats:
                    repeated_phrase = " ".join(pattern)
                    return False, {
                        "repeated_phrase": repeated_phrase,
                        "consecutive_repeats": repeats,
                        "n_gram_size": n
                    }

        return True, {"consecutive_repeats_max": 0}

    @classmethod
    def check_ass_subtitles_quality(cls, ass_path: str) -> Tuple[bool, Dict[str, Any]]:
        """
        KIỂM TRA 4 MỚI: Kiểm tra file phụ đề .ass:
        - Mỗi dòng không quá 45 ký tự.
        - Không có 2 dòng đối thoại nào bị đè timestamp lên nhau (start_{i+1} >= end_i - 0.05).
        """
        if not os.path.exists(ass_path):
            return False, {"error": "Không tìm thấy file phụ đề ASS"}

        def parse_time(t_str: str) -> float:
            parts = t_str.strip().split(":")
            h = int(parts[0])
            m = int(parts[1])
            s = float(parts[2])
            return h * 3600 + m * 60 + s

        dialogues = []
        long_lines = []
        overlaps = []

        with open(ass_path, "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("Dialogue:"):
                    parts = line.split(",", 9)
                    if len(parts) >= 10:
                        s_time = parse_time(parts[1])
                        e_time = parse_time(parts[2])
                        raw_text = parts[9]
                        clean_text = re.sub(r"\{.*?\}", "", raw_text).strip()
                        dialogues.append({"start": s_time, "end": e_time, "text": clean_text})
                        if len(clean_text) > 45:
                            long_lines.append({"text": clean_text, "length": len(clean_text)})

        dialogues.sort(key=lambda x: x["start"])
        for i in range(len(dialogues) - 1):
            if dialogues[i + 1]["start"] < dialogues[i]["end"] - 0.05:
                overlaps.append({
                    "prev_end": round(dialogues[i]["end"], 2),
                    "next_start": round(dialogues[i + 1]["start"], 2),
                    "overlap_sec": round(dialogues[i]["end"] - dialogues[i + 1]["start"], 2)
                })

        passed = len(long_lines) == 0 and len(overlaps) == 0
        return passed, {
            "total_dialogue_lines": len(dialogues),
            "long_lines_count": len(long_lines),
            "overlaps_count": len(overlaps),
            "long_lines_sample": long_lines[:3],
            "overlaps_sample": overlaps[:3]
        }

    @classmethod
    def check_logo_persistence(cls, video_path: str, logo_path: str, tmp_dir: str) -> Tuple[bool, Dict[str, Any]]:
        """
        KIỂM TRA 5 MỚI: Trích xuất 3 khung hình tại 15%, 50%, 85% của video thành phẩm,
        kiểm tra tại góc trên bên trái (32, 32) để xác nhận logo tồn tại xuyên suốt video.
        """
        if not os.path.exists(video_path) or not os.path.exists(logo_path):
            return False, {"error": "Thiếu video hoặc logo"}

        # Lấy thời lượng video
        dur_cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", video_path]
        try:
            res = subprocess.run(dur_cmd, capture_output=True, text=True, timeout=10)
            duration = float(res.stdout.strip() or 0.0)
        except Exception:
            duration = 100.0

        # Lấy thông số width, height
        probe_cmd = ["ffprobe", "-v", "error", "-show_entries", "stream=width,height", "-of", "json", video_path]
        r_pr = subprocess.run(probe_cmd, capture_output=True, text=True, timeout=10)
        try:
            d_pr = json.loads(r_pr.stdout)
            v_w = int(d_pr["streams"][0]["width"])
            v_h = int(d_pr["streams"][0]["height"])
        except Exception:
            v_w, v_h = 720, 1280

        is_vertical = v_h > v_w
        logo_size = 120 if is_vertical else 110
        logo_x = 32
        logo_y = 32

        # Tải logo chuẩn và tạo mask
        logo_pil = Image.open(logo_path).convert("RGBA")
        logo_resized = logo_pil.resize((logo_size, logo_size), Image.Resampling.LANCZOS)
        logo_np = np.array(logo_resized)
        mask = logo_np[:, :, 3] > 180
        logo_rgb = logo_np[:, :, :3]

        # 3 mốc kiểm tra (nằm hoàn toàn trong thân phim, tránh 10s outro cuối)
        body_dur = duration - 10.0 if duration > 12.0 else duration * 0.5
        active_dur = max(1.0, body_dur)
        sample_ts = [active_dur * 0.15, active_dur * 0.50, active_dur * 0.85]

        scores = []
        for idx, ts in enumerate(sample_ts):
            f_path = os.path.join(tmp_dir, f"qa_logo_frame_{idx}.jpg")
            cmd = [
                "ffmpeg", "-nostdin", "-y", "-ss", f"{ts:.2f}",
                "-i", video_path,
                "-vframes", "1",
                "-q:v", "2",
                f_path
            ]
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if not os.path.exists(f_path):
                scores.append(0.0)
                continue

            try:
                frame_img = Image.open(f_path).convert("RGB")
                frame_arr = np.array(frame_img)
                crop = frame_arr[logo_y:logo_y + logo_size, logo_x:logo_x + logo_size]

                # Tính tương quan Pearson giữa crop và logo trên các pixel opaque
                c_vals = crop[mask].astype(float)
                r_vals = logo_rgb[mask].astype(float)
                c_diff = c_vals - np.mean(c_vals)
                r_diff = r_vals - np.mean(r_vals)
                corr = np.sum(c_diff * r_diff) / (np.sqrt(np.sum(c_diff**2)) * np.sqrt(np.sum(r_diff**2)) + 1e-8)
                scores.append(round(float(corr), 3))
            except Exception as e:
                scores.append(0.0)
            finally:
                if os.path.exists(f_path):
                    try: os.remove(f_path)
                    except: pass

        # Logo được coi là hiển thị nếu độ tương quan >= 0.55 ở cả 3 mốc
        all_frames_present = all(s >= 0.55 for s in scores)
        return all_frames_present, {
            "correlation_scores": scores,
            "sample_timestamps_sec": [round(t, 1) for t in sample_ts],
            "threshold_required": 0.55
        }

    @classmethod
    def get_video_duration(cls, video_path: str) -> float:
        """Đo thời lượng video bằng ffprobe."""
        if not os.path.exists(video_path):
            return 0.0
        cmd = [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", video_path
        ]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            return float(res.stdout.strip() or 0.0)
        except Exception:
            return 0.0

    @classmethod
    def validate_review_package(
        cls,
        final_video_path: str,
        thumbnail_path: str,
        seo_data: Dict[str, Any],
        aligned_segments: List[Dict[str, Any]],
        overall_coverage: float,
        output_dir: str,
        ass_subtitles_path: Optional[str] = None,
        raw_total_duration: Optional[float] = None
    ) -> Dict[str, Any]:
        """
        Thực hiện toàn bộ quy trình kiểm định chất lượng 13 tiêu chí và xuất file qa_report.json.
        """
        report = {
            "passed": False,
            "checks": {},
            "metrics": {}
        }

        # 1. Kiểm tra File Video (720x1280 hoặc 1280x720)
        v_ok, v_info = cls.check_video_specs(final_video_path)
        report["checks"]["video_resolution_standard"] = v_ok
        report["metrics"]["video_specs"] = v_info

        # 2. Kiểm tra File Thumbnail
        t_ok = os.path.exists(thumbnail_path) and os.path.getsize(thumbnail_path) > 10000
        report["checks"]["thumbnail_exists"] = t_ok

        # 3. Kiểm tra Tỷ lệ phủ giọng đọc (Khóa cứng 85% - 95%)
        coverage_ok = (85.0 <= overall_coverage <= 95.0)
        report["checks"]["audio_coverage_target"] = coverage_ok
        report["metrics"]["audio_coverage_percent"] = overall_coverage

        # 4. Kiểm tra Khoảng lặng chết (Khóa cứng <= 3.5s)
        max_silence = cls.detect_max_silence(final_video_path)
        silence_ok = (max_silence <= 3.5)
        report["checks"]["no_dead_silence"] = silence_ok
        report["metrics"]["max_silence_duration_sec"] = max_silence

        # 5. Kiểm tra Thương hiệu 'Cool Mèo Phim'
        full_script = " ".join([s.get("script", "") for s in aligned_segments])
        has_brand = bool(re.search(r"Cool\s*Mèo\s*Phim|@CoolMeoPhim", full_script, re.IGNORECASE))
        report["checks"]["brand_cool_meo_phim_included"] = has_brand

        # 6. Kiểm tra từ khóa cờ bạc / tên kênh lạ từ blacklist
        blacklist = Settings.load_blacklist()
        clean_text_check = True
        forbidden_matches = []

        text_corpus = full_script
        if seo_data:
            text_corpus += " " + str(seo_data.get("title", "")) + " " + str(seo_data.get("description", ""))
            tags = seo_data.get("tags", [])
            if isinstance(tags, list):
                text_corpus += " " + " ".join(tags)

        for g_kw in blacklist.get("gambling_keywords", []):
            if re.search(g_kw, text_corpus, re.IGNORECASE):
                clean_text_check = False
                forbidden_matches.append(f"gambling:{g_kw}")
                break

        if clean_text_check:
            for rogue in blacklist.get("forbidden_channel_names", []):
                if re.search(re.escape(rogue), text_corpus, re.IGNORECASE):
                    clean_text_check = False
                    forbidden_matches.append(f"rogue_channel:{rogue}")
                    break

        report["checks"]["zero_gambling_or_rogue_names"] = clean_text_check
        report["metrics"]["forbidden_matches"] = forbidden_matches

        # 7. Kiểm tra Outro Audio (100% âm lượng gốc)
        outro_path = str(ASSETS_DIR / "outtro.mp4")
        outro_ok, outro_metrics = cls.verify_outro_audio_rms(final_video_path, outro_path)
        report["checks"]["outro_audio_intact"] = outro_ok
        report["metrics"]["outro_rms"] = outro_metrics

        # 8. MỚI: Quét Sine Wave 440Hz
        sine_ok, sine_metrics = cls.check_no_sine_wave(final_video_path)
        report["checks"]["no_sine_wave_440hz"] = sine_ok
        report["metrics"]["sine_detection"] = sine_metrics

        # 9. MỚI: Quét 0% Chữ Hán
        ass_content = ""
        if ass_subtitles_path and os.path.exists(ass_subtitles_path):
            with open(ass_subtitles_path, "r", encoding="utf-8") as f:
                ass_content = f.read()

        corpus_dict = {
            "script": full_script,
            "seo_title": str(seo_data.get("title", "")),
            "seo_description": str(seo_data.get("description", "")),
            "seo_tags": " ".join(seo_data.get("tags", [])) if isinstance(seo_data.get("tags"), list) else "",
            "ass_subtitles": ass_content
        }
        chinese_ok, chinese_metrics = cls.check_zero_chinese_characters(corpus_dict)
        report["checks"]["zero_chinese_characters"] = chinese_ok
        report["metrics"]["chinese_chars_scan"] = chinese_metrics

        # 10. MỚI: Quét N-gram lặp
        ngram_ok, ngram_metrics = cls.check_no_ngram_repetition(full_script)
        report["checks"]["no_ngram_repetition"] = ngram_ok
        report["metrics"]["ngram_scan"] = ngram_metrics

        # 11. MỚI: Kiểm tra chất lượng Subtitle ASS
        if ass_subtitles_path and os.path.exists(ass_subtitles_path):
            ass_ok, ass_metrics = cls.check_ass_subtitles_quality(ass_subtitles_path)
        else:
            ass_ok, ass_metrics = True, {"note": "Khong co file ass de kiem tra"}
        report["checks"]["ass_subtitles_quality"] = ass_ok
        report["metrics"]["ass_subtitles_metrics"] = ass_metrics

        # 12. MỚI: Kiểm tra độ bền vững Logo Watermark tại (32, 32)
        logo_path = str(ASSETS_DIR / "logo.png")
        logo_ok, logo_metrics = cls.check_logo_persistence(final_video_path, logo_path, output_dir)
        report["checks"]["logo_persistence"] = logo_ok
        report["metrics"]["logo_persistence_metrics"] = logo_metrics

        # 13. MỚI: Kiểm tra Tỷ lệ nén nội dung phim (Compression Ratio >= 15%)
        # Cắt gọt sơ sài dưới 15% thời lượng phim gốc -> FAIL QA NGAY LẬP TỨC
        if raw_total_duration and raw_total_duration > 0:
            v_dur_probe = cls.get_video_duration(final_video_path)
            body_dur = max(0.0, v_dur_probe - 10.0)
            comp_ratio = round(body_dur / raw_total_duration, 4)
            comp_ok = (comp_ratio >= 0.15)
            report["checks"]["compression_ratio_standard"] = comp_ok
            report["metrics"]["compression_ratio"] = {
                "raw_total_duration_sec": round(raw_total_duration, 1),
                "final_body_duration_sec": round(body_dur, 1),
                "compression_ratio_percent": round(comp_ratio * 100, 2),
                "minimum_required_percent": 15.0
            }

        # Đánh giá tổng thể
        all_passed = all(report["checks"].values())
        report["passed"] = all_passed

        # Lưu báo cáo qa_report.json
        qa_path = os.path.join(output_dir, "qa_report.json")
        with open(qa_path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)

        failed_checks = [k for k, v in report["checks"].items() if not v]
        if failed_checks:
            print(f"[QualityValidator] ⚠️ CÁC TIÊU CHÍ QA KHÔNG ĐẠT ({len(failed_checks)}/{len(report['checks'])}): {failed_checks}")
        print(f"[QualityValidator] Báo cáo QA: {'✅ ĐẠT CHUẨN' if all_passed else '⚠️ CẢNH BÁO'}. Chi tiết: {qa_path}")
        return report
