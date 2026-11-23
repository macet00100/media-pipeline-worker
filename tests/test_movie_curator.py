import os
import json
import tempfile
import unittest
from pathlib import Path

from config.settings import Settings
from src.state.state_manager import StateManager
from src.sources.movie_curator import MovieCurator, run_curation_cli

class TestMovieCurator(unittest.TestCase):

    def setUp(self):
        self.blacklist = {
            "copyright_high_risk": ["marvel", "disney", "netflix", "anime", "18+"],
            "gambling_keywords": ["casino", "nhà cái", "bắn cá"],
            "forbidden_channel_names": ["kênh lậu"]
        }

    def test_normalize_title(self):
        self.assertEqual(MovieCurator.normalize_title("【短剧】豪门千金归来（第一季）"), "豪门千金归来")
        self.assertEqual(MovieCurator.normalize_title("Chiến Thần Ở Rể Season 1"), "chiếnthầnởrể")
        self.assertEqual(MovieCurator.normalize_title(""), "")

    def test_detect_category(self):
        item_short = {"type_id": 46, "type_name": "短剧", "vod_class": "短片"}
        self.assertEqual(MovieCurator.detect_category(item_short), "short_drama")

        item_long = {"type_id": 13, "type_name": "国产剧", "vod_class": "连续剧"}
        self.assertEqual(MovieCurator.detect_category(item_long), "long_series")

    def test_blacklist_rejection(self):
        # Marvel
        item_marvel = {"vod_name": "Marvel Iron Man 4", "vod_content": "Super hero"}
        is_bl, reason = MovieCurator.is_blacklisted(item_marvel, self.blacklist)
        self.assertTrue(is_bl)
        self.assertIn("marvel", reason)

        # Anime
        item_anime = {"vod_name": "Naruto Anime Siêu Cấp", "vod_content": "Hoạt hình nhật bản"}
        is_bl, reason = MovieCurator.is_blacklisted(item_anime, self.blacklist)
        self.assertTrue(is_bl)

        # 18+
        item_18 = {"vod_name": "Tình Yêu 18+ Erotic", "vod_content": "Adult film"}
        is_bl, reason = MovieCurator.is_blacklisted(item_18, self.blacklist)
        self.assertTrue(is_bl)

        # Clean movie
        item_clean = {"vod_name": "Trọng Sinh Vả Mặt Tổng Tài", "vod_content": "Nghịch tập báo thù"}
        is_bl, reason = MovieCurator.is_blacklisted(item_clean, self.blacklist)
        self.assertFalse(is_bl)

    def test_episode_and_completion_rules(self):
        # Short drama < 30 eps and not full -> Should be rejected
        raw_short_insufficient = [
            {
                "vod_id": 101,
                "vod_name": "Đoản Kịch Ngắn Mới Ra",
                "type_id": 46,
                "type_name": "短剧",
                "vod_remarks": "更新至第15集",
                "vod_isend": 0,
                "vod_total": 0,
                "vod_play_url": "第01集$http://a.m3u8#第02集$http://b.m3u8",
                "_scan_source_id": "lz",
                "_scan_source_name": "Lượng Tử"
            }
        ]
        passed, stats = MovieCurator.filter_layer_1(raw_short_insufficient)
        self.assertEqual(len(passed), 0)
        self.assertEqual(stats["incomplete_rejected_count"], 1)

        # Short drama >= 30 eps and Full -> Passed
        raw_short_30 = [
            {
                "vod_id": 102,
                "vod_name": "Đoản Kịch 35 Tập Đủ Dài",
                "type_id": 46,
                "type_name": "短剧",
                "vod_remarks": "全35集",
                "vod_isend": 1,
                "vod_total": 35,
                "vod_play_url": "#".join([f"第{i:02d}集$http://a.m3u8" for i in range(1, 36)]),
                "_scan_source_id": "lz",
                "_scan_source_name": "Lượng Tử"
            }
        ]
        passed, _ = MovieCurator.filter_layer_1(raw_short_30)
        self.assertEqual(len(passed), 1)

        # Short drama Full (完结) -> Passed even if 20 eps
        raw_short_full = [
            {
                "vod_id": 103,
                "vod_name": "Đoản Kịch Full Trọn Bộ",
                "type_id": 46,
                "type_name": "短剧",
                "vod_remarks": "已完结",
                "vod_isend": 1,
                "vod_total": 20,
                "vod_play_url": "#".join([f"第{i:02d}集$http://a.m3u8" for i in range(1, 21)]),
                "_scan_source_id": "lz",
                "_scan_source_name": "Lượng Tử"
            }
        ]
        passed, _ = MovieCurator.filter_layer_1(raw_short_full)
        self.assertEqual(len(passed), 1)

        # Long series < 12 eps and not full -> Rejected
        raw_long_insufficient = [
            {
                "vod_id": 104,
                "vod_name": "Phim Dài Đang Chiếu Tập 5",
                "type_id": 13,
                "type_name": "国产剧",
                "vod_remarks": "更新至第05集",
                "vod_isend": 0,
                "vod_total": 24,
                "vod_play_url": "第01集$http://a.m3u8",
                "_scan_source_id": "lz",
                "_scan_source_name": "Lượng Tử"
            }
        ]
        passed, stats = MovieCurator.filter_layer_1(raw_long_insufficient)
        self.assertEqual(len(passed), 0)
        self.assertEqual(stats["incomplete_rejected_count"], 1)

        # Long series >= 12 eps Full -> Passed
        raw_long_12 = [
            {
                "vod_id": 105,
                "vod_name": "Phim Dài Đạt 15 Tập",
                "type_id": 13,
                "type_name": "国产剧",
                "vod_remarks": "全15集",
                "vod_isend": 1,
                "vod_total": 15,
                "vod_play_url": "#".join([f"第{i:02d}集$http://a.m3u8" for i in range(1, 16)]),
                "_scan_source_id": "lz",
                "_scan_source_name": "Lượng Tử"
            }
        ]
        passed, _ = MovieCurator.filter_layer_1(raw_long_12)
        self.assertEqual(len(passed), 1)

    def test_deduplication_between_lz_and_ff(self):
        candidates = [
            {
                "vod_id": 201,
                "vod_name": "Chiến Thần Ở Rể",
                "type_id": 46,
                "type_name": "短剧",
                "vod_remarks": "已完结",
                "vod_isend": 1,
                "vod_total": 80,
                "vod_play_url": "#".join([f"第{i:02d}集$http://lz.m3u8" for i in range(1, 81)]),
                "_scan_source_id": "lz",
                "_scan_source_name": "Lượng Tử"
            },
            {
                "vod_id": 305,
                "vod_name": "【短剧】Chiến Thần Ở Rể",
                "type_id": 36,
                "type_name": "短剧",
                "vod_remarks": "已完结",
                "vod_isend": 1,
                "vod_total": 80,
                "vod_play_url": "#".join([f"第{i:02d}集$http://ff.m3u8" for i in range(1, 81)]),
                "_scan_source_id": "ff",
                "_scan_source_name": "Phi Phàm"
            }
        ]
        passed, stats = MovieCurator.filter_layer_1(candidates)
        self.assertEqual(len(passed), 1)
        self.assertEqual(stats["deduplicated_count"], 1)
        self.assertEqual(set(passed[0]["sources_available"]), {"lz", "ff"})

    def test_scoring_system_layer_2(self):
        candidate = {
            "id": "test_1",
            "title": "Trọng Sinh Vả Mặt Tổng Tài",
            "category": "short_drama",
            "episodes_count": 80,
            "is_completed": True,
            "synopsis": "Kiếp trước bị hãm hại phản bội, kiếp này trùng sinh vả mặt toàn bộ hào môn?",
            "director": "Trương Đạo",
            "actors": "Lý Hạo",
            "m3u8_groups": ["http://test.m3u8"]
        }
        scores = MovieCurator._calculate_heuristic_scores(candidate)
        self.assertGreaterEqual(scores["hook_score"], 0)
        self.assertLessEqual(scores["hook_score"], 35)

        self.assertGreaterEqual(scores["viral_score"], 0)
        self.assertLessEqual(scores["viral_score"], 30)

        self.assertGreaterEqual(scores["completeness_score"], 0)
        self.assertLessEqual(scores["completeness_score"], 20)

        self.assertGreaterEqual(scores["copyright_score"], 0)
        self.assertLessEqual(scores["copyright_score"], 15)

        self.assertEqual(
            scores["total_score"],
            scores["hook_score"] + scores["viral_score"] + scores["completeness_score"] + scores["copyright_score"]
        )
        self.assertLessEqual(scores["total_score"], 100)

    def test_top_3_selection_and_fallback(self):
        scored = [
            {"id": "s1", "title": "Short 1", "category": "short_drama", "score": 95},
            {"id": "s2", "title": "Short 2", "category": "short_drama", "score": 90},
            {"id": "s3", "title": "Short 3", "category": "short_drama", "score": 85},
            {"id": "l1", "title": "Long 1", "category": "long_series", "score": 92},
            {"id": "l2", "title": "Long 2", "category": "long_series", "score": 88}
        ]
        # Standard: 2 short, 1 long -> s1, s2, l1
        selected = MovieCurator.select_top_candidates(scored, target_total=3, target_short=2, target_long=1)
        self.assertEqual(len(selected), 3)
        selected_ids = {c["id"] for c in selected}
        self.assertEqual(selected_ids, {"s1", "s2", "l1"})

        # Fallback when 0 long series: take 3 shorts
        shorts_only = [
            {"id": "s1", "title": "Short 1", "category": "short_drama", "score": 95},
            {"id": "s2", "title": "Short 2", "category": "short_drama", "score": 90},
            {"id": "s3", "title": "Short 3", "category": "short_drama", "score": 85}
        ]
        selected_fallback = MovieCurator.select_top_candidates(shorts_only, target_total=3, target_short=2, target_long=1)
        self.assertEqual(len(selected_fallback), 3)
        self.assertEqual({c["id"] for c in selected_fallback}, {"s1", "s2", "s3"})

if __name__ == "__main__":
    unittest.main()
