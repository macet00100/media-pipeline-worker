# 🎬 COOL MÈO PHIM AUTOMATION 24/7 (`@CoolMeoPhim`)

Hệ thống tự động hóa sản xuất video review phim ngắn vi kịch Trung Quốc chuẩn Production-Ready, vận hành liên tục 24/24 trên **GitHub Actions** với cơ chế đóng gói **Gói Xét Duyệt 3 phần** và **Khóa an toàn tự động đăng tải**.

---

## 📁 1. Cấu Trúc Dự Án Hoàn Chỉnh

```text
cool-meo-phim-automation/
├── .github/
│   └── workflows/
│       └── pipeline_24_7.yml       # Cronjob 24/7 + chạy thủ công trên GitHub Actions
├── assets/
│   ├── logo.png                    # Watermark Mèo Thần Tài (120x120px nền trong suốt tại 32,32)
│   ├── avatar.png                  # Avatar kênh dùng cho Thumbnail
│   ├── outtro.mp4                  # Video outro Mèo Thần Tài (100% âm thanh sống động gốc)
│   ├── bgm/                        # Nhạc nền không bản quyền (ambient_bgm.mp3)
│   └── fonts/                      # Font Arial.ttf hỗ trợ 100% dấu Tiếng Việt
├── config/
│   ├── settings.py                 # Load & validate biến môi trường / GitHub Secrets
│   └── blacklist.json              # Danh sách đen cờ bạc, nhà cái, web lậu, studio rủi ro
├── src/
│   ├── sources/
│   │   ├── cms_client.py           # Kết nối API Lượng Tử (LZ) & Phi Phàm (FF) có failover
│   │   └── downloader.py           # Tải M3U8 + Lớp lọc quảng cáo 1 (#EXT-X-DISCONTINUITY)
│   ├── cleaner/
│   │   └── ad_detector.py          # Lớp lọc quảng cáo 2 bằng quét khung hình + OCR
│   ├── intelligence/
│   │   ├── script_extractor.py     # Trích xuất phân đoạn 1-to-1 (60s - 90s)
│   │   ├── review_writer.py        # Gemini biên tập ngôi thứ 3 + khóa độ phủ 85-95%
│   │   └── seo_generator.py        # Tiêu đề, Mô tả, Hashtag chuẩn SEO cho @CoolMeoPhim
│   ├── audio/
│   │   ├── tts_engine.py           # Giọng vi-VN-NamMinhNeural + retry exponential backoff
│   │   └── sync_aligner.py         # Khử khoảng lặng chết, đồng bộ thời lượng audio-video
│   ├── video/
│   │   ├── subtitle_builder.py     # Phụ đề ASS tiếng Việt 4-6 từ/dòng căn giữa dải kính mờ
│   │   ├── copyright_shield.py     # Biến đổi fingerprint chống Content ID / Reused Content
│   │   ├── renderer.py             # FFmpeg Filtergraph: Kính mờ, Logo, Sub, Hòa âm, Outro
│   │   └── thumbnail_maker.py      # Render Thumbnail hoàn chỉnh (dọc 720x1280 & ngang 1280x720)
│   ├── qa/
│   │   └── validator.py            # Cổng kiểm định chất lượng tự động xuất qa_report.json
│   ├── delivery/
│   │   ├── package_exporter.py     # Đóng gói Gói Xét Duyệt & gửi thông báo Telegram
│   │   └── publisher_stub.py       # Khóa an toàn ngăn chặn tự động đăng khi chưa duyệt
│   └── state/
│       └── state_manager.py        # Quản lý lịch sử phim đã làm / lỗi tránh trùng lặp
├── state/
│   └── processed_db.json           # Cơ sở dữ liệu phim đã xử lý (commit nhẹ lên Git)
├── tests/
│   └── test_end_to_end_mock.py     # Bộ kiểm thử Dry-Run toàn diện không cần phim thật
├── main.py                         # Trình điều phối trung tâm (Orchestrator)
├── requirements.txt
└── .env.example
```

---

## 🔑 2. Danh Sách GitHub Secrets Cần Thiết Lập

Truy cập GitHub Repo: **Settings** -> **Secrets and variables** -> **Actions** -> **New repository secret**:

