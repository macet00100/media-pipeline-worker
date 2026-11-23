import os
import subprocess
from pathlib import Path
from typing import List, Dict, Any, Tuple
from config.settings import Settings
from src.audio.tts_engine import TTSEngine
from src.intelligence.review_writer import ReviewWriter

class SyncAligner:
    """
    Thuật toán đóng vòng lặp kiểm soát độ phủ âm thanh 85% - 95%
    Xóa bỏ khoảng lặng chết và đồng bộ thời lượng phân đoạn audio-video.
    """

    @classmethod
    def apply_atempo(cls, input_audio: str, output_audio: str, speed_factor: float) -> bool:
        """Tăng tốc giọng đọc tự nhiên bằng atempo (1.03x - 1.15x) không làm méo tiếng."""
        # Giới hạn an toàn của atempo
        speed = max(1.0, min(1.15, speed_factor))
        cmd = [
            "ffmpeg", "-y",
            "-i", input_audio,
            "-filter:a", f"atempo={speed:.4f}",
            output_audio
        ]
        res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return res.returncode == 0 and os.path.exists(output_audio)

    @classmethod
    def align_and_produce_voice_track(
        cls,
        segments: List[Dict[str, Any]],
        tmp_dir: str
    ) -> Tuple[str, List[Dict[str, Any]], float]:
        """
        Quy trình khép kín:
        1. Đo và kiểm tra coverage = tts_dur / seg_dur.
        2. Tự điều chỉnh kịch bản (mở rộng hoặc rút gọn) nếu ngoài khoảng 85% - 95%.
        3. Ghép thành một dải âm thanh giọng đọc hoàn chỉnh đồng bộ theo đúng timeline.
        """
        print("[SyncAligner] Bắt đầu đồng bộ và khử khoảng lặng chết...")
        aligned_segments = []
        audio_parts = []
        total_movie_duration = segments[-1]["end_sec"] if segments else 0.0

        for s in segments:
            idx = s["index"]
            seg_dur = s["duration"]
            seg_audio = os.path.join(tmp_dir, f"voice_seg_{idx}.mp3")
            script_text = s["script"]

            # Vòng lặp điều chỉnh độ phủ
            max_rounds = 3
            current_round = 0
            tts_dur, sent_timings = TTSEngine.synthesize_with_timestamps(script_text, seg_audio)

            while current_round < max_rounds:
                coverage = tts_dur / seg_dur if seg_dur > 0 else 1.0
                print(f"[SyncAligner] Đoạn {idx} [Lần {current_round + 1}]: coverage = {coverage * 100:.1f}% (TTS: {tts_dur:.1f}s / Video: {seg_dur:.1f}s)")

                if coverage < Settings.TARGET_AUDIO_COVERAGE_MIN:
                    # Bị hụt lời -> Mở rộng kịch bản
                    missing_sec = (seg_dur * 0.90) - tts_dur
                    print(f"[SyncAligner] Hụt {missing_sec:.1f}s -> Bổ sung mô tả chi tiết hành động...")
                    script_text = ReviewWriter.expand_segment_script(s, script_text, missing_sec)
                    tts_dur, sent_timings = TTSEngine.synthesize_with_timestamps(script_text, seg_audio)
                    current_round += 1

                elif coverage > 0.96:
                    if coverage <= 1.15:
                        # Hơi dài hơn một chút -> Dùng atempo nhẹ
                        tempo_audio = os.path.join(tmp_dir, f"voice_tempo_{idx}.mp3")
                        tempo_factor = coverage / 0.92
                        if cls.apply_atempo(seg_audio, tempo_audio, tempo_factor):
                            os.replace(tempo_audio, seg_audio)
                            new_dur = TTSEngine.get_audio_duration(seg_audio)
                            ratio = new_dur / tts_dur if tts_dur > 0 else 1.0
                            tts_dur = new_dur
                            # Scale sentence timings theo atempo để giữ mốc thời gian đồng bộ
                            scaled_timings = []
                            for st in sent_timings:
                                scaled_timings.append({
                                    **st,
                                    "start_sec": round(st["start_sec"] * ratio, 3),
                                    "end_sec": round(st["end_sec"] * ratio, 3),
                                    "duration": round(st["duration"] * ratio, 3)
                                })
                            sent_timings = scaled_timings
                        break
                    else:
                        # Quá dài (> 115%) -> Gọi Gemini rút gọn từ thừa
                        excess_sec = tts_dur - seg_dur
                        print(f"[SyncAligner] Tràn {excess_sec:.1f}s -> Rút gọn từ thừa...")
                        script_text = ReviewWriter.shorten_segment_script(s, script_text, excess_sec)
                        tts_dur, sent_timings = TTSEngine.synthesize_with_timestamps(script_text, seg_audio)
                        current_round += 1
                else:
                    # Đạt chuẩn vàng 85% - 95%
                    break

            # Tạo khoảng đệm (pad silence) cho khớp mốc thời gian chuyển cảnh tự nhiên
            padded_audio = os.path.join(tmp_dir, f"voice_padded_{idx}.wav")
            pad_dur = max(0.0, seg_dur - tts_dur)

            # FFmpeg apad filter để khớp đúng độ dài phân đoạn
            cmd = [
                "ffmpeg", "-y",
                "-i", seg_audio,
                "-af", f"apad=whole_dur={seg_dur:.3f}",
                padded_audio
            ]
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            aligned_segments.append({
                **s,
                "script": script_text,
                "actual_tts_duration": tts_dur,
                "coverage_percent": round((tts_dur / seg_dur) * 100, 1) if seg_dur > 0 else 100.0,
                "audio_file": padded_audio,
                "sentence_timings": sent_timings
            })
            audio_parts.append(padded_audio)

        # Ghép nối toàn bộ track giọng đọc thành 1 file hoàn chỉnh
        full_voice_path = os.path.join(tmp_dir, "full_voice_track.wav")
        concat_txt = os.path.join(tmp_dir, "voice_concat.txt")

        with open(concat_txt, "w", encoding="utf-8") as f:
            for ap in audio_parts:
                f.write(f"file '{ap.replace(os.sep, '/')}'\n")

        cmd_concat = [
            "ffmpeg", "-y", "-f", "concat", "-safe", "0",
            "-i", concat_txt,
            "-c:a", "pcm_s16le",
            full_voice_path
        ]
        subprocess.run(cmd_concat, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        total_voice_duration = TTSEngine.get_audio_duration(full_voice_path)
        overall_coverage = (sum(s["actual_tts_duration"] for s in aligned_segments) / total_movie_duration * 100) if total_movie_duration > 0 else 0.0

        print(f"[SyncAligner] Hoàn tất dải giọng đọc: {total_voice_duration:.1f}s. Tổng độ phủ: {overall_coverage:.1f}%")
        return full_voice_path, aligned_segments, overall_coverage
