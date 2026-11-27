import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import main as pipeline_main
from src.intelligence.gemini_client import GeminiClient, GeminiAuthError
from src.sources.downloader import Downloader
from src.state.state_manager import StateManager


class TestGeminiFailFastAndTimeout(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.temp_dir.name)
        self.db_path = self.tmp_path / "processed_db.json"
        self.queue_path = self.tmp_path / "production_queue.json"

        with open(self.db_path, "w", encoding="utf-8") as f:
            json.dump({
                "processed_ids": [],
                "failed_ids": {},
                "copyright_blocked_ids": {},
                "pending_review": {},
                "completed": {},
            }, f)

        with open(self.queue_path, "w", encoding="utf-8") as f:
            json.dump({
                "queue": [
                    {
                        "id": "lz_test_queued",
                        "title": "Test Queued Drama",
                        "source_id": "lz",
                        "m3u8_groups": ["https://cdn.example.com/index.m3u8"],
                        "status": "queued",
                    }
                ]
            }, f)

        self.patch_db = patch("src.state.state_manager.DB_FILE", self.db_path)
        self.patch_queue = patch("src.state.state_manager.QUEUE_FILE", self.queue_path)
        self.patch_db.start()
        self.patch_queue.start()
        GeminiClient._cached_client = None
        GeminiClient._cached_model = None

    def tearDown(self):
        self.patch_db.stop()
        self.patch_queue.stop()
        GeminiClient._cached_client = None
        GeminiClient._cached_model = None
        self.temp_dir.cleanup()

    def test_get_client_raises_gemini_auth_error_fast_on_401(self):
        """Lỗi 401/403 phải ngắt thử model của key đó ngay và ném GeminiAuthError."""
        mock_client = MagicMock()
        mock_client.models.generate_content.side_effect = RuntimeError(
            "401 UNAUTHENTICATED. API key not valid. Please pass a valid API key. ACCESS_TOKEN_TYPE_UNSUPPORTED"
        )

        with patch("src.intelligence.gemini_client.Settings.GEMINI_API_KEYS", ["BAD_KEY_1"]), \
             patch("src.intelligence.gemini_client.genai.Client", return_value=mock_client):
            with self.assertRaises(GeminiAuthError):
                GeminiClient.get_client_and_model()

            # Chỉ gọi 1 lần cho key lỗi thay vì thử vô ích 7 models
            self.assertEqual(mock_client.models.generate_content.call_count, 1)

    def test_is_gemini_auth_exception_distinguishes_cdn_403_from_gemini_401(self):
        """Không được nhầm lỗi HTTP 403 của CDN phim với lỗi API Key Gemini."""
        cdn_err = RuntimeError("Tải tập #1 thất bại từ: https://lzv34.com/index.m3u8 (HTTP 403)")
        self.assertFalse(GeminiClient.is_gemini_auth_exception(cdn_err))

        gemini_err = RuntimeError("401 UNAUTHENTICATED. {'error': {'code': 401, 'message': 'API key not valid.'}}")
        self.assertTrue(GeminiClient.is_gemini_auth_exception(gemini_err))
        self.assertTrue(GeminiClient.is_gemini_auth_exception(GeminiAuthError("invalid")))

    def test_main_preflight_fail_fast_never_marks_queue_failed(self):
        """Khi Gemini trả 401 ở preflight: bắn Telegram, thoát ngay và giữ nguyên 0 failed_ids."""
        with patch.object(pipeline_main, "verify_assets_ready", return_value=True), \
             patch.object(GeminiClient, "verify_api_key_preflight", side_effect=GeminiAuthError("401 API key not valid")), \
             patch("src.delivery.publisher.Publisher.send_telegram_alert") as mock_tg, \
             patch("src.sources.movie_curator.MovieCurator.run_curation_pipeline") as mock_curate, \
             patch("src.sources.downloader.Downloader.download_episode") as mock_dl, \
             patch.object(sys, "argv", ["main.py"]):

            with self.assertRaises(SystemExit) as cm:
                pipeline_main.main()

            self.assertEqual(cm.exception.code, 1)
            mock_tg.assert_called_once()
            mock_curate.assert_not_called()
            mock_dl.assert_not_called()

            # Hàng chờ và failed_ids hoàn toàn nguyên vẹn
            db_data = json.loads(self.db_path.read_text(encoding="utf-8"))
            self.assertEqual(db_data["failed_ids"], {})
            q_data = json.loads(self.queue_path.read_text(encoding="utf-8"))
            self.assertEqual(q_data["queue"][0]["status"], "queued")

    def test_process_drama_midrun_401_does_not_mark_failed_and_stops_main(self):
        """Nếu gặp 401 giữa chừng trong lúc xử lý 1 phim -> dừng pipeline ngay, không ghi vào failed_ids."""
        fake_q_file = self.queue_path
        orig_path_cls = pipeline_main.Path

        def path_side_effect(arg):
            if arg == "state/production_queue.json":
                return fake_q_file
            return orig_path_cls(arg)

        with patch.object(pipeline_main, "verify_assets_ready", return_value=True), \
             patch.object(pipeline_main, "preflight_check_gemini_or_exit", return_value="gemini-3.1-flash-lite"), \
             patch.object(pipeline_main, "Path", side_effect=path_side_effect), \
             patch("src.sources.movie_curator.MovieCurator.run_curation_pipeline"), \
             patch("src.sources.movie_curator.MovieCurator.preflight_check_m3u8", return_value=(True, "ok")), \
             patch.object(pipeline_main, "process_drama_voice_driven", side_effect=GeminiAuthError("401 UNAUTHENTICATED")), \
             patch("src.delivery.publisher.Publisher.send_telegram_alert") as mock_tg, \
             patch.object(sys, "argv", ["main.py"]):

            with self.assertRaises(SystemExit) as cm:
                pipeline_main.main()

            self.assertEqual(cm.exception.code, 1)
            mock_tg.assert_called_once()
            db_data = json.loads(self.db_path.read_text(encoding="utf-8"))
            self.assertNotIn("lz_test_queued", db_data["failed_ids"])
            q_data = json.loads(self.queue_path.read_text(encoding="utf-8"))
            self.assertEqual(q_data["queue"][0]["status"], "queued")

    @patch("src.sources.downloader.subprocess.run")
    @patch("src.sources.downloader.requests.Session")
    def test_downloader_smart_extension_300s_when_near_completion_and_fast(self, mock_session_cls, mock_subprocess):
        """Kiểm tra cơ chế gia hạn +300s khi tiến độ >= 85% và tốc độ > 250 KB/s ở trần 1800s."""
        m3u8_file = self.tmp_path / "long_20m.m3u8"
        lines = ["#EXTM3U"]
        # 20 mảnh -> khi đã có sẵn 18/20 mảnh (90% >= 85%), chỉ thiếu 2 mảnh cuối
        for i in range(20):
            lines.append(f"#EXTINF:5.0,\nhttps://cdn.example.com/seg{i}.ts")
        m3u8_file.write_text("\n".join(lines), encoding="utf-8")
        out_mp4 = self.tmp_path / "long_out.mp4"

        seg_dir = self.tmp_path / f"ts_parts_{out_mp4.stem}"
        seg_dir.mkdir(parents=True, exist_ok=True)
        for i in range(18):
            (seg_dir / f"seg_{i:05d}.ts").write_bytes(b"\x47" * 2048)

        # Mock tải 2 mảnh cuối, mỗi mảnh ~512KB để tốc độ > 250 KB/s dù elapsed = 1s
        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"Content-Type": "video/MP2T"}
        mock_resp.iter_content.side_effect = lambda chunk_size=65536: [b"\x47" * (512 * 1024)]
        mock_session.get.return_value = mock_resp

        def fake_ffmpeg(cmd, **kwargs):
            out_mp4.write_bytes(b"0" * 1500000)
            r = MagicMock()
            r.returncode = 0
            return r

        mock_subprocess.side_effect = fake_ffmpeg

        # Giả lập đồng hồ: t0=0, trước mảnh 19 vừa chạm sát trần 1796s (1800 - 4s) -> kích hoạt gia hạn +300s (đến 2100s) -> mảnh 20 tại 1805s vẫn tiếp tục tải xong!
        fake_times = iter([
            0.0,    # t0
            1792.0, # outer loop now_outer
            1792.0, # outer deadline check
            1792.5, # t_circuit_start
            1793.5, # Mảnh 18 completed: elapsed=1.0s, speed=512 KB/s, done=19/20 (95%) -> gia hạn +300s -> deadline=2100.0
            1793.5, # deadline check -> < 2100, không hủy!
            1805.0, # Mảnh 19 completed (> 1800s cũ, nhưng < 2100s mới)
            1805.0, # deadline check -> ok!
            1805.5, # total_elapsed
            1805.6, # t_concat
            1806.0, # concat_dur
        ])
        last_t = 1806.0

        def fake_time():
            nonlocal last_t
            try:
                last_t = next(fake_times)
            except StopIteration:
                last_t += 0.1
            return last_t

        with patch("src.sources.downloader.time.time", side_effect=fake_time), \
             patch("src.sources.downloader.time.sleep"):
            ok = Downloader.download_segment_to_mp4(str(m3u8_file), str(out_mp4))

        self.assertTrue(ok)
        self.assertTrue(out_mp4.exists())


if __name__ == "__main__":
    unittest.main()
