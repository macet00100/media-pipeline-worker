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

    GENRE_MODERN = "modern"          # Hiện đại / Đô thị / Hào môn / Tổng tài
    GENRE_ANCIENT = "ancient"        # Cổ trang / Cung đấu / Kiếm hiệp
    GENRE_TRANSMIGRATION = "transmigration"  # Xuyên không / Trọng sinh
    GENRE_ACTION = "action"          # Hành động / Chiến thần / Sát thủ
    GENRE_GENERAL = "general"        # Kịch tính chung

    GENRE_NAMES_VI = {
        GENRE_MODERN: "Hiện đại / Đô thị / Hào môn",
        GENRE_ANCIENT: "Cổ trang / Cung đấu",
        GENRE_TRANSMIGRATION: "Xuyên không / Trọng sinh",
        GENRE_ACTION: "Hành động / Chiến thần",
        GENRE_GENERAL: "Kịch tính / Xung đột gia tộc"
    }

    FORBIDDEN_GENRE_KEYWORDS = {
        GENRE_MODERN: [
            "cung đình", "xuyên không", "hoàng thượng", "hoàng đế", "vương phi", "vương gia",
            "thái tử", "hoàng hậu", "di nương", "trắc phi", "thứ nữ", "lãnh cung", "bệ hạ",
            "thần thiếp", "hoàng huynh", "nương nương", "thái giám", "triều đình"
        ],
        GENRE_ANCIENT: [
            "tổng tài", "xe hơi", "bệnh viện", "công ty", "tập đoàn", "chủ tịch", "hợp đồng",
            "thực tập", "ly hôn", "du học"
        ]
    }

    @classmethod
    def detect_genre(
        cls,
        movie_title: str = "",
        rolling_summary: str = "",
        script_corpus: str = ""
    ) -> str:
        """
        Phân loại thể loại phim dựa trên tiêu đề, tóm tắt và kịch bản (Single Source of Truth).
        Trả về: 'modern', 'ancient', 'transmigration', 'action', hoặc 'general'.
        """
        corpus = f"{movie_title} {rolling_summary} {script_corpus}".lower()

        scores = {
            cls.GENRE_MODERN: 0,
            cls.GENRE_ANCIENT: 0,
            cls.GENRE_TRANSMIGRATION: 0,
            cls.GENRE_ACTION: 0
        }

        ancient_kws = [
            "cung đình", "hoàng đế", "hoàng hậu", "phi tần", "vương gia", "vương phi",
            "thái tử", "lãnh cung", "trắc phi", "thứ nữ", "di nương", "nương nương",
            "bệ hạ", "thần thiếp", "hoàng huynh", "thái giám", "triều đình", "tiên hiệp",
            "kiếm hiệp", "tu tiên", "cung đấu", "nhiếp chính vương", "hoàng triều", "hoàng cung"
        ]
        for kw in ancient_kws:
            cnt = corpus.count(kw)
            if cnt:
                scores[cls.GENRE_ANCIENT] += cnt * 3

        trans_kws = [
            "xuyên không", "trọng sinh", "sống lại một đời", "chuyển sinh",
            "kiếp trước", "sống lại", "xuyên thành", "xuyên về"
        ]
        for kw in trans_kws:
            cnt = corpus.count(kw)
            if cnt:
                scores[cls.GENRE_TRANSMIGRATION] += cnt * 4

        modern_kws = [
            "tổng tài", "hào môn", "tập đoàn", "công ty", "hợp đồng", "ly hôn",
            "mẹ kế", "tiểu thư", "thiếu gia", "bệnh viện", "bác sĩ", "chủ tịch",
            "đô thị", "hiện đại", "thực tập", "hôn nhân", "phá sản", "thư ký",
            "thương trường", "người yêu cũ", "thủ tiết", "trả thù", "vợ cũ", "chồng cũ",
            "bạn gái", "bạn trai", "mười tám năm", "18 năm", "năm xưa", "thẩm gia", "cố gia"
        ]
        for kw in modern_kws:
            cnt = corpus.count(kw)
            if cnt:
                scores[cls.GENRE_MODERN] += cnt * 2

        action_kws = [
            "chiến thần", "chiến vương", "đại soái", "binh vương", "sát thủ",
            "dị năng", "võ thần", "đặc công", "vua chiến trường"
        ]
        for kw in action_kws:
            cnt = corpus.count(kw)
            if cnt:
                scores[cls.GENRE_ACTION] += cnt * 3

        max_genre = max(scores, key=scores.get)
        if scores[max_genre] > 0:
            return max_genre

        title_lower = movie_title.lower()
        if any(w in title_lower for w in ["hoàng", "phi", "cung", "vương", "đế", "kiếm", "nương"]):
            return cls.GENRE_ANCIENT
        if any(w in title_lower for w in ["xuyên", "trọng sinh", "sống lại"]):
            return cls.GENRE_TRANSMIGRATION
        if any(w in title_lower for w in ["chiến thần", "soái", "sát thủ"]):
            return cls.GENRE_ACTION

        return cls.GENRE_MODERN

    @classmethod
    def generate_clean_synopsis(
        cls,
        movie_title: str,
        raw_summary: str,
        script_corpus: str = ""
    ) -> str:
        """
        Gọi Gemini viết riêng 1 đoạn Tóm tắt tổng quan cả bộ phim duy nhất:
        - Tổng hợp TOÀN CỤC toàn bộ nội dung thành 3-4 câu súc tích, hấp dẫn.
        - TUYỆT ĐỐI CẤM mở đầu bằng các câu kiểu tập lẻ như 'Tập phim tiếp tục...', 'Diễn biến tiếp theo...', 'Ở tập này...'.
        - TUYỆT ĐỐI KHÔNG chứa rác [Diễn biến mới]: hay lặp ý.
        """
        combined_source = f"{raw_summary} {script_corpus[:1500]}".strip()
        if not combined_source:
            return ""

        # Lọc sạch rác tag sơ bộ và các cụm mở đầu kiểu tập lẻ
        cleaned_input = re.sub(r"\[(?:Diễn biến mới|Tập\s*\d+)\]:\s*", "", combined_source)
        cleaned_input = re.sub(r"(?i)\b(?:tập\s*phim\s*tiếp\s*tục|diễn\s*biến\s*tiếp\s*theo|ở\s*tập\s*này|tập\s*\d+|tiếp\s*tục\s*câu\s*chuyện)\b[:,-]?\s*", "", cleaned_input)
        cleaned_input = re.sub(r"\s+", " ", cleaned_input).strip()

        # Thử gọi Gemini AI
        try:
            client, model_name = GeminiClient.get_client_and_model()
            prompt = (
                f"Bạn là biên kịch chuyên nghiệp của kênh YouTube Cool Mèo Phim (@CoolMeoPhim).\n"
                f"Hãy viết một đoạn TÓM TẮT TOÀN CỤC CẢ BỘ PHIM duy nhất cho tác phẩm '{movie_title}'.\n\n"
                f"Dữ liệu nội dung câu chuyện:\n\"{cleaned_input[:2000]}\"\n\n"
                f"YÊU CẦU BẮT BUỘC:\n"
                f"1. Độ dài đúng 3 đến 4 câu súc tích, ngữ điệu kịch tính, lôi cuốn, văn phong review phim hấp dẫn.\n"
                f"2. Nêu bật hoàn cảnh, mâu thuẫn chính và hành trình của nhân vật chính từ đầu đến cao trào.\n"
                f"3. TUYỆT ĐỐI CẤM mở đầu bằng các cụm từ kiểu tập lẻ như 'Tập phim tiếp tục...', 'Diễn biến tiếp theo...', 'Ở tập này...', 'Tập 1:...'. Mở đầu trực tiếp bằng bối cảnh số phận nhân vật.\n"
                f"4. TUYỆT ĐỐI KHÔNG spoil cái kết của phim.\n"
                f"5. TUYỆT ĐỐI KHÔNG chứa các nhãn rác như '[Diễn biến mới]:', '[Tập 1]:', 'Review:', v.v.\n"
                f"6. TUYỆT ĐỐI KHÔNG lặp câu hoặc lặp ý giữa các tập.\n"
                f"7. 100% Tiếng Việt tự nhiên chuẩn văn phong review phim, không chữ Hán."
            )
            resp = client.models.generate_content(
                model=model_name,
                contents=prompt
            )
            if resp and resp.text:
                ai_synopsis = resp.text.strip()
                ai_synopsis = re.sub(r"^[\"']|[\"']$", "", ai_synopsis)
                ai_synopsis = re.sub(r"\[(?:Diễn biến mới|Tập\s*\d+)\]:\s*", "", ai_synopsis)
                # Xóa sạch nếu Gemini vô tình mở đầu bằng 'Tập phim tiếp tục'
                ai_synopsis = re.sub(r"(?i)^(?:tập\s*phim\s*tiếp\s*tục|diễn\s*biến\s*tiếp\s*theo|ở\s*tập\s*này|tập\s*\d+|tiếp\s*tục\s*câu\s*chuyện)\s*[:,-]?\s*", "", ai_synopsis).strip()
                ai_synopsis = ReviewWriter.clean_text_for_vietnamese(ai_synopsis)
                if len(ai_synopsis.split()) >= 20:
                    return ai_synopsis
        except Exception as e:
            print(f"[SEOGenerator] Gọi Gemini viết tóm tắt gặp lỗi: {e}. Áp dụng bộ lọc khử trùng tự động.")

        # Fallback lọc thông minh bằng Python (khử trùng câu, tag và loại bỏ câu mở đầu kiểu tập lẻ)
        sentences = re.split(r"(?<=[.!?])\s+", cleaned_input)
        unique_sents = []
        seen = set()
        for s in sentences:
            s_clean = s.strip()
            if re.search(r"(?i)^(?:tập\s*phim\s*tiếp\s*tục|diễn\s*biến\s*tiếp\s*theo|ở\s*tập\s*này|tập\s*\d+|tiếp\s*tục)", s_clean):
                continue
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
        fallback_title: str = "",
        script_corpus: str = "",
        genre: str = ""
    ) -> Dict[str, str]:
        """
        Dùng Gemini tạo cấu trúc tiêu đề Thumbnail 3 tầng chuẩn kênh triệu view:
        {
          "line1_red_brush": "2-3 từ bối cảnh/hook, tối đa 16 ký tự",
          "line2_big_yellow": "2-3 từ hành động cực lớn, tối đa 15 ký tự",
          "line3_yellow_block": "3-5 từ kết quả gây sốc (tự ngắt 2 dòng nếu dài)"
        }
        Bắt buộc bám sát 100% thể loại thật (Single Source of Truth), cấm lẫn lộn từ khóa thể loại khác.
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

        # Xác định thể loại chuẩn xác
        if not genre:
            genre = cls.detect_genre(movie_title, rolling_summary, script_corpus)
        genre_vi = cls.GENRE_NAMES_VI.get(genre, "Hiện đại / Đô thị")

        # Thử Gemini với cơ chế retry tối đa 2 lần (tổng 3 attempts)
        for attempt in range(3):
            try:
                client, model_name = GeminiClient.get_client_and_model()
                extra_hint = ""
                if attempt > 0:
                    extra_hint = "\nCHÚ Ý: Lần thử trước trả về JSON sai hoặc xung đột thể loại. Bắt buộc trả về đúng định dạng JSON chuẩn không có code fence!"

                prompt = (
                    f"Bạn là chuyên gia thiết kế thumbnail YouTube triệu view của kênh Cool Mèo Phim (@CoolMeoPhim).\n"
                    f"Hãy viết tiêu đề Thumbnail 3 tầng cực kỳ giật gân, tò mò, chuẩn tiếng Việt cho phim: '{movie_title}'.\n"
                    f"Thể loại phim: {genre_vi}.\n\n"
                    f"Nội dung tóm tắt phim:\n\"{rolling_summary[:800]}\"\n\n"
                    f"QUY TẮC BẮT BUỘC THEO THỂ LOẠI:\n"
                    f"- Bắt buộc 100% bám sát thể loại '{genre_vi}'.\n"
                    f"- TUYỆT ĐỐI CẤM dùng từ ngữ thuộc thể loại khác. "
                    f"(Ví dụ: Phim Hiện đại / Đô thị TUYỆT ĐỐI CẤM dùng 'XUYÊN KHÔNG', 'CUNG ĐÌNH', 'HOÀNG ĐẾ', 'VƯƠNG PHI', 'LÃNH CUNG', 'DI NƯƠNG').\n\n"
                    f"QUY TẮC ĐỘ DÀI & ĐA DẠNG BẮT BUỘC:\n"
                    f"- line1_red_brush: CHỈ 2–3 TỪ, TỐI ĐA 16 KÝ TỰ (VD: {('TIỂU THƯ THẬT, TỔNG TÀI, HÀO MÔN' if genre == cls.GENRE_MODERN else 'XUYÊN KHÔNG, CỔ TRANG, THÁI TỬ PHI')}).\n"
                    f"- line2_big_yellow: CHỈ 2–3 TỪ, TỐI ĐA 15 KÝ TỰ (VD: {('VẢ MẶT HÀO MÔN, LẬP NGHIỆP, TRẢ GIÁ' if genre == cls.GENRE_MODERN else 'ĐOẠT QUYỀN, TRANH SỦNG, TRẢ THÙ')}).\n"
                    f"- line3_yellow_block: CHỈ 3–5 TỪ, TỐI ĐA 28 KÝ TỰ (Bắt buộc bám sát tình tiết thực tế, TUYỆT ĐỐI KHÔNG lặp 'CẢ NHÀ QUỲ XIN').\n\n"
                    f"YÊU CẦU CẤU TRÚC JSON CHÍNH XÁC:\n"
                    f"{{\n"
                    f'  "line1_red_brush": "2-3 từ bối cảnh/hook",\n'
                    f'  "line2_big_yellow": "2-3 từ hành động cực sốc",\n'
                    f'  "line3_yellow_block": "3-5 từ kết quả gây sốc bám sát cốt truyện"\n'
                    f"}}\n"
                    f"Lưu ý: 100% tiếng Việt có dấu Unicode NFC, viết HOA toàn bộ các chữ. Trả về đúng định dạng JSON thuần túy.{extra_hint}"
                )
                resp = client.models.generate_content(
                    model=model_name,
                    contents=prompt
                )
                if resp and resp.text:
                    txt = resp.text.strip()
                    txt = re.sub(r"^```(?:json)?", "", txt, flags=re.MULTILINE)
                    txt = re.sub(r"```$", "", txt, flags=re.MULTILINE).strip()
                    data = json.loads(txt)
                    l1 = ReviewWriter.clean_text_for_vietnamese(data.get("line1_red_brush", "")).upper().strip()
                    l2 = ReviewWriter.clean_text_for_vietnamese(data.get("line2_big_yellow", "")).upper().strip()
                    l3 = ReviewWriter.clean_text_for_vietnamese(data.get("line3_yellow_block", "")).upper().strip()

                    # Chống lặp câu 'CẢ NHÀ QUỲ XIN'
                    if "QUỲ XIN" in l3 and ("CẢ NHÀ" in l3 or "CẢ HỌ" in l3):
                        if genre == cls.GENRE_ANCIENT:
                            l3 = "CUNG ĐÌNH CHẤN ĐỘNG!"
                        elif genre == cls.GENRE_MODERN:
                            l3 = "MẸ KẾ HOẢNG LOẠN!"
                        else:
                            l3 = "BẤT NGỜ PHÚT CUỐI!"

                    # Kiểm tra xem có dính từ cấm của thể loại không
                    combined_check = f"{l1} {l2} {l3}".lower()
                    forbidden_list = cls.FORBIDDEN_GENRE_KEYWORDS.get(genre, [])
                    has_conflict = any(kw in combined_check for kw in forbidden_list)
                    if has_conflict:
                        print(f"[SEOGenerator] ⚠️ Gemini sinh chữ thumbnail xung đột thể loại '{genre}'. Đang thử lại (lần {attempt + 1}/3)...")
                        continue

                    if l1 and l2 and l3:
                        return {
                            "line1_red_brush": _clamp_line(l1, 3, 16),
                            "line2_big_yellow": _clamp_line(l2, 4, 16),
                            "line3_yellow_block": _clamp_line(l3, 5, 28)
                        }
            except Exception as e:
                print(f"[SEOGenerator] Lỗi khi tạo thumbnail_layers với Gemini (lần {attempt + 1}/3): {e}")

        # Fallback thông minh TUYỆT ĐỐI BÁM THEO THỂ LOẠI (Cấm fallback ngầm sai thể loại)
        print(f"[SEOGenerator] Áp dụng fallback chuẩn thể loại '{genre}' cho thumbnail...")
        sum_lower = (rolling_summary + " " + movie_title + " " + fallback_title).lower()

        if genre == cls.GENRE_ANCIENT:
            return {
                "line1_red_brush": "CỔ TRANG",
                "line2_big_yellow": "ĐOẠT QUYỀN",
                "line3_yellow_block": "CUNG ĐÌNH CHẤN ĐỘNG!"
            }
        elif genre == cls.GENRE_TRANSMIGRATION:
            return {
                "line1_red_brush": "TRỌNG SINH",
                "line2_big_yellow": "VẢ MẶT",
                "line3_yellow_block": "CẢ GIA TỘC HỐI HẬN!"
            }
        elif genre == cls.GENRE_ACTION:
            return {
                "line1_red_brush": "CHIẾN THẦN",
                "line2_big_yellow": "QUY LÃI",
                "line3_yellow_block": "TOÀN THÀNH RUN RẨY!"
            }
        elif genre == cls.GENRE_MODERN:
            if any(k in sum_lower for k in ["hào môn", "tổng tài", "tiểu thư", "mẹ kế", "thẩm gia", "cố gia"]):
                return {
                    "line1_red_brush": "TIỂU THƯ THẬT",
                    "line2_big_yellow": "VẢ MẶT HÀO MÔN",
                    "line3_yellow_block": "MẸ KẾ HOẢNG LOẠN!"
                }
            words = (fallback_title or movie_title).split()
            if len(words) >= 4:
                return {
                    "line1_red_brush": "ĐÔ THỊ HÀO MÔN",
                    "line2_big_yellow": _clamp_line(" ".join(words[:2]).upper(), 3, 15),
                    "line3_yellow_block": _clamp_line(" ".join(words[2:]).upper() + "!", 5, 28)
                }
            return {
                "line1_red_brush": "HÀO MÔN THẾ GIA",
                "line2_big_yellow": "QUAY VỀ BÁO THÙ",
                "line3_yellow_block": "TỔNG TÀI HỐI HẬN!"
            }

        # Fallback chung
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
        rolling_summary: str = "",
        script_corpus: str = "",
        genre: str = ""
    ) -> Dict[str, Any]:
        """Tạo trọn bộ thông tin SEO hoàn chỉnh sẵn sàng copy (Thống nhất theo Genre và Script)."""
        # Ưu tiên tiêu đề tiếng Việt đã dịch chuẩn, xóa sạch các hậu tố tập/phần (ví dụ: - Tập 1)
        clean_title = vietnamese_title.strip() if vietnamese_title else raw_title.strip()
        clean_title = ReviewWriter.clean_text_for_vietnamese(clean_title)
        clean_title = re.sub(r"(?:[-–—]\s*)?(?:tập|tap|ep|episode)\s*\d+.*$", "", clean_title, flags=re.IGNORECASE).strip()
        clean_title = re.sub(r"(?:[-–—]\s*)?phần\s*\d+.*$", "", clean_title, flags=re.IGNORECASE).strip()
        clean_title = re.sub(r"^(?:review\s*phim|review\s*trọn\s*bộ)\s*", "", clean_title, flags=re.IGNORECASE).strip()
        clean_title = re.sub(r"[|:\"'«»]", "", clean_title).strip()
        if not clean_title:
            clean_title = "Siêu Phẩm Kịch Tính"

        # Xác định thể loại cốt lõi
        if not genre:
            genre = cls.detect_genre(clean_title, rolling_summary, script_corpus)

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

        # 2.1 Tiêu đề 3 tầng cho Thumbnail kiểu vệt cọ (triệu view) chuẩn theo Genre
        thumbnail_layers = cls.generate_thumbnail_layers(
            clean_title,
            rolling_summary=rolling_summary,
            fallback_title=thumb_title,
            script_corpus=script_corpus,
            genre=genre
        )

        # 3. Danh sách Hashtag xu hướng (Khớp 100% với thể loại thật)
        hashtags = [
            "#CoolMeoPhim",
            "#ReviewPhim",
            "#TomTatPhim",
            "#PhimNganTrungQuoc",
            "#PhimHay2026",
            "#PhimMoi",
            "#DramaTrungQuoc",
            "#TikTokPhim"
        ]
        if genre == cls.GENRE_MODERN:
            hashtags.extend(["#PhimTongTai", "#PhimHaoMon", "#PhimDoThi", "#ReviewPhimHay"])
        elif genre == cls.GENRE_ANCIENT:
            hashtags.extend(["#PhimCoTrang", "#PhimCungDau", "#KiemHiep", "#ReviewPhimHay"])
        elif genre == cls.GENRE_TRANSMIGRATION:
            hashtags.extend(["#PhimXuyenKhong", "#PhimTrongSinh", "#ReviewPhimHay"])
        elif genre == cls.GENRE_ACTION:
            hashtags.extend(["#PhimChienThan", "#PhimHanhDong", "#ReviewPhimHay"])
        else:
            hashtags.extend(["#ReviewPhimHay", "#PhimKichTinh"])

        # 4. Mô tả chi tiết kèm Fair Use & CTA (Tóm tắt sạch từ Gemini, không rác tag [Diễn biến mới]:)
        clean_synopsis = cls.generate_clean_synopsis(clean_title, rolling_summary, script_corpus=script_corpus)
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
            "genre": genre,
            "description": description,
            "tags": [h.replace("#", "") for h in hashtags],
            "channel_handle": Settings.CHANNEL_HANDLE,
            "channel_name": Settings.CHANNEL_NAME
        }

    @classmethod
    def export_seo_files(cls, seo_data: Dict[str, Any], output_dir: str):
        """Xuất ra cả file json và txt tiện dụng để copy ngay lập tức, kèm metadata thumbnail."""
        os.makedirs(output_dir, exist_ok=True)
        # 1. File JSON SEO
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
            f.write(f"=== TIÊU ĐỀ VIDEO (TITLE) ===\n{seo_data.get('title', '')}\n\n")
            f.write(f"=== CHỮ TRÊN THUMBNAIL (1 DÒNG) ===\n{seo_data.get('thumbnail_title', '')}\n\n")
            if layers_str:
                f.write(f"=== TIÊU ĐỀ THUMBNAIL 3 TẦNG (TRIỆU VIEW) ===\n{layers_str}\n")
            f.write(f"=== MÔ TẢ VIDEO (DESCRIPTION) ===\n{seo_data.get('description', '')}\n\n")
            tags = seo_data.get('tags', [])
            f.write(f"=== DANH SÁCH TAGS ===\n{', '.join(tags)}\n")

        # 3. File metadata Thumbnail chuyên dụng cho QA Gate đối chiếu trực tiếp
        thumb_meta_path = os.path.join(output_dir, "thumbnail_metadata.json")
        with open(thumb_meta_path, "w", encoding="utf-8") as f:
            json.dump({
                "genre": seo_data.get("genre", cls.GENRE_MODERN),
                "line1_red_brush": layers.get("line1_red_brush", ""),
                "line2_big_yellow": layers.get("line2_big_yellow", ""),
                "line3_yellow_block": layers.get("line3_yellow_block", ""),
                "movie_title": seo_data.get("title", "")
            }, f, ensure_ascii=False, indent=2)

        print(f"[SEOGenerator] Đã xuất trọn bộ file SEO & Thumbnail Metadata: {output_dir}")
