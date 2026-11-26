import os
import sys
import shutil
import tempfile
import subprocess
from pathlib import Path
# Ensure root dir in path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import Settings, ASSETS_DIR, TMP_DIR, STATE_DIR, OUTPUT_DIR
from src.intelligence.story_bible import StoryBible
from src.video.voice_driven_editor import (
    CompressionPlanner,
    VoiceDrivenEditor,
    VERTICAL_PROFILE,
    HORIZONTAL_PROFILE,
    OrientationProfile
)
from src.video.series_stitcher import SeriesStitcher
from src.delivery.cloud_storage import CloudStorageManager

def create_synthetic_video(output_path: str, width: int = 720, height: int = 1280, duration: int = 10):
    """Tạo video tổng hợp lavfi nhanh cho test."""
    vf = f"testsrc=duration={duration}:size={width}x{height}:rate=30,format=yuv420p"
    af = f"sine=frequency=440:duration={duration}"
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
        raise RuntimeError(f"Không thể tạo synthetic video: {output_path}")

def test_story_bible():
    test_id = "test_story_bible_series"
    series_title = "Đế Vương Trọng Sinh"

    bible = StoryBible.load_or_create(test_id, series_title)
    assert bible.series_id == test_id
    assert bible.series_title == series_title

    # 1. Khóa cứng nhân vật
    bible.lock_character("Gu Yizhou", "Cố Dịch Chu", "Tổng tài Cố Thị", ["Gu Yizhou", "Cố thiếu", "A Chu"])
    bible.lock_character("Shen Yao", "Thẩm Dao", "Nữ chính", ["Shen Yao", "Dao Dao"])

    # 2. Reload từ đĩa kiểm tra tính toàn vẹn
    reloaded = StoryBible.load_or_create(test_id)
    assert "Cố Dịch Chu" in reloaded.character_glossary
    assert "Thẩm Dao" in reloaded.character_glossary

    # 3. Chuẩn hóa tên trong kịch bản
    raw_text = "Hôm đó Gu Yizhou đến tìm Shen Yao nhưng Cố thiếu không gặp được Dao Dao."
    normalized = reloaded.apply_glossary_to_text(raw_text)
    assert "Gu Yizhou" not in normalized
    assert "Shen Yao" not in normalized
    assert "Cố Dịch Chu" in normalized
    assert "Thẩm Dao" in normalized

    # 4. Rolling summary
    reloaded.update_rolling_summary("Cố Dịch Chu ký hợp đồng hôn nhân với Thẩm Dao.", 1)
    reloaded.update_rolling_summary("Thẩm Dao phát hiện thân thế thực sự của mình.", 2)
    assert len(reloaded.episode_history) == 2
    assert "Tập 1" in reloaded.rolling_summary
    assert "Tập 2" in reloaded.rolling_summary

    # 5. Format prompt context
    ctx = reloaded.format_prompt_context()
    assert "Cố Dịch Chu" in ctx
    assert "Thẩm Dao" in ctx
    assert "ROLLING SUMMARY" in ctx

    # Dọn dẹp
    shutil.rmtree(STATE_DIR / "active_series" / test_id, ignore_errors=True)

def test_compression_planner():
    # Đoản kịch dọc (gốc 2.5h = 9000s) -> nén còn 40%-50% (45-90p = 2700s - 5400s)
    raw_short = 9000.0
    plan_short = CompressionPlanner.calculate_plan(raw_short, "short_drama")
    assert 0.40 <= plan_short["target_ratio"] <= 0.50
    assert 2700.0 <= plan_short["target_duration_sec"] <= 5400.0
    assert plan_short["num_parts"] == 1

    # Phim bộ dài (gốc 20h = 72000s) -> nén còn 15%-25% (2-3.5h/part, tự chia Part nếu > 3.5h)
    raw_long = 72000.0
    plan_long = CompressionPlanner.calculate_plan(raw_long, "long_series")
    assert 0.15 <= plan_long["target_ratio"] <= 0.25
    assert plan_long["num_parts"] >= 2
    assert plan_long["part_duration_sec"] <= CompressionPlanner.MAX_PART_DURATION_SEC

    # Lọc thời lượng chết
    raw_ranges = [
        {"start": 10.0, "end": 20.0},
        {"start": 40.0, "end": 100.0},
        {"start": 550.0, "end": 590.0}
    ]
    filtered = CompressionPlanner.filter_dead_time(raw_ranges, episode_duration=600.0, intro_cut_sec=30.0, outro_cut_sec=40.0)
    # Đoạn đầu < 30s bị loại hoặc co lại, đoạn cuối > 560s bị co lại
    assert len(filtered) >= 1
    assert all(r["start"] >= 30.0 for r in filtered)
    assert all(r["end"] <= 560.0 for r in filtered)

