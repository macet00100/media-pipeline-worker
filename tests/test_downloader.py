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

    @patch("src.sources.downloader.time.sleep")
    @patch("src.sources.downloader.socket.create_connection")
    def test_reload_tor_circuit_controlport_socket(self, mock_create_conn, mock_sleep):
        """Kiểm tra đổi mạch Tor qua socket ControlPort 9051 với SIGNAL NEWNYM."""
        mock_socket = MagicMock()
        mock_create_conn.return_value.__enter__.return_value = mock_socket
        # Mô phỏng phản hồi 250 OK cho AUTHENTICATE và SIGNAL NEWNYM
        mock_socket.recv.side_effect = [b"250 OK\r\n", b"250 OK\r\n"]

        with patch.dict(os.environ, {"http_proxy": "http://127.0.0.1:9080"}):
            ok = Downloader.reload_tor_circuit()

        self.assertTrue(ok)
        mock_create_conn.assert_called_with(("127.0.0.1", 9051), timeout=5)
        # Xác nhận đã gửi lệnh AUTHENTICATE và SIGNAL NEWNYM
        sent_calls = [call[0][0] for call in mock_socket.sendall.call_args_list]
        self.assertIn(b'AUTHENTICATE ""\r\n', sent_calls)
        self.assertIn(b'SIGNAL NEWNYM\r\n', sent_calls)
        # Xác nhận có chờ 4s
        mock_sleep.assert_called_with(4)

    @patch("src.sources.downloader.subprocess.run")
    @patch("src.sources.downloader.requests.Session")
    def test_download_segment_resume_existing(self, mock_session_cls, mock_subprocess):
        """Kiểm tra cơ chế tải nối tiếp (Resume): Giữ nguyên phân đoạn đã có, chỉ tải mảnh thiếu."""
        m3u8_file = self.tmp_path / "resume_test.m3u8"
        m3u8_file.write_text(
            "#EXTM3U\n"
            "#EXTINF:5.0,\n"
            "https://cdn.example.com/part0.ts\n"
            "#EXTINF:5.0,\n"
            "https://cdn.example.com/part1.ts\n"
            "#EXTINF:5.0,\n"
            "https://cdn.example.com/part2.ts\n",
            encoding="utf-8"
        )
        out_mp4 = self.tmp_path / "output.mp4"

        # Giả lập mảnh part0 đã tồn tại hợp lệ sẵn (> 1024 bytes) trong thư mục tạm
        seg_dir = self.tmp_path / f"ts_parts_{out_mp4.stem}"
        seg_dir.mkdir(parents=True, exist_ok=True)
        seg0 = seg_dir / "seg_00000.ts"
        seg0.write_bytes(b"\x47" * 2048)  # Dữ liệu TS hợp lệ

        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"Content-Type": "video/MP2T"}
        mock_resp.iter_content = MagicMock(return_value=[b"\x47" * 2048])
        mock_session.get.return_value = mock_resp

        def fake_ffmpeg(cmd, **kwargs):
            with open(out_mp4, "wb") as f:
                f.write(b"0" * 1500000)
            res = MagicMock()
            res.returncode = 0
            return res

        mock_subprocess.side_effect = fake_ffmpeg

        ok = Downloader.download_segment_to_mp4(str(m3u8_file), str(out_mp4))
        self.assertTrue(ok)

        # Kiểm tra session.get chỉ được gọi 2 lần cho part1 và part2 (không tải lại part0)
        requested_urls = [call[0][0] for call in mock_session.get.call_args_list]
        self.assertEqual(len(requested_urls), 2)
        self.assertNotIn("https://cdn.example.com/part0.ts", requested_urls)
        self.assertIn("https://cdn.example.com/part1.ts", requested_urls)
        self.assertIn("https://cdn.example.com/part2.ts", requested_urls)

    @patch("src.sources.downloader.time.sleep")
    @patch("src.sources.downloader.Downloader.reload_tor_circuit")
    @patch("src.sources.downloader.subprocess.run")
    @patch("src.sources.downloader.requests.Session")
    def test_session_closed_and_recreated_on_circuit_switch(self, mock_session_cls, mock_subprocess, mock_reload_tor, mock_sleep):
        """Kiểm tra Session cũ bắt buộc được session.close() và tạo mới khi đổi mạch."""
        m3u8_file = self.tmp_path / "circuit_session_test.m3u8"
        m3u8_file.write_text(
            "#EXTM3U\n"
            "#EXTINF:5.0,\n"
            "https://cdn.example.com/part0.ts\n",
            encoding="utf-8"
        )
        out_mp4 = self.tmp_path / "output.mp4"

        # Mạch 1 trả về 403 (bị CDN rate limit), mạch 2 thành công
        session_instance_1 = MagicMock()
        session_instance_2 = MagicMock()
        mock_session_cls.side_effect = [session_instance_1, session_instance_2]

        resp_fail = MagicMock()
        resp_fail.status_code = 403
        session_instance_1.get.return_value = resp_fail

        resp_ok = MagicMock()
        resp_ok.status_code = 200
        resp_ok.headers = {"Content-Type": "video/MP2T"}
        resp_ok.iter_content = MagicMock(return_value=[b"\x47" * 2048])
        session_instance_2.get.return_value = resp_ok

        def fake_ffmpeg(cmd, **kwargs):
            with open(out_mp4, "wb") as f:
                f.write(b"0" * 1500000)
            res = MagicMock()
            res.returncode = 0
            return res

        mock_subprocess.side_effect = fake_ffmpeg

        ok = Downloader.download_segment_to_mp4(str(m3u8_file), str(out_mp4))
        self.assertTrue(ok)

        # Kiểm tra session_instance_1 đã được close() để hủy Keep-Alive
        self.assertTrue(session_instance_1.close.called)
        # Kiểm tra session_instance_2 cũng được close() khi hoàn tất
        self.assertTrue(session_instance_2.close.called)
        # Kiểm tra reload_tor_circuit được gọi giữa 2 mạch
        self.assertTrue(mock_reload_tor.called)

if __name__ == "__main__":
    unittest.main()
