import os
import json
import urllib.request
import urllib.parse
from pathlib import Path
from typing import Dict, Any, Optional
from config.settings import Settings

class PackageExporter:
    """
    Đóng gói Gói Xét Duyệt 3 phần (GitHub Release / Artifact / Telegram Bot).
    """

    @classmethod
    def send_telegram_notification(
        cls,
        package_dir: str,
        seo_data: Dict[str, Any],
        qa_report: Dict[str, Any]
    ) -> bool:
        """
        Gửi thông báo và ảnh Thumbnail kèm nội dung bài đăng về Telegram của chủ kênh.
        """
        bot_token = Settings.TELEGRAM_BOT_TOKEN
        chat_id = Settings.TELEGRAM_CHAT_ID

        if not bot_token or not chat_id:
            return False

        caption = (
            f"🎬 [GÓI XÉT DUYỆT MỚI] @CoolMeoPhim\n"
            f"📌 Tiêu đề: {seo_data.get('title')}\n"
            f"🎯 QA Status: {'✅ ĐẠT CHUẨN' if qa_report.get('passed') else '⚠️ CẦN XEM LẠI'}\n"
            f"📊 Độ phủ giọng đọc: {qa_report.get('metrics', {}).get('audio_coverage_percent', 0):.1f}%\n\n"
            f"👉 Video hoàn chỉnh đã sẵn sàng trong thư mục:\n`{os.path.basename(package_dir)}`"
        )

        thumb_path = os.path.join(package_dir, "thumbnail.jpg")
        schedule_file = os.path.join(package_dir, "schedule_plan.json")
        schedule_text = ""
        if os.path.exists(schedule_file):
            try:
                with open(schedule_file, "r", encoding="utf-8") as sf:
                    s_data = json.load(sf)
                    s_golden = s_data.get("schedule_timing", {}).get("golden_hour_vn", "")
                    s_slot = s_data.get("schedule_timing", {}).get("slot_vn_iso", "")
                    if s_golden:
                        schedule_text = f"⏰ Giờ Vàng dự kiến: {s_golden} ({s_slot})\n"
            except Exception:
                pass

        if schedule_text:
            caption = caption.replace("📊 Độ phủ giọng đọc:", f"{schedule_text}📊 Độ phủ giọng đọc:")
        try:
            url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
            payload = {
                "chat_id": chat_id,
                "text": caption,
                "parse_mode": "Markdown"
            }
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                print("[PackageExporter] Đã gửi thông báo xét duyệt về Telegram thành công!")
                return True
        except Exception as e:
            print(f"[PackageExporter] Gửi Telegram thất bại: {e}")
            return False

    @classmethod
    def export_summary(cls, package_dir: str, drama_info: Dict[str, Any], qa_report: Dict[str, Any]):
        """Tạo file tóm tắt gói xét duyệt markdown để hiển thị trên GitHub Actions."""
        summary_md = os.path.join(package_dir, "REVIEW_SUMMARY.md")
        
        v_specs = qa_report.get("metrics", {}).get("video_specs", {})
        res_w = v_specs.get("width", 720)
        res_h = v_specs.get("height", 1280)
        orient = v_specs.get("orientation", "vertical")
        v_codec = v_specs.get("video_codec", "h264")
        a_codec = v_specs.get("audio_codec", "aac")

        checks = qa_report.get("checks", {})
        
        content = (
            f"# 🎬 GÓI XÉT DUYỆT VIDEO - {Settings.CHANNEL_NAME}\n\n"
            f"- **Phim gốc**: `{drama_info.get('title')}` (Nguồn: {drama_info.get('source_name', 'Quantum')})\n"
            f"- **Thư mục xuất**: `{package_dir}`\n"
            f"- **Độ phân giải thực tế**: `{res_w}x{res_h}` ({orient.upper()}) | Codec: `{v_codec}/{a_codec}`\n"
            f"- **Kết quả QA Gate**: **{'✅ ĐẠT CHUẨN PRODUCTION' if qa_report.get('passed') else '⚠️ CẢNH BÁO KIỂM TRA'}**\n"
            f"- **Tỷ lệ phủ âm thanh**: `{qa_report.get('metrics', {}).get('audio_coverage_percent', 0):.1f}%` (Ngưỡng: 85% - 95%)\n"
            f"- **Khoảng lặng chết lớn nhất**: `{qa_report.get('metrics', {}).get('max_silence_duration_sec', 0):.2f}s` (Chuẩn: <= 3.5s)\n"
            f"- **Sine Wave 440Hz**: `{'✅ KHÔNG PHÁT HIỆN' if checks.get('no_sine_wave_440hz') else '❌ PHÁT HIỆN TIẾNG BÍP'}`\n"
            f"- **Chữ Hán còn sót**: `{'✅ 0% (TUYỆT ĐỐI KHÔNG CÒN)' if checks.get('zero_chinese_characters') else '❌ CÒN CHỮ HÁN'}`\n"
            f"- **Độ bền vững Logo**: `{'✅ TỒN TẠI XUYÊN SUỐT' if checks.get('logo_persistence') else '❌ BIẾN MẤT'}`\n"
            f"- **Chất lượng Subtitle ASS**: `{'✅ CHUẨN 4-6 TỪ/DÒNG' if checks.get('ass_subtitles_quality') else '❌ LỖI SUB'}`\n\n"
            "## 📦 3 Phần Trong Gói Xét Duyệt:\n"
            f"1. **`final_review.mp4`**: Video hoàn chỉnh ({res_w}x{res_h} 30fps, kính mờ, phụ đề Việt, logo watermark bền vững, mix audio, outro 100% âm lượng).\n"
            "2. **`thumbnail.jpg`** & **`thumbnail_horizontal.jpg`**: Thumbnail hoàn chỉnh 2 tỷ lệ chuẩn SEO (che sạch sub Trung, avatar 1:1, badge REVIEW PHIM, tiêu đề giật gân).\n"
            "3. **`seo_ready_to_copy.txt`** & **`seo_metadata.json`**: Tiêu đề và mô tả sẵn sàng copy đăng kênh.\n"
            "4. **`schedule_plan.json`**: Lịch hẹn giờ đăng Giờ Vàng VN (GMT+7) và cấu hình YouTube Data API v3 / Facebook Graph API.\n"
        )
        with open(summary_md, "w", encoding="utf-8") as f:
            f.write(content)