def test_visual_slice_algorithm():
    # Audio dài 14.5 giây
    t_audio = 14.5
    source_ranges = [{"start": 10.0, "end": 60.0}]
    cuts = VoiceDrivenEditor.slice_visual_to_audio(t_audio, source_ranges)

    # 1. Tổng thời lượng các lát cắt phải khớp đúng T_audio
    total_dur = round(sum(c["duration"] for c in cuts), 2)
    assert abs(total_dur - t_audio) <= 0.05, f"Tổng thời lượng lát cắt {total_dur} != {t_audio}"

    # 2. Mỗi lát cắt nằm trong khoảng 2.0s - 2.8s (tối đa 3.0s), có xen kẽ zoom và tốc độ
    for c in cuts:
        assert c["duration"] <= 3.0, f"Lát cắt quá dài: {c['duration']}s"
        assert c["duration"] >= 1.9, f"Lát cắt quá ngắn: {c['duration']}s"
        assert "speed" in c and 1.15 <= c["speed"] <= 1.25, f"Tốc độ lát cắt không hợp lệ: {c.get('speed')}"
        assert "zoom" in c and c["zoom"] in (1.08, 1.18), f"Zoom lát cắt không hợp lệ: {c.get('zoom')}"

    # 3. Non-contiguous Jump-Cut: Lát cắt kế tiếp trong cùng dải gốc phải nhảy cóc >= 4.0s
    for i in range(len(cuts) - 1):
        c_cur = cuts[i]
        c_next = cuts[i + 1]
        jump = c_next["start"] - c_cur["end"]
        assert jump >= 4.0, f"Khoảng nhảy cóc giữa lát {i} và {i+1} quá ngắn: {jump}s (< 4.0s)"

def test_dual_orientation_probe(tmp_path):
    # Test dò video dọc
    v_vert = str(tmp_path / "vert.mp4")
    create_synthetic_video(v_vert, width=720, height=1280, duration=3)
    info_v = VoiceDrivenEditor.probe_video(v_vert)
    assert info_v["is_vertical"] is True
    assert info_v["profile"].name == "vertical_720x1280"

    # Test dò video ngang
    v_horiz = str(tmp_path / "horiz.mp4")
    create_synthetic_video(v_horiz, width=1280, height=720, duration=3)
    info_h = VoiceDrivenEditor.probe_video(v_horiz)
    assert info_h["is_vertical"] is False
    assert info_h["profile"].name == "horizontal_1280x720"

def test_ass_subtitles_for_profiles(tmp_path):
    sub_data = [
        {"start": 0.0, "end": 4.5, "text": "Anh chàng lập tức sững người khi nhận ra bí mật."},
        {"start": 4.5, "end": 9.0, "text": "Đối phương không hề nao núng trước lời đe dọa."}
    ]

    ass_v = str(tmp_path / "sub_v.ass")
    VoiceDrivenEditor.generate_profile_subtitles_ass(sub_data, ass_v, VERTICAL_PROFILE)
    assert os.path.exists(ass_v)
    with open(ass_v, "r", encoding="utf-8") as f:
        content_v = f.read()
    assert r"\pos(360,864)" in content_v
    assert "PlayResX: 720" in content_v
    assert "PlayResY: 1280" in content_v

    ass_h = str(tmp_path / "sub_h.ass")
    VoiceDrivenEditor.generate_profile_subtitles_ass(sub_data, ass_h, HORIZONTAL_PROFILE)
    assert os.path.exists(ass_h)
    with open(ass_h, "r", encoding="utf-8") as f:
        content_h = f.read()
    assert HORIZONTAL_PROFILE.sub_pos in content_h
    assert "PlayResX: 1280" in content_h
    assert "PlayResY: 720" in content_h

def test_series_stitcher_and_checkpoint(tmp_path):
    test_id = "test_series_batch"
    title = "Tuyệt Đỉnh Chiến Thần"
    episodes = [{"index": i} for i in range(1, 10)]

    # 1. Khởi tạo progress
    p = SeriesStitcher.init_progress(test_id, title, episodes, batch_size=4)
    assert len(p["batches"]) == 3  # 9 eps chia 4 = [4, 4, 1] -> 3 batches
    assert p["batches"][0]["episode_indices"] == [1, 2, 3, 4]
    assert p["batches"][1]["episode_indices"] == [5, 6, 7, 8]
    assert p["batches"][2]["episode_indices"] == [9]

    # 2. Giả lập batch render callback
    batch_dir = tmp_path / "batches"
    batch_dir.mkdir(parents=True, exist_ok=True)

    def dummy_render(batch_idx, ep_list, out_file):
        create_synthetic_video(out_file, width=720, height=1280, duration=3)
        return True

    ok = SeriesStitcher.render_series_batches(
        series_id=test_id,
        title=title,
        episodes=episodes,
        batch_render_fn=dummy_render,
        batch_output_dir=batch_dir,
        batch_size=4
    )
    assert ok is True

    # 3. Kiểm tra checkpoint lưu thành công
    p_after = SeriesStitcher.load_progress(test_id)
    assert p_after["completed_batch_count"] == 3
    assert p_after["is_all_rendered"] is True

    # 4. Kiểm tra resume / checkpoint hit
    call_count = {"count": 0}
    def dummy_render_count(batch_idx, ep_list, out_file):
        call_count["count"] += 1
        return True

    # Chạy lại -> Tất cả batch đều hit checkpoint và không render lại
    ok2 = SeriesStitcher.render_series_batches(
        series_id=test_id,
        title=title,
        episodes=episodes,
        batch_render_fn=dummy_render_count,
        batch_output_dir=batch_dir,
        batch_size=4
    )
    assert ok2 is True
    assert call_count["count"] == 0, "Lẽ ra phải skip toàn bộ batch đã render!"

    # 5. Ghép nối `-c copy`
    final_dir = tmp_path / "final_series"
    final_files = SeriesStitcher.stitch_series_copy(test_id, final_dir)
    assert len(final_files) == 1
    assert os.path.exists(final_files[0])
    assert os.path.getsize(final_files[0]) > 10000

    # Dọn dẹp
    shutil.rmtree(STATE_DIR / "active_series" / test_id, ignore_errors=True)

