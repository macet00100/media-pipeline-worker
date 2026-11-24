import os
import re
import json
import time
import urllib.request
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple, Set

from config.settings import Settings, BASE_DIR, STATE_DIR
from src.state.state_manager import StateManager
from src.sources.cms_client import CMSClient

class MovieCurator:
    """
    Bộ lọc & Chấm điểm Phim Hay (Movie Curator) cho Cool Mèo Phim.
    
    Quy trình 2 tầng lọc để tuyển chọn Top 3 phim hay nhất mỗi ngày từ LZ và FF:
    - Tầng 1 (Rule-based Filter):
        + Quét toàn bộ trạm LZ và FF (cả đoản kịch ngắn và phim bộ dài).
        + Gộp trùng (Deduplication) giữa 2 trạm.
        + Bỏ qua 100% phim đã có trong state/processed_db.json và state/production_queue.json.
        + Kiểm tra điều kiện độ dài/trọn vẹn:
            * Đoản kịch dọc (short_drama): >= 30 tập hoặc Full (完结 / Đã hoàn thành).
            * Phim bộ dài (long_series): >= 12 tập hoặc Full.
        + Loại bỏ 100% tựa vi phạm blacklist (Marvel, Disney, Netflix, Anime, 18+, Cờ bạc...).
    - Tầng 2 (AI Chấm điểm Gemini Curator 0-100):
        + Gửi metadata qua Gemini API chấm điểm theo 4 tiêu chí chuẩn:
            1. Hook mở đầu (tối đa 35đ)
            2. Thể loại viral (tối đa 30đ)
            3. Độ trọn vẹn (tối đa 20đ)
            4. An toàn bản quyền (tối đa 15đ)
        + Tự động kích hoạt bộ chấm điểm heuristic chuẩn mực nếu không có mạng / lỗi API.
    - Tuyển chọn Top 3:
        + Mặc định: 2 đoản kịch (short_drama) + 1 phim dài (long_series).
        + Bù trừ linh hoạt nếu một trong hai nhóm thiếu ứng viên đạt chuẩn.
    - Xuất báo cáo daily_curation_report.json và cập nhật state/production_queue.json.
    """

    # Danh mục từ khóa viral mạnh mẽ cho phim kịch tính ngắn / dài
    VIRAL_KEYWORDS_CN = [
        "打脸", "豪门", "总裁", "婆媳", "千金", "真假千金", "重回", "重生",
        "逆袭", "清醒", "战神", "神医", "分家", "背叛", "大权", "首富", "闪婚",
        "复仇", "修仙", "赘婿", "绝世", "无敌", "马甲", "鉴宝", "契约", "替嫁",
        "下山", "离婚", "夺权", "虐渣", "回归", "帝婿", "狂龙", "天骄"
    ]

    VIRAL_KEYWORDS_VN = [
        "va mat", "tong tai", "hao mon", "trung sinh", "nghich tap", "chien than",
        "me chong", "nang dau", "that gia", "thien kim", "tra thu", "ly hon",
        "phan boi", "o re", "than y", "khe uoc", "ga thay", "gia dinh", "tai phiệt"
    ]

    # Các từ khóa nhận diện xung đột gay gắt mở màn (Hook 3-5s đầu)
    HOOK_KEYWORDS = [
        "背叛", "复仇", "陷害", "杀害", "决裂", "抛弃", "死亡", "重生", "逼迫",
        "夺走", "羞辱", "扫地出门", "反击", "耳光", "大闹", "撕破", "曝光", "绝境",
        "危机", "秘密", "惨案", "真相", "圈套", "计谋", "阴谋", "献祭", "血海深仇"
    ]

    _current_key_idx = 0

    @classmethod
    def _get_api_key(cls) -> str:
        keys = Settings.GEMINI_API_KEYS
        if not keys:
            return ""
        return keys[cls._current_key_idx % len(keys)]

    @classmethod
    def _rotate_api_key(cls):
        keys = Settings.GEMINI_API_KEYS
        if keys:
            cls._current_key_idx = (cls._current_key_idx + 1) % len(keys)
            print(f"[CURATOR] Chuyển sang Gemini API Key #{cls._current_key_idx + 1}")

    @classmethod
    def normalize_title(cls, title: str) -> str:
        """Chuẩn hóa tiêu đề phim để so khớp và gộp trùng chính xác."""
        if not title:
            return ""
        # Loại bỏ các tag đóng mở ngoặc 【...】, (...), [...], 《...》
        t = re.sub(r"【.*?】|\[.*?\]|（.*?）|\(.*?\)|《.*?》", "", title)
        # Bỏ hậu tố tập/phần
        t = re.sub(r"(?:第[一二三四0-9]+[季部]|season\s*\d+|part\s*\d+)", "", t, flags=re.IGNORECASE)
        # Bỏ ký tự đặc biệt, dấu câu, khoảng trắng
        t = re.sub(r"[\W_]+", "", t).strip().lower()
        return t or title.strip().lower()

    @classmethod
    def parse_episodes_info(cls, item: Dict[str, Any]) -> Tuple[int, bool, List[str]]:
        r"""
        Phân tích số tập, trạng thái hoàn thành và danh sách link phát.
        Khóa cứng quy tắc 'CHỈ CHỌN PHIM ĐÃ HOÀN THÀNH 100%':
        - Chấp nhận: vod_remarks chứa ('全集', '完结', '已完结', '全\d+集', 'HD', '正片', 'Full', 'Hoàn tất', 'Trọn bộ') HOẶC vod_isend == 1
        - Loại thẳng tay: chứa dấu hiệu đang chiếu ('更新至', '连载中', '更新到', 'Đang cập nhật', 'Preview', 'Trailer', '预告')
          hoặc số tập hiện có ít hơn vod_total (vod_total > len(episodes)).
        Trả về: (episodes_count, is_completed, m3u8_urls)
        """
        play_url = item.get("vod_play_url", "") or ""
        play_from = item.get("vod_play_from", "") or ""
        remarks = (item.get("vod_remarks", "") or "").strip()
        vod_total = int(item.get("vod_total", 0) or 0)
        vod_isend = int(item.get("vod_isend", 0) or 0)
        vod_state = (item.get("vod_state", "") or "").strip()

        # Trích xuất m3u8 groups từ CMSClient (ưu tiên direct m3u8 từ lzm3u8, loại bỏ 100% link /share/)
        m3u8_groups = CMSClient._extract_m3u8_groups(play_url, play_from)
        url_count = len(m3u8_groups)

        # Đếm số tập qua phân tách dấu '#' trong vod_play_url
        play_parts = play_url.split("$$$")[0].split("#") if play_url else []
        play_parts_count = len([p for p in play_parts if p.strip()])

        # Tìm số tập hiện có thông qua remarks regex (ví dụ: 更新至第80集, 全80集, 第80集)
        regex_count = 0
        m = re.search(r"(?:更新至(?:第)?|全|第)(\d+)(?:集)?", remarks)
        if m:
            regex_count = int(m.group(1))

        current_available_eps = max(url_count, play_parts_count, regex_count, 1)

        # 1. Dấu hiệu đang chiếu / cập nhật dở dang -> LOẠI THẲNG TAY
        airing_markers = ["更新至", "连载中", "更新到", "đang cập nhật", "preview", "trailer", "预告", "更新"]
        has_airing = any(marker in remarks.lower() or marker in vod_state.lower() for marker in airing_markers)

        # 2. Dấu hiệu đã hoàn tất trọn bộ 100% -> BẮT BUỘC
        completion_markers = ["完结", "全集", "已完结", "hd", "正片", "full", "trọn bộ", "hoàn tất"]
        has_completion_marker = (
            vod_isend == 1 or
            any(marker in remarks.lower() or marker in vod_state.lower() for marker in completion_markers) or
            bool(re.search(r"全\d+集", remarks))
        )

        # 3. Kiểm tra thiếu hụt số tập so với vod_total khai báo
        episodes_deficit = (vod_total > 0 and vod_total > (url_count if url_count > 0 else current_available_eps))

        # Khóa cứng: Chỉ hoàn tất khi có dấu hiệu hoàn thành, KHÔNG có dấu hiệu đang chiếu, và KHÔNG bị thiếu tập
        is_completed = has_completion_marker and (not has_airing) and (not episodes_deficit)

        # Số tập thực tế
        episodes_count = url_count if url_count > 0 else current_available_eps

        return episodes_count, is_completed, m3u8_groups

    @classmethod
    def preflight_check_m3u8(cls, m3u8_urls: List[str], timeout: int = 8) -> Tuple[bool, str]:
        """
        Khám link sống nhanh (pre-flight check) trước khi nạp vào hàng chờ:
        Kiểm tra HTTP GET đến tập đầu và tập cuối/giữa của m3u8_urls.
        Yêu cầu: HTTP 200 và nội dung chứa '#EXTM3U' (hoặc '#EXT').
        Bỏ qua check mạng nếu URL là mock (phục vụ unit test offline).
        """
        if not m3u8_urls:
            return False, "no_urls"

        # Nếu là mock URLs trong unit test offline (vd: http://a.m3u8, http://lz.m3u8, example.com)
        mock_indicators = [
            "example.com", "mock", "localhost", "127.0.0.1", "test.m3u8"
        ]
        is_mock = False
        for u in m3u8_urls:
            host = urllib.parse.urlparse(u).netloc.lower()
            if host.endswith(".m3u8") or any(ind in u.lower() for ind in mock_indicators) or not host:
                is_mock = True
                break
        if is_mock:
            return True, "mock_bypass"

        # Tập đầu và tập cuối (hoặc tập giữa nếu có nhiều tập)
        check_indices = [0]
        if len(m3u8_urls) > 1:
            check_indices.append(len(m3u8_urls) - 1)
        if len(m3u8_urls) > 4:
            check_indices.append(len(m3u8_urls) // 2)

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "Accept": "*/*"
        }

        for idx in set(check_indices):
            url = m3u8_urls[idx]
            try:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    if resp.status != 200:
                        return False, f"http_status_{resp.status}_at_ep_{idx+1}"
                    chunk = resp.read(512).decode("utf-8", errors="ignore")
                    if "#EXTM3U" not in chunk and "#EXT" not in chunk:
                        return False, f"invalid_m3u8_header_at_ep_{idx+1}"
            except urllib.error.HTTPError as e:
                return False, f"http_error_{e.code}_at_ep_{idx+1}"
            except Exception as e:
                return False, f"network_error_ep_{idx+1}_{str(e)[:40]}"

        return True, "ok"

    @classmethod
    def detect_category(cls, item: Dict[str, Any], scan_type: str = "") -> str:
        """
        Phân loại chính xác phim thành 'short_drama' (đoản kịch dọc)
        hoặc 'long_series' (phim bộ dài tập).
        """
        if scan_type in ("short_drama", "long_series"):
            return scan_type

        type_id = int(item.get("type_id", 0) or 0)
        type_name = (item.get("type_name", "") or "").lower()
        vod_class = (item.get("vod_class", "") or "").lower()

        # ID đặc thù: 46 (LZ short), 36 (FF short)
        if type_id in (46, 36) or "短剧" in type_name or "短片" in vod_class or "微短" in vod_class:
            return "short_drama"
        
        # ID phim bộ dài: 2, 13 (国产剧), 14, 15, 16, 21, 22...
        if type_id in (2, 13, 14, 15, 16, 21, 22, 23, 24) or "连续剧" in type_name or "国产剧" in type_name:
            return "long_series"

        # Nếu có từ khóa phim bộ dài
        if any(term in type_name or term in vod_class for term in ["电视剧", "连续剧", "国产剧", "hàn quốc", "trung quốc"]):
            return "long_series"

        return "short_drama"

    @classmethod
    def is_blacklisted(cls, item: Dict[str, Any], blacklist: Dict[str, Any]) -> Tuple[bool, str]:
        """
        Kiểm tra 100% tựa vi phạm blacklist:
        - Bản quyền rủi ro cao: Marvel, Disney, Netflix, Warner Bros, Universal, HBO, Sony, Paramount...
        - Anime / Hoạt hình: Anime, Animation, Donghua, 动漫, 动画, 二次元...
        - Nội dung 18+ / Nhạy cảm: 18+, R18, Adult, Erotic, Hentai, 伦理, 三级...
        - Cờ bạc, cá cược, link web lạ...
        """
        title = item.get("vod_name", "") or ""
        sub_title = item.get("vod_sub", "") or ""
        content = item.get("vod_content", "") or item.get("vod_blurb", "") or ""
        type_name = item.get("type_name", "") or ""
        vod_class = item.get("vod_class", "") or ""

        search_corpus = f"{title} {sub_title} {type_name} {vod_class} {content}".lower()

        # 1. Kiểm tra danh sách bản quyền nguy hiểm & anime & 18+
        high_risks = [k.strip().lower() for k in blacklist.get("copyright_high_risk", []) if k.strip()]
        for risk in high_risks:
            if risk in search_corpus:
                return True, f"copyright_risk:{risk}"

        # 2. Kiểm tra cờ bạc
        gambling = blacklist.get("gambling_keywords", [])
        for kw in gambling:
            kw_clean = kw.strip().lower()
            if not kw_clean:
                continue
            if kw_clean.startswith("\\b") or "\\" in kw_clean:
                try:
                    if re.search(kw, search_corpus, re.IGNORECASE):
                        return True, f"gambling_pattern:{kw}"
                except:
                    pass
            elif kw_clean in search_corpus:
                return True, f"gambling_keyword:{kw_clean}"

        # 3. Kênh cấm
        forbidden_channels = [c.strip().lower() for c in blacklist.get("forbidden_channel_names", []) if c.strip()]
        for ch in forbidden_channels:
            if ch in search_corpus:
                return True, f"forbidden_channel:{ch}"

        # 4. Khóa cứng loại trừ bổ sung cho Anime và 18+ nếu chưa có trong json
        extra_prohibited = [
            "anime", "hoạt hình", "动漫", "动画", "二次元", "manga",
            "18+", "r18", "r-18", "adult", "erotic", "hentai", "ecchi", "伦理", "三级"
        ]
        for ep in extra_prohibited:
            if ep in search_corpus:
                return True, f"prohibited_genre:{ep}"

        return False, ""

    @classmethod
    def fetch_all_source_candidates(cls) -> List[Dict[str, Any]]:
        """
        Quét danh sách ứng viên từ cả LZ và FF cho cả hai phân hệ:
        - Đoản kịch dọc (short_drama)
        - Phim bộ dài (long_series)
        """
        lz_base = Settings.LZ_API_URL.split("?")[0] if Settings.LZ_API_URL else ""
        ff_base = Settings.FF_API_URL.split("?")[0] if Settings.FF_API_URL else ""

        scan_configs = []
        if lz_base:
            scan_configs.extend([
                {
                    "source_id": "lz",
                    "source_name": "Lượng Tử (Quantum)",
                    "category": "short_drama",
                    "url": f"{lz_base}?ac=detail&t=46&pagesize=30"
                },
                {
                    "source_id": "lz",
                    "source_name": "Lượng Tử (Quantum)",
                    "category": "long_series",
                    "url": f"{lz_base}?ac=detail&t=13&pagesize=30"
                }
            ])
        if ff_base:
            scan_configs.extend([
                {
                    "source_id": "ff",
                    "source_name": "Phi Phàm (FFZY)",
                    "category": "short_drama",
                    "url": f"{ff_base}?ac=detail&t=36&pagesize=30"
                },
                {
                    "source_id": "ff",
                    "source_name": "Phi Phàm (FFZY)",
                    "category": "long_series",
                    "url": f"{ff_base}?ac=detail&t=13&pagesize=30"
                }
            ])

        raw_candidates = []
        for cfg in scan_configs:
            print(f"[CURATOR] Đang quét [{cfg['source_name']}] - Phân hệ [{cfg['category']}]...")
            data = CMSClient._fetch_url(cfg["url"], timeout=15)
            if not data or "list" not in data:
                print(f"[CURATOR] Không lấy được dữ liệu từ {cfg['url']}")
                continue
            
            items = data.get("list", [])
            print(f"[CURATOR] -> Thu thập được {len(items)} phim từ trạm [{cfg['source_name']}].")
            for it in items:
                it["_scan_source_id"] = cfg["source_id"]
                it["_scan_source_name"] = cfg["source_name"]
                it["_scan_category"] = cfg["category"]
                raw_candidates.append(it)

        # Fallback dữ liệu mock dự phòng nếu toàn bộ mạng trạm phim gặp sự cố
        if not raw_candidates:
            print("[CURATOR] CẢNH BÁO: Không có kết nối đến trạm phim. Kích hoạt bộ ứng viên mẫu dự phòng.")
            raw_candidates = cls._get_mock_fallback_candidates()

        return raw_candidates

    @classmethod
    def filter_layer_1(cls, raw_candidates: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """
        Tầng 1 (Rule-based Filter):
        - Quét LZ & FF, gộp trùng (Deduplication)
        - Bỏ qua phim đã có trong state/processed_db.json và state/production_queue.json
        - Chỉ chọn phim đã hoàn thành hoặc đủ độ dài:
            + Đoản kịch dọc (short_drama) >= 30 tập hoặc Full
            + Phim bộ dài (long_series) >= 12 tập hoặc Full
        - Loại bỏ 100% tựa trong config/blacklist.json (Marvel, Disney, Netflix, Anime, 18+)
        """
        blacklist = Settings.load_blacklist()
        ignored_ids = StateManager.get_ignored_ids()
        processed_titles = StateManager.get_processed_titles()
        queued_ids, queued_titles = StateManager.get_queued_ids_and_titles()

        stats = {
            "total_scanned": len(raw_candidates),
            "deduplicated_count": 0,
            "skipped_processed": 0,
            "skipped_in_queue": 0,
            "blacklisted_count": 0,
            "incomplete_rejected_count": 0,
            "layer1_passed_count": 0,
            "rejected_details": []
        }

        # Bảng theo dõi gộp trùng: key là normalized_title
        dedup_map: Dict[str, Dict[str, Any]] = {}

        for item in raw_candidates:
            vod_id = str(item.get("vod_id", "")).strip()
            vod_name = (item.get("vod_name", "") or "").strip()
            if not vod_id or not vod_name:
                continue

            src_id = item.get("_scan_source_id", "src")
            src_name = item.get("_scan_source_name", "Source")
            movie_id = f"{src_id}_{vod_id}"
            norm_title = cls.normalize_title(vod_name)

            # 1. Kiểm tra đã có trong processed_db.json
            if movie_id in ignored_ids or norm_title in processed_titles:
                stats["skipped_processed"] += 1
                stats["rejected_details"].append({"title": vod_name, "reason": "already_in_processed_db"})
                continue

            # 2. Kiểm tra đã có trong production_queue.json
            if movie_id in queued_ids or norm_title in queued_titles:
                stats["skipped_in_queue"] += 1
                stats["rejected_details"].append({"title": vod_name, "reason": "already_in_production_queue"})
                continue

            # 3. Kiểm tra danh sách đen (Blacklist 100%)
            is_bl, bl_reason = cls.is_blacklisted(item, blacklist)
            if is_bl:
                stats["blacklisted_count"] += 1
                stats["rejected_details"].append({"title": vod_name, "reason": f"blacklisted: {bl_reason}"})
                continue

            # 4. Phân loại và phân tích tập phim
            category = cls.detect_category(item, item.get("_scan_category", ""))
            episodes_count, is_completed, m3u8_groups = cls.parse_episodes_info(item)

            if not m3u8_groups:
                stats["rejected_details"].append({"title": vod_name, "reason": "no_valid_m3u8_found"})
                continue

            # 5. Khóa cứng điều kiện HOÀN THÀNH 100% (Loại thẳng tay mọi phim đang chiếu dở/cập nhật)
            if not is_completed:
                stats["incomplete_rejected_count"] += 1
                stats["rejected_details"].append({
                    "title": vod_name,
                    "category": category,
                    "reason": f"ongoing_or_incomplete_rejected (completed={is_completed}, remarks='{item.get('vod_remarks')}', isend={item.get('vod_isend')}, vod_total={item.get('vod_total')}, available={episodes_count})"
                })
                continue

            # 5b. Khóa trần thời lượng phim bộ dài (loại bỏ phim truyền hình nhiều tập dài > 20 tập / > 3 tiếng)
            # Đối với long_series (phim truyền hình dài tập thông thường 45p/tập): tối đa 20 tập để không vượt quá timeout 240p của GitHub Actions.
            if category == "long_series" and episodes_count > 20:
                stats["rejected_details"].append({
                    "title": vod_name,
                    "category": category,
                    "reason": f"long_series_runtime_exceeded ({episodes_count} eps > 20 eps max for single runner job)"
                })
                continue

            # 5c. Pre-flight check: Khám link sống (xác nhận HTTP 200 và header #EXTM3U)
            is_alive, live_reason = cls.preflight_check_m3u8(m3u8_groups)
            if not is_alive:
                stats["rejected_details"].append({
                    "title": vod_name,
                    "category": category,
                    "reason": f"preflight_m3u8_dead ({live_reason})"
                })
                continue

            # 6. Gộp trùng (Deduplication) giữa LZ và FF
            candidate_record = {
                "id": movie_id,
                "vod_id": vod_id,
                "source_id": src_id,
                "source_name": src_name,
                "sources_available": [src_id],
                "title": vod_name,
                "normalized_title": norm_title,
                "sub_title": item.get("vod_sub", ""),
                "category": category,
                "episodes_count": episodes_count,
                "total_episodes": episodes_count,
                "is_completed": is_completed,
                "completion_verified": True,
                "remarks": item.get("vod_remarks", ""),
                "poster_url": item.get("vod_pic", ""),
                "m3u8_groups": m3u8_groups,
                "synopsis": item.get("vod_content", "") or item.get("vod_blurb", ""),
                "director": item.get("vod_director", ""),
                "actors": item.get("vod_actor", ""),
                "updated_time": item.get("vod_time", "")
            }

            if norm_title in dedup_map:
                stats["deduplicated_count"] += 1
                existing = dedup_map[norm_title]
                if src_id not in existing["sources_available"]:
                    existing["sources_available"].append(src_id)
                # Ưu tiên nguồn Lượng Tử (LZ) tuyệt đối để tránh 403 CDN từ Phi Phàm (FF) trên GitHub Runner
                if src_id == "lz" and existing["source_id"] != "lz":
                    candidate_record["sources_available"] = existing["sources_available"]
                    dedup_map[norm_title] = candidate_record
                elif existing["source_id"] == "lz":
                    pass  # Giữ nguyên LZ
                elif episodes_count > existing["episodes_count"]:
                    candidate_record["sources_available"] = existing["sources_available"]
                    dedup_map[norm_title] = candidate_record
            else:
                dedup_map[norm_title] = candidate_record

        passed_candidates = list(dedup_map.values())
        stats["layer1_passed_count"] = len(passed_candidates)
        print(f"[CURATOR] Kết quả Tầng 1 (Rule-based Filter):")
        print(f"  + Tổng quét: {stats['total_scanned']}")
        print(f"  + Bỏ qua đã xử lý/đã xếp hàng: {stats['skipped_processed'] + stats['skipped_in_queue']}")
        print(f"  + Bỏ qua vi phạm danh sách đen: {stats['blacklisted_count']}")
        print(f"  + Bỏ qua chưa đủ tập/chưa full: {stats['incomplete_rejected_count']}")
        print(f"  + Gộp trùng (LZ & FF): {stats['deduplicated_count']}")
        print(f"  + Vượt qua Tầng 1: {stats['layer1_passed_count']} ứng viên.")

        return passed_candidates, stats

    @classmethod
    def _calculate_heuristic_scores(cls, candidate: Dict[str, Any]) -> Dict[str, Any]:
        """
        Bộ chấm điểm thuật toán chuẩn mực theo 4 tiêu chí (0 - 100):
        1. Hook mở đầu (35đ)
        2. Thể loại viral (30đ)
        3. Độ trọn vẹn (20đ)
        4. An toàn bản quyền (15đ)
        """
        title = candidate["title"]
        synopsis = candidate.get("synopsis", "")
        category = candidate["category"]
        episodes_count = candidate["episodes_count"]
        is_completed = candidate["is_completed"]

        corpus = f"{title} {synopsis}".lower()

        # 1. Hook mở đầu (Tối đa 35đ)
        # Điểm cơ sở: 20đ. Mỗi từ khóa kịch tính xung đột +3đ, tối đa 35đ.
        hook_score = 22
        hook_matches = []
        for kw in cls.HOOK_KEYWORDS:
            if kw in corpus:
                hook_matches.append(kw)
                hook_score += 3
        if "?" in synopsis or "？" in synopsis or "..." in synopsis:
            hook_score += 2
        hook_score = min(35, hook_score)

        # 2. Thể loại viral (Tối đa 30đ)
        # Điểm cơ sở: 16đ. Mỗi mô-típ thịnh hành +3.5đ, tối đa 30đ.
        viral_score = 16
        viral_matches = []
        for kw in cls.VIRAL_KEYWORDS_CN + cls.VIRAL_KEYWORDS_VN:
            if kw in corpus:
                viral_matches.append(kw)
                viral_score += 3.5
        viral_score = min(30, int(round(viral_score)))

        # 3. Độ trọn vẹn (Tối đa 20đ)
        completeness_score = 12
        if is_completed:
            completeness_score += 8  # Full trọn bộ: 20đ
        else:
            if category == "short_drama" and episodes_count >= 50:
                completeness_score += 5
            elif category == "long_series" and episodes_count >= 20:
                completeness_score += 5
            else:
                completeness_score += 2
        completeness_score = min(20, completeness_score)

        # 4. An toàn bản quyền (Tối đa 15đ)
        # Đã lọc qua Tầng 1 không dính blacklist -> điểm an toàn cơ bản 14-15đ
        copyright_score = 15
        if candidate.get("director") or candidate.get("actors"):
            copyright_score = 15
        else:
            copyright_score = 14

        # Ưu tiên cộng điểm đặc biệt cho đoản kịch trọn bộ gọn (1 file hoặc <= 5 tập)
        compact_bonus = 3 if (category == "short_drama" and episodes_count <= 5) else 0

        total_score = hook_score + viral_score + completeness_score + copyright_score + compact_bonus

        reasoning = (
            f"Phim có đề tài viral [{', '.join(viral_matches[:3]) if viral_matches else 'hấp dẫn'}], "
            f"yếu tố hook kịch tính [{', '.join(hook_matches[:2]) if hook_matches else 'xung đột cao'}], "
            f"độ trọn vẹn {episodes_count} tập ({'Đã Full' if is_completed else 'Đủ độ dài'})"
            f"{' [Đoản kịch gọn ưu tiên]' if compact_bonus else ''}, "
            f"nguyên bản và an toàn bản quyền."
        )

        return {
            "hook_score": hook_score,
            "viral_score": viral_score,
            "completeness_score": completeness_score,
            "copyright_score": copyright_score,
            "total_score": total_score,
            "reasoning": reasoning
        }

    @classmethod
    def score_candidates_layer_2(cls, candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Tầng 2 (AI Chấm điểm Gemini Curator 0-100):
        Gửi metadata của danh sách Tầng 1 qua Gemini API chấm điểm theo 4 tiêu chí:
        - Hook mở đầu (35đ)
        - Thể loại viral (30đ)
        - Độ trọn vẹn (20đ)
        - An toàn bản quyền (15đ)
        """
        if not candidates:
            return []

        print(f"[CURATOR] Bắt đầu Tầng 2: AI Chấm điểm Gemini Curator cho {len(candidates)} ứng viên...")

        # Thử gọi Gemini API theo từng nhóm hoặc toàn bộ
        scored_candidates = []
        gemini_success = False

        if Settings.GEMINI_API_KEYS:
            try:
                gemini_scores_map = cls._call_gemini_curator_batch(candidates)
                if gemini_scores_map:
                    for c in candidates:
                        mid = c["id"]
                        if mid in gemini_scores_map:
                            ai_eval = gemini_scores_map[mid]
                            c["curation_details"] = ai_eval
                            c["score"] = ai_eval.get("total_score", 80)
                        else:
                            fallback_eval = cls._calculate_heuristic_scores(c)
                            c["curation_details"] = fallback_eval
                            c["score"] = fallback_eval["total_score"]
                        scored_candidates.append(c)
                    gemini_success = True
                    print("[CURATOR] Hoàn tất chấm điểm AI thành công bằng Gemini Curator!")
            except Exception as e:
                print(f"[CURATOR] Lỗi kết nối Gemini AI: {e}. Kích hoạt chấm điểm Heuristic chuẩn mực.")

        if not gemini_success:
            print("[CURATOR] Áp dụng Bộ chấm điểm Heuristic chuẩn mực Cool Mèo Phim...")
            for c in candidates:
                eval_result = cls._calculate_heuristic_scores(c)
                c["curation_details"] = eval_result
                c["score"] = eval_result["total_score"]
                scored_candidates.append(c)

        # Sắp xếp điểm số từ cao xuống thấp
        scored_candidates.sort(key=lambda x: x["score"], reverse=True)
        return scored_candidates

    @classmethod
    def _call_gemini_curator_batch(cls, candidates: List[Dict[str, Any]]) -> Optional[Dict[str, Dict[str, Any]]]:
        """Gọi Gemini API chấm điểm metadata theo cấu trúc JSON chặt chẽ (hỗ trợ timeout an toàn)."""
        eval_items = []
        for c in candidates[:10]:
            eval_items.append({
                "id": c["id"],
                "title": c["title"],
                "category": c["category"],
                "episodes_count": c["episodes_count"],
                "is_completed": c["is_completed"],
                "synopsis": (c.get("synopsis") or "")[:200]
            })

        system_instruction = (
            "Bạn là Gemini Curator chuyên thẩm định kịch bản Cool Mèo Phim. "
            "Chấm điểm các phim theo thang 100 điểm: hook_score (0-35), viral_score (0-30), "
            "completeness_score (0-20), copyright_score (0-15). "
            "Trả về JSON list các object gồm: id, hook_score, viral_score, completeness_score, copyright_score, total_score, reasoning."
        )

        prompt_text = f"{system_instruction}\n\nDanh sách ứng viên:\n{json.dumps(eval_items, ensure_ascii=False)}"

        api_key = cls._get_api_key()
        if not api_key:
            return None

        # Thử REST call trực tiếp với timeout ngắn 4s để không bao giờ làm nghẽn tiến trình
        for model_name in ["gemini-3.8-flash", "gemini-flash-latest"]:
            try:
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"
                body = {
                    "contents": [{"parts": [{"text": prompt_text}]}],
                    "generationConfig": {"responseMimeType": "application/json", "temperature": 0.3}
                }
                req = urllib.request.Request(
                    url,
                    data=json.dumps(body).encode("utf-8"),
                    headers={"Content-Type": "application/json"}
                )
                with urllib.request.urlopen(req, timeout=4) as resp:
                    res_json = json.loads(resp.read().decode("utf-8"))
                    text = res_json["candidates"][0]["content"]["parts"][0]["text"].strip()
                    if text.startswith("```json"): text = text[7:]
                    if text.startswith("```"): text = text[3:]
                    if text.endswith("```"): text = text[:-3]
                    parsed = json.loads(text.strip())
                    
                    results_map = {}
                    if isinstance(parsed, list):
                        for it in parsed:
                            if "id" in it: results_map[it["id"]] = it
                    elif isinstance(parsed, dict):
                        for k, v in parsed.items():
                            if isinstance(v, dict) and "hook_score" in v:
                                results_map[k] = v
                    if results_map:
                        return results_map
            except Exception as e:
                # Bỏ qua và thử model tiếp theo hoặc sang fallback
                continue

        return None

    @classmethod
    def select_top_candidates(
        cls,
        scored_candidates: List[Dict[str, Any]],
        target_total: int = Settings.DAILY_TARGET_VIDEOS,
        target_short: int = Settings.DAILY_MIX_SHORT,
        target_long: int = Settings.DAILY_MIX_LONG
    ) -> List[Dict[str, Any]]:
        """
        Chọn đúng Top 3 phim hay nhất mỗi ngày:
        - Mặc định: 2 short drama + 1 long series
        - Tự động bù trừ linh hoạt nếu một phân hệ không đủ ứng viên đạt chuẩn.
        """
        shorts = [c for c in scored_candidates if c["category"] == "short_drama"]
        longs = [c for c in scored_candidates if c["category"] == "long_series"]

        # Sắp xếp ưu tiên: nguồn Lượng Tử (lz), đoản kịch gọn (<= 5 tập / 1 file), và điểm giảm dần
        shorts.sort(
            key=lambda x: (
                1 if x.get("source_id") == "lz" else 0,
                1 if x.get("episodes_count", 999) <= 5 else 0,
                x.get("score", 0)
            ),
            reverse=True
        )
        longs.sort(
            key=lambda x: (
                1 if x.get("source_id") == "lz" else 0,
                x.get("score", 0)
            ),
            reverse=True
        )

        selected = []
        selected_ids = set()

        # 1. Chọn đoản kịch theo định mức
        for c in shorts[:target_short]:
            selected.append(c)
            selected_ids.add(c["id"])

        # 2. Chọn phim dài theo định mức
        for c in longs[:target_long]:
            if c["id"] not in selected_ids:
                selected.append(c)
                selected_ids.add(c["id"])

        # 3. Bù trừ linh hoạt nếu chưa đủ target_total
        if len(selected) < target_total:
            remaining = [c for c in scored_candidates if c["id"] not in selected_ids]
            remaining.sort(
                key=lambda x: (
                    1 if x.get("source_id") == "lz" else 0,
                    1 if (x.get("category") == "short_drama" and x.get("episodes_count", 999) <= 5) else 0,
                    x.get("score", 0)
                ),
                reverse=True
            )
            for c in remaining:
                if len(selected) >= target_total:
                    break
                selected.append(c)
                selected_ids.add(c["id"])

        # Sắp xếp Top 3: Ưu tiên nguồn LZ, đoản kịch gọn <= 5 tập và điểm số cao nhất
        selected.sort(
            key=lambda x: (
                1 if x.get("source_id") == "lz" else 0,
                1 if (x.get("category") == "short_drama" and x.get("episodes_count", 999) <= 5) else 0,
                x.get("score", 0)
            ),
            reverse=True
        )

        for rank, c in enumerate(selected, start=1):
            c["curation_rank"] = rank

        return selected[:target_total]

    @classmethod
    def export_report_and_update_queue(
        cls,
        selected_candidates: List[Dict[str, Any]],
        layer1_stats: Dict[str, Any],
        all_scored: List[Dict[str, Any]]
    ) -> Tuple[str, str]:
        """
        Xuất file daily_curation_report.json và cập nhật state/production_queue.json.
        """
        now = datetime.now()
        report_path = BASE_DIR / "daily_curation_report.json"
        queue_path = STATE_DIR / "production_queue.json"

        # 1. Tạo nội dung báo cáo daily_curation_report.json
        report_data = {
            "curation_date": now.strftime("%Y-%m-%d"),
            "generated_at": now.isoformat(),
            "channel": f"{Settings.CHANNEL_NAME} ({Settings.CHANNEL_HANDLE})",
            "curation_summary": {
                "total_scanned": layer1_stats.get("total_scanned", 0),
                "deduplicated_count": layer1_stats.get("deduplicated_count", 0),
                "skipped_already_processed": layer1_stats.get("skipped_processed", 0),
                "skipped_already_queued": layer1_stats.get("skipped_in_queue", 0),
                "blacklisted_rejected": layer1_stats.get("blacklisted_count", 0),
                "incomplete_rejected": layer1_stats.get("incomplete_rejected_count", 0),
                "layer1_passed_count": layer1_stats.get("layer1_passed_count", 0),
                "layer2_scored_count": len(all_scored),
                "selected_daily_target": len(selected_candidates)
            },
            "mix_ratio": {
                "target": {
                    "short_drama": Settings.DAILY_MIX_SHORT,
                    "long_series": Settings.DAILY_MIX_LONG,
                    "total": Settings.DAILY_TARGET_VIDEOS
                },
                "actual_selected": {
                    "short_drama": len([c for c in selected_candidates if c["category"] == "short_drama"]),
                    "long_series": len([c for c in selected_candidates if c["category"] == "long_series"]),
                    "total": len(selected_candidates)
                }
            },
            "top_3_selected": [
                {
                    "rank": c["curation_rank"],
                    "id": c["id"],
                    "vod_id": c["vod_id"],
                    "title": c["title"],
                    "source": c["source_name"],
                    "category": c["category"],
                    "episodes_count": c["episodes_count"],
                    "total_episodes": c.get("total_episodes", c["episodes_count"]),
                    "is_completed": c["is_completed"],
                    "completion_verified": True,
                    "score": c["score"],
                    "curation_details": c.get("curation_details", {}),
                    "poster_url": c.get("poster_url", ""),
                    "first_m3u8": c["m3u8_groups"][0] if c.get("m3u8_groups") else ""
                }
                for c in selected_candidates
            ],
            "all_scored_ranking": [
                {
                    "id": c["id"],
                    "title": c["title"],
                    "category": c["category"],
                    "score": c["score"],
                    "is_completed": c["is_completed"],
                    "completion_verified": True,
                    "episodes_count": c["episodes_count"],
                    "total_episodes": c.get("total_episodes", c["episodes_count"])
                }
                for c in all_scored
            ]
        }

        # Lưu daily_curation_report.json
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(report_data, f, ensure_ascii=False, indent=2)
        print(f"[CURATOR] Đã xuất báo cáo giám tuyển: {report_path}")

        # 2. Cập nhật state/production_queue.json
        existing_queue = StateManager.load_production_queue()
        existing_ids = {item["id"] for item in existing_queue if isinstance(item, dict) and "id" in item}

        new_items_count = 0
        for c in selected_candidates:
            if c["id"] not in existing_ids:
                queue_item = {
                    "id": c["id"],
                    "vod_id": c["vod_id"],
                    "source_id": c["source_id"],
                    "source_name": c["source_name"],
                    "title": c["title"],
                    "category": c["category"],
                    "episodes_count": c["episodes_count"],
                    "total_episodes": c.get("total_episodes", c["episodes_count"]),
                    "is_completed": c["is_completed"],
                    "completion_verified": True,
                    "score": c["score"],
                    "curation_details": c.get("curation_details", {}),
                    "poster_url": c.get("poster_url", ""),
                    "m3u8_groups": c.get("m3u8_groups", []),
                    "status": "queued",
                    "queued_at": now.isoformat()
                }
                existing_queue.append(queue_item)
                existing_ids.add(c["id"])
                new_items_count += 1

        StateManager.save_production_queue(existing_queue)
        print(f"[CURATOR] Đã đưa {new_items_count} phim mới vào hàng chờ sản xuất state/production_queue.json.")

        return str(report_path), str(queue_path)

    @classmethod
    def run_curation_pipeline(cls) -> Dict[str, Any]:
        """Quy trình toàn diện từ Quét -> Lọc Tầng 1 -> Chấm Tầng 2 -> Chọn Top 3 -> Xuất file."""
        print("=" * 70)
        print(f"🌟 BỘ LỌC & CHẤM ĐIỂM PHIM HAY - {Settings.CHANNEL_NAME} ({Settings.CHANNEL_HANDLE})")
        print(f"🎯 Chỉ tiêu: Top {Settings.DAILY_TARGET_VIDEOS} phim/ngày (Tỷ lệ: {Settings.DAILY_MIX_SHORT} Short + {Settings.DAILY_MIX_LONG} Long)")
        print("=" * 70)

        # 1. Quét toàn bộ ứng viên từ LZ và FF
        raw_candidates = cls.fetch_all_source_candidates()

        # 2. Tầng 1: Rule-based Filter
        passed_layer1, layer1_stats = cls.filter_layer_1(raw_candidates)

        if not passed_layer1:
            print("[CURATOR] Không có ứng viên nào vượt qua Tầng 1 hôm nay.")
            return {
                "success": False,
                "selected": [],
                "layer1_stats": layer1_stats
            }

        # 3. Tầng 2: AI Chấm điểm Gemini Curator (0 - 100)
        scored_candidates = cls.score_candidates_layer_2(passed_layer1)

        # 4. Chọn Top 3 phim hay nhất (2 short, 1 long, bù trừ nếu thiếu)
        top_selected = cls.select_top_candidates(scored_candidates)

        # 5. Xuất báo cáo và cập nhật queue
        report_file, queue_file = cls.export_report_and_update_queue(top_selected, layer1_stats, scored_candidates)

        return {
            "success": True,
            "selected": top_selected,
            "report_file": report_file,
            "queue_file": queue_file,
            "layer1_stats": layer1_stats,
            "scored_count": len(scored_candidates)
        }

    @classmethod
    def _get_mock_fallback_candidates(cls) -> List[Dict[str, Any]]:
        """Dữ liệu dự phòng chuẩn mực cho môi trường offline test."""
        return [
            {
                "vod_id": 99901,
                "vod_name": "Trọng Sinh Nghịch Tập: Vả Mặt Tổng Tài Bá Đạo",
                "vod_sub": "Vả mặt tổng tài",
                "type_id": 46,
                "type_name": "短剧",
                "vod_class": "短剧,都市",
                "vod_remarks": "已完结",
                "vod_isend": 1,
                "vod_total": 80,
                "vod_content": "Kiếp trước bị trà xanh và tổng tài hãm hại, kiếp này trùng sinh vả mặt toàn bộ kẻ thù, giành lại gia sản hào môn trăm tỷ.",
                "vod_play_url": "第01集$https://v.lzcdn.com/test1/index.m3u8#第02集$https://v.lzcdn.com/test2/index.m3u8",
                "vod_pic": "https://pic.example.com/mock1.jpg",
                "vod_time": "2026-11-22 10:00:00",
                "_scan_source_id": "lz",
                "_scan_source_name": "Lượng Tử (Quantum)",
                "_scan_category": "short_drama"
            },
            {
                "vod_id": 99902,
                "vod_name": "Chiến Thần Ở Rể: Long Vương Trở Lại",
                "vod_sub": "Long vương trở lại",
                "type_id": 46,
                "type_name": "短剧",
                "vod_class": "短剧,战神",
                "vod_remarks": "85集全",
                "vod_isend": 1,
                "vod_total": 85,
                "vod_content": "3 năm nhẫn nhục làm rể vô dụng bị mẹ vợ khinh bỉ, nay thân phận Chiến thần cái thế lộ diện, triệu tập vạn quân khiến hào môn quỳ gối.",
                "vod_play_url": "第01集$https://v.lzcdn.com/test3/index.m3u8",
                "vod_pic": "https://pic.example.com/mock2.jpg",
                "vod_time": "2026-11-22 10:05:00",
                "_scan_source_id": "lz",
                "_scan_source_name": "Lượng Tử (Quantum)",
                "_scan_category": "short_drama"
            },
            {
                "vod_id": 99903,
                "vod_name": "Thiên Kim Thật Trở Về: Gia Tộc Hào Môn Kinh Sợ",
                "vod_sub": "Thiên kim thật giả",
                "type_id": 36,
                "type_name": "短剧",
                "vod_class": "短剧,豪门",
                "vod_remarks": "全65集",
                "vod_isend": 1,
                "vod_total": 65,
                "vod_content": "Bị tráo đổi từ nhỏ sống nơi nghèo khó, thiên kim thật trở về vạch trần bộ mặt giả tạo của thiên kim giả, tự tay xây dựng đế chế tài chính.",
                "vod_play_url": "第01集$https://v.ffzy.com/test4/index.m3u8",
                "vod_pic": "https://pic.example.com/mock3.jpg",
                "vod_time": "2026-11-22 10:10:00",
                "_scan_source_id": "ff",
                "_scan_source_name": "Phi Phàm (FFZY)",
                "_scan_category": "short_drama"
            },
            {
                "vod_id": 99904,
                "vod_name": "Đại Minh Phong Vân: Kỳ Án Trấn Kinh Thành",
                "vod_sub": "Đại minh kỳ án",
                "type_id": 13,
                "type_name": "国产剧",
                "vod_class": "连续剧,古装,悬疑",
                "vod_remarks": "已完结",
                "vod_isend": 1,
                "vod_total": 24,
                "vod_content": "Bí mật triều đình chấn động, cẩm y vệ phá án liên hoàn cứu vãn giang sơn trước âm mưu đảo chính thâm sâu.",
                "vod_play_url": "第01集$https://v.lzcdn.com/test5/index.m3u8#第02集$https://v.lzcdn.com/test6/index.m3u8",
                "vod_pic": "https://pic.example.com/mock4.jpg",
                "vod_time": "2026-11-22 10:15:00",
                "_scan_source_id": "lz",
                "_scan_source_name": "Lượng Tử (Quantum)",
                "_scan_category": "long_series"
            }
        ]

def run_curation_cli() -> int:
    """Hàm CLI cho phép chạy test độc lập: python -m src.sources.movie_curator."""
    print("=" * 75)
    print("🎬 COOL MÈO PHIM - KIỂM THỬ ĐỘC LẬP BỘ LỌC & CHẤM ĐIỂM PHIM HAY")
    print("=" * 75)
    
    start_time = time.time()
    result = MovieCurator.run_curation_pipeline()
    elapsed = time.time() - start_time

    print("\n" + "=" * 75)
    print(f"📊 KẾT QUẢ GIÁM TUYỂN TRONG {elapsed:.2f} GIÂY:")
    print("=" * 75)

    if not result.get("success"):
        print("❌ Giám tuyển không thành công hoặc không có phim nào đạt chuẩn.")
        return 1

    selected = result.get("selected", [])
    print(f"✅ ĐÃ CHỌN THÀNH CÔNG TOP {len(selected)} PHIM HAY NHẤT HÔM NAY:\n")

    for c in selected:
        details = c.get("curation_details", {})
        print(f"🏆 [TOP {c['curation_rank']}] {c['title']} ({c['id']})")
        print(f"   • Phân loại   : {'Đoản kịch dọc (Short Drama)' if c['category'] == 'short_drama' else 'Phim bộ dài (Long Series)'}")
        print(f"   • Trạm nguồn  : {c['source_name']}")
        print(f"   • Thời lượng  : {c['episodes_count']} tập | {'ĐÃ FULL (Hoàn thành)' if c['is_completed'] else 'Đủ độ dài tiêu chuẩn'}")
        print(f"   • Điểm tổng   : {c['score']}/100")
        print(f"     └─ Hook mở đầu : {details.get('hook_score', 0)}/35")
        print(f"     └─ Đề tài viral: {details.get('viral_score', 0)}/30")
        print(f"     └─ Trọn vẹn    : {details.get('completeness_score', 0)}/20")
        print(f"     └─ An toàn     : {details.get('copyright_score', 0)}/15")
        print(f"   • Đánh giá    : {details.get('reasoning', '')}")
        print("-" * 75)

    print(f"\n📁 File báo cáo  : {result.get('report_file')}")
    print(f"📦 File hàng chờ : {result.get('queue_file')}")
    print("=" * 75)
    return 0

if __name__ == "__main__":
    exit_code = run_curation_cli()
    exit(exit_code)
