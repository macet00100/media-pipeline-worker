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
    def generate_thumbnail_layers(
        cls,
        movie_title: str,
        rolling_summary: str = "",
        fallback_title: str = ""
    ) -> Dict[str, str]:
        """
        Dùng Gemini tạo cấu trúc tiêu đề Thumbnail 3 tầng chuẩn kênh triệu view:
        {
          "line1_red_brush": "2-3 từ bối cảnh/hook, tối đa 16 ký tự",
          "line2_big_yellow": "2-3 từ hành động cực lớn, tối đa 15 ký tự",
          "line3_yellow_block": "3-5 từ kết quả gây sốc (tự ngắt 2 dòng nếu dài)"
        }
        """
        def _clamp_line(text: str, max_words: int, max_chars: int) -> str:
            cleaned = re.sub(r"\s+", " ", text).strip()
            words = cleaned.split()
            if len(words) > max_words:
                words = words[:max_words]
            res = " ".join(words)
            while len(res) > max_chars and len(words) > 1:
                words.pop()
                res = " ".join(words)
            if len(res) > max_chars:
                res = res[:max_chars].strip()
            return res

        try:
            client, model_name = GeminiClient.get_client_and_model()
            prompt = (
                f"Bạn là chuyên gia thiết kế thumbnail YouTube triệu view của kênh Cool Mèo Phim (@CoolMeoPhim).\n"
                f"Hãy viết tiêu đề Thumbnail 3 tầng cực kỳ giật gân, tò mò, chuẩn tiếng Việt cho phim: '{movie_title}'.\n\n"
                f"Nội dung tóm tắt phim:\n\"{rolling_summary[:800]}\"\n\n"
                f"QUY TẮC ĐỘ DÀI & ĐA DẠNG BẮT BUỘC:\n"
                f"- line1_red_brush: CHỈ 2–3 TỪ, TỐI ĐA 16 KÝ TỰ (VD: XUYÊN KHÔNG, TIỂU THƯ THẬT, CHIẾN THẦN, THÁI TỬ PHI).\n"
                f"- line2_big_yellow: CHỈ 2–3 TỪ, TỐI ĐA 15 KÝ TỰ (VD: VẢ MẶT, ĐOẠT QUYỀN, BÁN MÌ, TRẢ THÙ, LẬP NGHIỆP).\n"
                f"- line3_yellow_block: CHỈ 3–5 TỪ, TỐI ĐA 28 KÝ TỰ.\n"
                f"  * TUYỆT ĐỐI KHÔNG DÙNG CÂU SÁO RỖNG HOẶC LẶP 'CẢ NHÀ QUỲ XIN' CHO MỌI PHIM.\n"
                f"  * Bắt buộc bám sát tình tiết và nhân vật cụ thể của câu chuyện:\n"
                f"    - Phim cổ trang/cung đấu: 'DI NƯƠNG SỢ HÃI!', 'HOÀNG ĐẾ HỐI HẬN!', 'CUNG ĐÌNH CHẤN ĐỘNG!', 'HOÀNG HẬU BẼ MẶT!'.\n"
                f"    - Phim hào môn/đô thị: 'MẸ KẾ HOẢNG LOẠN!', 'CẢ HỌ THẨM BẼ MẶT!', 'TỔNG TÀI QUỲ GỐI!', 'GIA TỘC SỤP ĐỔ!'.\n\n"
                f"YÊU CẦU CẤU TRÚC JSON CHÍNH XÁC:\n"
                f"{{\n"
                f'  "line1_red_brush": "2-3 từ bối cảnh/hook",\n'
                f'  "line2_big_yellow": "2-3 từ hành động cực sốc",\n'
                f'  "line3_yellow_block": "3-5 từ kết quả gây sốc bám sát cốt truyện"\n'
                f"}}\n"
                f"Lưu ý: 100% tiếng Việt có dấu Unicode NFC, viết HOA toàn bộ các chữ. Trả về đúng định dạng JSON thuần túy."
            )
            resp = client.models.generate_content(
                model=model_name,
                contents=prompt
            )
            if resp and resp.text:
                txt = resp.text.strip()
                # Remove code blocks if present
                txt = re.sub(r"^```(?:json)?", "", txt, flags=re.MULTILINE)
                txt = re.sub(r"```$", "", txt, flags=re.MULTILINE).strip()
                data = json.loads(txt)
                l1 = ReviewWriter.clean_text_for_vietnamese(data.get("line1_red_brush", "")).upper().strip()
                l2 = ReviewWriter.clean_text_for_vietnamese(data.get("line2_big_yellow", "")).upper().strip()
                l3 = ReviewWriter.clean_text_for_vietnamese(data.get("line3_yellow_block", "")).upper().strip()

                # Chống lặp câu 'CẢ NHÀ QUỲ XIN'
                if "QUỲ XIN" in l3 and ("CẢ NHÀ" in l3 or "CẢ HỌ" in l3):
                    sum_lower = (rolling_summary + " " + movie_title).lower()
                    if any(k in sum_lower for k in ["cung", "hoàng", "phi", "đế", "thù"]):
                        l3 = "CUNG ĐÌNH CHẤN ĐỘNG!"
                    elif any(k in sum_lower for k in ["thẩm", "mẹ kế", "hào môn", "tổng tài", "tiểu thư"]):
                        l3 = "MẸ KẾ HOẢNG LOẠN!"

                if l1 and l2 and l3:
                    return {
                        "line1_red_brush": _clamp_line(l1, 3, 16),
                        "line2_big_yellow": _clamp_line(l2, 3, 15),
                        "line3_yellow_block": _clamp_line(l3, 5, 28)
                    }
        except Exception as e:
            print(f"[SEOGenerator] Gemini tạo thumbnail_layers gặp lỗi: {e}. Áp dụng fallback.")

        # Fallback thông minh đa dạng theo thể loại
        sum_lower = (rolling_summary + " " + movie_title + " " + fallback_title).lower()
        if any(k in sum_lower for k in ["cung", "hoàng", "phi", "đế", "thù", "song thù"]):
            return {
                "line1_red_brush": "XUYÊN KHÔNG",
                "line2_big_yellow": "ĐOẠT QUYỀN",
                "line3_yellow_block": "CUNG ĐÌNH CHẤN ĐỘNG!"
            }
        elif any(k in sum_lower for k in ["thẩm", "mẹ kế", "hào môn", "tổng tài", "tiểu thư"]):
            return {
                "line1_red_brush": "TIỂU THƯ THẬT",
                "line2_big_yellow": "VẢ MẶT HÀO MÔN",
                "line3_yellow_block": "MẸ KẾ HOẢNG LOẠN!"
            }

        words = (fallback_title or movie_title).split()
        if len(words) >= 4:
            return {
                "line1_red_brush": "TRỌN BỘ FULL",
                "line2_big_yellow": _clamp_line(" ".join(words[:2]).upper(), 3, 15),
                "line3_yellow_block": _clamp_line(" ".join(words[2:]).upper() + "!", 5, 28)
            }
        return {
            "line1_red_brush": "SIÊU PHẨM",
            "line2_big_yellow": "KỊCH TÍNH",
            "line3_yellow_block": "BẤT NGỜ PHÚT CUỐI!"
        }

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

        # 2.1 Tiêu đề 3 tầng cho Thumbnail kiểu vệt cọ (triệu view)
        thumbnail_layers = cls.generate_thumbnail_layers(
            clean_title, rolling_summary=rolling_summary, fallback_title=thumb_title
        )

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
            "thumbnail_layers": thumbnail_layers,
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
        layers = seo_data.get("thumbnail_layers", {})
        layers_str = ""
        if layers:
            layers_str = (
                f"- Tầng 1 (Vệt cọ đỏ): {layers.get('line1_red_brush', '')}\n"
                f"- Tầng 2 (Chữ vàng lớn): {layers.get('line2_big_yellow', '')}\n"
                f"- Tầng 3 (Mảng cọ vàng): {layers.get('line3_yellow_block', '')}\n"
            )

        with open(txt_path, "w", encoding="utf-8") as f:
            f.write(f"=== TIÊU ĐỀ VIDEO (TITLE) ===\n{seo_data['title']}\n\n")
            f.write(f"=== CHỮ TRÊN THUMBNAIL (1 DÒNG) ===\n{seo_data['thumbnail_title']}\n\n")
            if layers_str:
                f.write(f"=== TIÊU ĐỀ THUMBNAIL 3 TẦNG (TRIỆU VIEW) ===\n{layers_str}\n")
            f.write(f"=== MÔ TẢ VIDEO (DESCRIPTION) ===\n{seo_data['description']}\n\n")
            f.write(f"=== DANH SÁCH TAGS ===\n{', '.join(seo_data['tags'])}\n")
        print(f"[SEOGenerator] Đã xuất file SEO: {txt_path}")