def test_cloud_storage_manager(tmp_path):
    # Test file nhỏ (< 2GB)
    small_file = tmp_path / "video_small.mp4"
    small_file.write_bytes(b"small_video_content" * 100)

    res_small = CloudStorageManager.handle_video_delivery(str(small_file), drama_slug="test_slug")
    assert res_small["is_cloud"] is False
    assert res_small["status"] == "standard_local"
    assert os.path.exists(res_small["output_path"])

    # Test file lớn (> 2GB threshold) với rclone stub & fallback an toàn
    large_file = tmp_path / "video_large.mp4"
    large_file.write_bytes(b"large_video_content" * 100)

    # Giả lập threshold nhỏ để trigger logic > 2GB
    original_threshold = CloudStorageManager.SIZE_THRESHOLD_BYTES
    try:
        CloudStorageManager.SIZE_THRESHOLD_BYTES = 50
        res_large = CloudStorageManager.handle_video_delivery(str(large_file), drama_slug="test_slug")
        if res_large["is_cloud"]:
            assert res_large["status"] == "cloud_uploaded"
            assert "gdrive:" in res_large["remote_path"]
        else:
            assert res_large["status"] == "fallback_local"
            assert os.path.exists(res_large["output_path"])
            assert "Fallback" in res_large["note"]
    finally:
        CloudStorageManager.SIZE_THRESHOLD_BYTES = original_threshold
        shutil.rmtree(OUTPUT_DIR / "test_slug", ignore_errors=True)

def test_copyright_shield_filter_and_inset():
    from src.video.copyright_shield import CopyrightShield
    # 1. Kiểm tra cut filter với speed và zoom
    cut_f = CopyrightShield.get_cut_filter(720, 1280, frosted_glass_y=1100, frosted_glass_h=96, frosted_glass_w=720, zoom_ratio=1.18, speed_factor=1.22)
    assert "hflip" in cut_f
    assert "setpts=PTS/1.22" in cut_f
    assert "scale=850:1510" in cut_f or "scale=850:1510" in cut_f.replace(" ", "")

    # 2. Kiểm tra body filter với khung lồng 80% (576x1024 trên 720x1280) và viền kép tương phản cao
    body_f = CopyrightShield.get_body_render_filter(720, 1280, safe_ass_path="sub.ass", safe_fonts_dir="fonts", inset_ratio=0.80)
    assert "scale=576:1024" in body_f
    assert "overlay=72:128" in body_f
    assert "color=black@0.70:t=4" in body_f
    assert "color=white@0.45:t=2" in body_f
    assert "boxblur=32:4" in body_f

if __name__ == "__main__":
    print("=" * 65)
    print("🚀 BẮT ĐẦU CHẠY TOÀN BỘ UNIT TESTS (HẠNG MỤC 3 & 4)")
    print("=" * 65)
    print("[1/7] Kiểm thử StoryBible...")
    test_story_bible()
    print("[2/7] Kiểm thử CompressionPlanner...")
    test_compression_planner()
    print("[3/7] Kiểm thử Visual Slice Algorithm...")
    test_visual_slice_algorithm()
    print("[4/7] Kiểm thử CopyrightShield Filters & Inset...")
    test_copyright_shield_filter_and_inset()
    with tempfile.TemporaryDirectory() as td:
        tmp_p = Path(td)
        print("[5/7] Kiểm thử Dual Orientation Probe & ASS...")
        test_dual_orientation_probe(tmp_p)
        test_ass_subtitles_for_profiles(tmp_p)
        print("[6/7] Kiểm thử SeriesStitcher & Checkpoint...")
        test_series_stitcher_and_checkpoint(tmp_p)
        print("[7/7] Kiểm thử CloudStorageManager...")
        test_cloud_storage_manager(tmp_p)
    print("=" * 65)
    print("✅ TẤT CẢ UNIT TESTS HẠNG MỤC 3 & 4 ĐÃ VƯỢT QUA 100%!")
    print("=" * 65)
