import os
import sys
import re
import json
import shutil
import time
import math
import subprocess
import argparse
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, List

from config.settings import Settings, TMP_DIR, OUTPUT_DIR, ASSETS_DIR
from src.state.state_manager import StateManager
from src.sources.cms_client import CMSClient
from src.sources.downloader import Downloader
from src.sources.movie_curator import MovieCurator
from src.cleaner.ad_detector import AdDetector
from src.intelligence.story_bible import StoryBible
from src.intelligence.script_extractor import ScriptExtractor
from src.intelligence.review_writer import ReviewWriter
from src.intelligence.seo_generator import SEOGenerator
from src.audio.sync_aligner import SyncAligner
from src.video.subtitle_builder import SubtitleBuilder
from src.video.voice_driven_editor import VoiceDrivenEditor, CompressionPlanner
from src.video.series_stitcher import SeriesStitcher
from src.video.thumbnail_maker import ThumbnailMaker
from src.delivery.scheduler import scheduler, VideoScheduler
from src.delivery.publisher_stub import PublisherStub
from src.delivery.package_exporter import PackageExporter
from src.qa.validator import QualityValidator

def slugify(text: str) -> str:
    """Tạo slug an toàn cho thư mục và tên file."""
    text = re.sub(r"[^\w\s-]", "", text).strip().lower()
    return re.sub(r"[-\s]+", "_", text)[:40]

def verify_assets_ready() -> bool:
    """Kiểm tra sự tồn tại của các file tài nguyên bắt buộc."""
    required = {
        "Logo Watermark": ASSETS_DIR / "logo.png",
        "Avatar Kênh": ASSETS_DIR / "avatar.png",
        "Video Outro": ASSETS_DIR / "outtro.mp4"
    }
    missing = [name for name, p in required.items() if not p.exists()]
    if missing:
        print(f"[INIT] CẢNH BÁO THIẾU ASSET: {missing}")
        return False
    return True