| Tên Secret | Bắt buộc | Mô tả |
| :--- | :---: | :--- |
| `GEMINI_API_KEYS` | **Có** | API Key của Google Gemini (hỗ trợ nhiều key phân tách bằng dấu phẩy để tự xoay vòng khi chạm quota). |
| `LZ_API_URL` | Không | URL API kho Lượng Tử (đã có mặc định). |
| `FF_API_URL` | Không | URL API kho Phi Phàm (đã có mặc định). |
| `TELEGRAM_BOT_TOKEN` | Không | Token Bot Telegram để nhận thông báo xét duyệt ngay về điện thoại. |
| `TELEGRAM_CHAT_ID` | Không | Chat ID người nhận Telegram. |
| `YT_CLIENT_ID` | Không | Giữ sẵn từ `.env` cho việc mở khóa đăng tải sau này. |
| `YT_CLIENT_SECRET` | Không | Giữ sẵn từ `.env`. |
| `YT_REFRESH_TOKEN` | Không | Giữ sẵn từ `.env`. |
| `FB_PAGE_ID` | Không | ID Fanpage Facebook. |
| `FB_ACCESS_TOKEN` | Không | Token quản trị Fanpage. |

---

## 🎨 3. Quy Chuẩn Vị Trí File Assets

Tất cả các tài nguyên thương hiệu được đặt trong thư mục `assets/`:
1. **`assets/logo.png`**: Logo Watermark Mèo Thần Tài (120x120px, nền trong suốt, chèn góc trên bên trái tại tọa độ `x=32, y=32`).
2. **`assets/avatar.png`**: Ảnh đại diện kênh Cool Mèo Phim (bo tròn viền vàng chèn vào Thumbnail).
3. **`assets/outtro.mp4`**: Video Outro Mèo Thần Tài 10 giây (giữ nguyên 100% âm lượng sống động gốc).
4. **`assets/bgm/ambient_bgm.mp3`**: Nhạc nền không bản quyền hòa âm siêu nhỏ (`volume=0.06`).
5. **`assets/fonts/Arial.ttf`**: Font chữ Unicode tiếng Việt chuẩn render phụ đề và thumbnail.

---

## 🧪 4. Chạy Kiểm Thử Dry-Run (Không Cần Phim Thật)

Trước khi kích hoạt hoặc sau khi sửa đổi bất kỳ logic FFmpeg nào, hãy chạy bộ kiểm thử giả lập để xác nhận hệ thống hoàn toàn không có lỗi:

```bash
python tests/test_end_to_end_mock.py
```

Kết quả mong đợi:
```text
🎉 TẤT CẢ CÁC BƯỚC KIỂM THỬ (MOCK TEST) ĐỀU THÀNH CÔNG 100%!
   - Lệnh FFmpeg: Cú pháp chính xác tuyệt đối, không có bất kỳ lỗi nào.
```

---

## 🚀 5. Hướng Dẫn Bấm Chạy Thủ Công Lần Đầu Trên GitHub Actions

1. Đẩy toàn bộ mã nguồn lên nhánh `main` của repository GitHub:
   ```bash
   git add .
   git commit -m "feat: complete Cool Meo Phim automation pipeline 24/7"
   git push origin main
   ```
2. Trên giao diện GitHub của bạn, nhấn vào tab **Actions**.
3. Ở danh sách bên trái, chọn workflow **🎬 Cool Mèo Phim 24/7 Automation Pipeline**.
4. Nhấn nút **Run workflow** -> Chọn nhánh `main` -> Nhấn nút xanh **Run workflow**.
5. Sau khi job chạy xong (khoảng 5–15 phút tùy độ dài phim):
   - Kéo xuống mục **Artifacts** của lần chạy đó để tải về file zip `cool-meo-phim-review-packages-X`.
   - Giải nén file zip sẽ có đầy đủ **Gói Xét Duyệt 3 phần**:
     * `final_review.mp4`: Video review hoàn chỉnh không dính bản quyền.
     * `thumbnail.jpg` & `thumbnail_horizontal.jpg`: Thumbnail dọc & ngang hút click.
     * `seo_ready_to_copy.txt`: Tiêu đề và mô tả sẵn sàng đăng bài.
     * `qa_report.json`: Báo cáo thẩm định chất lượng tự động.
