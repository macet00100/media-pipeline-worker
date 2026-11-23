import os
import json
import time
import urllib.request
import urllib.parse
import subprocess
from typing import Dict, Any, List, Optional
from core.logger import Logger

class FilmSources:
    """
    Kết nối trực tiếp vào 2 kho phim tài nguyên vi kịch Trung Quốc:
    1. Kho Lượng Tử (Quantum / LZ API)
    2. Kho Phi Phàm (FFZY / Feifan API)
    """

    SOURCES = [
        {
            "id": "liangzi",
            "name": "Lượng Tử (Quantum)",
            "api_url": os.getenv("LZ_API_URL", ""),
            "type_id": 46
        },
        {
            "id": "feifan",
            "name": "Phi Phàm (FFZY)",
            "api_url": os.getenv("FF_API_URL", ""),
            "type_id": 36
        }
    ]

    # Các từ khóa phim hot / viral cao
    VIRAL_KEYWORDS = [
        "打脸", "豪门", "总裁", "婆媳", "千金", "真假千金", "重回", "重生",
        "逆袭", "清醒", "战神", "神医", "分家", "背叛", "大权", "首富", "闪婚"
    ]

    @classmethod
    def fetch_candidates(
        cls,
        limit: int = 10,
        source_id: Optional[str] = None,
        exclude_ids: Optional[set] = None
    ) -> List[Dict[str, Any]]:
        """
        Quét danh sách các bộ phim ngắn mới cập nhật từ Kho Lượng Tử và Phi Phàm.
        """
        if exclude_ids is None:
            exclude_ids = set()

        candidates = []
        active_sources = [s for s in cls.SOURCES if not source_id or s["id"] == source_id]

        for src in active_sources:
            src_name = src["name"]
            api_url = src["api_url"]
            try:
                Logger.info(f"Đang kết nối kho [{src_name}]...", "CRAWLER")
                req = urllib.request.Request(
                    api_url,
                    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
                )
                with urllib.request.urlopen(req, timeout=12) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    items = data.get("list", [])
                    Logger.info(f"Kho [{src_name}] trả về {len(items)} bộ phim.", "CRAWLER")

                    for item in items:
                        vod_id = str(item.get("vod_id", ""))
                        vod_name = item.get("vod_name", "").strip()
                        vod_pic = item.get("vod_pic", "")
                        vod_time = item.get("vod_time", "")
                        play_url = item.get("vod_play_url", "")

                        if not vod_id or not vod_name:
                            continue

                        unique_key = f"{src['id']}_{vod_id}"
                        if unique_key in exclude_ids:
                            continue

                        m3u8_urls = cls._extract_m3u8_urls(play_url)
                        if not m3u8_urls:
                            continue

                        # Tính điểm viral từ khóa
                        score = 0
                        for kw in cls.VIRAL_KEYWORDS:
                            if kw in vod_name:
                                score += 10

                        candidates.append({
                            "id": unique_key,
                            "vod_id": vod_id,
                            "source_id": src["id"],
                            "source_name": src_name,
                            "title": vod_name,
                            "poster_url": vod_pic,
                            "m3u8_urls": m3u8_urls,
                            "updated_time": vod_time,
                            "score": score
                        })
            except Exception as e:
                Logger.warning(f"Lỗi khi kết nối kho [{src_name}]: {e}", "CRAWLER")

        # Sắp xếp theo điểm viral
        candidates.sort(key=lambda x: x["score"], reverse=True)
        return candidates[:limit]

    @classmethod
    def _extract_m3u8_urls(cls, play_url: str) -> List[str]:
        """Bóc tách danh sách link m3u8 từ chuỗi vod_play_url chuẩn CMS."""
        if not play_url:
            return []

        all_valid_groups = []
        for group in play_url.split("$$$"):
            group_urls = []
            for ep in group.split("#"):
                if ".m3u8" in ep.lower():
                    parts = ep.split("$")
                    for p in parts:
                        p = p.strip()
                        if p.startswith("http") and ".m3u8" in p.lower():
                            group_urls.append(p)
                            break
            if group_urls:
                all_valid_groups.append(group_urls)

        if not all_valid_groups:
            return []

        # Ưu tiên nhóm có link direct m3u8
        for grp in all_valid_groups:
            if any("/202" in u or "mixed" in u or "index.m3u8" in u for u in grp):
                return grp

        return all_valid_groups[0]

    @classmethod
    def resolve_m3u8_stream(cls, m3u8_url: str) -> str:
        """Phân giải URL playlist để lấy luồng direct stream m3u8 chuẩn nhất."""
        if "/lzv" in m3u8_url and m3u8_url.endswith("/index.m3u8"):
            # Chuẩn CDN Lượng Tử: 1080p stream
            return m3u8_url.replace("/index.m3u8", "/2000k/hls/index.m3u8")

        try:
            req = urllib.request.Request(
                m3u8_url,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                text = resp.read().decode("utf-8", errors="ignore")
                base_url = m3u8_url.rsplit("/", 1)[0]
                for line in text.splitlines():
                    line = line.strip()
                    if line and not line.startswith("#"):
                        if line.startswith("http"):
                            return line
                        elif line.startswith("/"):
                            parsed = urllib.parse.urlparse(m3u8_url)
                            return f"{parsed.scheme}://{parsed.netloc}{line}"
                        else:
                            return f"{base_url}/{line}"
        except Exception:
            pass
        return m3u8_url

    @classmethod
    def download_poster(cls, poster_url: str, save_path: str) -> bool:
        """Tải poster phim từ trạm về máy."""
        if not poster_url or not poster_url.startswith("http"):
            return False
        try:
            req = urllib.request.Request(
                poster_url,
                headers={"User-Agent": "Mozilla/5.0"}
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                with open(save_path, "wb") as f:
                    f.write(resp.read())
            return os.path.exists(save_path) and os.path.getsize(save_path) > 1000
        except Exception as e:
            Logger.warning(f"Không thể tải poster: {e}", "CRAWLER")
            return False

    @classmethod
    def download_video(
        cls,
        drama: Dict[str, Any],
        output_path: str,
        max_duration_seconds: int = 7200
    ) -> bool:
        """
        Tải video phim bằng ffmpeg stream copy (-c copy) siêu tốc.
        """
        m3u8_urls = drama.get("m3u8_urls", [])
        if not m3u8_urls:
            Logger.error("Không tìm thấy link m3u8 để tải.", "CRAWLER")
            return False

        # Nếu là phim trọn bộ 1 file:
        if len(m3u8_urls) == 1:
            stream_url = cls.resolve_m3u8_stream(m3u8_urls[0])
            Logger.info(f"Đang tải video phim: {drama.get('title')}...", "CRAWLER")
            return cls._run_ffmpeg_m3u8_download(stream_url, output_path, max_duration_seconds)

        # Nếu là phim nhiều tập nhỏ (ví dụ FFZY nhiều tập 1-2 phút)
        Logger.info(f"Phim có {len(m3u8_urls)} tập. Đang tải và ghép nối...", "CRAWLER")
        temp_dir = os.path.dirname(output_path)
        ep_files = []
        concat_txt = os.path.join(temp_dir, "concat_temp.txt")

        # Tải các tập đầu (tối đa 10 tập để làm video review chuẩn)
        num_eps = min(len(m3u8_urls), 10)
        try:
            for i in range(num_eps):
                ep_file = os.path.join(temp_dir, f"ep_{i}.mp4")
                ep_stream = cls.resolve_m3u8_stream(m3u8_urls[i])
                ok = cls._run_ffmpeg_m3u8_download(ep_stream, ep_file, max_duration_seconds=180)
                if ok and os.path.exists(ep_file) and os.path.getsize(ep_file) > 100000:
                    ep_files.append(ep_file)

            if len(ep_files) < 2:
                Logger.warning(f"Không tải đủ các tập phim ({len(ep_files)} tập).", "CRAWLER")
                return False

            with open(concat_txt, "w", encoding="utf-8") as f:
                for f_ep in ep_files:
                    f.write(f"file '{f_ep.replace('\\', '/')}'\n")

            cmd = [
                "ffmpeg", "-y", "-f", "concat", "-safe", "0",
                "-i", concat_txt, "-c", "copy",
                "-bsf:a", "aac_adtstoasc",
                output_path
            ]
            res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return res.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 1000000
        finally:
            if os.path.exists(concat_txt):
                try: os.remove(concat_txt)
                except: pass
            for f_ep in ep_files:
                if os.path.exists(f_ep):
                    try: os.remove(f_ep)
                    except: pass

    @classmethod
    def _run_ffmpeg_m3u8_download(cls, stream_url: str, output_path: str, max_duration_seconds: int) -> bool:
        t0 = time.time()
        try:
            parsed = urllib.parse.urlparse(stream_url)
            referer = f"{parsed.scheme}://{parsed.netloc}/"
            headers_str = (
                "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36\r\n"
                f"Referer: {referer}\r\n"
            )
            cmd = [
                "ffmpeg", "-y",
                "-headers", headers_str,
                "-i", stream_url,
                "-t", str(max_duration_seconds),
                "-c", "copy",
                "-bsf:a", "aac_adtstoasc",
                output_path
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
            if res.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 1000000:
                elapsed = round(time.time() - t0, 1)
                size_mb = round(os.path.getsize(output_path) / (1024 * 1024), 1)
                Logger.success(f"Tải video thành công ({size_mb} MB trong {elapsed}s)", "CRAWLER")
                return True
            else:
                Logger.error(f"FFmpeg tải không thành công: {res.stderr[-200:]}", "CRAWLER")
                return False
        except Exception as e:
            Logger.error(f"Lỗi tải video m3u8: {e}", "CRAWLER")
            return False
