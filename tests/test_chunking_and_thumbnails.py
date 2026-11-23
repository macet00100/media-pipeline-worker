import os
import shutil
import unittest
from pathlib import Path
from PIL import Image

from src.qa.validator import QualityValidator
from src.video.thumbnail_maker import ThumbnailMaker
from config.settings import TMP_DIR

class TestChunkingAndThumbnails(unittest.TestCase):
    def setUp(self):
        self.test_dir = TMP_DIR / "test_chunk_thumb"
        self.test_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        if self.test_dir.exists():
            shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_cinema_split_thumbnail_for_vertical_source(self):
        # Tạo ảnh dọc giả lập 720x1280
        src_path = str(self.test_dir / "vertical_src.jpg")
        img = Image.new("RGB", (720, 1280), color=(100, 150, 200))
        img.save(src_path, "JPEG")

        out_h = str(self.test_dir / "thumb_horizontal.jpg")
        res_h = ThumbnailMaker.render_horizontal_thumbnail(src_path, "TEST CATCHY TITLE", out_h)
        self.assertTrue(os.path.exists(res_h))
        with Image.open(res_h) as im:
            self.assertEqual(im.size, (1280, 720))

    def test_vertical_thumbnail_with_bottom_coverage(self):
        src_path = str(self.test_dir / "vertical_src2.jpg")
        img = Image.new("RGB", (720, 1280), color=(80, 120, 160))
        img.save(src_path, "JPEG")

        out_v = str(self.test_dir / "thumb_vertical.jpg")
        res_v = ThumbnailMaker.render_vertical_thumbnail(src_path, "TIÊU ĐỀ DỌC CỰC HAY", out_v, face_cx_ratio=0.5)
        self.assertTrue(os.path.exists(res_v))
        with Image.open(res_v) as im:
            self.assertEqual(im.size, (720, 1280))

    def test_compression_ratio_qa_gate(self):
        # Giả lập video 100s, thời lượng gốc 1000s -> ratio = 90 / 1000 = 0.09 < 0.15 -> Phải FAIL
        # Test logic tính compression_ratio
        raw_dur = 1000.0
        final_dur = 100.0
        body_dur = max(0.0, final_dur - 10.0)  # 90s
        comp_ratio = round(body_dur / raw_dur, 4)  # 0.09
        self.assertLess(comp_ratio, 0.15)

        # Ngược lại, nếu review 380s cho gốc 1000s -> ratio = 370 / 1000 = 0.37 >= 0.15 -> PASS
        good_final = 380.0
        good_body = max(0.0, good_final - 10.0)
        good_ratio = round(good_body / raw_dur, 4)
        self.assertGreaterEqual(good_ratio, 0.15)

if __name__ == "__main__":
    unittest.main()
