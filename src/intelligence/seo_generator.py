import os
import json
import re
import unicodedata
from typing import Dict, Any, List, Optional
from config.settings import Settings
from src.intelligence.gemini_client import GeminiClient
from src.intelligence.review_writer import ReviewWriter

class SEOGenerator:
    """Tạo Tiêu đề, Mô tả, Hashtag và Chữ trên Thumbnail chuẩn SEO cho @CoolMeoPhim."""

    @classmethod
    def generate_clean_synopsis(cls, movie_title: str, raw_summary: str) -> str:
        """
        Gọi Gemini viết riêng 1 đoạn Tóm tắt tổng quan cả bộ phim duy nhất:
        - 3-4 câu súc tích, hấp dẫn, không spoil cái kết.
        - TUYỆT ĐỐI KHÔNG chứa rác [Diễn biến mới]: hay lặp ý.
        """
        if not raw_summary:
            return ""

        # Lọc sạch rác tag sơ bộ
        cleaned_input = re.sub(r"\[(?:Diễn biến mới|Tập\s*\d+)\]:\s*", "", raw_summary)
        cleaned_input = re.sub(r"\s+", " ", cleaned_input).strip()

        # Thử gọi Gemini AI
        try:
            client, model_name = GeminiClient.get_client_and_model()
            prompt = (
                f"Bạn là biên kịch chuyên nghiệp của kênh YouTube Cool Mèo Phim (@CoolMeoPhim).\n"
                f"Hãy viết một đoạn TÓM TẮT TỔNG QUAN NỘI DUNG duy nhất cho bộ phim '{movie_title}'.\n\n"
                f"Dữ liệu diễn biến các tập:\n\"{cleaned_input}\"\n\n"
                f"YÊU CẦU BẮT BUỘC:\n"
                f"1. Độ dài đúng 3 đến 4 câu súc tích, ngữ điệu kịch tính, lôi cuốn, văn phong review phim hấp dẫn.\n"
                f"2. Nêu bật hoàn cảnh, mâu thuẫn chính và hành trình của nhân vật chính.\n"
                f"3. TUYỆT ĐỐI KHÔNG spoil cái kết của phim.\n"
                f"4. TUYỆT ĐỐI KHÔNG chứa các nhãn rác như '[Diễn biến mới]:', '[Tập 1]:', 'Review:', v.v.\n"
                f"5. TUYỆT ĐỐI KHÔNG lặp ý trùng lặp giữa các tập.\n"
                f"6. 100% Tiếng Việt tự nhiên chuẩn văn phong review phim, không chữ Hán."
            )
            resp = client.models.generate_content(
                model=model_name,
                contents=prompt
            )
            if resp and resp.text:
                ai_synopsis = resp.text.strip()
                ai_synopsis = re.sub(r"^[\"']|[\"']$", "", ai_synopsis)
                ai_synopsis = re.sub(r"\[(?:Diễn biến mới|Tập\s*\d+)\]:\s*", "", ai_synopsis)
                ai_synopsis = ReviewWriter.clean_text_for_vietnamese(ai_synopsis)
                if len(ai_synopsis.split()) >= 20:
                    return ai_synopsis
        except Exception as e:
            print(f"[SEOGenerator] Gọi Gemini viết tóm tắt gặp lỗi: {e}. Áp dụng bộ lọc khử trùng tự động.")

        # Fallback lọc thông minh bằng Python (khử trùng câu và tag)
        sentences = re.split(r"(?<=[.!?])\s+", cleaned_input)
        unique_sents = []
        seen = set()
        for s in sentences:
            s_clean = s.strip()
            # Bỏ các câu ngắn hoặc lặp ý
            key = re.sub(r"[^\w\s]", "", s_clean.lower())[:40]
            if len(s_clean.split()) >= 6 and key not in seen:
                seen.add(key)
                unique_sents.append(s_clean)
            if len(unique_sents) >= 4:
                break

        return " ".join(unique_sents)

    @classmethod
    def generate_seo_package(
        cls,
        raw_title: str,
        vietnamese_title: str = "",
        thumbnail_title: str = "",
        rolling_summary: str = ""
    ) -> Dict[str, Any]:
        """Tạo trọn bộ thông tin SEO hoàn chỉnh sẵn sàng copy."""
        # Ưu tiên tiêu đề tiếng Việt đã dịch chuẩn, xóa sạch các hậu tố tập/phần (ví dụ: - Tập 1)
        clean_title = vietnamese_title.strip() if vietnamese_title else raw_title.strip()
        clean_title = ReviewWriter.clean_text_for_vietnamese(clean_title)
        clean_title = re.sub(r"(?:[-–—]\s*)?(?:tập|tap|ep|episode)\s*\d+.*$", "", clean_title, flags=re.IGNORECASE).strip()
        clean_title = re.sub(r"(?:[-–—]\s*)?phần\s*\d+.*$", "", clean_title, flags=re.IGNORECASE).strip()
        clean_title = re.sub(r"^(?:review\s*phim|review\s*trọn\s*bộ)\s*", "", clean_title, flags=re.IGNORECASE).strip()
        clean_title = re.sub(r"[|:\"'«»]", "", clean_title).strip()
        if not clean_title:
            clean_title = "Siêu Phẩm Kịch Tính"

        # 1. Tiêu đề chuẩn SEO theo yêu cầu: "Review Trọn Bộ [Tên Phim] | Full Từ Đầu Đến Kết"
        final_title = f"Review Trọn Bộ {clean_title} | Full Từ Đầu Đến Kết"
        final_title = ReviewWriter.clean_text_for_vietnamese(final_title)[:95].strip()

        # 2. Tiêu đề ngắn cho Thumbnail (3-5 từ giật tít, tò mò)
        if thumbnail_title:
            thumb_title = ReviewWriter.clean_text_for_vietnamese(thumbnail_title).upper()
        else:
            words = clean_title.split()
            if len(words) >= 3:
                thumb_title = " ".join(words[:4]).upper()
            else:
                thumb_title = "SỰ THẬT KINH HOÀNG"

        # 3. Danh sách Hashtag xu hướng
        hashtags = [
            "#CoolMeoPhim",
            "#ReviewPhim",
            "#TomTatPhim",
            "#PhimNganTrungQuoc",
            "#PhimHay2026",
            "#PhimMoi",
            "#PhimXuyenKhong",
            "#PhimTrongSinh",
            "#PhimTongTai",
            "#ReviewPhimHay",
            "#DramaTrungQuoc",
            "#TikTokPhim"
        ]

        # 4. Mô tả chi tiết kèm Fair Use & CTA (Tóm tắt sạch từ Gemini, không rác tag [Diễn biến mới]:)
        clean_synopsis = cls.generate_clean_synopsis(clean_title, rolling_summary)
        summary_block = f"📖 TÓM TẮT NỘI DUNG:\n{clean_synopsis}\n\n" if clean_synopsis else ""

        description = (
            f"🎬 {final_title}\n\n"
            f"Chào mừng các bạn đến với kênh {Settings.CHANNEL_NAME} ({Settings.CHANNEL_HANDLE})!\n"
            f"Đón xem toàn bộ diễn biến kịch tính và nghẹt thở của bộ phim '{clean_title}'.\n\n"
            f"{summary_block}"
            "👉 Hãy nhấn LIKE, BÌNH LUẬN cảm nghĩ của bạn và bấm ĐĂNG KÝ KÊNH để ủng hộ Cool Mèo Phim ra thêm nhiều video review chất lượng nhé!\n\n"
            "--------------------------------------------------\n"
            "⚖️ TUYÊN BỐ MIỄN TRỪ TRÁCH NHIỆM (FAIR USE):\n"
            "- Kênh @CoolMeoPhim không sở hữu toàn bộ hình ảnh gốc của bộ phim. Video được biên tập, "
            "thuyết minh và sáng tạo lại với mục đích bình luận, phê bình và giải trí theo nguyên tắc Sử dụng Hợp lý (Fair Use Law).\n"
            "- Mọi vấn đề liên quan đến bản quyền hoặc hợp tác nội dung, xin vui lòng liên hệ qua email của kênh.\n\n"
            + " ".join(hashtags)
        )

        return {
            "title": final_title,
            "thumbnail_title": thumb_title,
            "description": description,
            "tags": [h.replace("#", "") for h in hashtags],
            "channel_handle": Settings.CHANNEL_HANDLE,
            "channel_name": Settings.CHANNEL_NAME
        }

    @classmethod
    def export_seo_files(cls, seo_data: Dict[str, Any], output_dir: str):
        """Xuất ra cả file json và txt tiện dụng để copy ngay lập tức."""
        os.makedirs(output_dir, exist_ok=True)
        # 1. File JSON
        json_path = os.path.join(output_dir, "seo_metadata.json")
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(seo_data, f, ensure_ascii=False, indent=2)

        # 2. File TXT
        txt_path = os.path.join(output_dir, "seo_ready_to_copy.txt")
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write(f"=== TIÊU ĐỀ VIDEO (TITLE) ===\n{seo_data['title']}\n\n")
            f.write(f"=== CHỮ TRÊN THUMBNAIL ===\n{seo_data['thumbnail_title']}\n\n")
            f.write(f"=== MÔ TẢ VIDEO (DESCRIPTION) ===\n{seo_data['description']}\n\n")
            f.write(f"=== DANH SÁCH TAGS ===\n{', '.join(seo_data['tags'])}\n")
        print(f"[SEOGenerator] Đã xuất file SEO: {txt_path}")
