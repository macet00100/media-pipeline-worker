import os
import re
import time
import socket
import random
import urllib.request
import urllib.parse
import subprocess
import shutil
import threading
import concurrent.futures
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from config.settings import Settings

class Downloader:
    """
    Tải video M3U8/MP4 thông minh tích hợp Lớp lọc quảng cáo Lớp 1
    ngay trên M3U8 Manifest (#EXT-X-DISCONTINUITY).
    """

    @classmethod
    def _fetch_text(cls, url: str, timeout: int = 15) -> str:
        """Tải nội dung text (M3U8 manifest hoặc HTML) bằng curl (vượt qua WAF chặn fingerprint Python urllib)."""
        # 1. Thử dùng curl (ưu tiên tuyệt đối vì vượt qua TLS fingerprint check của Alibaba Cloud / Cloudflare)
        try:
            curl_bin = "curl.exe" if os.name == "nt" else "curl"
            curl_cmd = [
                curl_bin, "-s", "-L", "-m", str(timeout),
                "-A", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/122.0"
            ]
            if "ffzy" in url:
                curl_cmd.extend(["-H", "Referer: https://ffzy1.tv/"])

            proxy = os.getenv("http_proxy") or os.getenv("HTTP_PROXY") or os.getenv("https_proxy") or os.getenv("HTTPS_PROXY")
            if proxy and proxy.startswith("http://"):
                curl_cmd.extend(["-x", proxy])

            curl_cmd.append(url)
            res = subprocess.run(curl_cmd, capture_output=True, text=True, timeout=timeout + 2)
            if res.returncode == 0 and res.stdout.strip():
                if "<!DOCTYPE" in res.stdout or "<html" in res.stdout:
                    # Rút gọn để debug xem WAF trả về lỗi gì
                    clean_err = re.sub(r'\s+', ' ', res.stdout[:250]).strip()
                    print(f"[Downloader] CDN trả về HTML thay vì M3U8 ({url}): {clean_err}")
                return res.stdout
        except Exception:
            pass

        # 2. Dự phòng bằng urllib nếu không có curl
        try:
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/122.0"}
            if "ffzy" in url:
                headers["Referer"] = "https://ffzy1.tv/"
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", errors="ignore")
        except Exception as e:
            print(f"[Downloader] Lỗi tải URL {url}: {e}")
            return ""

    @classmethod
    def resolve_stream_url(cls, m3u8_url: str) -> str:
        """Phân giải master playlist hoặc link share để lấy direct stream URL."""
        # 1. Nếu là link share (Liangzi), tải trang share để lấy link M3U8 có chữ ký (?sign=...)
        if "/share/" in m3u8_url:
            html = cls._fetch_text(m3u8_url, timeout=12)
            if html:
                m = re.search(r'var\s+main\s*=\s*["\']([^"\']+)["\']', html)
                if m:
                    main_path = m.group(1)
                    parsed = urllib.parse.urlparse(m3u8_url)
                    m3u8_url = f"{parsed.scheme}://{parsed.netloc}{main_path}"
                    print(f"[Downloader] Phân giải thành công link có chữ ký: {m3u8_url}")
            if "/share/" in m3u8_url:
                print(f"[Downloader] Link share không trích xuất được stream m3u8: {m3u8_url}")
                return ""

        if "/lzv" in m3u8_url and m3u8_url.endswith("/index.m3u8"):
            return m3u8_url.replace("/index.m3u8", "/2000k/hls/index.m3u8")

        try:
            text = cls._fetch_text(m3u8_url, timeout=12)
            if text:
                # Đảm bảo nội dung tải về là M3U8 hợp lệ, không phải trang lỗi HTML (403/404 block page)
                if "<!DOCTYPE" in text or "<html" in text or "<body" in text or not text.strip().startswith("#"):
                    print(f"[Downloader] Phản hồi không phải M3U8 hợp lệ (HTML error page): {m3u8_url}")
                    return m3u8_url

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
            content = cls._fetch_text(stream_url, timeout=12)
            if not content or "<!DOCTYPE" in content or "<html" in content or not content.strip().startswith("#"):
                print(f"[Downloader] Không tải được manifest hợp lệ từ stream {stream_url}. Sử dụng luồng trực tiếp.")
                return stream_url

            base_url = stream_url.rsplit("/", 1)[0]
            query = urllib.parse.urlparse(stream_url).query
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
                        if query and "?" not in seg_url:
                            seg_url = f"{seg_url}?{query}"

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
        if not raw_stream:
            print(f"[Downloader] Không thể phân giải URL stream: {m3u8_url}")
            return False
        clean_stream = cls.sanitize_m3u8_manifest(raw_stream, tmp_manifest)
        ok = cls.download_segment_to_mp4(clean_stream, output_path, max_seconds=max_duration_sec)
        if os.path.exists(tmp_manifest):
            try: os.remove(tmp_manifest)
            except: pass
        return ok

    @classmethod
    def reload_tor_circuit(cls) -> bool:
        """
        Tự động đổi mạch Tor mới (new circuit) khi gặp 403 Forbidden hoặc timeout CDN.
        Gửi lệnh:
            AUTHENTICATE ""\r\n
            SIGNAL NEWNYM\r\n
        trực tiếp qua socket 127.0.0.1:9051 (ControlPort), nhận phản hồi '250 OK'.
        Fallback sang sudo systemctl reload tor nếu socket thất bại.
        Chờ 4s sau khi gửi lệnh để Tor kịp thiết lập mạch thoát mới.
        """
        proxy = os.getenv("http_proxy") or os.getenv("HTTP_PROXY") or os.getenv("https_proxy") or os.getenv("HTTPS_PROXY")
        if not proxy:
            return False

        print("[Downloader] 🔄 Gửi tín hiệu đổi mạch Tor (SIGNAL NEWNYM qua ControlPort 127.0.0.1:9051)...")
        # 1. Thử gửi lệnh trực tiếp qua Tor ControlPort socket
        try:
            with socket.create_connection(("127.0.0.1", 9051), timeout=5) as s:
                s.sendall(b'AUTHENTICATE ""\r\n')
                auth_resp = s.recv(1024).decode("ascii", errors="ignore")
                if "250" in auth_resp:
                    s.sendall(b'SIGNAL NEWNYM\r\n')
                    sig_resp = s.recv(1024).decode("ascii", errors="ignore")
                    if "250" in sig_resp:
                        print("[Downloader] ✅ Tor ControlPort: Gửi SIGNAL NEWNYM thành công (250 OK). Chờ 4s để mạch Tor mới sẵn sàng...")
                        time.sleep(4)
                        return True
                    else:
                        print(f"[Downloader] ⚠️ Tor ControlPort SIGNAL không trả lời 250 OK: {sig_resp.strip()}")
                else:
                    print(f"[Downloader] ⚠️ Tor ControlPort AUTHENTICATE thất bại: {auth_resp.strip()}")
        except Exception as e:
            print(f"[Downloader] ⚠️ Không thể kết nối Tor ControlPort (127.0.0.1:9051): {e}")

        # 2. Fallback: sudo systemctl reload tor
        if os.name != "nt":
            print("[Downloader] 🔄 Fallback: Thử reload daemon Tor bằng sudo systemctl reload tor...")
            try:
                res = subprocess.run(["sudo", "systemctl", "reload", "tor"], capture_output=True, timeout=5)
                if res.returncode == 0:
                    print("[Downloader] ✅ Fallback sudo systemctl reload tor thành công. Chờ 4s...")
                    time.sleep(4)
                    return True
            except Exception:
                pass
            try:
                subprocess.run(["pkill", "-HUP", "tor"], capture_output=True, timeout=5)
                time.sleep(4)
                return True
            except Exception:
                pass

        return False

    @classmethod
    def _is_valid_ts_file(cls, path: Path) -> bool:
        """Kiểm tra phân đoạn .ts hợp lệ: tồn tại, kích thước > 1024 bytes và không phải HTML lỗi."""
        try:
            if not path.exists():
                return False
            if path.stat().st_size <= 1024:
                return False
            with open(path, "rb") as f:
                head = f.read(64).lower()
            if head.startswith(b"<html") or head.startswith(b"<!doctype"):
                return False
            return True
        except Exception:
            return False

    @classmethod
    def _parse_m3u8_segments(cls, stream_url: str, max_seconds: int = 7200) -> Tuple[List[Tuple[int, str, float]], str]:
        """
        Phân tích danh sách phân đoạn video .ts từ file manifest cục bộ hoặc URL m3u8.
        Tự động bóc tách master playlist thành sub-playlist và chuẩn hóa URL phân đoạn thành tuyệt đối.
        Trả về: (danh sách (index, absolute_url, duration), base_or_resolved_url)
        """
        content = ""
        base_url = stream_url if stream_url.startswith("http") else ""

        # 1. Đọc nội dung manifest
        if os.path.exists(stream_url):
            try:
                with open(stream_url, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
            except Exception as e:
                print(f"[Downloader] Lỗi đọc manifest cục bộ {stream_url}: {e}")
                return [], stream_url
        elif stream_url.startswith("http"):
            content = cls._fetch_text(stream_url, timeout=15)
            if not content:
                return [], stream_url
        else:
            return [], stream_url

        if not content.strip() or ("<!DOCTYPE" in content or "<html" in content or not content.strip().startswith("#")):
            return [], stream_url

        # 2. Xử lý Master Playlist nếu chứa link sub-playlist (.m3u8)
        if "#EXT-X-STREAM-INF" in content or (not "#EXTINF:" in content and ".m3u8" in content):
            for line in content.splitlines():
                line = line.strip()
                if line and not line.startswith("#") and ".m3u8" in line:
                    child_url = urllib.parse.urljoin(stream_url, line) if stream_url.startswith("http") else line
                    sub_content = cls._fetch_text(child_url, timeout=15)
                    if sub_content and "#EXTINF:" in sub_content:
                        content = sub_content
                        stream_url = child_url
                        base_url = child_url
                        break

        # 3. Phân tách từng phân đoạn video .ts
        segments: List[Tuple[int, str, float]] = []
        current_dur = 6.0
        accum_sec = 0.0
        query = urllib.parse.urlparse(stream_url).query if stream_url.startswith("http") else ""

        for line in content.splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("#EXTINF:"):
                m = re.search(r"#EXTINF:([0-9.]+)", line)
                if m:
                    try:
                        current_dur = float(m.group(1))
                    except ValueError:
                        current_dur = 6.0
            elif not line.startswith("#"):
                seg_url = line
                if not seg_url.startswith("http"):
                    if seg_url.startswith("/") and base_url:
                        parsed = urllib.parse.urlparse(base_url)
                        seg_url = f"{parsed.scheme}://{parsed.netloc}{seg_url}"
                    elif base_url:
                        seg_url = f"{base_url.rsplit('/', 1)[0]}/{seg_url}"
                if query and "?" not in seg_url:
                    seg_url = f"{seg_url}?{query}"

                segments.append((len(segments), seg_url, current_dur))
                accum_sec += current_dur
                if max_seconds and accum_sec >= max_seconds:
                    break

        return segments, stream_url

    @classmethod
    def _fallback_direct_download(cls, stream_url: str, output_path: str, max_seconds: int, base_proxy: Optional[str], is_ffzy: bool) -> bool:
        """Dự phòng tải trực tiếp bằng FFmpeg nếu không trích xuất được playlist phân đoạn."""
        cmd = ["ffmpeg", "-y"]
        if is_ffzy:
            cmd.extend(["-headers", "Referer: https://ffzy1.tv/\r\n"])
        if base_proxy and base_proxy.startswith("http://"):
            cmd.extend(["-http_proxy", base_proxy])
        cmd.extend([
            "-i", stream_url,
            "-t", str(max_seconds),
            "-c", "copy",
            "-bsf:a", "aac_adtstoasc",
            output_path
        ])
        run_env = os.environ.copy()
        if base_proxy:
            run_env["http_proxy"] = base_proxy
            run_env["https_proxy"] = base_proxy
        try:
            res = subprocess.run(cmd, env=run_env, capture_output=True, text=True, timeout=180)
            return res.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 1000000
        except Exception as e:
            print(f"[Downloader] Lỗi fallback FFmpeg: {e}")
            return False

    @classmethod
    def download_segment_to_mp4(cls, stream_url: str, output_path: str, max_seconds: int = 7200) -> bool:
        """
        Tải một luồng stream m3u8 thành file mp4 bằng cơ chế tải phân đoạn song song qua requests.Session,
        hỗ trợ tải nối tiếp (resume), ghi file nguyên tử (.ts.part -> .ts), chống dính Keep-Alive,
        tự động đổi mạch Tor qua ControlPort 9051 (tối đa 15 mạch, trần 1800s + tự gia hạn +300s khi tiến độ >= 85% & tốc độ > 250 KB/s),
        và ghép nối siêu tốc bằng FFmpeg.
        """
        t0 = time.time()
        tmp_dir = Path(output_path).parent
        tmp_dir.mkdir(parents=True, exist_ok=True)

        # 1. Phân tích danh sách phân đoạn
        segments, resolved_url = cls._parse_m3u8_segments(stream_url, max_seconds=max_seconds)
        total_segments = len(segments)

        base_proxy = os.getenv("http_proxy") or os.getenv("HTTP_PROXY") or os.getenv("https_proxy") or os.getenv("HTTPS_PROXY")
        is_ffzy = "ffzy" in stream_url or (total_segments > 0 and "ffzy" in segments[0][1])

        # Dự phòng: Nếu không trích xuất được segments (vd file mp4 trực tiếp hoặc link non-hls)
        if total_segments == 0:
            print(f"[Downloader] ⚠️ Không trích xuất được phân đoạn M3U8 từ: {stream_url}. Thử tải trực tiếp stream...")
            return cls._fallback_direct_download(stream_url, output_path, max_seconds, base_proxy, is_ffzy)

        # 2. Thư mục lưu phân đoạn tạm (Cố định theo file output để hỗ trợ Tải nối tiếp / Resume qua nhiều mạch Tor)
        seg_dir = tmp_dir / f"ts_parts_{Path(output_path).stem}"
        seg_dir.mkdir(parents=True, exist_ok=True)

        # Cấu hình chống rate-limit & xoay vòng
        max_circuits = 15
        base_timeout_sec = 1800.0   # Trần tải tổng cơ sở 1800s (30 phút)
        extension_sec = 300.0       # Gia hạn thông minh tối đa +300s (5 phút)
        extension_used = False
        overall_deadline = t0 + base_timeout_sec
        last_speed_kb_s = 0.0

        def _maybe_extend_deadline(now_ts: float, done_count: int, speed_kb_s: float) -> bool:
            nonlocal overall_deadline, extension_used
            if extension_used or done_count >= total_segments:
                return False
            if now_ts < (overall_deadline - 10.0):
                return False
            progress_ratio = done_count / max(total_segments, 1)
            if progress_ratio >= 0.85 and speed_kb_s > 250.0:
                extension_used = True
                overall_deadline += extension_sec
                print(
                    f"[Downloader] ⏱️ Gia hạn thông minh +{int(extension_sec)}s "
                    f"(tiến độ {progress_ratio*100:.1f}% >= 85%, tốc độ {speed_kb_s:.1f} KB/s > 250 KB/s) "
                    f"để hoàn tất {total_segments - done_count} mảnh cuối..."
                )
                return True
            return False

        proxy_desc = f"qua Tor ({base_proxy})" if base_proxy else "trực tiếp (không proxy)"
        print(f"[Downloader] 🚀 Khởi chạy tải phân đoạn an toàn ({total_segments} mảnh) {proxy_desc} [Max 15 mạch Tor | 4 threads | 1800s timeout (+300s gia hạn)]...")

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/122.0",
            "Accept": "*/*",
            "Connection": "keep-alive"
        }
        if is_ffzy:
            headers["Referer"] = "https://ffzy1.tv/"

        downloaded_files: Dict[int, Path] = {}
        circuits_run = 0

        for circuit_attempt in range(1, max_circuits + 1):
            circuits_run = circuit_attempt
            # Quét kiểm tra các file .ts hợp lệ đã có trong thư mục tạm (Resume)
            downloaded_files = {}
            for s in segments:
                dest = seg_dir / f"seg_{s[0]:05d}.ts"
                if cls._is_valid_ts_file(dest):
                    downloaded_files[s[0]] = dest

            if len(downloaded_files) == total_segments:
                print(f"[Downloader] 🎯 Đã có đủ 100% ({total_segments}/{total_segments}) phân đoạn hợp lệ.")
                break

            now_outer = time.time()
            _maybe_extend_deadline(now_outer, len(downloaded_files), last_speed_kb_s)
            if time.time() >= overall_deadline:
                total_limit = int(base_timeout_sec + (extension_sec if extension_used else 0))
                print(f"[Downloader] ⚠️ Đã chạm trần thời gian tối đa cho phép ({total_limit}s).")
                break

            # Nếu từ mạch 2 trở đi: Kích hoạt đổi IP Tor mới (reload_tor_circuit chờ 4s)
            if circuit_attempt > 1:
                print(f"[Downloader] 🧅 Mạch Tor #{circuit_attempt}/{max_circuits}: Đổi IP Tor mới (reload_tor_circuit)...")
                cls.reload_tor_circuit()

            missing_segments = [s for s in segments if s[0] not in downloaded_files]
            print(f"[Downloader] 📦 Mạch #{circuit_attempt}/{max_circuits}: Đã tích lũy {len(downloaded_files)}/{total_segments} mảnh. Bắt đầu tải tiếp {len(missing_segments)} mảnh còn thiếu (4 threads)...")

            # CHỐNG DÍNH KEEP-ALIVE: Khởi tạo lại Session mới hoàn toàn cho mỗi mạch
            session = requests.Session()
            retry_strategy = Retry(total=2, backoff_factor=0.3, status_forcelist=[500, 502, 503, 504], raise_on_status=False)
            adapter = HTTPAdapter(pool_connections=8, pool_maxsize=8, max_retries=retry_strategy)
            session.mount("http://", adapter)
            session.mount("https://", adapter)
            session.headers.update(headers)
            if base_proxy:
                session.proxies.update({"http": base_proxy, "https": base_proxy})

            abort_event = threading.Event()

            def download_one_segment(idx: int, seg_url: str) -> Tuple[int, Optional[Path], int, bool]:
                if abort_event.is_set():
                    return idx, None, 0, False
                dest = seg_dir / f"seg_{idx:05d}.ts"
                if cls._is_valid_ts_file(dest):
                    return idx, dest, dest.stat().st_size, True

                part_dest = seg_dir / f"seg_{idx:05d}.ts.part"
                # Thêm độ trễ ngẫu nhiên 0.10s - 0.18s giữa các request chống CDN rate-limit
                time.sleep(random.uniform(0.10, 0.18))

                for sub_try in range(2):
                    if abort_event.is_set():
                        return idx, None, 0, False
                    try:
                        resp = session.get(seg_url, timeout=15, stream=True)
                        if resp.status_code == 200:
                            ct = resp.headers.get("Content-Type", "").lower()
                            if "text/html" in ct:
                                return idx, None, 0, False

                            size = 0
                            with open(part_dest, "wb") as f:
                                for chunk in resp.iter_content(chunk_size=65536):
                                    if abort_event.is_set():
                                        f.close()
                                        part_dest.unlink(missing_ok=True)
                                        return idx, None, 0, False
                                    if chunk:
                                        f.write(chunk)
                                        size += len(chunk)

                            # Ghi file nguyên tử (Atomic Write): Kiểm tra HTTP 200, len > 1024, không bắt đầu bằng <html / <!DOCTYPE
                            if cls._is_valid_ts_file(part_dest):
                                os.replace(part_dest, dest)
                                return idx, dest, size, True
                            else:
                                part_dest.unlink(missing_ok=True)
                        elif resp.status_code in (403, 429):
                            # CDN đã chặn IP / rate limit
                            return idx, None, 0, False
                        elif resp.status_code == 404:
                            return idx, None, 0, False
                    except Exception:
                        if part_dest.exists():
                            part_dest.unlink(missing_ok=True)
                    if sub_try == 0 and not abort_event.is_set():
                        time.sleep(0.3)

                return idx, None, 0, False

            circuit_downloaded_count = 0
            circuit_downloaded_bytes = 0
            failed_count = 0
            consecutive_fails = 0
            t_circuit_start = time.time()
            last_log_time = t_circuit_start

            try:
                with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
                    futures = {executor.submit(download_one_segment, s[0], s[1]): s[0] for s in missing_segments}

                    for future in concurrent.futures.as_completed(futures):
                        idx, path, size, ok = future.result()
                        if ok and path:
                            circuit_downloaded_count += 1
                            circuit_downloaded_bytes += size
                            downloaded_files[idx] = path
                            consecutive_fails = 0
                        else:
                            failed_count += 1
                            consecutive_fails += 1

                        now = time.time()
                        elapsed = now - t_circuit_start
                        current_speed_kb_s = (circuit_downloaded_bytes / 1024.0) / max(elapsed, 0.01)
                        last_speed_kb_s = current_speed_kb_s

                        # Log tiến độ định kỳ mỗi 5s hoặc khi hoàn tất toàn bộ
                        if now - last_log_time >= 5.0 or len(downloaded_files) == total_segments:
                            last_log_time = now
                            pct = int((len(downloaded_files) / total_segments) * 100)
                            print(f"[Downloader] ⏳ Tiến độ: {len(downloaded_files)}/{total_segments} mảnh ({pct}%) | Mạch #{circuit_attempt}: +{circuit_downloaded_count} mảnh ({current_speed_kb_s:.1f} KB/s) | Lỗi: {failed_count}")

                        # Phát hiện CDN rate-limit qua chuỗi lỗi liên tiếp (>= 8 lỗi liên tiếp)
                        if consecutive_fails >= 8:
                            print(f"[Downloader] ⚠️ Dính {consecutive_fails} lỗi liên tiếp (CDN rate-limit IP hiện tại) -> Hủy mạch #{circuit_attempt}, đổi IP Tor mới...")
                            abort_event.set()
                            for f in futures:
                                f.cancel()
                            break

                        # Giám sát tốc độ & lỗi sau 15s đầu của mạch
                        if elapsed >= 15.0 and len(downloaded_files) < total_segments:
                            if (current_speed_kb_s < 100.0 and circuit_downloaded_count < 3) or (failed_count >= 10 and failed_count > circuit_downloaded_count * 2):
                                print(f"[Downloader] ⚠️ Mạch Tor #{circuit_attempt} chậm ({current_speed_kb_s:.1f} KB/s) hoặc nhiều lỗi ({failed_count} lỗi / {circuit_downloaded_count} thành công) -> Hủy mạch, đổi IP Tor mới...")
                                abort_event.set()
                                for f in futures:
                                    f.cancel()
                                break

                        _maybe_extend_deadline(now, len(downloaded_files), current_speed_kb_s)
                        if time.time() >= overall_deadline:
                            total_limit = int(base_timeout_sec + (extension_sec if extension_used else 0))
                            print(f"[Downloader] ⚠️ Chạm trần thời gian tối đa {total_limit}s. Hủy mạch...")
                            abort_event.set()
                            for f in futures:
                                f.cancel()
                            break
            finally:
                # CHỐNG DÍNH KEEP-ALIVE: Bắt buộc gọi session.close() sau mỗi mạch
                session.close()

            # Dọn sạch các file .part dang dở, GIỮ NGUYÊN toàn bộ các file .ts hợp lệ đã tải
            for p in seg_dir.glob("*.ts.part"):
                try: p.unlink()
                except: pass

            # Nếu đã có đủ toàn bộ phân đoạn, thoát vòng lặp mạch để ghép nối
            if len(downloaded_files) == total_segments:
                break

        # 3. Kiểm tra kết quả và ghép nối FFmpeg concat
        if len(downloaded_files) == total_segments:
            total_elapsed = time.time() - t0
            total_bytes = sum(f.stat().st_size for f in downloaded_files.values())
            total_mb = total_bytes / (1024 * 1024)
            avg_speed = (total_bytes / 1024) / max(total_elapsed, 0.01)
            print(f"[Downloader] ✅ Đã tải đủ 100% ({total_segments}/{total_segments}) phân đoạn ({total_mb:.1f} MB, trung bình {avg_speed:.1f} KB/s) trong {total_elapsed:.1f}s.")

            # Ghép nối siêu tốc nội bộ bằng FFmpeg concat
            concat_file = seg_dir / "segments.txt"
            with open(concat_file, "w", encoding="utf-8") as f:
                for i in range(total_segments):
                    f.write(f"file 'seg_{i:05d}.ts'\n")

            print(f"[Downloader] ⚡ Đang ghép nối {total_segments} phân đoạn bằng FFmpeg concat...")
            t_concat = time.time()
            concat_cmd = [
                "ffmpeg", "-y", "-f", "concat", "-safe", "0",
                "-i", str(concat_file),
                "-c", "copy",
                "-bsf:a", "aac_adtstoasc",
                output_path
            ]
            res = subprocess.run(concat_cmd, cwd=str(seg_dir), capture_output=True, text=True, timeout=300)
            concat_dur = time.time() - t_concat

            if res.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 1000000:
                out_mb = os.path.getsize(output_path) / (1024 * 1024)
                print(f"[Downloader] 🎉 Ghép nối FFmpeg hoàn tất trong {concat_dur:.1f}s: {output_path} ({out_mb:.1f} MB)")
                shutil.rmtree(seg_dir, ignore_errors=True)
                return True
            else:
                err = res.stderr[-300:] if res.stderr else "Unknown error"
                print(f"[Downloader] ❌ Lỗi FFmpeg concat: {err.strip()}")
                shutil.rmtree(seg_dir, ignore_errors=True)
                return False
        else:
            print(f"[Downloader] ❌ Thất bại: Chỉ tải được {len(downloaded_files)}/{total_segments} phân đoạn sau {circuits_run} mạch Tor.")
            shutil.rmtree(seg_dir, ignore_errors=True)
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
        # 1. Thử tải bằng curl
        try:
            curl_bin = "curl.exe" if os.name == "nt" else "curl"
            cmd = [curl_bin, "-s", "-L", "-m", "15", "-A", "Mozilla/5.0", "-o", output_path, poster_url]
            res = subprocess.run(cmd, capture_output=True, timeout=18)
            if res.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 1000:
                return True
        except Exception:
            pass

        # 2. Dự phòng bằng urllib
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
