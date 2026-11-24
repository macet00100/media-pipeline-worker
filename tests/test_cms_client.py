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
        with patch("urllib.request.urlopen") as mock_urlopen:
            import urllib.error
            mock_urlopen.side_effect = urllib.error.HTTPError(
                url="https://real-cdn.com/test/index.m3u8",
                code=404,
                msg="Not Found",
                hdrs={},
                fp=None
            )
            is_alive, reason = MovieCurator.preflight_check_m3u8(["https://real-cdn.com/test/index.m3u8"])
            self.assertFalse(is_alive)
            self.assertIn("404", reason)

        # Test link trả về HTML thay vì M3U8
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.status = 200
            mock_resp.read.return_value = b"<!DOCTYPE html><html><body>Error</body></html>"
            mock_urlopen.return_value.__enter__.return_value = mock_resp

            is_alive, reason = MovieCurator.preflight_check_m3u8(["https://real-cdn.com/test/index.m3u8"])
            self.assertFalse(is_alive)
            self.assertIn("invalid_m3u8_header", reason)

        # Test link M3U8 chuẩn 200 với #EXTM3U
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.status = 200
            mock_resp.read.return_value = b"#EXTM3U\n#EXT-X-VERSION:3\n#EXTINF:10.0,"
            mock_urlopen.return_value.__enter__.return_value = mock_resp

            is_alive, reason = MovieCurator.preflight_check_m3u8(["https://real-cdn.com/test/index.m3u8"])
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