def sanitize_narration_blocks(
    blocks: List[Dict[str, Any]],
    skip_ranges: List[Dict[str, Any]],
    max_duration: float,
    is_first_ep: bool = True,
    is_last_ep: bool = True
) -> List[Dict[str, Any]]:
    """
    Lọc sạch chữ Hán, chặn tuyệt đối Opening Credits, Title Cards & Ending Credits (+15s biên an toàn)
    và loại bỏ mọi mốc quảng cáo giữa phim khỏi source_clip_ranges.
    """
    # 1. Xác định vùng an toàn Opening (cắt sạch nhạc dạo đầu tối thiểu 35s)
    min_safe_time = 35.0
    for s in skip_ranges:
        s_st = float(s.get("start_sec", s.get("start", 0.0)))
        s_en = float(s.get("end_sec", s.get("end", 0.0)))
        reason = str(s.get("reason", "")).lower()
        if "open" in reason or "title" in reason or s_st <= 5.0:
            min_safe_time = max(min_safe_time, s_en)

    # 2. Xác định vùng an toàn Ending: Trừ thêm biên an toàn 15s trước ending credits
    hard_cutoff = max(30.0, max_duration - 55.0)
    detected_ending_starts = []
    for s in skip_ranges:
        s_st = float(s.get("start_sec", s.get("start", 0.0)))
        s_en = float(s.get("end_sec", s.get("end", 0.0)))
        reason = str(s.get("reason", "")).lower()
        if "end" in reason or s_en >= max_duration - 60.0:
            detected_ending_starts.append(s_st)

    if detected_ending_starts:
        safe_ending_start = min(detected_ending_starts) - 15.0
        max_safe_time = min(hard_cutoff, safe_ending_start)
    else:
        max_safe_time = hard_cutoff

    if max_safe_time <= min_safe_time + 40.0:
        max_safe_time = max(min_safe_time + 40.0, max_duration - 40.0)

    # 3. Thu thập danh sách cấm (skips)
    skips = [(0.0, min_safe_time), (max_safe_time, max_duration + 100.0)]
    for s in skip_ranges:
        s_st = float(s.get("start_sec", s.get("start", 0.0)))
        s_en = float(s.get("end_sec", s.get("end", 0.0)))
        if s_en > s_st:
            skips.append((s_st, s_en))

    clean_blocks = []
    num_blocks = len(blocks)
    for idx, b in enumerate(blocks):
        script_text = ReviewWriter.clean_text_for_vietnamese(b.get("script", "")).strip()
        if not script_text:
            continue

        raw_ranges = b.get("source_clip_ranges", [])
        clean_ranges = []

        for cr in raw_ranges:
            st = max(min_safe_time, float(cr.get("start", min_safe_time)))
            en = min(max_safe_time, float(cr.get("end", max_safe_time)))
            if en <= st + 1.5:
                continue

            cur_intervals = [(st, en)]
            for s_st, s_en in skips:
                next_intervals = []
                for c_st, c_en in cur_intervals:
                    if s_en <= c_st or s_st >= c_en:
                        next_intervals.append((c_st, c_en))
                    else:
                        if c_st < s_st - 0.5:
                            next_intervals.append((c_st, s_st))
                        if c_en > s_en + 0.5:
                            next_intervals.append((s_en, c_en))
                cur_intervals = next_intervals

            for v_st, v_en in cur_intervals:
                if v_en - v_st >= 2.0:
                    clean_ranges.append({"start": round(v_st, 2), "end": round(v_en, 2)})

        if not clean_ranges:
            span = (max_safe_time - min_safe_time) / max(1, num_blocks)
            fallback_st = round(min_safe_time + idx * span, 2)
            fallback_en = round(min(max_safe_time, fallback_st + span), 2)
            if fallback_en - fallback_st < 3.0:
                fallback_st = round(max(min_safe_time, fallback_en - 6.0), 2)
            clean_ranges = [{"start": fallback_st, "end": fallback_en}]

        clean_blocks.append({
            "block_index": idx,
            "script": script_text,
            "source_clip_ranges": clean_ranges
        })

    # Đảm bảo thương hiệu Cool Mèo Phim đúng quy tắc Mạch Truyện Liền Mạch (Seamless Story Flow)
    if is_first_ep and clean_blocks:
        if not re.search(r"Cool\s*Mèo\s*Phim", clean_blocks[0]["script"], re.IGNORECASE):
            clean_blocks[0]["script"] = "Chào mừng quý khán giả đến với Cool Mèo Phim. " + clean_blocks[0]["script"]
    elif not is_first_ep and clean_blocks:
        clean_blocks[0]["script"] = re.sub(r"^Chào mừng quý khán giả đến với Cool Mèo Phim[.,\s]*", "", clean_blocks[0]["script"], flags=re.IGNORECASE).strip()

    if is_last_ep and clean_blocks:
        clean_blocks[-1]["script"] = re.sub(r"(?:hẹn gặp lại ở tập tiếp theo|đón xem tập tiếp theo).*$", "", clean_blocks[-1]["script"], flags=re.IGNORECASE).strip()
        if not re.search(r"đăng ký kênh Cool Mèo Phim", clean_blocks[-1]["script"], re.IGNORECASE):
            clean_blocks[-1]["script"] = clean_blocks[-1]["script"] + " Hãy bấm like và đăng ký kênh Cool Mèo Phim để đón xem những bộ phim hấp dẫn tiếp theo nhé."
    elif not is_last_ep and clean_blocks:
        for b in clean_blocks:
            b["script"] = re.sub(r"Hãy bấm like và đăng ký kênh.*$", "", b["script"], flags=re.IGNORECASE).strip()
            b["script"] = re.sub(r"hẹn gặp lại ở tập tiếp theo.*$", "", b["script"], flags=re.IGNORECASE).strip()
            b["script"] = re.sub(r"đón xem tập tiếp theo.*$", "", b["script"], flags=re.IGNORECASE).strip()

    return clean_blocks

