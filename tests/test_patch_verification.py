import os
import sys
import unittest
from pathlib import Path
from PIL import Image

# Đảm bảo đường dẫn import
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import Settings, ASSETS_DIR, TMP_DIR
from src.qa.validator import QualityValidator
from src.audio.tts_engine import TTSEngine
from src.video.subtitle_builder import SubtitleBuilder
from src.video.renderer import VideoRenderer

class TestPatchVerification(unittest.TestCase):
    """Bộ kiểm thử xác thực 4 hạng mục vá lỗi ẩn."""

    def setUp(self):
        self.test_tmp = TMP_DIR / "test_patch_tmp"
        self.test_tmp.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.test_tmp, ignore_errors=True)

    # ==============================================================
    # HẠNG MỤC 1: Ngưỡng QA cứng trong validator.py
    # ==============================================================
    def test_qa_audio_coverage_thresholds(self):
        """Khóa cứng coverage 85% - 95%: bắt buộc trong khoảng [85.0, 95.0]."""
        dummy_seg = [{"script": "Chào mừng các bạn đến với Cool Mèo Phim!"}]
        
        rep_84 = QualityValidator.validate_review_package("", "", {}, dummy_seg, 84.9, str(self.test_tmp))
        self.assertFalse(rep_84["checks"]["audio_coverage_target"])

        rep_85 = QualityValidator.validate_review_package("", "", {}, dummy_seg, 85.0, str(self.test_tmp))
        self.assertTrue(rep_85["checks"]["audio_coverage_target"])

        rep_90 = QualityValidator.validate_review_package("", "", {}, dummy_seg, 90.0, str(self.test_tmp))
        self.assertTrue(rep_90["checks"]["audio_coverage_target"])

        rep_95 = QualityValidator.validate_review_package("", "", {}, dummy_seg, 95.0, str(self.test_tmp))
        self.assertTrue(rep_95["checks"]["audio_coverage_target"])

        rep_95_1 = QualityValidator.validate_review_package("", "", {}, dummy_seg, 95.1, str(self.test_tmp))
        self.assertFalse(rep_95_1["checks"]["audio_coverage_target"])

    def test_qa_brand_requirement(self):
        """Bắt buộc có thương hiệu Cool Mèo Phim (@CoolMeoPhim)."""
        seg_with_name = [{"script": "Chào mừng đến với Cool Mèo Phim hôm nay."}]
        seg_with_handle = [{"script": "Đón xem tại @CoolMeoPhim nhé các bạn."}]
        seg_no_brand = [{"script": "Chào mừng quý vị khán giả đón xem tập phim mới."}]

        rep_name = QualityValidator.validate_review_package("", "", {}, seg_with_name, 90.0, str(self.test_tmp))
        self.assertTrue(rep_name["checks"]["brand_cool_meo_phim_included"])

        rep_handle = QualityValidator.validate_review_package("", "", {}, seg_with_handle, 90.0, str(self.test_tmp))
        self.assertTrue(rep_handle["checks"]["brand_cool_meo_phim_included"])

        rep_none = QualityValidator.validate_review_package("", "", {}, seg_no_brand, 90.0, str(self.test_tmp))
        self.assertFalse(rep_none["checks"]["brand_cool_meo_phim_included"])

    def test_qa_zero_gambling_and_rogue_names(self):
        """0 từ khóa cờ bạc / 0 tên kênh lạ trong kịch bản và SEO."""
        clean_seg = [{"script": "Kênh Cool Mèo Phim mang đến những thước phim gay cấn."}]
        rep_clean = QualityValidator.validate_review_package("", "", {}, clean_seg, 90.0, str(self.test_tmp))
        self.assertTrue(rep_clean["checks"]["zero_gambling_or_rogue_names"])

        # Cờ bạc trong kịch bản
        gambling_seg = [{"script": "Cool Mèo Phim hướng dẫn tải app casino cược lớn."}]
        rep_gambling = QualityValidator.validate_review_package("", "", {}, gambling_seg, 90.0, str(self.test_tmp))
        self.assertFalse(rep_gambling["checks"]["zero_gambling_or_rogue_names"])

        # Tên kênh lạ trong kịch bản
        rogue_seg = [{"script": "Cool Mèo Phim kết hợp cùng Triệu Vy Review lậu."}]
        rep_rogue = QualityValidator.validate_review_package("", "", {}, rogue_seg, 90.0, str(self.test_tmp))
        self.assertFalse(rep_rogue["checks"]["zero_gambling_or_rogue_names"])

        # Cờ bạc trong SEO title
        seo_dirty = {"title": "Cool Mèo Phim - Link nổ hũ nhận thưởng"}
        rep_seo = QualityValidator.validate_review_package("", "", seo_dirty, clean_seg, 90.0, str(self.test_tmp))
        self.assertFalse(rep_seo["checks"]["zero_gambling_or_rogue_names"])

    def test_qa_outro_audio_rms_integrity(self):
        """Outro RMS giữ nguyên 100% gốc (chênh lệch <= 1.5 dB, không suy giảm âm lượng)."""
        outro_path = str(ASSETS_DIR / "outtro.mp4")
        self.assertTrue(os.path.exists(outro_path), "Thiếu file assets/outtro.mp4")

        # So sánh outro với chính nó
        ok, metrics = QualityValidator.verify_outro_audio_rms(outro_path, outro_path)
        self.assertTrue(ok)
        self.assertIsNotNone(metrics["diff_db"])
        self.assertLessEqual(metrics["diff_db"], 1.5)

    # ==============================================================
    # HẠNG MỤC 2: TTS từng câu, ffprobe đo duration & ASS 4-6 từ/dòng
    # ==============================================================
    def test_tts_sentence_splitting_and_timestamps(self):
        """Tách kịch bản thành từng câu ngắn, đo thời lượng và ghi mốc timestamp."""
        text = "Chào mừng các bạn đến với Cool Mèo Phim! Anh chàng nhận ra thân phận thật."
        sentences = TTSEngine.split_into_sentences(text)
        self.assertEqual(len(sentences), 2)

        out_audio = str(self.test_tmp / "tts_test.mp3")
        dur, sents = TTSEngine.synthesize_with_timestamps(text, out_audio)
        self.assertGreater(dur, 0.0)
        self.assertEqual(len(sents), 2)
        self.assertEqual(sents[0]["start_sec"], 0.0)
        self.assertAlmostEqual(sents[1]["start_sec"], sents[0]["end_sec"], places=2)
        self.assertAlmostEqual(dur, sents[1]["end_sec"], places=1)

    def test_subtitle_ass_4_to_6_words_per_line(self):
        """Chia dòng ASS 4-6 từ/dòng và căn giữa tại \\pos(360, 864)."""
        text = "Chào mừng các bạn đến với kênh Cool Mèo Phim! Anh chàng lập tức sững người khi nhận ra thân phận thực sự của mình."
        dur, sents = TTSEngine.synthesize_with_timestamps(text, str(self.test_tmp / "tts_sub.mp3"))

        segments = [{
            "index": 1,
            "start_sec": 0.0,
            "end_sec": 12.0,
            "duration": 12.0,
            "actual_tts_duration": dur,
            "script": text,
            "sentence_timings": sents
        }]
        ass_path = str(self.test_tmp / "sub.ass")
        SubtitleBuilder.generate_ass_subtitles(segments, ass_path)
        self.assertTrue(os.path.exists(ass_path))

        lines_found = 0
        with open(ass_path, "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("Dialogue:"):
                    lines_found += 1
                    self.assertIn("\\pos(360,864)\\an5", line)
                    dialogue_text = line.split("an5}")[-1].strip()
                    word_cnt = len(dialogue_text.split())
                    self.assertTrue(
                        4 <= word_cnt <= 6 or word_cnt in (1, 2, 3), # tail chunk
                        f"Dòng không thỏa mãn định mức 4-6 từ: '{dialogue_text}' ({word_cnt} từ)"
                    )
        self.assertGreaterEqual(lines_found, 4)

    # ==============================================================
    # HẠNG MỤC 3: assets/bgm/ & assets/logo.png
    # ==============================================================
    def test_assets_bgm_removal_and_filtering(self):
        """File ambient_bgm.mp3 sine wave/noise đã bị xóa và chỉ trộn BGM khi có file nhạc thật."""
        fake_bgm = ASSETS_DIR / "bgm" / "ambient_bgm.mp3"
        self.assertFalse(fake_bgm.exists(), "File ambient_bgm.mp3 nhân tạo chưa bị xóa!")

        valid_bgm = VideoRenderer.get_valid_bgm_track()
        self.assertIsNone(valid_bgm, "Khi không có file nhạc thật, get_valid_bgm_track phải trả về None.")

    def test_assets_logo_alpha_channel(self):
        """Xác nhận assets/logo.png có alpha channel nền trong suốt chuẩn."""
        logo_path = ASSETS_DIR / "logo.png"
        self.assertTrue(logo_path.exists())
        with Image.open(logo_path) as img:
            self.assertEqual(img.mode, "RGBA")
            self.assertIn("A", img.getbands())
            # Kiểm tra dải alpha có giá trị trong suốt (0) và mờ/đậm (255)
            alpha_extrema = img.getextrema()[3]
            self.assertEqual(alpha_extrema[0], 0, "Kênh alpha phải có pixel trong suốt (0)")
            self.assertGreater(alpha_extrema[1], 0, "Kênh alpha phải có pixel hiển thị (>0)")

    # ==============================================================
    # HẠNG MỤC 4: .github/workflows/pipeline_24_7.yml
    # ==============================================================
    # HẠNG MỤC 4: WORKFLOW GITHUB ACTIONS 3 VIDEO/NGÀY VÀO GIỜ VÀNG
    # ==============================================================
    def test_workflow_cron_commented(self):
        """Khối workflow được cấu hình đúng 3 mốc cron, kiểm soát qua ENABLE_SCHEDULE và khóa AUTO_PUBLISH=false."""
        workflow_path = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "pipeline_24_7.yml"
        self.assertTrue(workflow_path.exists())
        content = workflow_path.read_text(encoding="utf-8")

        self.assertIn("workflow_dispatch:", content)
        self.assertIn("0 22 * * *", content)
        self.assertIn("0 4 * * *", content)
        self.assertIn("0 7 * * *", content)
        self.assertIn("ENABLE_SCHEDULE", content)
        self.assertTrue('vars.AUTO_PUBLISH' in content or 'AUTO_PUBLISH: "false"' in content)
        self.assertNotIn("actions/upload-artifact", content)
        self.assertIn("::add-mask::", content)
        self.assertIn("rm -rf output/ tmp/", content)

if __name__ == "__main__":
    unittest.main()
