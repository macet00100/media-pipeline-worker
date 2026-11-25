import os
import shutil
import unittest
from unittest.mock import patch, MagicMock
from pathlib import Path
from PIL import Image

from config.settings import TMP_DIR
from src.intelligence.seo_generator import SEOGenerator
from src.qa.validator import QualityValidator

class TestGenreAndThumbnailQA(unittest.TestCase):
    """
    Bộ kiểm thử đối chứng thể loại phim & Cổng QA Gate kiểm tra tính nhất quán Thumbnail:
    1. Phân loại thể loại chuẩn (Single Source of Truth): hiện đại, cổ trang, xuyên không.
    2. Sinh chữ thumbnail bám sát thể loại, cấm lẫn lộn từ khóa (cả luồng Gemini lẫn fallback).
    3. Tóm tắt nội dung loại bỏ hoàn toàn các câu mở đầu kiểu tập lẻ.
    4. Đối chứng QA Gate:
       (a) Phim hiện đại có thumbnail hiện đại -> PASS QA.
       (b) Phim hiện đại bị gắn chữ CUNG ĐÌNH / XUYÊN KHÔNG -> Bị QA Gate chặn FAIL ngay lập tức.
       (c) Thumbnail chứa placeholder kỹ thuật -> Bị QA Gate chặn FAIL.
    """

    def setUp(self):
        self.test_dir = TMP_DIR / "test_genre_thumb_qa"
        self.test_dir.mkdir(parents=True, exist_ok=True)

        # Tạo file thumbnail giả lập hợp lệ (> 10KB)
        self.mock_thumb = str(self.test_dir / "thumbnail.jpg")
        img = Image.new("RGB", (720, 1280), color=(120, 80, 200))
        img.save(self.mock_thumb, "JPEG", quality=95)
        self.assertGreater(os.path.getsize(self.mock_thumb), 10000)

    def tearDown(self):
        if self.test_dir.exists():
            shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_detect_genre_classification(self):
        """Kiểm tra độ chính xác của bộ phân loại thể loại detect_genre."""
        # 1. Phim Hiện đại đô thị / Hào môn (VD: lz_168038)
        genre_modern = SEOGenerator.detect_genre(
            movie_title="Người Tình Đợi Chờ Mười Tám Năm",
            rolling_summary="Cô gái bị chồng cũ phản bội và ly hôn, 18 năm sau quay lại trở thành tổng tài tập đoàn hào môn.",
            script_corpus="Tổng tài Thẩm gia sững sờ khi gặp lại người yêu cũ năm xưa trong buổi ký hợp đồng công ty."
        )
        self.assertEqual(genre_modern, SEOGenerator.GENRE_MODERN)

        # 2. Phim Cổ trang / Cung đấu
        genre_ancient = SEOGenerator.detect_genre(
            movie_title="Phế Hậu Trùng Sinh",
            rolling_summary="Hoàng hậu bị giam vào lãnh cung, hoàng đế sủng ái trắc phi khiến gia tộc bị tru di.",
            script_corpus="Nương nương bước vào triều đình, bệ hạ và các vương gia đều kinh ngạc trước khí chất của phế hậu."
        )
        self.assertEqual(genre_ancient, SEOGenerator.GENRE_ANCIENT)

        # 3. Phim Xuyên không / Trọng sinh
        genre_trans = SEOGenerator.detect_genre(
            movie_title="Trọng Sinh Chi Đích Nữ Khuynh Thành",
            rolling_summary="Nữ chính sống lại một đời kiếp trước, quyết tâm trả thù di nương và thứ muội độc ác.",
            script_corpus="Vừa xuyên không mở mắt ra, nàng nhận ra mình đã trọng sinh về năm mười sáu tuổi."
        )
        self.assertEqual(genre_trans, SEOGenerator.GENRE_TRANSMIGRATION)

    @patch("src.intelligence.gemini_client.GeminiClient.get_client_and_model")
    def test_modern_movie_thumbnail_generation_fallback_no_ancient_words(self, mock_client):
        """Khi Gemini lỗi, fallback cho phim hiện đại TUYỆT ĐỐI KHÔNG sinh chữ cổ trang / cung đình."""
        mock_client.side_effect = RuntimeError("Mất kết nối Gemini API")

        layers = SEOGenerator.generate_thumbnail_layers(
            movie_title="Người Tình Đợi Chờ Mười Tám Năm",
            rolling_summary="Chuyện tình đầy thù hận và ly hôn giữa tổng tài và cô vợ cũ.",
            genre=SEOGenerator.GENRE_MODERN
        )
        combined_text = f"{layers['line1_red_brush']} {layers['line2_big_yellow']} {layers['line3_yellow_block']}".lower()

        # Kiểm tra tuyệt đối không chứa từ khóa cổ trang
        for ancient_kw in ["cung đình", "xuyên không", "hoàng thượng", "hoàng đế", "vương phi", "lãnh cung", "di nương"]:
            self.assertNotIn(ancient_kw, combined_text, f"Phát hiện từ khóa cổ trang '{ancient_kw}' trong thumbnail phim hiện đại!")

        self.assertIn("HÀO MÔN", layers["line1_red_brush"] + " " + layers["line2_big_yellow"])

    @patch("src.intelligence.gemini_client.GeminiClient.get_client_and_model")
    def test_gemini_retry_on_genre_conflict(self, mock_client):
        """Gemini sinh nhầm chữ cổ trang cho phim hiện đại sẽ bị từ chối và retry tạo lại."""
        mock_ai = MagicMock()
        mock_client.return_value = (mock_ai, "gemini-model")

        # Lần 1: Gemini sinh nhầm XUYÊN KHÔNG / CUNG ĐÌNH
        resp1 = MagicMock()
        resp1.text = '{"line1_red_brush": "XUYÊN KHÔNG", "line2_big_yellow": "ĐOẠT QUYỀN", "line3_yellow_block": "CUNG ĐÌNH CHẤN ĐỘNG!"}'

        # Lần 2: Gemini sửa sai thành Hào Môn Đô Thị
        resp2 = MagicMock()
        resp2.text = '{"line1_red_brush": "TIỂU THƯ THẬT", "line2_big_yellow": "VẢ MẶT HÀO MÔN", "line3_yellow_block": "MẸ KẾ HOẢNG LOẠN!"}'

        mock_ai.models.generate_content.side_effect = [resp1, resp2]

        layers = SEOGenerator.generate_thumbnail_layers(
            movie_title="Người Tình Đợi Chờ Mười Tám Năm",
            rolling_summary="Hào môn đô thị tình thù",
            genre=SEOGenerator.GENRE_MODERN
        )
        self.assertEqual(layers["line1_red_brush"], "TIỂU THƯ THẬT")
        self.assertEqual(layers["line2_big_yellow"], "VẢ MẶT HÀO MÔN")
        self.assertEqual(layers["line3_yellow_block"], "MẸ KẾ HOẢNG LOẠN!")
        self.assertEqual(mock_ai.models.generate_content.call_count, 2)

    @patch("src.intelligence.gemini_client.GeminiClient.get_client_and_model")
    def test_synopsis_strips_episodic_openers(self, mock_client):
        """Tóm tắt nội dung không được mở đầu bằng kiểu tập lẻ như 'Tập phim tiếp tục...'."""
        mock_client.side_effect = RuntimeError("Mất kết nối Gemini API")

        raw_summary = "Tập phim tiếp tục với cảnh cô gái gặp lại người yêu cũ. Diễn biến tiếp theo là sự xuất hiện của mẹ kế độc ác. Ở tập này hai người quyết định ly hôn."
        synopsis = SEOGenerator.generate_clean_synopsis(
            movie_title="Người Tình Đợi Chờ Mười Tám Năm",
            raw_summary=raw_summary
        )
        self.assertFalse(synopsis.startswith("Tập phim tiếp tục"))
        self.assertFalse(synopsis.startswith("Diễn biến tiếp theo"))
        self.assertFalse(synopsis.startswith("Ở tập này"))

    def test_case_a_modern_movie_with_modern_thumbnail_passes_qa(self):
        """TRƯỜNG HỢP A: Phim hiện đại sinh chữ thumbnail đúng bối cảnh hiện đại -> PASS QA."""
        seo_data = {
            "title": "Review Trọn Bộ Người Tình Đợi Chờ Mười Tám Năm | Full Từ Đầu Đến Kết",
            "thumbnail_title": "VẢ MẶT HÀO MÔN",
            "genre": SEOGenerator.GENRE_MODERN,
            "thumbnail_layers": {
                "line1_red_brush": "TIỂU THƯ THẬT",
                "line2_big_yellow": "VẢ MẶT HÀO MÔN",
                "line3_yellow_block": "MẸ KẾ HOẢNG LOẠN!"
            },
            "description": "Mô tả review trọn bộ phim kịch tính.",
            "tags": ["CoolMeoPhim", "PhimTongTai"]
        }
        # Xuất metadata vào thư mục kiểm thử
        SEOGenerator.export_seo_files(seo_data, str(self.test_dir))

        ok, metrics = QualityValidator.check_thumbnail_consistency(
            thumbnail_path=self.mock_thumb,
            seo_data=seo_data,
            output_dir=str(self.test_dir)
        )
        self.assertTrue(ok, f"Thumbnail phim hiện đại hợp lệ nhưng bị đánh trượt: {metrics.get('error')}")
        self.assertEqual(metrics.get("genre"), "modern")

    def test_case_b_modern_movie_with_ancient_thumbnail_fails_qa(self):
        """TRƯỜNG HỢP B: Cố tình gắn chữ CUNG ĐÌNH / XUYÊN KHÔNG cho phim hiện đại -> QA Gate chặn FAIL."""
        # Phim hiện đại đô thị nhưng gắn nhầm thumbnail cổ trang
        seo_dirty = {
            "title": "Review Trọn Bộ Người Tình Đợi Chờ Mười Tám Năm | Full Từ Đầu Đến Kết",
            "thumbnail_title": "CUNG ĐÌNH TRANH SỦNG",
            "genre": SEOGenerator.GENRE_MODERN,
            "thumbnail_layers": {
                "line1_red_brush": "XUYÊN KHÔNG",
                "line2_big_yellow": "ĐOẠT QUYỀN",
                "line3_yellow_block": "CUNG ĐÌNH CHẤN ĐỘNG!"
            },
            "description": "Mô tả review trọn bộ.",
            "tags": ["CoolMeoPhim"]
        }
        SEOGenerator.export_seo_files(seo_dirty, str(self.test_dir))

        # Kiểm tra qua check_thumbnail_consistency
        ok, metrics = QualityValidator.check_thumbnail_consistency(
            thumbnail_path=self.mock_thumb,
            seo_data=seo_dirty,
            output_dir=str(self.test_dir)
        )
        self.assertFalse(ok, "QA Gate không chặn được thumbnail xung đột thể loại (Cung Đình trên phim Hiện Đại)!")
        self.assertIn("Xung đột thể loại", metrics.get("error", ""))

        # Kiểm tra qua cổng tổng thể validate_review_package -> Bắt buộc failed
        dummy_seg = [{"script": "Kênh Cool Mèo Phim xin chào các bạn."}]
        rep = QualityValidator.validate_review_package(
            final_video_path="",
            thumbnail_path=self.mock_thumb,
            seo_data=seo_dirty,
            aligned_segments=dummy_seg,
            overall_coverage=90.0,
            output_dir=str(self.test_dir)
        )
        self.assertFalse(rep["checks"]["thumbnail_exists"], "Tiêu chí thumbnail_exists phải là False khi xung đột thể loại!")
        self.assertFalse(rep["passed"], "QA Gate tổng thể phải đánh trượt (passed=False)!")

    def test_case_c_thumbnail_with_technical_placeholder_fails_qa(self):
        """TRƯỜNG HỢP C: Thumbnail chứa placeholder kỹ thuật mặc định -> Bị QA Gate chặn FAIL."""
        seo_placeholder = {
            "title": "Review Phim Thử Nghiệm",
            "thumbnail_title": "THỬ NGHIỆM",
            "genre": SEOGenerator.GENRE_MODERN,
            "thumbnail_layers": {
                "line1_red_brush": "CHỈ 2–3 TỪ",
                "line2_big_yellow": "LINE2_BIG_YELLOW",
                "line3_yellow_block": "KẾT QUẢ GÂY SỐC"
            },
            "description": "Mô tả",
            "tags": ["CoolMeoPhim"]
        }
        SEOGenerator.export_seo_files(seo_placeholder, str(self.test_dir))

        ok, metrics = QualityValidator.check_thumbnail_consistency(
            thumbnail_path=self.mock_thumb,
            seo_data=seo_placeholder,
            output_dir=str(self.test_dir)
        )
        self.assertFalse(ok)
        self.assertIn("placeholder", metrics.get("error", "").lower())

if __name__ == "__main__":
    unittest.main()