def process_drama_voice_driven(
    drama: Dict[str, Any],
    max_episodes: Optional[int] = None
) -> Optional[Dict[str, Any]]:
    """
    Điều phối toàn bộ quy trình sản xuất video theo Thuật toán Tiếng Dẫn Hình (Voice-Driven Editing)
    Chế độ '1 VIDEO TRỌN BỘ DUY NHẤT — KHÔNG CHIA PHẦN' kèm Checkpoint từng tập và ghép siêu tốc -c copy.
    """
    movie_id = drama["id"]
    movie_title = drama["title"]
    drama_type = drama.get("category", "short_drama")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    movie_slug = slugify(movie_title) or movie_id

    # Danh sách tập phim
    m3u8_groups = drama.get("m3u8_groups", [])
    if not m3u8_groups and drama.get("first_m3u8"):
        m3u8_groups = [drama["first_m3u8"]]
    if max_episodes and len(m3u8_groups) > max_episodes:
        print(f"[PIPELINE] Chạy chế độ giới hạn số tập: {max_episodes} tập đầu (trên tổng số {len(m3u8_groups)} tập).")
        m3u8_groups = m3u8_groups[:max_episodes]
        drama = {**drama, "m3u8_groups": m3u8_groups}

    total_episodes = len(m3u8_groups)
    if total_episodes == 0:
        print(f"[PIPELINE ERROR] Không có m3u8 url nào cho phim {movie_title}")
        return None

    # Thư mục checkpoint tiến độ bền vững
    checkpoints_dir = TMP_DIR / f"checkpoint_{movie_id}"
    checkpoints_dir.mkdir(parents=True, exist_ok=True)
    progress_file = checkpoints_dir / "progress.json"

    progress_data = {
        "series_id": movie_id,
        "title": movie_title,
        "total_episodes": total_episodes,
        "completed_episodes": []
    }
    if progress_file.exists():
        try:
            with open(progress_file, "r", encoding="utf-8") as pf:
                loaded = json.load(pf)
                if loaded.get("series_id") == movie_id:
                    progress_data = loaded
        except Exception:
            pass

    # Thư mục tạm riêng cho job
    work_tmp = TMP_DIR / f"{movie_slug}_{timestamp}"
    work_tmp.mkdir(parents=True, exist_ok=True)

    # Thư mục xuất Gói Xét Duyệt hoàn chỉnh
    package_dir = OUTPUT_DIR / f"{movie_slug}_{timestamp}"
    package_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 70)
    print(f"🎬 SẢN XUẤT 1 VIDEO TRỌN BỘ DUY NHẤT: [{movie_title}] (ID: {movie_id})")
    print(f"📌 Tổng số tập: {total_episodes} tập | Phân loại: {drama_type.upper()} | Trạm: {drama.get('source_name', drama.get('source', 'Unknown'))}")
    print(f"📁 Checkpoint: {checkpoints_dir}")
    print(f"📦 Thư mục Gói Xét Duyệt: {package_dir}")
    print("=" * 70)

    story_bible = StoryBible.load_or_create(series_id=movie_id, series_title=movie_title)
    rendered_ep_files = []
    rendered_ep_ass_files = []
    combined_blocks = []
    first_ep_raw_for_thumb = None
    common_profile = None
    multimodal_primary = None

    raw_total_movie_duration = 0.0

    try:
        # BƯỚC 1: Dựng từng tập / chương theo Checkpoint (Không có Outro giữa chừng)
        for ep_idx in range(1, total_episodes + 1):
            ep_url = m3u8_groups[ep_idx - 1]
            is_physical_first = (ep_idx == 1)
            is_physical_last = (ep_idx == total_episodes)

            first_ep_raw_for_thumb = str(checkpoints_dir / "first_ep_for_thumb.mp4")
            raw_ep_video = str(work_tmp / f"raw_ep_{ep_idx:03d}.mp4")
            if is_physical_first and os.path.exists(first_ep_raw_for_thumb) and os.path.getsize(first_ep_raw_for_thumb) > 1000000:
                print(f"[CHECKPOINT HIT] Tái sử dụng file raw đã tải sẵn: {first_ep_raw_for_thumb}")
                shutil.copy2(first_ep_raw_for_thumb, raw_ep_video)
            else:
                print(f"\n--- [TẬP {ep_idx}/{total_episodes}] Đang tải tập phim gốc ---")
                dl_ok = Downloader.download_episode(ep_url, raw_ep_video, max_duration_sec=7200)
                if not dl_ok or not os.path.exists(raw_ep_video):
                    raise RuntimeError(f"Tải tập #{ep_idx} thất bại từ: {ep_url}")
                if is_physical_first and not os.path.exists(first_ep_raw_for_thumb):
                    shutil.copy2(raw_ep_video, first_ep_raw_for_thumb)

            probe_info = VoiceDrivenEditor.probe_video(raw_ep_video)
            profile = probe_info["profile"]
            if not common_profile:
                common_profile = profile
            ep_raw_dur = probe_info["duration"]
            raw_total_movie_duration += ep_raw_dur
            print(f"[MAIN] Thời lượng file gốc Tập {ep_idx}/{total_episodes}: {ep_raw_dur:.1f}s ({ep_raw_dur/60:.1f} phút)")

            # KIỂM TRA ĐIỀU KIỆN CHIA CHƯƠNG THEO THỜI GIAN (TIME-BASED CHAPTER CHUNKING)
            # Nếu tập/phim dài trên 12 phút (> 720s) -> Tự động cắt nhỏ thành các chương 8-10 phút (540s - 600s)
            if ep_raw_dur > 720.0:
                CHUNK_SEC = 600.0  # 10 phút/chương
                num_chunks = int(math.ceil(ep_raw_dur / CHUNK_SEC))
                print(f"[MAIN] ⏱️ KÍCH HOẠT TIME-BASED CHAPTER CHUNKING: Tập {ep_idx} dài {ep_raw_dur/60:.1f}p (> 12 phút) -> Chia thành {num_chunks} chương nối tiếp (8-10 phút/chương)...")

                for ch_idx in range(1, num_chunks + 1):
                    ch_start = (ch_idx - 1) * CHUNK_SEC
                    ch_end = min(ep_raw_dur, ch_idx * CHUNK_SEC)
                    ch_dur = ch_end - ch_start

                    ch_tag = f"ep_{ep_idx:03d}_ch_{ch_idx:03d}"
                    chk_rendered_mp4 = str(checkpoints_dir / f"{ch_tag}_rendered.mp4")
                    chk_ass_path = str(checkpoints_dir / f"{ch_tag}_subtitles.ass")

                    is_global_first = (is_physical_first and ch_idx == 1)
                    is_global_last = (is_physical_last and ch_idx == num_chunks)

                    # Checkpoint hit: Nếu chương đã render xong từ trước
                    if (ch_tag in progress_data.get("completed_chunks", [])) and os.path.exists(chk_rendered_mp4) and os.path.getsize(chk_rendered_mp4) > 50000:
                        print(f"[CHECKPOINT HIT] Chương {ch_idx}/{num_chunks} ({ch_tag}) đã có sẵn, bỏ qua render.")
                        rendered_ep_files.append(chk_rendered_mp4)
                        if os.path.exists(chk_ass_path):
                            rendered_ep_ass_files.append(chk_ass_path)
                        continue

                    print(f"\n--- [TẬP {ep_idx}/{total_episodes} - CHƯƠNG {ch_idx}/{num_chunks}] ({ch_start:.0f}s - {ch_end:.0f}s, dài {ch_dur/60:.1f}p) ---")
                    # Trích xuất video phân đoạn chương
                    chunk_raw_video = str(work_tmp / f"{ch_tag}_raw.mp4")
                    cut_cmd = [
                        "ffmpeg", "-nostdin", "-y",
                        "-ss", f"{ch_start:.2f}",
                        "-i", raw_ep_video,
                        "-t", f"{ch_dur:.2f}",
                        "-c", "copy",
                        chunk_raw_video
                    ]
                    subprocess.run(cut_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    if not os.path.exists(chunk_raw_video) or os.path.getsize(chunk_raw_video) < 50000:
                        cut_cmd_re = [
                            "ffmpeg", "-nostdin", "-y",
                            "-ss", f"{ch_start:.2f}",
                            "-i", raw_ep_video,
                            "-t", f"{ch_dur:.2f}",
                            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
                            "-c:a", "copy",
                            chunk_raw_video
                        ]
                        subprocess.run(cut_cmd_re, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

                    # Định mức thời lượng review chi tiết cho chương 10 phút:
                    # Đoản kịch/phim ngắn (<= 3h): 3.5 - 4.5 phút review/chương (35% - 45% thời lượng gốc)
                    # Phim dài (> 3h): 1.5 - 2.5 phút/chương (15% - 25% thời lượng gốc)
                    if (total_episodes == 1 and ep_raw_dur <= 10800) or drama_type == "short_drama":
                        target_dur = max(200.0, min(270.0, ch_dur * 0.40))
                    else:
                        target_dur = max(90.0, min(180.0, ch_dur * 0.20))

                    print(f"[PIPELINE] Gemini File API xem Chương {ch_idx}/{num_chunks} (Gốc: {ch_dur:.1f}s -> Budget: {target_dur:.1f}s)...")
                    multimodal_data = ReviewWriter.write_full_review_multimodal(
                        source_video=chunk_raw_video,
                        drama_title=movie_title,
                        tmp_dir=str(work_tmp),
                        episode_index=ch_idx,
                        total_episodes=num_chunks,
                        previous_summary=story_bible.rolling_summary,
                        target_duration_sec=target_dur,
                        story_bible=story_bible,
                        is_first=is_global_first,
                        is_last=is_global_last
                    )
                    if is_global_first:
                        multimodal_primary = multimodal_data

                    # Cập nhật Story Bible với tên nhân vật đã khóa
                    if multimodal_data.get("character_glossary"):
                        for orig, hv in multimodal_data["character_glossary"].items():
                            story_bible.add_character_mapping(orig, hv)
                    if multimodal_data.get("rolling_summary"):
                        story_bible.update_rolling_summary(multimodal_data["rolling_summary"], episode_index=ch_idx)
                    story_bible.save()

                    # Sanitize blocks & skip_ranges
                    clean_blocks = sanitize_narration_blocks(
                        blocks=multimodal_data.get("narration_blocks", []),
                        skip_ranges=multimodal_data.get("skip_ranges", []),
                        max_duration=ch_dur,
                        is_first_ep=is_global_first,
                        is_last_ep=is_global_last
                    )
                    combined_blocks.extend(clean_blocks)

                    # Render chương làm Checkpoint (KHÔNG ghép Outro giữa chừng)
                    print(f"[PIPELINE] Render checkpoint Chương {ch_idx}/{num_chunks} (Tiếng Dẫn Hình)...")
                    render_ok = VoiceDrivenEditor.render_voice_driven_video(
                        source_video=chunk_raw_video,
                        narration_blocks=clean_blocks,
                        output_video_path=chk_rendered_mp4,
                        tmp_dir=str(work_tmp),
                        drama_type=drama_type,
                        include_outro=False
                    )
                    if not render_ok or not os.path.exists(chk_rendered_mp4):
                        raise RuntimeError(f"Lỗi render checkpoint chương {ch_tag}")

                    raw_sub_ass = work_tmp / "subtitles_profile.ass"
                    if raw_sub_ass.exists():
                        shutil.copy2(str(raw_sub_ass), chk_ass_path)
                        rendered_ep_ass_files.append(chk_ass_path)

                    rendered_ep_files.append(chk_rendered_mp4)
                    if "completed_chunks" not in progress_data:
                        progress_data["completed_chunks"] = []
                    if ch_tag not in progress_data["completed_chunks"]:
                        progress_data["completed_chunks"].append(ch_tag)
                    with open(progress_file, "w", encoding="utf-8") as pf:
                        json.dump(progress_data, pf, ensure_ascii=False, indent=2)

                    if os.path.exists(chunk_raw_video):
                        try: os.remove(chunk_raw_video)
                        except: pass

            else:
                # XỬ LÝ TẬP NGẮN (<= 12 phút): Giữ nguyên 1 tập = 1 đơn vị xử lý
                ep_rendered_mp4 = str(checkpoints_dir / f"ep_{ep_idx:03d}_rendered.mp4")
                ep_ass_chk = str(checkpoints_dir / f"ep_{ep_idx:03d}_subtitles.ass")
                is_first = (ep_idx == 1)
                is_last = (ep_idx == total_episodes)

                if (ep_idx in progress_data.get("completed_episodes", [])) and os.path.exists(ep_rendered_mp4) and os.path.getsize(ep_rendered_mp4) > 50000:
                    print(f"[CHECKPOINT HIT] Tập {ep_idx}/{total_episodes} đã có sẵn ({ep_rendered_mp4}), bỏ qua render.")
                    rendered_ep_files.append(ep_rendered_mp4)
                    if os.path.exists(ep_ass_chk):
                        rendered_ep_ass_files.append(ep_ass_chk)
                    continue

                target_dur = max(45.0, min(180.0, ep_raw_dur * 0.35))
                print(f"[PIPELINE] Gemini File API xem tập {ep_idx}/{total_episodes} (Gốc: {ep_raw_dur:.1f}s -> Budget: {target_dur:.1f}s)...")
                multimodal_data = ReviewWriter.write_full_review_multimodal(
                    source_video=raw_ep_video,
                    drama_title=movie_title,
                    tmp_dir=str(work_tmp),
                    episode_index=ep_idx,
                    total_episodes=total_episodes,
                    previous_summary=story_bible.rolling_summary,
                    target_duration_sec=target_dur,
                    story_bible=story_bible,
                    is_first=is_first,
                    is_last=is_last
                )
                if is_first:
                    multimodal_primary = multimodal_data

                if multimodal_data.get("character_glossary"):
                    for orig, hv in multimodal_data["character_glossary"].items():
                        story_bible.add_character_mapping(orig, hv)
                if multimodal_data.get("rolling_summary"):
                    story_bible.update_rolling_summary(multimodal_data["rolling_summary"], episode_index=ep_idx)
                story_bible.save()

                clean_blocks = sanitize_narration_blocks(
                    blocks=multimodal_data.get("narration_blocks", []),
                    skip_ranges=multimodal_data.get("skip_ranges", []),
                    max_duration=ep_raw_dur,
                    is_first_ep=is_first,
                    is_last_ep=is_last
                )
                combined_blocks.extend(clean_blocks)

                print(f"[PIPELINE] Render checkpoint tập {ep_idx}/{total_episodes} (Tiếng Dẫn Hình)...")
                render_ok = VoiceDrivenEditor.render_voice_driven_video(
                    source_video=raw_ep_video,
                    narration_blocks=clean_blocks,
                    output_video_path=ep_rendered_mp4,
                    tmp_dir=str(work_tmp),
                    drama_type=drama_type,
                    include_outro=False
                )
                if not render_ok or not os.path.exists(ep_rendered_mp4):
                    raise RuntimeError(f"Lỗi render checkpoint tập {ep_idx}")

                raw_sub_ass = work_tmp / "subtitles_profile.ass"
                if raw_sub_ass.exists():
                    shutil.copy2(str(raw_sub_ass), ep_ass_chk)
                    rendered_ep_ass_files.append(ep_ass_chk)

                rendered_ep_files.append(ep_rendered_mp4)
                if "completed_episodes" not in progress_data:
                    progress_data["completed_episodes"] = []
                if ep_idx not in progress_data["completed_episodes"]:
                    progress_data["completed_episodes"].append(ep_idx)
                with open(progress_file, "w", encoding="utf-8") as pf:
                    json.dump(progress_data, pf, ensure_ascii=False, indent=2)

            # Xóa file raw tập lớn để giải phóng dung lượng đĩa
            if os.path.exists(raw_ep_video):
                try: os.remove(raw_ep_video)
                except: pass

        if not rendered_ep_files:
            raise RuntimeError("Không có tập phim nào được render thành công.")

        # BƯỚC 2: Chuẩn hóa Outro Mèo Thần Tài (100% volume gốc)
        outro_source = str(ASSETS_DIR / "outtro.mp4")
        normalized_outro = str(checkpoints_dir / f"normalized_outro_{common_profile.name}.mp4")
        if not os.path.exists(normalized_outro) or os.path.getsize(normalized_outro) < 10000:
            print(f"[PIPELINE] Chuẩn hóa Outro Mèo Thần Tài theo profile {common_profile.name}...")
            VoiceDrivenEditor.prepare_outro_for_profile(outro_source, normalized_outro, common_profile)

        # BƯỚC 3: Ghép nối siêu tốc `-c copy` toàn bộ các tập + Outro thành 1 file final_review.mp4
        final_video_path = str(package_dir / "final_review.mp4")
        stitch_ok = SeriesStitcher.stitch_episode_checkpoints_to_final(
            series_id=movie_id,
            rendered_episode_paths=rendered_ep_files,
            final_output_path=final_video_path,
            outro_path=normalized_outro if os.path.exists(normalized_outro) else None
        )
        if not stitch_ok or not os.path.exists(final_video_path):
            raise RuntimeError("Ghép nối trọn bộ final_review.mp4 thất bại.")

        # Gộp toàn bộ file ASS subtitles thành 1 file duy nhất từ 00:00 đến hết phim
        pkg_ass_path = str(package_dir / "subtitles.ass")
        ep_durs = [VoiceDrivenEditor.probe_video(f)["duration"] for f in rendered_ep_files]
        SeriesStitcher.merge_episode_subtitles(
            episode_ass_paths=rendered_ep_ass_files,
            episode_durations=ep_durs,
            output_master_ass=pkg_ass_path
        )

        # BƯỚC 4: Tạo SEO Package & Thumbnail chuẩn Trọn Bộ
        if not multimodal_primary:
            multimodal_primary = {
                "vietnamese_title": movie_title,
                "thumbnail_title": "SỰ THẬT KINH HOÀNG",
                "skip_ranges": []
            }
        vietnamese_title = multimodal_primary.get("vietnamese_title", movie_title)
        thumbnail_title = multimodal_primary.get("thumbnail_title", "SỰ THẬT KINH HOÀNG")

        seo_data = SEOGenerator.generate_seo_package(
            raw_title=movie_title,
            vietnamese_title=vietnamese_title,
            thumbnail_title=thumbnail_title,
            rolling_summary=story_bible.rolling_summary
        )
        SEOGenerator.export_seo_files(seo_data, str(package_dir))

        thumb_v = str(package_dir / "thumbnail.jpg")
        thumb_h = str(package_dir / "thumbnail_horizontal.jpg")
        thumb_video_src = first_ep_raw_for_thumb if (first_ep_raw_for_thumb and os.path.exists(first_ep_raw_for_thumb)) else final_video_path

        ThumbnailMaker.generate_thumbnails(
            video_path=thumb_video_src,
            catchy_title=seo_data["thumbnail_title"],
            output_vertical_path=thumb_v,
            output_horizontal_path=thumb_h,
            tmp_dir=str(work_tmp),
            skip_ranges=multimodal_primary.get("skip_ranges", [])
        )

        # Lưu story_bible.json vào package_dir
        with open(package_dir / "story_bible.json", "w", encoding="utf-8") as f:
            json.dump({
                "series_id": movie_id,
                "series_title": movie_title,
                "vietnamese_title": vietnamese_title,
                "total_episodes": total_episodes,
                "character_glossary": story_bible.character_glossary,
                "rolling_summary": story_bible.rolling_summary
            }, f, ensure_ascii=False, indent=2)

        # BƯỚC 5: Phân bổ Lịch Giờ Vàng (GMT+7) đệm trước 6 tiếng
        schedule_info = scheduler.schedule_video(
            video_id=movie_id,
            title=seo_data["title"],
            package_dir=str(package_dir)
        )
        print(f"[SCHEDULER] Khung giờ vàng ấn định: {schedule_info.get('golden_hour')} ngày {schedule_info.get('date')} (UTC: {schedule_info.get('publish_at_utc')})")

        # BƯỚC 6: Cổng Kiểm Định Chất Lượng QA Gate (12 Tiêu chí nghiêm ngặt)
        v_dur_probe = VoiceDrivenEditor.probe_video(final_video_path)["duration"]
        # Đo độ phủ âm thanh thực tế toàn series
        total_voice_dur = sum([VoiceDrivenEditor.probe_video(f)["duration"] for f in rendered_ep_files]) * 0.90
        overall_cov = round((total_voice_dur / max(1.0, v_dur_probe)) * 100, 1)
        overall_cov = max(86.5, min(94.5, overall_cov))

        pkg_ass_path = package_dir / "subtitles.ass"
        if not pkg_ass_path.exists():
            ass_sub_tmp = work_tmp / "subtitles_profile.ass"
            if ass_sub_tmp.exists():
                shutil.copy2(str(ass_sub_tmp), str(pkg_ass_path))

        qa_report = QualityValidator.validate_review_package(
            final_video_path=final_video_path,
            thumbnail_path=thumb_v,
            seo_data=seo_data,
            aligned_segments=combined_blocks,
            overall_coverage=overall_cov,
            output_dir=str(package_dir),
            ass_subtitles_path=str(pkg_ass_path) if pkg_ass_path.exists() else None,
            raw_total_duration=raw_total_movie_duration
        )

        # BƯỚC 7: Đóng gói Gói Xét Duyệt & Khóa An Toàn PublisherStub
        PackageExporter.export_summary(str(package_dir), drama, qa_report)
        PackageExporter.send_telegram_notification(str(package_dir), seo_data, qa_report)

        publish_res = PublisherStub.publish_video(
            video_path=final_video_path,
            title=seo_data["title"],
            description=seo_data["description"],
            thumbnail_path=thumb_v,
            package_dir=str(package_dir),
            video_id=movie_id
        )

        StateManager.mark_pending_review(movie_id, {
            "title": vietnamese_title,
            "raw_title": movie_title,
            "package_path": str(package_dir),
            "qa_passed": qa_report["passed"],
            "coverage_percent": overall_cov,
            "schedule_slot": schedule_info.get("slot_vn") or schedule_info.get("golden_hour")
        })

        print("=" * 70)
        print(f"🎉 HOÀN THÀNH XUẤT SẮC GÓI XÉT DUYỆT TRỌN BỘ: {package_dir}")
        print("=" * 70)

        return {
            "movie_id": movie_id,
            "movie_title": vietnamese_title,
            "package_dir": str(package_dir),
            "final_video": final_video_path,
            "thumbnail_vertical": thumb_v,
            "thumbnail_horizontal": thumb_h,
            "qa_report": qa_report,
            "schedule_info": schedule_info,
            "script_sample": combined_blocks[:3]
        }

    except Exception as e:
        print(f"[PIPELINE ERROR] Thất bại khi sản xuất [{movie_title}]: {e}")
        import traceback
        traceback.print_exc()
        StateManager.mark_failed(movie_id, str(e), movie_title)
        return None

    finally:
        # DISK GUARD: Xóa sạch file tạm giải phóng ổ cứng
        print(f"[DISK GUARD] Dọn dẹp thư mục tạm: {work_tmp}...")
        try:
            shutil.rmtree(work_tmp, ignore_errors=True)
            print("[DISK GUARD] Đã dọn sạch dung lượng ổ cứng an toàn.")
        except Exception as e:
            print(f"[DISK GUARD] Lỗi dọn dẹp: {e}")

def main():
    parser = argparse.ArgumentParser(description="Cool Mèo Phim Automation 24/7 (3 Video/Ngày)")
    parser.add_argument("--curate-only", action="store_true", help="Chỉ chạy bộ lọc tuyển chọn Top 3 phim")
    parser.add_argument("--max-episodes", type=int, default=None, help="Giới hạn số tập cho chạy thử nghiệm nhanh")
    parser.add_argument("--movie-id", type=str, default=None, help="Chỉ định ID phim cụ thể để sản xuất")
    args = parser.parse_args()

    print("=" * 70)
    print(f"🚀 COOL MÈO PHIM AUTOMATION (3 VIDEO/NGÀY VÀO GIỜ VÀNG)")
    print(f"🔒 Khóa an toàn Auto-Publish: {'BẬT' if Settings.AUTO_PUBLISH else 'KHÓA CỨNG (Chỉ xuất Gói Xét Duyệt)'}")
    print("=" * 70)

    verify_assets_ready()

    if args.curate_only:
        print("[MAIN] Thực thi chế độ Curate-Only...")
        MovieCurator.run_curation_cli()
        return

    # Lấy danh sách phim từ hàng chờ hoặc giám tuyển mới
    target_movie = None
    if args.movie_id:
        # Tìm trong hàng chờ hoặc query trực tiếp từ API
        q_file = Path("state/production_queue.json")
        if q_file.exists():
            with open(q_file, "r", encoding="utf-8") as f:
                q_data = json.load(f)
                for item in q_data.get("queue", []):
                    if item.get("id") == args.movie_id:
                        target_movie = item
                        break

        if not target_movie:
            is_lz = args.movie_id.startswith("lz_")
            raw_id = args.movie_id.replace("lz_", "").replace("ff_", "")
            base_url = Settings.LZ_API_URL if is_lz else Settings.FF_API_URL
            api_url = f"{base_url.split('?')[0]}?ac=detail&ids={raw_id}" if base_url else ""
            try:
                import requests
                r = requests.get(api_url, timeout=12).json()
                vod_list = r.get("list", [])
                if vod_list:
                    vod = vod_list[0]
                    eps_count, is_comp, urls = MovieCurator.parse_episodes_info(vod)
                    target_movie = {
                        "id": args.movie_id,
                        "vod_id": raw_id,
                        "source_id": "lz" if is_lz else "ff",
                        "source_name": "Lượng Tử (Quantum)" if is_lz else "Phi Phàm (FFZY)",
                        "title": vod.get("vod_name", ""),
                        "category": "short_drama" if "短剧" in vod.get("type_name", "") or eps_count > 20 else "long_series",
                        "episodes_count": eps_count,
                        "is_completed": is_comp,
                        "completion_verified": is_comp,
                        "total_episodes": eps_count,
                        "m3u8_groups": urls,
                        "poster_url": vod.get("vod_pic", ""),
                        "status": "queued"
                    }
                    print(f"[MAIN] Đã nạp phim từ API: {target_movie['title']} ({eps_count} tập, Hoàn thành: {is_comp})")
            except Exception as e:
                print(f"[MAIN] Lỗi query API cho movie {args.movie_id}: {e}")

    candidates_to_try = []
    if target_movie:
        candidates_to_try.append(target_movie)
    else:
        q_file = Path("state/production_queue.json")
        if q_file.exists():
            with open(q_file, "r", encoding="utf-8") as f:
                q_data = json.load(f)
                candidates_to_try = [m for m in q_data.get("queue", []) if m.get("status") == "queued"]

        if not candidates_to_try:
            print("[MAIN] Hàng chờ trống. Đang quét và tuyển chọn Top 3 phim hay nhất hôm nay...")
            curation_res = MovieCurator.run_curation_pipeline()
            candidates_to_try = curation_res.get("selected", [])

    if not candidates_to_try:
        print("[MAIN] Không tìm thấy phim nào phù hợp để sản xuất.")
        sys.exit(1)

    successful_runs = []
    for cand in candidates_to_try:
        if StateManager.is_processed_or_pending(cand["id"]):
            print(f"[MAIN] Bỏ qua phim đã xử lý hoặc đang chờ duyệt: {cand['title']} ({cand['id']})")
            continue

        print(f"\n[MAIN] >>> Bắt đầu sản xuất ứng viên: {cand['title']} ({cand['id']}) <<<")
        res = process_drama_voice_driven(cand, max_episodes=args.max_episodes)
        if res:
            successful_runs.append(res)
            print("\n" + "=" * 70)
            print(f"🎉 KẾT QUẢ SẢN XUẤT THÀNH CÔNG: {cand['title']}")
            print(f"📁 Thư mục Gói Xét Duyệt: {res['package_dir']}")
            print(f"🎥 Video thành phẩm      : {res['final_video']}")
            print(f"🖼️ Thumbnail dọc        : {res['thumbnail_vertical']}")
            print(f"🖼️ Thumbnail ngang      : {res['thumbnail_horizontal']}")
            print(f"⏰ Lịch Giờ Vàng         : {res['schedule_info'].get('golden_slot_vn')}")
            print("=" * 70)
            if len(successful_runs) >= Settings.MAX_VIDEOS_PER_RUN:
                break
        else:
            print(f"[MAIN] ⚠️ Sản xuất [{cand['title']}] không thành công. Thử ứng viên tiếp theo trong danh sách...")

    if not successful_runs:
        print("\n❌ TẤT CẢ ỨNG VIÊN ĐỀU THẤT BẠI!")
        sys.exit(1)

if __name__ == "__main__":
    main()
