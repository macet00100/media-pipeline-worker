import os
import sys
import shutil
import subprocess
from pathlib import Path

# Đảm bảo đường dẫn import hoạt động đúng
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import Settings, ASSETS_DIR, TMP_DIR
from src.video.subtitle_builder import SubtitleBuilder
from src.video.renderer import VideoRenderer
from src.video.thumbnail_maker import ThumbnailMaker
from src.qa.validator import QualityValidator
from src.audio.tts_engine import TTSEngine

def create_synthetic_mock_video(output_path: str, duration_sec: int = 15):
    """
    Tạo một video mẫu dọc 720x1280 30fps có giả lập dải phụ đề tiếng Trung tại y=816..912
    và dải âm thanh mẫu để kiểm thử trọn vẹn chuỗi filtergraph FFmpeg.
    """
    print(f"[TEST] Đang tạo video mẫu dọc 720x1280 ({duration_sec}s)...")
    # Vẽ nền động + dải chữ trắng mô phỏng phụ đề tiếng Trung tại y=850
    vf = (
        f"testsrc=duration={duration_sec}:size=720x1280:rate=30,"
        f"drawbox=x=120:y=840:w=480:h=45:color=white@0.9:t=fill,"
        f"drawtext=text='模拟中文字幕测试':fontsize=32:fontcolor=black:x=(w-tw)/2:y=846,"
        f"format=yuv420p"
    )
    af = f"sine=frequency=440:duration={duration_sec}"

    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", vf,
        "-f", "lavfi", "-i", af,
        "-c:v", "libx264", "-preset", "ultrafast",
        "-c:a", "aac", "-b:a", "128k",
        output_path
    ]
    res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if res.returncode != 0 or not os.path.exists(output_path):
        raise RuntimeError("Không thể tạo video mẫu bằng FFmpeg lavfi.")
    print(f"[TEST] Tạo video mẫu thành công: {output_path}")

def test_full_pipeline_mock():
    print("=" * 65)
    print("🧪 BẮT ĐẦU KIỂM THỬ DRY-RUN TOÀN DIỆN (END-TO-END MOCK TEST)")
    print("=" * 65)

    test_dir = TMP_DIR / "dry_run_test"
    shutil.rmtree(test_dir, ignore_errors=True)
    test_dir.mkdir(parents=True, exist_ok=True)

    try:
        # 1. Tạo video mẫu 8s khớp định mức độ phủ
        mock_video = str(test_dir / "mock_video.mp4")
        create_synthetic_mock_video(mock_video, duration_sec=8)

        # 2. Tạo giọng đọc mẫu bằng TTSEngine hoặc sine wave nếu offline
        mock_voice = str(test_dir / "mock_voice.mp3")
        print("[TEST] Tạo track thuyết minh mẫu (TTS)...")
        sample_text = (
            "Chào mừng các bạn đến với kênh Cool Mèo Phim! "
            "Anh chàng lập tức sững người khi nhận ra thân phận thực sự của mình."
        )
        tts_dur, sent_timings = TTSEngine.synthesize_with_timestamps(sample_text, mock_voice)
        if tts_dur <= 0 or not os.path.exists(mock_voice):
            # Fallback tạo audio sine wave nếu không có mạng
            subprocess.run([
                "ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=600:duration=7.2",
                "-c:a", "libmp3lame", mock_voice
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            tts_dur = 7.2
            sent_timings = TTSEngine.get_last_sentence_timings()

        # 3. Tạo phụ đề ASS tiếng Việt
        mock_ass = str(test_dir / "mock_sub.ass")
        print("[TEST] Tạo phụ đề ASS căn giữa dải kính mờ \\pos(360,864)...")
        segments = [{
            "index": 1,
            "start_sec": 0.0,
            "end_sec": 8.0,
            "duration": 8.0,
            "actual_tts_duration": tts_dur,
            "script": sample_text,
            "sentence_timings": sent_timings
        }]
        SubtitleBuilder.generate_ass_subtitles(segments, mock_ass)
        assert os.path.exists(mock_ass), "File phụ đề ASS không được tạo!"

        # 4. Render thân phim kết hợp toàn bộ filtergraph
        print("[TEST] Kiểm thử FFmpeg Filtergraph (Scale, Frosted Glass, Logo, Sub, Audio Mix)...")
        mock_body = str(test_dir / "mock_body.mp4")
        render_ok = VideoRenderer.render_movie_body(
            mock_video, mock_voice, mock_ass, mock_body, str(test_dir)
        )
        assert render_ok and os.path.exists(mock_body), "Lỗi FFmpeg render thân phim!"

        # 5. Ghép Outro 100% âm lượng gốc
        print("[TEST] Kiểm thử ghép nối Outro...")
        final_video = str(test_dir / "final_mock_review.mp4")
        outro_file = str(ASSETS_DIR / "outtro.mp4")
        concat_ok = VideoRenderer.concatenate_outro(mock_body, outro_file, final_video, str(test_dir))
        assert concat_ok and os.path.exists(final_video), "Lỗi ghép nối Outro!"

        # 6. Kiểm thử thiết kế Thumbnail (dọc + ngang)
        print("[TEST] Kiểm thử xuất Thumbnail hoàn chỉnh (dọc + ngang)...")
        thumb_v = str(test_dir / "thumbnail.jpg")
        thumb_h = str(test_dir / "thumbnail_horizontal.jpg")
        ThumbnailMaker.generate_thumbnails(mock_video, "SỰ THẬT BẤT NGỜ", thumb_v, thumb_h, str(test_dir))
        assert os.path.exists(thumb_v), "Thiếu thumbnail dọc!"
        assert os.path.exists(thumb_h), "Thiếu thumbnail ngang!"

        # 7. Kiểm định QA Gate
        print("[TEST] Kiểm định chất lượng qua Cổng QA Gate...")
        seo_sample = {"title": "Review Phim Thử Nghiệm", "tags": ["CoolMeoPhim"]}
        overall_cov = round((tts_dur / 8.0) * 100, 1)
        qa_rep = QualityValidator.validate_review_package(
            final_video, thumb_v, seo_sample, segments, overall_coverage=overall_cov, output_dir=str(test_dir)
        )
        assert os.path.exists(test_dir / "qa_report.json"), "Thiếu qa_report.json!"
        assert qa_rep["passed"], f"QA Gate không đạt chuẩn: {qa_rep['checks']}"

        print("=" * 65)
        print("🎉 TẤT CẢ CÁC BƯỚC KIỂM THỬ (MOCK TEST) ĐỀU THÀNH CÔNG 100%!")
        print("   - Lệnh FFmpeg: Cú pháp chính xác tuyệt đối, không có bất kỳ lỗi nào.")
        print(f"   - File video thành phẩm: {final_video}")
        print("=" * 65)
        return True

    finally:
        # Dọn dẹp
        shutil.rmtree(test_dir, ignore_errors=True)

if __name__ == "__main__":
    test_full_pipeline_mock()
