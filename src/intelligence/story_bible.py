import os
import re
import json
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional
from config.settings import STATE_DIR, Settings

def slugify(text: str) -> str:
    """Tạo slug an toàn cho đường dẫn thư mục."""
    text = re.sub(r"[^\w\s-]", "", text).strip().lower()
    return re.sub(r"[-\s]+", "_", text)[:50] or "unnamed_series"

class StoryBible:
    """
    Quản trị Hồ Sơ Mạch Truyện (Story Bible) cho từng bộ phim/series:
    - Lưu trữ tại: state/active_series/<series_id>/story_bible.json
    - character_glossary: Khóa cứng tên Hán-Việt nhân vật xuyên suốt các tập
    - rolling_summary: Tóm tắt mạch truyện gần nhất, tránh mâu thuẫn cốt truyện
    - episodes_record: Lịch sử diễn biến từng tập đã xử lý
    """

    def __init__(
        self,
        series_id: str,
        series_title: str = "",
        character_glossary: Optional[Dict[str, Any]] = None,
        rolling_summary: str = "",
        episode_history: Optional[List[Dict[str, Any]]] = None,
        key_relationships: Optional[Dict[str, str]] = None,
        metadata: Optional[Dict[str, Any]] = None
    ):
        self.series_id = slugify(series_id)
        self.series_title = series_title or series_id
        self.character_glossary: Dict[str, Dict[str, Any]] = character_glossary or {}
        self.chinese_name_registry: Dict[str, str] = metadata.get("chinese_name_registry", {}) if metadata else {}
        # Khôi phục từ character_glossary nếu đã có
        for k, v in self.character_glossary.items():
            if re.search(r"[\u4e00-\u9fff]", k) and isinstance(v, dict):
                hv = v.get("han_viet", "")
                if hv and k not in self.chinese_name_registry:
                    self.chinese_name_registry[k] = hv
        self.rolling_summary: str = rolling_summary or ""
        self.episode_history: List[Dict[str, Any]] = episode_history or []
        self.key_relationships: Dict[str, str] = key_relationships or {}
        self.metadata: Dict[str, Any] = metadata or {}
        self.metadata["chinese_name_registry"] = self.chinese_name_registry
        self.created_at = datetime.now().isoformat()
        self.last_updated = self.created_at

    @property
    def series_dir(self) -> Path:
        """Thư mục lưu trữ active series."""
        path = STATE_DIR / "active_series" / self.series_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def file_path(self) -> Path:
        """Đường dẫn file story_bible.json."""
        return self.series_dir / "story_bible.json"

    @classmethod
    def load_or_create(cls, series_id: str, series_title: str = "") -> "StoryBible":
        """
        Tải Story Bible hiện có hoặc khởi tạo mới nếu chưa tồn tại.
        """
        safe_id = slugify(series_id)
        target_file = STATE_DIR / "active_series" / safe_id / "story_bible.json"

        if target_file.exists():
            try:
                with open(target_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    bible = cls(
                        series_id=data.get("series_id", safe_id),
                        series_title=data.get("series_title", series_title),
                        character_glossary=data.get("character_glossary", {}),
                        rolling_summary=data.get("rolling_summary", ""),
                        episode_history=data.get("episode_history", []),
                        key_relationships=data.get("key_relationships", {}),
                        metadata=data.get("metadata", {})
                    )
                    bible.created_at = data.get("created_at", bible.created_at)
                    bible.last_updated = data.get("last_updated", bible.last_updated)
                    return bible
            except Exception as e:
                print(f"[StoryBible] Cảnh báo lỗi đọc story_bible.json ({e}), khởi tạo lại.")

        new_bible = cls(series_id=safe_id, series_title=series_title)
        new_bible.save()
        return new_bible

    def save(self) -> str:
        """Ghi dữ liệu nguyên tử (atomic write) vào story_bible.json."""
        self.last_updated = datetime.now().isoformat()
        data = {
            "series_id": self.series_id,
            "series_title": self.series_title,
            "character_glossary": self.character_glossary,
            "rolling_summary": self.rolling_summary,
            "episode_history": self.episode_history,
            "key_relationships": self.key_relationships,
            "metadata": self.metadata,
            "created_at": self.created_at,
            "last_updated": self.last_updated
        }

        temp_path = self.file_path.with_suffix(".tmp")
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        temp_path.replace(self.file_path)
        return str(self.file_path)

    def lock_character(
        self,
        char_key: str,
        han_viet: str,
        role: str = "",
        aliases: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """
        Khóa cứng tên Hán-Việt cho một nhân vật trong hồ sơ:
        - Sử dụng tên tiếng Trung làm khóa chính duy nhất.
        - Khi nhân vật đã được chốt tên ở Tập 1, KHÓA CỨNG không cho phép ghi đè sang tên khác ở các tập sau.
        """
        char_key_clean = char_key.strip()
        han_viet_clean = han_viet.strip()
        is_chinese = bool(re.search(r"[\u4e00-\u9fff]", char_key_clean))

        # Nếu tên tiếng Trung đã được khóa từ trước, giữ nguyên tên đã chốt, coi tên mới là biến thể
        if is_chinese and char_key_clean in self.chinese_name_registry:
            canonical_name = self.chinese_name_registry[char_key_clean]
            entry = self.character_glossary.get(canonical_name, {
                "han_viet": canonical_name,
                "role": role.strip(),
                "aliases": [char_key_clean]
            })
            if han_viet_clean and han_viet_clean != canonical_name:
                if "aliases" not in entry:
                    entry["aliases"] = []
                if han_viet_clean not in entry["aliases"]:
                    entry["aliases"].append(han_viet_clean)
                # Map tên biến thể về entry canonical
                self.character_glossary[han_viet_clean] = entry
            self.character_glossary[canonical_name] = entry
            self.save()
            return entry

        # Nếu là nhân vật mới
        if is_chinese:
            self.chinese_name_registry[char_key_clean] = han_viet_clean

        all_aliases = set(aliases or [])
        all_aliases.add(char_key_clean)

        entry = {
            "han_viet": han_viet_clean,
            "role": role.strip(),
            "aliases": sorted(list(all_aliases))
        }
        self.character_glossary[han_viet_clean] = entry
        if char_key_clean != han_viet_clean:
            self.character_glossary[char_key_clean] = entry

        self.save()
        print(f"[StoryBible] Đã khóa cứng nhân vật: {char_key_clean} -> {han_viet_clean} ({role})")
        return entry

    def add_character_mapping(self, orig: str, han_viet: str, role: str = ""):
        """Alias cho lock_character."""
        return self.lock_character(char_key=orig, han_viet=han_viet, role=role)

    def batch_lock_characters(self, glossary_data: Dict[str, Any]):
        """Khóa cứng danh sách nhiều nhân vật cùng lúc."""
        for key, val in glossary_data.items():
            if isinstance(val, dict):
                han_viet = val.get("han_viet", key)
                role = val.get("role", "")
                aliases = val.get("aliases", [])
            else:
                han_viet = str(val)
                role = ""
                aliases = [key]
            self.lock_character(key, han_viet, role, aliases)

    def apply_glossary_to_text(self, text: str) -> str:
        """
        Chuẩn hóa văn bản kịch bản: Thay thế mọi tên biến thể/pinyin/dịch lệch
        thành tên Hán-Việt đã được khóa cứng (Canonical Replace).
        """
        if not text or not self.character_glossary:
            return text

        result = text
        replacement_pairs = []

        # 1. Từ chinese_name_registry sang tên đã khóa
        for zh, canonical in self.chinese_name_registry.items():
            replacement_pairs.append((zh, canonical))

        # 2. Từ các alias sang canonical
        for primary_name, info in self.character_glossary.items():
            han_viet = info.get("han_viet", primary_name)
            for alias in info.get("aliases", []):
                if alias and alias != han_viet:
                    replacement_pairs.append((alias, han_viet))

        # Ưu tiên chuỗi dài thay thế trước
        replacement_pairs.sort(key=lambda x: len(x[0]), reverse=True)
        seen = set()
        dedup_pairs = []
        for a, b in replacement_pairs:
            if (a, b) not in seen:
                seen.add((a, b))
                dedup_pairs.append((a, b))

        for alias, locked_name in dedup_pairs:
            if not alias or not locked_name:
                continue
            if re.search(r"[\u4e00-\u9fff]", alias):
                result = result.replace(alias, locked_name)
            else:
                pattern = r"(?<!\w)" + re.escape(alias) + r"(?!\w)"
                result = re.sub(pattern, locked_name, result, flags=re.IGNORECASE)

        return result

    def canonical_replace(self, text: str) -> str:
        """Alias cho apply_glossary_to_text."""
        return self.apply_glossary_to_text(text)

    def update_rolling_summary(
        self,
        new_events: str,
        episode_index: Optional[int] = None,
        max_words: int = 350
    ):
        """
        Cập nhật rolling summary tóm tắt mạch truyện gần nhất.
        Nếu quá dài, cô đọng lại để giữ đúng bối cảnh then chốt.
        """
        timestamp = datetime.now().isoformat()
        ep_label = f"Tập {episode_index}" if episode_index is not None else "Diễn biến mới"

        # Ghi log tập vào history
        self.episode_history.append({
            "episode": episode_index,
            "summary": new_events.strip(),
            "timestamp": timestamp
        })

        if not self.rolling_summary:
            self.rolling_summary = f"[{ep_label}]: {new_events.strip()}"
        else:
            self.rolling_summary += f"\n[{ep_label}]: {new_events.strip()}"

        # Kiểm tra độ dài từ
        words = self.rolling_summary.split()
        if len(words) > max_words:
            # Giữ lại các đoạn gần nhất
            lines = [l for l in self.rolling_summary.split("\n") if l.strip()]
            while len(" ".join(lines).split()) > max_words and len(lines) > 2:
                lines.pop(0)
            self.rolling_summary = "\n".join(lines)

        self.save()
        print(f"[StoryBible] Đã cập nhật rolling_summary cho series [{self.series_title}]")

    def format_prompt_context(self) -> str:
        """
        Xuất context Story Bible dạng markdown để nhúng vào prompt của Gemini.
        """
        lines = [
            f"### [HỒ SƠ MẠCH TRUYỆN - STORY BIBLE: {self.series_title}]",
            "🔒 BẢNG TÊN NHÂN VẬT ĐÃ KHÓA CỨNG (BẮT BUỘC DÙNG ĐÚNG 100%, TUYỆT ĐỐI CẤM TỰ Ý ĐỔI SANG TÊN KHÁC):"
        ]

        if self.chinese_name_registry:
            for zh, canonical in self.chinese_name_registry.items():
                info = self.character_glossary.get(canonical, {})
                role = info.get("role", "Nhân vật")
                lines.append(f"- **{zh}** ➔ **{canonical}** ({role})")
        else:
            unique_chars = {}
            for k, v in self.character_glossary.items():
                hv = v.get("han_viet", k) if isinstance(v, dict) else str(v)
                if hv not in unique_chars:
                    unique_chars[hv] = v

            if unique_chars:
                for hv, info in unique_chars.items():
                    role = info.get("role", "Nhân vật") if isinstance(info, dict) else "Nhân vật"
                    lines.append(f"- **{hv}** [{role}]")
            else:
                lines.append("- (Chưa có nhân vật nào bị khóa, hãy dịch chuẩn Hán-Việt ở tập đầu tiên)")

        lines.append("\n### [MẠCH TRUYỆN GẦN NHẤT (ROLLING SUMMARY)]:")
        if self.rolling_summary:
            lines.append(self.rolling_summary)
        else:
            lines.append("(Bắt đầu tập đầu tiên)")

        return "\n".join(lines)

    def extract_and_lock_with_gemini(self, synopsis: str) -> Dict[str, Any]:
        """
        Sử dụng Gemini API để tự động trích xuất danh sách nhân vật,
        dịch sang tên Hán-Việt chuẩn và khóa cứng vào glossary.
        """
        if not synopsis:
            return self.character_glossary

        prompt = (
            f"Bạn là chuyên gia chuyển ngữ phim truyền hình và đoản kịch Trung Quốc.\n"
            f"Hãy đọc nội dung sau và trích xuất danh sách nhân vật chính, "
            f"dịch chính xác sang tên Hán-Việt chuẩn mực (ví dụ: 顾逸舟 -> Cố Dịch Chu, 沈瑶 -> Thẩm Dao):\n\n"
            f"'{synopsis}'\n\n"
            "Trả về JSON định dạng:\n"
            "{\n"
            "  \"characters\": [\n"
            "    {\"han_viet\": \"Tên Hán-Việt chuẩn\", \"role\": \"Vai trò chính\", \"aliases\": [\"tên gốc\", \"biệt danh\"]}\n"
            "  ]\n"
            "}"
        )

        try:
            from src.intelligence.review_writer import ReviewWriter
            data = ReviewWriter._call_gemini_json(prompt)
            if data and "characters" in data:
                for c in data["characters"]:
                    self.lock_character(
                        char_key=c.get("han_viet", ""),
                        han_viet=c.get("han_viet", ""),
                        role=c.get("role", ""),
                        aliases=c.get("aliases", [])
                    )
                return self.character_glossary
        except Exception as e:
            print(f"[StoryBible] Không thể gọi Gemini trích xuất nhân vật: {e}")

        # Fallback heuristic: Tách các cụm từ phổ biến
        return self.character_glossary
