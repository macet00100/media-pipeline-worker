import os
import re
import json
import subprocess
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional, Callable
from config.settings import STATE_DIR, OUTPUT_DIR, TMP_DIR

def slugify(text: str) -> str:
    """Tạo slug an toàn cho đường dẫn thư mục và file."""
    text = re.sub(r"[^\w\s-]", "", text).strip().lower()
    return re.sub(r"[-\s]+", "_", text)[:50] or "series"

class SeriesStitcher:
    """
    Bộ quản lý dựng và ghép nối phim bộ dài/đoản kịch theo Batch:
    - Render theo batch (3-5 tập/batch) để tránh tràn bộ nhớ và timeout.
    - Checkpoint tiến độ tại: state/active_series/<series_id>/progress.json.
    - Bỏ qua các batch đã hoàn thành khi resume.
    - Ghép nối cuối cùng cực nhanh bằng FFmpeg `-c copy` không encode lại.
    - Tự động chia Part nếu tổng thời lượng > 3.5 giờ.
    """

    MAX_PART_DURATION_SEC: float = 3.5 * 3600.0  # 12600s = 3.5h

    @classmethod
    def get_progress_path(cls, series_id: str) -> Path:
        safe_id = slugify(series_id)
        path = STATE_DIR / "active_series" / safe_id / "progress.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    @classmethod
    def load_progress(cls, series_id: str) -> Optional[Dict[str, Any]]:
        """Tải checkpoint tiến độ nếu đã tồn tại."""
        p_path = cls.get_progress_path(series_id)
        if p_path.exists():
            try:
                with open(p_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                print(f"[SeriesStitcher] Cảnh báo lỗi đọc progress.json ({e})")
        return None

    @classmethod
    def save_progress(cls, series_id: str, progress: Dict[str, Any]) -> str:
        """Lưu checkpoint tiến độ nguyên tử."""
        p_path = cls.get_progress_path(series_id)
        progress["last_updated"] = datetime.now().isoformat()
        temp_file = p_path.with_suffix(".tmp")
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(progress, f, ensure_ascii=False, indent=2)
        temp_file.replace(p_path)
        return str(p_path)

    @classmethod
    def init_progress(
        cls,
        series_id: str,
        title: str,
        episodes: List[Any],
        batch_size: int = 4,
        drama_type: str = "short_drama"
    ) -> Dict[str, Any]:
        """
        Khởi tạo cấu trúc quản lý batch (3-5 tập/batch).
        Nếu đã có tiến độ trước đó, giữ lại các batch đã hoàn thành.
        """
        existing = cls.load_progress(series_id)
        if existing and existing.get("total_episodes") == len(episodes):
            print(f"[SeriesStitcher] Tìm thấy checkpoint trước đó cho series [{title}]. Khôi phục trạng thái.")
            return existing

        total_ep = len(episodes)
        batch_size = max(3, min(5, batch_size))  # Khóa trong ngưỡng 3-5 tập
        batches = []

        for i in range(0, total_ep, batch_size):
            b_episodes = episodes[i:i + batch_size]
            b_indices = [getattr(e, "index", idx + i + 1) if not isinstance(e, dict) else e.get("index", idx + i + 1) for idx, e in enumerate(b_episodes)]
            b_idx = len(batches)
            batches.append({
                "batch_index": b_idx,
                "episode_indices": b_indices,
                "output_file": "",
                "status": "pending",
                "duration_sec": 0.0,
                "created_at": datetime.now().isoformat(),
                "completed_at": None,
                "error": None
            })

        progress = {
            "series_id": slugify(series_id),
            "title": title,
            "total_episodes": total_ep,
            "batch_size": batch_size,
            "drama_type": drama_type,
            "batches": batches,
            "completed_batch_count": 0,
            "is_all_rendered": False,
            "is_completed": False,
            "final_outputs": [],
            "created_at": datetime.now().isoformat(),
            "last_updated": datetime.now().isoformat()
        }
        cls.save_progress(series_id, progress)
        print(f"[SeriesStitcher] Đã khởi tạo {len(batches)} batch ({batch_size} tập/batch) cho series [{title}]")
        return progress

    @classmethod
    def mark_batch_completed(
        cls,
        series_id: str,
        batch_index: int,
        output_file: str,
        duration_sec: float
    ):
        """Đánh dấu một batch đã render hoàn thành."""
        progress = cls.load_progress(series_id)
        if not progress:
            return

        for b in progress.get("batches", []):
            if b["batch_index"] == batch_index:
                b["status"] = "completed"
                b["output_file"] = str(output_file)
                b["duration_sec"] = round(duration_sec, 2)
                b["completed_at"] = datetime.now().isoformat()
                b["error"] = None
                break

        completed_cnt = sum(1 for b in progress["batches"] if b["status"] == "completed")
        progress["completed_batch_count"] = completed_cnt
        progress["is_all_rendered"] = (completed_cnt == len(progress["batches"]))
        cls.save_progress(series_id, progress)
        print(f"[SeriesStitcher] Batch #{batch_index + 1}/{len(progress['batches'])} đã hoàn thành ({duration_sec:.1f}s)")

    @classmethod
    def mark_batch_failed(cls, series_id: str, batch_index: int, error_msg: str):
        """Ghi nhận lỗi cho một batch."""
        progress = cls.load_progress(series_id)
        if not progress:
            return

        for b in progress.get("batches", []):
            if b["batch_index"] == batch_index:
                b["status"] = "failed"
                b["error"] = str(error_msg)
                break
        cls.save_progress(series_id, progress)
        print(f"[SeriesStitcher] Batch #{batch_index + 1} gặp sự cố: {error_msg}")

    @classmethod
    def get_media_duration(cls, media_path: str) -> float:
        """Đo thời lượng file media bằng ffprobe."""
        if not os.path.exists(media_path):
            return 0.0
        cmd = [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            media_path
        ]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            return float(r.stdout.strip() or 0.0)
        except Exception:
            return 0.0

    @classmethod
    def render_series_batches(
        cls,
        series_id: str,
        title: str,
        episodes: List[Any],
        batch_render_fn: Callable[[int, List[Any], str], bool],
        batch_output_dir: Path,
        batch_size: int = 4,
        drama_type: str = "short_drama"
    ) -> bool:
        """
        Thực thi quy trình render từng batch có checkpoint bảo vệ:
        - Bỏ qua các batch đã render xong nếu có file hợp lệ.
        - Render batch chưa hoàn thành qua hàm batch_render_fn callback.
        """
        progress = cls.init_progress(series_id, title, episodes, batch_size, drama_type)
        batch_output_dir.mkdir(parents=True, exist_ok=True)

        for b in progress["batches"]:
            b_idx = b["batch_index"]
            out_file = b.get("output_file")

            # Checkpoint Hit: Nếu batch đã hoàn tất và file còn tồn tại
            if b["status"] == "completed" and out_file and os.path.exists(out_file) and os.path.getsize(out_file) > 50000:
                print(f"[SeriesStitcher] [CHECKPOINT HIT] Batch #{b_idx + 1} đã có sẵn, bỏ qua render lại.")
                continue

            target_file = str(batch_output_dir / f"batch_{b_idx:03d}.mp4")
            ep_indices = b["episode_indices"]
            target_episodes = [episodes[i - 1] if isinstance(i, int) and i <= len(episodes) else episodes[idx] for idx, i in enumerate(ep_indices)]

            print(f"[SeriesStitcher] Đang render Batch #{b_idx + 1}/{len(progress['batches'])} (Tập {ep_indices})...")
            try:
                ok = batch_render_fn(b_idx, target_episodes, target_file)
                if ok and os.path.exists(target_file):
                    dur = cls.get_media_duration(target_file)
                    cls.mark_batch_completed(series_id, b_idx, target_file, dur)
                else:
                    cls.mark_batch_failed(series_id, b_idx, "Render trả về False hoặc file không tồn tại")
                    return False
            except Exception as e:
                cls.mark_batch_failed(series_id, b_idx, str(e))
                return False

        return True

    @classmethod
    def stitch_episode_checkpoints_to_final(
        cls,
        series_id: str,
        rendered_episode_paths: List[str],
        final_output_path: str,
        outro_path: Optional[str] = None
    ) -> bool:
        """
        Ghép nối siêu tốc toàn bộ các file checkpoint ep_XXX_rendered.mp4 thành 1 video duy nhất final_review.mp4
        sử dụng FFmpeg -c copy không encode lại (chỉ mất 10-30 giây).
        """
        if not rendered_episode_paths:
            print("[SeriesStitcher] LỖI: Danh sách file checkpoint tập rỗng!")
            return False

        # Kiểm tra file tồn tại
        valid_ep_files = []
        for ep_f in rendered_episode_paths:
            if os.path.exists(ep_f) and os.path.getsize(ep_f) > 10000:
                valid_ep_files.append(ep_f)
            else:
                print(f"[SeriesStitcher] Cảnh báo: File checkpoint không hợp lệ: {ep_f}")

        if not valid_ep_files:
            raise RuntimeError(f"Không có file checkpoint tập nào hợp lệ cho series {series_id}")

        concat_files = list(valid_ep_files)
        if outro_path and os.path.exists(outro_path) and os.path.getsize(outro_path) > 10000:
            concat_files.append(outro_path)

        tmp_dir = Path(final_output_path).parent
        tmp_dir.mkdir(parents=True, exist_ok=True)
        concat_txt = str(tmp_dir / f"concat_all_{slugify(series_id)}.txt")

        with open(concat_txt, "w", encoding="utf-8") as f:
            for fpath in concat_files:
                abs_posix = Path(fpath).resolve().as_posix()
                f.write(f"file '{abs_posix}'\n")

        print(f"[SeriesStitcher] Bắt đầu ghép nối siêu tốc `-c copy` {len(concat_files)} phân đoạn (tập 1..{len(valid_ep_files)} + outro) -> {final_output_path}...")
        cmd_copy = [
            "ffmpeg", "-nostdin", "-y",
            "-f", "concat", "-safe", "0",
            "-i", concat_txt,
            "-c", "copy",
            final_output_path
        ]
        res = subprocess.run(cmd_copy, capture_output=True, text=True, timeout=600)
        if res.returncode == 0 and os.path.exists(final_output_path) and os.path.getsize(final_output_path) > 50000:
            sz_mb = os.path.getsize(final_output_path) / (1024 * 1024)
            print(f"[SeriesStitcher] GHÉP NỐI SIÊU TỐC THÀNH CÔNG: {final_output_path} ({sz_mb:.1f} MB)")
            return True

        print(f"[SeriesStitcher] Concat copy chưa thành công ({res.stderr[-200:] if res.stderr else ''}). Thử lại bằng re-encode an toàn...")
        cmd_reencode = [
            "ffmpeg", "-nostdin", "-y",
            "-f", "concat", "-safe", "0",
            "-i", concat_txt,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
            "-c:a", "aac", "-b:a", "192k",
            final_output_path
        ]
        res_re = subprocess.run(cmd_reencode, capture_output=True, text=True, timeout=1200)
        if res_re.returncode == 0 and os.path.exists(final_output_path) and os.path.getsize(final_output_path) > 50000:
            sz_mb = os.path.getsize(final_output_path) / (1024 * 1024)
            print(f"[SeriesStitcher] Ghép nối re-encode thành công: {final_output_path} ({sz_mb:.1f} MB)")
            return True

        return False

    @classmethod
    def merge_episode_subtitles(
        cls,
        episode_ass_paths: List[str],
        episode_durations: List[float],
        output_master_ass: str
    ) -> bool:
        """
        Gộp toàn bộ file ASS subtitles của từng tập thành 1 file subtitles.ass duy nhất từ 00:00 đến hết phim.
        Tự động cộng dồn offset thời lượng (cumulative offset) của các tập trước.
        """
        if not episode_ass_paths:
            return False

        def parse_ass_time(time_str: str) -> float:
            parts = time_str.strip().split(":")
            if len(parts) == 3:
                h, m, s = parts
                return float(h) * 3600.0 + float(m) * 60.0 + float(s)
            elif len(parts) == 2:
                m, s = parts
                return float(m) * 60.0 + float(s)
            return float(time_str)

        def format_ass_time(sec: float) -> str:
            h = int(sec // 3600)
            m = int((sec % 3600) // 60)
            s = sec % 60
            return f"{h}:{m:02d}:{s:05.2f}"

        header_lines = []
        dialogue_lines = []
        cum_offset = 0.0

        for ep_idx, ass_p in enumerate(episode_ass_paths):
            if not os.path.exists(ass_p):
                continue
            with open(ass_p, "r", encoding="utf-8") as f:
                lines = f.readlines()

            in_events = False
            for line in lines:
                line_str = line.strip()
                if ep_idx == 0:
                    if line_str.startswith("[Events]"):
                        in_events = True
                        header_lines.append(line)
                    elif not in_events and not line_str.startswith("Dialogue:"):
                        header_lines.append(line)
                    elif line_str.startswith("Format:") and in_events:
                        header_lines.append(line)
                
                if line_str.startswith("Dialogue:"):
                    # Dialogue: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
                    parts = line_str.split(",", 9)
                    if len(parts) >= 10:
                        try:
                            start_t = parse_ass_time(parts[1]) + cum_offset
                            end_t = parse_ass_time(parts[2]) + cum_offset
                            parts[1] = format_ass_time(start_t)
                            parts[2] = format_ass_time(end_t)
                            dialogue_lines.append(",".join(parts) + "\n")
                        except Exception:
                            dialogue_lines.append(line if line.endswith("\n") else line + "\n")
                    else:
                        dialogue_lines.append(line if line.endswith("\n") else line + "\n")

            # Cộng dồn thời lượng tập này
            dur = episode_durations[ep_idx] if ep_idx < len(episode_durations) else 0.0
            cum_offset += dur

        # Sắp xếp và làm sạch mốc thời gian để 0% chồng lấn giữa các chương
        parsed_dialogues = []
        for dl in dialogue_lines:
            p = dl.strip().split(",", 9)
            if len(p) >= 10:
                try:
                    st = parse_ass_time(p[1])
                    et = parse_ass_time(p[2])
                    parsed_dialogues.append({"start": st, "end": et, "parts": p})
                except Exception:
                    pass

        parsed_dialogues.sort(key=lambda x: x["start"])
        clean_dlines = []
        for item in parsed_dialogues:
            if clean_dlines:
                prev = clean_dlines[-1]
                if prev["end"] > item["start"]:
                    prev["end"] = max(prev["start"] + 0.1, item["start"] - 0.02)
                    prev["parts"][2] = format_ass_time(prev["end"])
            if item["end"] > item["start"] + 0.05:
                clean_dlines.append(item)

        final_dialogue_lines = [",".join(it["parts"]) + "\n" for it in clean_dlines] if clean_dlines else dialogue_lines

        Path(output_master_ass).parent.mkdir(parents=True, exist_ok=True)
        with open(output_master_ass, "w", encoding="utf-8") as f:
            for hl in header_lines:
                f.write(hl if hl.endswith("\n") else hl + "\n")
            if not any("[Events]" in l for l in header_lines):
                f.write("[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
            for dl in final_dialogue_lines:
                f.write(dl)

        print(f"[SeriesStitcher] Đã xuất file phụ đề trọn bộ gộp ({len(final_dialogue_lines)} dòng): {output_master_ass}")
        return True

    @classmethod
    def stitch_series_copy(
        cls,
        series_id: str,
        output_dir: Path,
        custom_slug: Optional[str] = None
    ) -> List[str]:
        """
        Ghép nối các batch cực nhanh bằng FFmpeg `-c copy` không encode lại.
        - Nếu tổng thời lượng <= 3.5 giờ (12600s): Xuất 1 file duy nhất full_series.mp4.
        - Nếu tổng thời lượng > 3.5 giờ: Tự động gom các batch thành Part 1, Part 2...
        """
        progress = cls.load_progress(series_id)
        if not progress:
            raise RuntimeError(f"Không tìm thấy checkpoint tiến độ cho series {series_id}")

        batches = [b for b in progress.get("batches", []) if b.get("status") == "completed"]
        if not batches:
            raise RuntimeError(f"Chưa có batch nào hoàn thành để ghép cho series {series_id}")

        output_dir.mkdir(parents=True, exist_ok=True)
        slug = custom_slug or progress.get("series_id", slugify(series_id))

        total_dur = sum(b.get("duration_sec", 0.0) for b in batches)
        print(f"[SeriesStitcher] Tổng thời lượng {len(batches)} batch: {total_dur / 60:.1f} phút ({total_dur / 3600:.2f} giờ)")

        # Phân chia Parts nếu vượt quá 3.5h
        part_groups = []
        if total_dur <= cls.MAX_PART_DURATION_SEC:
            part_groups.append((1, batches))
        else:
            current_group = []
            cur_accum = 0.0
            p_idx = 1
            for b in batches:
                b_dur = b.get("duration_sec", 0.0)
                if current_group and (cur_accum + b_dur > cls.MAX_PART_DURATION_SEC):
                    part_groups.append((p_idx, current_group))
                    p_idx += 1
                    current_group = [b]
                    cur_accum = b_dur
                else:
                    current_group.append(b)
                    cur_accum += b_dur
            if current_group:
                part_groups.append((p_idx, current_group))

        final_files = []
        tmp_stitch_dir = TMP_DIR / f"stitch_{slug}"
        tmp_stitch_dir.mkdir(parents=True, exist_ok=True)

        for part_num, group_batches in part_groups:
            if len(part_groups) > 1:
                part_filename = f"{slug}_Part{part_num}.mp4"
            else:
                part_filename = f"{slug}_Full.mp4"

            final_dest = output_dir / part_filename
            concat_list_file = tmp_stitch_dir / f"concat_part_{part_num}.txt"

            with open(concat_list_file, "w", encoding="utf-8") as f:
                for gb in group_batches:
                    file_p = gb["output_file"].replace(os.sep, "/")
                    f.write(f"file '{file_p}'\n")

            print(f"[SeriesStitcher] Ghép nối siêu tốc `-c copy` cho Part {part_num} ({len(group_batches)} batch) -> {final_dest}...")
            cmd = [
                "ffmpeg", "-y",
                "-f", "concat",
                "-safe", "0",
                "-i", str(concat_list_file),
                "-c", "copy",
                str(final_dest)
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            if res.returncode == 0 and final_dest.exists() and final_dest.stat().st_size > 50000:
                print(f"[SeriesStitcher] Ghép thành công {part_filename} ({final_dest.stat().st_size / (1024*1024):.1f} MB)")
                final_files.append(str(final_dest))
            else:
                print(f"[SeriesStitcher] Lỗi ghép concat copy: {res.stderr[-300:]}")
                raise RuntimeError(f"Lỗi ghép batch copy cho {part_filename}")

        # Cập nhật checkpoint hoàn tất
        progress["is_completed"] = True
        progress["final_outputs"] = final_files
        cls.save_progress(series_id, progress)
        return final_files
