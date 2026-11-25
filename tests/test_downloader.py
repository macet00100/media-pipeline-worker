import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from src.sources.downloader import Downloader

class TestDownloader(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_parse_m3u8_segments_local(self):
        """Kiểm tra phân tích danh sách segment từ file m3u8 cục bộ."""
        m3u8_file = self.tmp_path / "test.m3u8"
        content = (
            "#EXTM3U\n"
            "#EXT-X-VERSION:3\n"
            "#EXT-X-TARGETDURATION:10\n"
            "#EXTINF:10.0,\n"
            "https://cdn.example.com/seg0.ts\n"
            "#EXTINF:9.5,\n"
            "https://cdn.example.com/seg1.ts\n"
            "#EXTINF:8.0,\n"
            "https://cdn.example.com/seg2.ts\n"
            "#EXT-X-ENDLIST\n"
        )
        m3u8_file.write_text(content, encoding="utf-8")

        segments, resolved_url = Downloader._parse_m3u8_segments(str(m3u8_file), max_seconds=25)
        self.assertEqual(len(segments), 3)
        self.assertEqual(segments[0][0], 0)
        self.assertEqual(segments[0][1], "https://cdn.example.com/seg0.ts")
        self.assertEqual(segments[0][2], 10.0)
        self.assertEqual(segments[1][1], "https://cdn.example.com/seg1.ts")
        self.assertEqual(segments[2][1], "https://cdn.example.com/seg2.ts")

    def test_parse_m3u8_segments_max_seconds_limit(self):
        """Kiểm tra giới hạn max_seconds khi phân tích segment."""
        m3u8_file = self.tmp_path / "long.m3u8"
        lines = ["#EXTM3U"]
        for i in range(10):
            lines.append(f"#EXTINF:10.0,\nhttps://cdn.example.com/seg{i}.ts")
        m3u8_file.write_text("\n".join(lines), encoding="utf-8")

        # Giới hạn 25s chỉ lấy tối đa 3 segments (10s + 10s + 10s >= 25s)
        segments, _ = Downloader._parse_m3u8_segments(str(m3u8_file), max_seconds=25)
        self.assertEqual(len(segments), 3)

    def test_parse_m3u8_relative_urls(self):
        """Kiểm tra chuẩn hóa URL tương đối trong manifest."""
        m3u8_file = self.tmp_path / "relative.m3u8"
        content = (
            "#EXTM3U\n"
            "#EXTINF:5.0,\n"
            "/hls/seg1.ts\n"
        )
        m3u8_file.write_text(content, encoding="utf-8")
        # Với file cục bộ, segment giữ nguyên hoặc format chuẩn
        segments, _ = Downloader._parse_m3u8_segments(str(m3u8_file))
        self.assertEqual(len(segments), 1)
        self.assertIn("seg1.ts", segments[0][1])

    @patch("src.sources.downloader.subprocess.run")
    @patch("src.sources.downloader.requests.Session")
    def test_download_segment_to_mp4_success(self, mock_session_cls, mock_subprocess):
        """Kiểm tra tải song song và ghép nối FFmpeg thành công."""
        # Chuẩn bị file manifest giả lập
        m3u8_file = self.tmp_path / "sample.m3u8"
        m3u8_file.write_text(
            "#EXTM3U\n"
            "#EXTINF:5.0,\n"
            "https://cdn.example.com/part1.ts\n"
            "#EXTINF:5.0,\n"
            "https://cdn.example.com/part2.ts\n",
            encoding="utf-8"
        )
        out_mp4 = self.tmp_path / "output.mp4"

        # Mock Session và response
        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"Content-Type": "video/MP2T"}
        mock_resp.iter_content = MagicMock(return_value=[b"MOCK_TS_DATA" * 100])
        mock_session.get.return_value = mock_resp

        # Mock subprocess FFmpeg concat
        def fake_ffmpeg(cmd, **kwargs):
            # Tạo file output giả lập với dung lượng > 1MB
            with open(out_mp4, "wb") as f:
                f.write(b"0" * 1500000)
            res = MagicMock()
            res.returncode = 0
            return res

        mock_subprocess.side_effect = fake_ffmpeg

        ok = Downloader.download_segment_to_mp4(str(m3u8_file), str(out_mp4))
        self.assertTrue(ok)
        self.assertTrue(out_mp4.exists())
        self.assertGreater(out_mp4.stat().st_size, 1000000)

    @patch("src.sources.downloader.Downloader.reload_tor_circuit")
    @patch("src.sources.downloader.subprocess.run")
    @patch("src.sources.downloader.requests.Session")
    def test_speed_monitor_triggers_tor_circuit_reload(self, mock_session_cls, mock_subprocess, mock_reload_tor):
        """Kiểm tra cơ chế phát hiện lỗi/chậm và kích hoạt reload mạch Tor."""
        m3u8_file = self.tmp_path / "sample_slow.m3u8"
        m3u8_file.write_text(
            "#EXTM3U\n"
            "#EXTINF:5.0,\n"
            "https://cdn.example.com/part1.ts\n"
            "#EXTINF:5.0,\n"
            "https://cdn.example.com/part2.ts\n",
            encoding="utf-8"
        )
        out_mp4 = self.tmp_path / "output.mp4"

        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session
        # Giả lập trả về 404 để gây ra lỗi mảnh cao
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_session.get.return_value = mock_resp

        # Tải thất bại do toàn 404
        ok = Downloader.download_segment_to_mp4(str(m3u8_file), str(out_mp4))
        self.assertFalse(ok)
        # Xác nhận reload_tor_circuit được gọi trong quá trình retry mạch tiếp theo
        self.assertTrue(mock_reload_tor.called)

if __name__ == "__main__":
    unittest.main()
