import os
import re
import time
import urllib.request
import urllib.parse
import subprocess
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
from config.settings import Settings

class Downloader:
    """
    Tải video M3U8/MP4 thông minh tích hợp Lớp lọc quảng cáo Lớp 1
    ngay trên M3U8 Manifest (#EXT-X-DISCONTINUITY).
    """

    @classmethod
    def resolve_stream_url(cls, m3u8_url: str) -> str:
        """Phân giải master playlist hoặc link share để lấy direct stream URL."""
        # 1. Nếu là link share (Liangzi), tải trang share để lấy link M3U8 có chữ ký (?sign=...)
        if "/share/" in m3u8_url:
            try:
                req = urllib.request.Request(
                    m3u8_url,
                    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/122.0"}
                )
                with urllib.request.urlopen(req, timeout=12) as resp:
                    html = resp.read().decode("utf-8", errors="ignore")
                    m = re.search(r'var\s+main\s*=\s*["\']([^"\']+)["\']', html)
                    if m:
                        main_path = m.group(1)
                        parsed = urllib.parse.urlparse(m3u8_url)
                        m3u8_url = f"{parsed.scheme}://{parsed.netloc}{main_path}"
                        print(f"[Downloader] Phân giải thành công link có chữ ký: {m3u8_url}")
            except Exception as e:
                print(f"[Downloader] Lỗi phân giải share page {m3u8_url}: {e}")

        if "/lzv" in m3u8_url and m3u8_url.endswith("/index.m3u8"):
            return m3u8_url.replace("/index.m3u8", "/2000k/hls/index.m3u8")

        try:
            req = urllib.request.Request(
                m3u8_url,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/122.0"}
            )
            with urllib.request.urlopen(req, timeout=12) as resp:
                text = resp.read().decode("utf-8", errors="ignore")
                base = m3u8_url.rsplit("/", 1)[0]
                query = urllib.parse.urlparse(m3u8_url).query
                for line in text.splitlines():
                    line = line.strip()
                    if line and not line.startswith("#"):
                        if line.startswith("http"):
                            return line
                        elif line.startswith("/"):
                            parsed = urllib.parse.urlparse(m3u8_url)
                            res_url = f"{parsed.scheme}://{parsed.netloc}{line}"
                            return f"{res_url}?{query}" if query and "?" not in res_url else res_url
                        else:
                            res_url = f"{base}/{line}"
                            return f"{res_url}?{query}" if query and "?" not in res_url else res_url
        except Exception as e:
            print(f"[Downloader] Lỗi phân giải master playlist: {e}")
        return m3u8_url

    @classmethod
    def sanitize_m3u8_manifest(cls, stream_url: str, output_manifest_path: str) -> str:
        """
        Lớp lọc quảng cáo 1: Phân tích file M3U8 và loại bỏ cụm phân đoạn quảng cáo
        sau thẻ #EXT-X-DISCONTINUITY có thời lượng bất thường (5s - 20s) hoặc domain lạ.
        """
        try:
            req = urllib.request.Request(
                stream_url,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/122.0"}
            )
            with urllib.request.urlopen(req, timeout=12) as resp:
                content = resp.read().decode("utf-8", errors="ignore")

            base_url = stream_url.rsplit("/", 1)[0]
            lines = content.splitlines()

            sanitized_lines = []
            in_discontinuity_block = False
            discontinuity_segments = []

            # Phân tích từng phân đoạn
            i = 0
            while i < len(lines):
                line = lines[i].strip()
                if line == "#EXT-X-DISCONTINUITY":
                    if not in_discontinuity_block:
                        # Bắt đầu khối discontinuity có nguy cơ là quảng cáo chèn
                        in_discontinuity_block = True
                        discontinuity_segments = []
                        i += 1
                        continue
                    else:
                        # Gặp thẻ DISCONTINUITY thứ 2: Kết thúc khối nghi vấn
                        total_disc_dur = sum(s[0] for s in discontinuity_segments)
                        # Nếu khối chỉ dài từ 3s đến 30s thì 99% là quảng cáo cờ bạc chèn vào -> CẮT BỎ
                        if 3.0 <= total_disc_dur <= 30.0:
                            print(f"[Downloader] Đã phát hiện và gạt bỏ cụm quảng cáo chèn ({total_disc_dur:.1f}s) sau DISCONTINUITY.")
                        else:
                            # Giữ lại nếu là diễn biến phim thực sự
                            sanitized_lines.append("#EXT-X-DISCONTINUITY")
                            for s_dur, s_url in discontinuity_segments:
                                sanitized_lines.append(f"#EXTINF:{s_dur},")
                                sanitized_lines.append(s_url)
                        in_discontinuity_block = False
                        discontinuity_segments = []
                        i += 1
                        continue

                if line.startswith("#EXTINF:"):
                    # Lấy thời lượng segment
                    match = re.search(r"#EXTINF:([0-9.]+)", line)
                    dur = float(match.group(1)) if match else 0.0
                    i += 1
                    if i < len(lines):
                        seg_url = lines[i].strip()
                        if not seg_url.startswith("http") and not seg_url.startswith("#"):
                            if seg_url.startswith("/"):
                                parsed = urllib.parse.urlparse(stream_url)
                                seg_url = f"{parsed.scheme}://{parsed.netloc}{seg_url}"
                            else:
                                seg_url = f"{base_url}/{seg_url}"

                        if in_discontinuity_block:
                            discontinuity_segments.append((dur, seg_url))
                        else:
                            sanitized_lines.append(f"#EXTINF:{dur},")
                            sanitized_lines.append(seg_url)
                    i += 1
                    continue

                if in_discontinuity_block and (line.startswith("#EXT-X-ENDLIST") or i == len(lines) - 1):
                    # Đánh giá khối discontinuity ở cuối playlist
                    total_disc_dur = sum(s[0] for s in discontinuity_segments)
                    if 3.0 <= total_disc_dur <= 30.0:
                        print(f"[Downloader] Đã phát hiện và gạt bỏ cụm quảng cáo chèn cuối ({total_disc_dur:.1f}s).")
                    else:
                        sanitized_lines.append("#EXT-X-DISCONTINUITY")
                        for s_dur, s_url in discontinuity_segments:
                            sanitized_lines.append(f"#EXTINF:{s_dur},")
                            sanitized_lines.append(s_url)
                    in_discontinuity_block = False
                    discontinuity_segments = []

                if not in_discontinuity_block:
                    sanitized_lines.append(line)
                i += 1

            # Ghi file m3u8 đã làm sạch
            with open(output_manifest_path, "w", encoding="utf-8") as f:
                f.write("\n".join(sanitized_lines) + "\n")

            return output_manifest_path
        except Exception as e:
            print(f"[Downloader] Lỗi lọc m3u8 manifest: {e}. Sử dụng luồng trực tiếp.")
            return stream_url

    @classmethod
    def download_episode(cls, m3u8_url: str, output_path: str, max_duration_sec: int = 7200) -> bool:
        """Tải 1 tập phim cụ thể từ m3u8 url có lọc manifest (#EXT-X-DISCONTINUITY). Hỗ trợ tải trọn bộ đến 2 giờ."""
        tmp_dir = Path(output_path).parent
        tmp_dir.mkdir(parents=True, exist_ok=True)
        tmp_manifest = str(tmp_dir / f"clean_ep_{int(time.time()*1000)}.m3u8")
        raw_stream = cls.resolve_stream_url(m3u8_url)
        clean_stream = cls.sanitize_m3u8_manifest(raw_stream, tmp_manifest)
        ok = cls.download_segment_to_mp4(clean_stream, output_path, max_seconds=max_duration_sec)
        if os.path.exists(tmp_manifest):
            try: os.remove(tmp_manifest)
            except: pass
        return ok

    @classmethod
    def download_segment_to_mp4(cls, stream_url: str, output_path: str, max_seconds: int = 7200) -> bool:
        """Tải một luồng stream m3u8 thành file mp4 bằng FFmpeg (-c copy)."""
        t0 = time.time()
        parsed = urllib.parse.urlparse(stream_url)
        referer = f"{parsed.scheme}://{parsed.netloc}/" if parsed.netloc else ""
        headers_str = (
            "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/122.0 Safari/537.36\r\n"
            f"Referer: {referer}\r\n"
        )

        cmd = ["ffmpeg", "-y"]
        if stream_url.startswith("http://") or stream_url.startswith("https://"):
            cmd.extend(["-headers", headers_str, "-rw_timeout", "15000000"])
        cmd.extend([
            "-allowed_extensions", "ALL",
            "-protocol_whitelist", "file,http,https,tcp,tls,crypto",
            "-i", stream_url,
            "-t", str(max_seconds),
            "-c", "copy",
            "-bsf:a", "aac_adtstoasc",
            output_path
        ])

        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            if res.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 1000000:
                elapsed = round(time.time() - t0, 1)
                size_mb = round(os.path.getsize(output_path) / (1024 * 1024), 1)
                print(f"[Downloader] Tải thành công ({size_mb} MB trong {elapsed}s)")
                return True
            else:
                print(f"[Downloader] FFmpeg tải thất bại: {res.stderr[-250:]}")
                return False
        except Exception as e:
            print(f"[Downloader] Ngoại lệ khi tải m3u8: {e}")
            return False

    @classmethod
    def download_drama(cls, drama: Dict[str, Any], output_path: str, max_duration_sec: int = 5400) -> bool:
        """Tải toàn bộ phim (hỗ trợ cả phim 1 file hoặc phim nhiều tập ghép nối)."""
        m3u8_groups = drama.get("m3u8_groups", [])
        if not m3u8_groups:
            print(f"[Downloader] Không tìm thấy link M3U8 cho {drama.get('title')}")
            return False

        tmp_dir = Path(output_path).parent
        tmp_manifest = str(tmp_dir / f"clean_{drama['id']}.m3u8")

        # Trường hợp 1: Phim trọn bộ 1 file (Lượng Tử)
        if len(m3u8_groups) == 1:
            raw_stream = cls.resolve_stream_url(m3u8_groups[0])
            clean_stream = cls.sanitize_m3u8_manifest(raw_stream, tmp_manifest)
            ok = cls.download_segment_to_mp4(clean_stream, output_path, max_seconds=max_duration_sec)
            if os.path.exists(tmp_manifest):
                try: os.remove(tmp_manifest)
                except: pass
            return ok

        # Trường hợp 2: Phim nhiều tập ngắn (Phi Phàm)
        num_eps = min(len(m3u8_groups), 10)
        print(f"[Downloader] Phim {len(m3u8_groups)} tập -> Đang tải và ghép nối {num_eps} tập đầu...")
        ep_files = []
        concat_txt = tmp_dir / f"concat_{drama['id']}.txt"

        try:
            for i in range(num_eps):
                ep_file = str(tmp_dir / f"ep_{i}.mp4")
                ep_url = cls.resolve_stream_url(m3u8_groups[i])
                if cls.download_segment_to_mp4(ep_url, ep_file, max_seconds=180):
                    ep_files.append(ep_file)

            if len(ep_files) < 2:
                print(f"[Downloader] Không tải đủ các tập phim ({len(ep_files)}/{num_eps})")
                return False

            with open(concat_txt, "w", encoding="utf-8") as f:
                for f_path in ep_files:
                    f.write(f"file '{f_path.replace(os.sep, '/')}'\n")

            cmd = [
                "ffmpeg", "-y", "-f", "concat", "-safe", "0",
                "-i", str(concat_txt),
                "-c", "copy",
                "-bsf:a", "aac_adtstoasc",
                output_path
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            return res.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 1000000
        finally:
            if concat_txt.exists():
                try: concat_txt.unlink()
                except: pass
            for f_ep in ep_files:
                if os.path.exists(f_ep):
                    try: os.remove(f_ep)
                    except: pass

    @classmethod
    def download_poster(cls, poster_url: str, output_path: str) -> bool:
        """Tải ảnh poster của phim để làm nguyên liệu thiết kế thumbnail."""
        if not poster_url or not poster_url.startswith("http"):
            return False
        try:
            req = urllib.request.Request(
                poster_url,
                headers={"User-Agent": "Mozilla/5.0"}
            )
            with urllib.request.urlopen(req, timeout=12) as resp:
                with open(output_path, "wb") as f:
                    f.write(resp.read())
            return os.path.exists(output_path) and os.path.getsize(output_path) > 1000
        except Exception as e:
            print(f"[Downloader] Không thể tải poster: {e}")
            return False
