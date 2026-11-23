import os
import json
import urllib.request
import urllib.parse
from typing import Dict, Any, List, Optional
from config.settings import Settings
from src.state.state_manager import StateManager

class CMSClient:
    """
    Client kết nối chuẩn MacCMS API (Lượng Tử & Phi Phàm)
    có cơ chế tự động chuyển đổi dự phòng (failover) và lọc danh sách đen.
    """

    VIRAL_KEYWORDS = [
        "打脸", "豪门", "总裁", "婆媳", "千金", "真假千金", "重回", "重生",
        "逆袭", "清醒", "战神", "神医", "分家", "背叛", "大权", "首富", "闪婚"
    ]

    @classmethod
    def get_sources_config(cls) -> List[Dict[str, Any]]:
        return [
            {
                "id": "lz",
                "name": "Lượng Tử (Quantum)",
                "api_url": Settings.LZ_API_URL,
                "type_id": 46
            },
            {
                "id": "ff",
                "name": "Phi Phàm (FFZY)",
                "api_url": Settings.FF_API_URL,
                "type_id": 36
            }
        ]

    @classmethod
    def _create_request(cls, url: str) -> urllib.request.Request:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*"
        }
        return urllib.request.Request(url, headers=headers)

    @classmethod
    def _fetch_url(cls, url: str, timeout: int = 15) -> Optional[Dict[str, Any]]:
        handlers = []
        if Settings.HTTP_PROXY:
            proxy_handler = urllib.request.ProxyHandler({
                "http": Settings.HTTP_PROXY,
                "https": Settings.HTTP_PROXY
            })
            handlers.append(proxy_handler)

        opener = urllib.request.build_opener(*handlers)
        req = cls._create_request(url)
        try:
            with opener.open(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="ignore")
                return json.loads(raw)
        except Exception as e:
            print(f"[CMSClient] Lỗi kết nối {url}: {e}")
            return None

    @classmethod
    def fetch_candidates(cls, limit: int = 10, preferred_source: Optional[str] = None) -> List[Dict[str, Any]]:
        """Quét tìm các ứng viên phim vi kịch mới nhất với cơ chế failover."""
        ignored_ids = StateManager.get_ignored_ids()
        blacklist = Settings.load_blacklist()
        copyright_risks = [k.lower() for k in blacklist.get("copyright_high_risk", [])]

        sources = cls.get_sources_config()
        if preferred_source:
            sources.sort(key=lambda s: 0 if s["id"] == preferred_source else 1)

        candidates = []

        for src in sources:
            src_id = src["id"]
            src_name = src["name"]
            api_url = src["api_url"]
            print(f"[CMSClient] Đang quét trạm [{src_name}]...")

            data = cls._fetch_url(api_url)
            if not data or "list" not in data:
                print(f"[CMSClient] Trạm [{src_name}] không phản hồi -> Chuyển sang trạm dự phòng tiếp theo.")
                continue

            items = data.get("list", [])
            print(f"[CMSClient] Nhận {len(items)} phim từ [{src_name}].")

            for item in items:
                vod_id = str(item.get("vod_id", "")).strip()
                vod_name = item.get("vod_name", "").strip()
                vod_pic = item.get("vod_pic", "").strip()
                play_url = item.get("vod_play_url", "").strip()
                vod_time = item.get("vod_time", "")

                if not vod_id or not vod_name:
                    continue

                movie_id = f"{src_id}_{vod_id}"
                if movie_id in ignored_ids:
                    continue

                # Kiểm tra danh sách đen bản quyền
                title_lower = vod_name.lower()
                if any(risk in title_lower for risk in copyright_risks):
                    print(f"[CMSClient] Bỏ qua [{vod_name}] do nằm trong danh sách đen bản quyền.")
                    StateManager.mark_failed(movie_id, "copyright_blacklisted", vod_name)
                    continue

                # Bóc tách link m3u8
                m3u8_groups = cls._extract_m3u8_groups(play_url)
                if not m3u8_groups:
                    continue

                # Tính điểm viral kịch tính
                score = 0
                for kw in cls.VIRAL_KEYWORDS:
                    if kw in vod_name:
                        score += 15

                candidates.append({
                    "id": movie_id,
                    "vod_id": vod_id,
                    "source_id": src_id,
                    "source_name": src_name,
                    "title": vod_name,
                    "poster_url": vod_pic,
                    "m3u8_groups": m3u8_groups,
                    "updated_time": vod_time,
                    "score": score
                })

            if len(candidates) >= limit:
                break

        # Sắp xếp điểm cao lên đầu
        candidates.sort(key=lambda x: x["score"], reverse=True)
        return candidates[:limit]

    @classmethod
    def _extract_m3u8_groups(cls, play_url: str) -> List[str]:
        """Trích xuất danh sách link m3u8 chuẩn từ chuỗi vod_play_url."""
        if not play_url:
            return []

        groups = []
        for grp in play_url.split("$$$"):
            urls = []
            for ep in grp.split("#"):
                if ".m3u8" in ep.lower():
                    for part in ep.split("$"):
                        part = part.strip()
                        if part.startswith("http") and ".m3u8" in part.lower():
                            urls.append(part)
                            break
            if urls:
                groups.append(urls)

        if not groups:
            return []

        # Ưu tiên nhóm direct m3u8 (chứa index.m3u8 / 202 / mixed)
        for g in groups:
            if any("/202" in u or "index.m3u8" in u or "mixed" in u for u in g):
                return g
        return groups[0]
