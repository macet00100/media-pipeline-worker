import os
import re
import unicodedata
import json
import time
from typing import List, Dict, Any, Optional
from config.settings import Settings
from src.intelligence.gemini_client import GeminiClient

class ReviewWriter:
    """
    Biên tập kịch bản Review phim ngôi thứ ba chuyên nghiệp
    kết nối trực tiếp với Gemini API. Tuyệt đối KHÔNG sử dụng văn mẫu bịa đặt.
    """

    @classmethod
    def clean_text_for_vietnamese(cls, text: str) -> str:
        """
        Lọc sạch 100% ký tự chữ Hán, tên kênh lạ hoặc web cờ bạc.
        Đảm bảo văn bản thuần Việt 100%.
        """
        if not text:
            return ""
        # 0. Chuẩn hóa Unicode NFC
        text = unicodedata.normalize("NFC", text)
        # 1. Xóa sạch ký tự chữ Hán (Unicode CJK Unified Ideographs)
        text = re.sub(r"[\u4e00-\u9fff]+", "", text)
        # 2. Xóa sạch ký tự mở ngoặc đóng ngoặc thừa nếu còn lại sau khi xóa chữ Hán
        text = re.sub(r"\(\s*\)|\[\s*\]", "", text)
        # Sửa từ lóng cợt nhả hoặc lỗi chính tả
        text = re.sub(r"\bHÀM\b", "HAI", text, flags=re.IGNORECASE)
        text = re.sub(r"\bsương\s*sương\b", "bất ngờ", text, flags=re.IGNORECASE)
        # 3. Chuẩn hóa thương hiệu về Cool Mèo Phim
        blacklist = Settings.load_blacklist()
        for rogue in blacklist.get("forbidden_channel_names", []):
            text = re.sub(re.escape(rogue), Settings.CHANNEL_NAME, text, flags=re.IGNORECASE)
        for g_kw in blacklist.get("gambling_keywords", []):
            text = re.sub(g_kw, "", text, flags=re.IGNORECASE)
        # 4. Xóa khoảng trắng thừa
        text = re.sub(r"\s+", " ", text).strip()
        return text

    @classmethod
    def write_full_review_multimodal(
        cls,
        source_video: str,
        drama_title: str,
        tmp_dir: str,
        episode_index: int = 1,
        total_episodes: int = 1,
        previous_summary: str = "",
        target_duration_sec: Optional[float] = None,
        story_bible: Optional[Any] = None,
        is_first: Optional[bool] = None,
        is_last: Optional[bool] = None
    ) -> Dict[str, Any]:
        """
        Biên tập review đa phương thức:
        Gửi video nháp cho Gemini Multimodal để AI thực sự xem phim, nghe thoại, đọc phụ đề gốc.
        Hỗ trợ điều phối mạch truyện liền mạch (Seamless Story Flow) và Dynamic Duration Budget.
        """
        print(f"[ReviewWriter] Bắt đầu phân tích Multimodal Video thật cho [{drama_title}] (Phần {episode_index}/{total_episodes})...")
        
        # Nếu có Story Bible, lấy context đầy đủ (gồm bảng tên nhân vật đã khóa)
        prompt_summary = previous_summary
        if story_bible and hasattr(story_bible, "format_prompt_context"):
            prompt_summary = story_bible.format_prompt_context()

        result = GeminiClient.analyze_movie_multimodal(
            source_video=source_video,
            drama_title=drama_title,
            tmp_dir=tmp_dir,
            episode_index=episode_index,
            total_episodes=total_episodes,
            previous_summary=prompt_summary,
            target_duration_sec=target_duration_sec,
            is_first=is_first,
            is_last=is_last
        )

        if not result or "narration_blocks" not in result or not result["narration_blocks"]:
            raise RuntimeError(f"LỖI: Gemini không trả về kịch bản hợp lệ cho phim {drama_title}!")

        # Chuẩn hóa sạch 100% tiếng Việt
        result["vietnamese_title"] = cls.clean_text_for_vietnamese(result.get("vietnamese_title", drama_title))
        if not result["vietnamese_title"]:
            result["vietnamese_title"] = "Siêu Phẩm Kịch Tính"

        result["thumbnail_title"] = cls.clean_text_for_vietnamese(result.get("thumbnail_title", "SỰ THẬT KINH HOÀNG"))

        if "rolling_summary" in result and result["rolling_summary"]:
            result["rolling_summary"] = cls.clean_text_for_vietnamese(result["rolling_summary"])

        for block in result["narration_blocks"]:
            raw_script = block.get("script", "")
            # Áp dụng chuẩn hóa tên nhân vật từ Story Bible nếu có
            if story_bible and hasattr(story_bible, "apply_glossary_to_text"):
                raw_script = story_bible.apply_glossary_to_text(raw_script)

            block["script"] = cls.clean_text_for_vietnamese(raw_script)
            if not block["script"]:
                raise RuntimeError("LỖI: Khối kịch bản review của Gemini bị rỗng!")

        print(f"[ReviewWriter] Đã hoàn tất biên soạn {len(result['narration_blocks'])} phân đoạn kịch bản thật từ Gemini.")
        return result
