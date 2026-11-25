import os
import re
import time
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
        Gửi tín hiệu SIGHUP tới daemon Tor trên Linux/GitHub Actions runner.
        """
        proxy = os.getenv("http_proxy") or os.getenv("HTTP_PROXY") or os.getenv("https_proxy") or os.getenv("HTTPS_PROXY")
        if not proxy:
            return False
        if os.name != "nt":
            print("[Downloader] 🔄 Phát hiện lỗi CDN/403/Timeout -> Gửi tín hiệu reload mạch Tor (pkill -HUP tor)...")
            try:
                subprocess.run(["pkill", "-HUP", "tor"], capture_output=True, timeout=5)
                time.sleep(2)
                print("[Downloader] ✅ Đã gửi lệnh reload mạch Tor thành công.")
                return True
            except Exception:
                try:
                    subprocess.run(["sudo", "systemctl", "reload", "tor"], capture_output=True, timeout=5)
                    time.sleep(2)
                    return True
                except Exception:
                    pass
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
        giám sát tốc độ thông minh (> 150 KB/s), tự động đổi mạch Tor khi bị bóp băng thông, và ghép nối siêu tốc bằng FFmpeg.
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

        # 2. Cấu hình tải phân đoạn song song qua requests.Session
        max_circuits = 3
        overall_deadline = time.time() + 600.0  # Trần tối đa 600s (10 phút)
        proxy_desc = f"qua Tor ({base_proxy})" if base_proxy else "trực tiếp (không proxy)"
        print(f"[Downloader] 🚀 Khởi chạy tải song song {total_segments} phân đoạn {proxy_desc} (6 threads)...")

        for circuit_attempt in range(1, max_circuits + 1):
            if time.time() >= overall_deadline:
                print("[Downloader] ⚠️ Đã chạm trần thời gian tối đa cho phép (600s).")
                break

            if circuit_attempt > 1:
                print(f"[Downloader] 🧅 Mạch Tor #{circuit_attempt}/{max_circuits}: Kích hoạt đổi IP Tor mới (reload_tor_circuit)...")
                cls.reload_tor_circuit()
                time.sleep(2)

            seg_dir = tmp_dir / f"ts_parts_{int(time.time()*1000)}"
            seg_dir.mkdir(parents=True, exist_ok=True)

            session = requests.Session()
            retry_strategy = Retry(total=2, backoff_factor=0.3, status_forcelist=[500, 502, 503, 504], raise_on_status=False)
            adapter = HTTPAdapter(pool_connections=12, pool_maxsize=12, max_retries=retry_strategy)
            session.mount("http://", adapter)
            session.mount("https://", adapter)

            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/122.0",
                "Accept": "*/*",
                "Connection": "keep-alive"
            }
            if is_ffzy:
                headers["Referer"] = "https://ffzy1.tv/"
            session.headers.update(headers)

            if base_proxy:
                session.proxies.update({"http": base_proxy, "https": base_proxy})

            abort_event = threading.Event()

            def download_one_segment(idx: int, seg_url: str) -> Tuple[int, Optional[Path], int, bool]:
                if abort_event.is_set():
                    return idx, None, 0, False
                dest = seg_dir / f"seg_{idx:05d}.ts"
                if dest.exists() and dest.stat().st_size > 500:
                    return idx, dest, dest.stat().st_size, True

                tmp_dest = dest.with_suffix(".tmp")
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
                            with open(tmp_dest, "wb") as f:
                                for chunk in resp.iter_content(chunk_size=65536):
                                    if abort_event.is_set():
                                        tmp_dest.unlink(missing_ok=True)
                                        return idx, None, 0, False
                                    if chunk:
                                        f.write(chunk)
                                        size += len(chunk)
                            if size > 500:
                                tmp_dest.replace(dest)
                                return idx, dest, size, True
                            else:
                                tmp_dest.unlink(missing_ok=True)
                        elif resp.status_code == 404:
                            return idx, None, 0, False
                    except Exception:
                        if tmp_dest.exists():
                            tmp_dest.unlink(missing_ok=True)
                    if sub_try == 0 and not abort_event.is_set():
                        time.sleep(0.5)

                return idx, None, 0, False

            downloaded_files: Dict[int, Path] = {}
            total_downloaded_bytes = 0
            completed_count = 0
            failed_count = 0
            circuit_aborted = False
            t_circuit_start = time.time()
            last_log_time = t_circuit_start

            with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
                futures = {executor.submit(download_one_segment, s[0], s[1]): s[0] for s in segments}

                for future in concurrent.futures.as_completed(futures):
                    if circuit_aborted:
                        break

                    idx, path, size, ok = future.result()
                    if ok and path:
                        completed_count += 1
                        total_downloaded_bytes += size
                        downloaded_files[idx] = path
                    else:
                        failed_count += 1

                    now = time.time()
                    elapsed = now - t_circuit_start

                    # Log tiến độ định kỳ mỗi 5s hoặc khi hoàn tất
                    if now - last_log_time >= 5.0 or completed_count == total_segments:
                        last_log_time = now
                        speed_kb_s = (total_downloaded_bytes / 1024) / max(elapsed, 0.01)
                        pct = int((completed_count / total_segments) * 100)
                        print(f"[Downloader] ⏳ Tiến độ: {completed_count}/{total_segments} mảnh ({pct}%) | Tốc độ: {speed_kb_s:.1f} KB/s | Lỗi: {failed_count} (Mạch #{circuit_attempt})")

                    # Giám sát tốc độ & lỗi sau 20s đầu tiên
                    if elapsed >= 20.0 and completed_count < total_segments:
                        speed_kb_s = (total_downloaded_bytes / 1024) / elapsed
                        if speed_kb_s < 150.0 or (failed_count > 5 and failed_count >= completed_count):
                            print(f"[Downloader] ⚠️ Tốc độ qua Tor quá chậm ({speed_kb_s:.1f} KB/s < 150 KB/s) hoặc nhiều lỗi ({failed_count} lỗi / {completed_count} thành công) sau {elapsed:.1f}s -> Hủy mạch, đổi IP Tor mới...")
                            abort_event.set()
                            circuit_aborted = True
                            for f in futures:
                                f.cancel()
                            break

                    if time.time() >= overall_deadline:
                        print("[Downloader] ⚠️ Chạm ngưỡng thời gian 600s. Hủy mạch...")
                        abort_event.set()
                        circuit_aborted = True
                        for f in futures:
                            f.cancel()
                        break

            session.close()

            if circuit_aborted:
                shutil.rmtree(seg_dir, ignore_errors=True)
                if circuit_attempt < max_circuits and time.time() < overall_deadline:
                    time.sleep(2)
                    continue
                else:
                    return False

            # Kiểm tra nếu đủ toàn bộ phân đoạn
            if len(downloaded_files) == total_segments:
                total_elapsed = time.time() - t_circuit_start
                total_mb = total_downloaded_bytes / (1024 * 1024)
                avg_speed = (total_downloaded_bytes / 1024) / max(total_elapsed, 0.01)
                print(f"[Downloader] ✅ Đã tải đủ {total_segments}/{total_segments} phân đoạn ({total_mb:.1f} MB, {avg_speed:.1f} KB/s) trong {total_elapsed:.1f}s.")

                # Ghép nối siêu tốc nội bộ bằng FFmpeg concat (2 giây)
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
                res = subprocess.run(concat_cmd, cwd=str(seg_dir), capture_output=True, text=True, timeout=120)
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
                print(f"[Downloader] ⚠️ Thiếu phân đoạn ({len(downloaded_files)}/{total_segments}). Thử lại mạch khác...")
                shutil.rmtree(seg_dir, ignore_errors=True)
                if circuit_attempt < max_circuits and time.time() < overall_deadline:
                    time.sleep(2)
                    continue

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
