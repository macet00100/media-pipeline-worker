import unittest
from unittest.mock import patch, MagicMock
from src.sources.cms_client import CMSClient
from src.sources.movie_curator import MovieCurator

class TestCMSClientAndCuratorLinkSafety(unittest.TestCase):

    def test_extract_m3u8_prioritizes_lzm3u8_and_ignores_share_links(self):
        """
        Kiểm tra CMSClient._extract_m3u8_groups:
        - Bỏ qua link /share/...
        - Ưu tiên nhóm player 'lzm3u8' (direct m3u8) thay vì player 'liangzi' (chứa /share/).
        """
        play_from = "liangzi$$$lzm3u8"
        play_url = (
            "第01集$https://lzv34.com/share/bad_token_ep1#第02集$https://lzv34.com/share/bad_token_ep2"
            "$$$"
            "第01集$https://v.lzcdn27.com/20261124/1001_abc/index.m3u8#第02集$https://v.lzcdn27.com/20261124/1002_xyz/index.m3u8"
        )

        extracted = CMSClient._extract_m3u8_groups(play_url, play_from)
        self.assertEqual(len(extracted), 2)
        self.assertTrue(all(".m3u8" in u for u in extracted))
        self.assertFalse(any("/share/" in u for u in extracted))
        self.assertEqual(extracted[0], "https://v.lzcdn27.com/20261124/1001_abc/index.m3u8")
        self.assertEqual(extracted[1], "https://v.lzcdn27.com/20261124/1002_xyz/index.m3u8")

    def test_extract_m3u8_rejects_share_only_group(self):
        """Nếu tất cả các link trong nhóm chỉ là /share/, hàm phải loại bỏ và trả về rỗng."""
        play_from = "liangzi"
        play_url = "第01集$https://lzv34.com/share/abcd#第02集$https://lzv34.com/share/xyz"
        extracted = CMSClient._extract_m3u8_groups(play_url, play_from)
        self.assertEqual(extracted, [])

    def test_preflight_check_m3u8_detects_http_404_and_invalid_header(self):
        """Kiểm tra preflight_check_m3u8 phát hiện link chết 404 và link không có #EXTM3U."""
        # Test link 404
        with patch("requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 404
            mock_get.return_value = mock_resp
            is_alive, reason = MovieCurator.preflight_check_m3u8(["https://real-cdn.com/test/index.m3u8"])
            self.assertFalse(is_alive)
            self.assertIn("404", reason)

        # Test link trả về HTML thay vì M3U8
        with patch("requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.text = "<!DOCTYPE html><html><body>Error</body></html>"
            mock_get.return_value = mock_resp

            is_alive, reason = MovieCurator.preflight_check_m3u8(["https://real-cdn.com/test/index.m3u8"])
            self.assertFalse(is_alive)
            self.assertTrue("html_error_page" in reason or "invalid_m3u8_header" in reason)

        # Test link M3U8 chuẩn 200 với #EXTM3U
        with patch("requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.text = "#EXTM3U\n#EXT-X-VERSION:3\n#EXTINF:10.0,"
            mock_get.return_value = mock_resp

            is_alive, reason = MovieCurator.preflight_check_m3u8(["https://real-cdn.com/test/index.m3u8"])
            self.assertTrue(is_alive)
            self.assertEqual(reason, "ok")

    def test_preflight_check_m3u8_ts_sample_verification(self):
        """Kiểm tra preflight_check_m3u8 tải thử mẫu 256 KB phân đoạn .ts và bắt lỗi tốc độ."""
        manifest_text = "#EXTM3U\n#EXT-X-VERSION:3\n#EXTINF:10.0,\nhttps://speed-cdn.com/seg1.ts"
        sample_ts_data = b"0" * (256 * 1024)

        with patch("requests.get") as mock_get:
            mock_manifest_resp = MagicMock()
            mock_manifest_resp.status_code = 200
            mock_manifest_resp.text = manifest_text
            mock_manifest_resp.headers = {"Content-Type": "application/vnd.apple.mpegurl"}

            mock_ts_resp = MagicMock()
            mock_ts_resp.status_code = 200
            mock_ts_resp.headers = {"Content-Type": "video/mp2t"}
            mock_ts_resp.iter_content = MagicMock(return_value=[sample_ts_data[:65536], sample_ts_data[65536:131072], sample_ts_data[131072:]])

            mock_get.side_effect = [mock_manifest_resp, mock_ts_resp]

            is_alive, reason = MovieCurator.preflight_check_m3u8(["https://speed-cdn.com/index.m3u8"])
            self.assertTrue(is_alive)
            self.assertEqual(reason, "ok")

    def test_short_drama_one_file_priority_in_selection(self):
        """Kiểm tra đoản kịch trọn bộ 1 file (<= 5 tập) được ưu tiên hàng đầu khi chọn Top 3."""
        candidates = [
            {
                "id": "lz_multi_ep",
                "source_id": "lz",
                "category": "short_drama",
                "episodes_count": 80,
                "score": 90,
                "is_completed": True
            },
            {
                "id": "lz_one_file",
                "source_id": "lz",
                "category": "short_drama",
                "episodes_count": 1,
                "score": 85,
                "is_completed": True
            },
            {
                "id": "lz_long",
                "source_id": "lz",
                "category": "long_series",
                "episodes_count": 16,
                "score": 88,
                "is_completed": True
            }
        ]

        top3 = MovieCurator.select_top_candidates(candidates, target_total=2, target_short=2, target_long=0)
        # Đoản kịch 1 file (episodes_count <= 5) phải được đứng trước bộ 80 tập
        self.assertEqual(top3[0]["id"], "lz_one_file")
        self.assertEqual(top3[1]["id"], "lz_multi_ep")

if __name__ == "__main__":
    unittest.main()
