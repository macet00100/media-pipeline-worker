import os
import re
import unicodedata
import subprocess
from pathlib import Path
from typing import Optional, Tuple, List, Dict, Any
from PIL import Image, ImageDraw, ImageFont, ImageFilter
from config.settings import Settings, ASSETS_DIR
from src.intelligence.gemini_client import GeminiClient

class ThumbnailMaker:
    """
    Bộ thiết kế Thumbnail chuyên nghiệp:
    1. Chọn khung hình có khuôn mặt rõ nét, biểu cảm kịch tính nhất qua Gemini Vision.
    2. Render RIÊNG BIỆT 2 bố cục chuẩn tỷ lệ (1280x720 ngang và 720x1280 dọc), TUYỆT ĐỐI KHÔNG KÉO BẸP ẢNH.
    3. Che sạch 100% phụ đề tiếng Trung gốc bằng dải Gradient đen mượt (24% đáy) kết hợp feathered GaussianBlur chuyển mềm.
    4. Chèn Avatar tròn 120x120 chuẩn (viền vàng kim), Badge 'REVIEW PHIM' sắc nét và Tiêu đề 100% tiếng Việt chuẩn NFC, auto-wrap & auto-fit.
    """

    @classmethod
    def select_best_frame_with_ai(
        cls,
        video_path: str,
        tmp_dir: str,
        skip_ranges: Optional[List[Dict[str, Any]]] = None
    ) -> str:
        """
        Trích xuất 6 khung hình ứng viên từ các đoạn sạch của phim
        và dùng Gemini Vision chọn ra 1 khung hình có khuôn mặt nhân vật biểu cảm kịch tính nhất.
        """
        output_dir = os.path.join(tmp_dir, "thumb_candidates")
        os.makedirs(output_dir, exist_ok=True)

        # Lấy thời lượng video
        dur_cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", video_path]
        try:
            res = subprocess.run(dur_cmd, capture_output=True, text=True)
            duration = float(res.stdout.strip() or 300.0)
        except Exception:
            duration = 300.0

        # Lọc các mốc thời gian an toàn tránh xa opening, ending và quảng cáo
        skip_intervals = []
        if skip_ranges:
            for s in skip_ranges:
                st = float(s.get("start_sec", s.get("start", 0.0)))
                en = float(s.get("end_sec", s.get("end", 0.0)))
                skip_intervals.append((st, en))

        # Trừ thêm biên an toàn 50s cuối video cho ending credits
        skip_intervals.append((max(0.0, duration - 50.0), duration))
        skip_intervals.append((0.0, 35.0))

        def is_in_skip(t: float) -> bool:
            for st, en in skip_intervals:
                if st - 1.0 <= t <= en + 1.0:
                    return True
            return False

        # Chọn 6 mốc thời gian sạch phân bổ đều từ 15% đến 80%
        candidate_ts = []
        num_samples = 14
        for i in range(1, num_samples + 1):
            t = duration * (0.15 + (i / (num_samples + 1)) * 0.65)
            if not is_in_skip(t):
                candidate_ts.append(t)
            if len(candidate_ts) >= 6:
                break

        if not candidate_ts:
            candidate_ts = [duration * 0.3, duration * 0.5, duration * 0.7]

        candidate_frames = []
        for idx, ts in enumerate(candidate_ts):
            f_path = os.path.join(output_dir, f"candidate_{idx:02d}.jpg")
            cmd = [
                "ffmpeg", "-nostdin", "-y", "-ss", f"{ts:.2f}",
                "-i", video_path,
                "-vframes", "1",
                "-q:v", "2",
                f_path
            ]
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if os.path.exists(f_path) and os.path.getsize(f_path) > 10000:
                candidate_frames.append(f_path)

        if not candidate_frames:
            raise RuntimeError("Không thể trích xuất khung hình ứng viên từ video!")

        print(f"[ThumbnailMaker] Đã trích xuất {len(candidate_frames)} khung hình ứng viên sạch.")
        try:
            sel_res = GeminiClient.select_best_thumbnail_frame(candidate_frames)
            if len(sel_res) == 3:
                sel_idx, best_path, face_cx_ratio = sel_res
            else:
                sel_idx, best_path = sel_res
                face_cx_ratio = 0.5
            return best_path, face_cx_ratio
        except Exception as e:
            print(f"[ThumbnailMaker] Gemini Vision chọn ảnh gặp lỗi: {e}. Dùng khung hình đầu tiên.")
            return candidate_frames[0], 0.5

    @classmethod
    def create_circular_avatar(cls, avatar_path: str, size: int = 120) -> Image.Image:
        """Tạo Avatar kênh tròn 120x120 chuẩn tỷ lệ 1:1 với viền vàng đôi."""
        if os.path.exists(avatar_path):
            av = Image.open(avatar_path).convert("RGBA")
        else:
            av = Image.new("RGBA", (size, size), (255, 200, 0, 255))

        # Center-crop hình vuông
        w, h = av.size
        min_dim = min(w, h)
        left = (w - min_dim) // 2
        top = (h - min_dim) // 2
        av = av.crop((left, top, left + min_dim, top + min_dim))
        av = av.resize((size, size), Image.Resampling.LANCZOS)

        # Mặt nạ hình tròn
        mask = Image.new("L", (size, size), 0)
        draw = ImageDraw.Draw(mask)
        draw.ellipse((0, 0, size, size), fill=255)

        output = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        output.paste(av, (0, 0), mask=mask)

        # Viền vàng đôi
        draw_border = ImageDraw.Draw(output)
        draw_border.ellipse((1, 1, size - 2, size - 2), outline=(255, 215, 0, 255), width=4)
        draw_border.ellipse((5, 5, size - 6, size - 6), outline=(255, 255, 255, 220), width=2)
        return output

    @classmethod
    def apply_bottom_gradient_mask(cls, base_img: Image.Image, gradient_height_ratio: float = 0.24) -> Image.Image:
        """
        Tạo dải Gradient đen mượt thu gọn vùng đáy (chỉ 24% tính từ đáy ảnh)
        kết hợp GaussianBlur chuyển tiếp mềm bằng feathered mask (alpha=0 -> 255).
        Tuyệt đối không bao giờ tạo đường cắt ngang cứng qua khuôn mặt hoặc miệng nhân vật.
        """
        w, h = base_img.size
        grad_h = int(h * gradient_height_ratio)
        grad_y_start = h - grad_h

        # 1. Bản copy làm mờ toàn phần
        blurred = base_img.filter(ImageFilter.GaussianBlur(radius=12))

        # 2. Feathered alpha mask cho lớp blur (chuyển mềm mượt từ 0 đến 255)
        blur_mask = Image.new("L", (w, h), 0)
        b_draw = ImageDraw.Draw(blur_mask)
        for y in range(grad_h):
            # Tăng dần alpha theo lũy thừa 1.6 để chuyển tiếp cực kỳ êm dịu ở đỉnh
            alpha = int(255 * ((y / float(grad_h)) ** 1.6))
            b_draw.line([(0, grad_y_start + y), (w, grad_y_start + y)], fill=alpha)

        base_img.paste(blurred, (0, 0), blur_mask)

        # 3. Feathered black gradient layer (từ alpha=0 đến alpha=245)
        black_layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        k_draw = ImageDraw.Draw(black_layer)
        for y in range(grad_h):
            alpha = int(245 * ((y / float(grad_h)) ** 1.3))
            k_draw.line([(0, grad_y_start + y), (w, grad_y_start + y)], fill=(0, 0, 0, alpha))

        base_img.paste(black_layer, (0, 0), black_layer)
        return base_img

    @classmethod
    def fit_and_wrap_text(
        cls,
        text: str,
        font_path: str,
        max_width: int,
        start_font_size: int = 52,
        min_font_size: int = 34
    ) -> Tuple[List[str], ImageFont.FreeTypeFont, int]:
        """
        Tự động ngắt dòng và điều chỉnh kích thước chữ (auto-wrap & auto-fit):
        - Nếu tiêu đề dài quá max_width: tự động xuống 2 dòng cân đối (2-4 từ/dòng).
        - Giảm dần cỡ chữ sao cho chữ nằm trọn trong vùng an toàn, không bao giờ bị cắt mép.
        """
        words = text.split()
        if not words:
            font = ImageFont.truetype(font_path, start_font_size)
            return [text], font, start_font_size

        # Thử 1 dòng trước
        font_size = start_font_size
        while font_size >= 40:
            font = ImageFont.truetype(font_path, font_size)
            bbox = font.getbbox(text)
            w = bbox[2] - bbox[0]
            if w <= max_width and len(words) <= 4:
                return [text], font, font_size
            font_size -= 2

        # Nếu dài hơn max_width hoặc > 4 từ -> Tự động chia 2 dòng cân đối
        mid = (len(words) + 1) // 2
        line1 = " ".join(words[:mid])
        line2 = " ".join(words[mid:])

        font_size = start_font_size - 4
        while font_size >= min_font_size:
            font = ImageFont.truetype(font_path, font_size)
            w1 = font.getbbox(line1)[2] - font.getbbox(line1)[0]
            w2 = font.getbbox(line2)[2] - font.getbbox(line2)[0]
            if max(w1, w2) <= max_width:
                return [line1, line2], font, font_size
            font_size -= 2

        font = ImageFont.truetype(font_path, min_font_size)
        return [line1, line2], font, min_font_size

    @classmethod
    def clean_catchy_title(cls, catchy_title: str) -> str:
        """Chuẩn hóa Unicode NFC, loại bỏ chữ Hán và sửa lỗi gõ sai phổ biến."""
        title = unicodedata.normalize("NFC", catchy_title or "").strip()
        title = re.sub(r"[\u4e00-\u9fff]+", "", title).strip()
        # Sửa các lỗi chính tả phổ biến
        title = re.sub(r"\bHÀM\b", "HAI", title, flags=re.IGNORECASE)
        title = re.sub(r"\bHAM\b", "HAI", title, flags=re.IGNORECASE)
        if not title:
            title = "SỰ THẬT KINH HOÀNG"
        return title.upper()

    @classmethod
    def render_horizontal_thumbnail(
        cls,
        source_frame_path: str,
        catchy_title: str,
        output_path: str
    ) -> str:
        """
        Render Thumbnail Ngang 1280x720 (chuẩn tỷ lệ 16:9).
        - Nếu nguồn là video dọc (height > width): Áp dụng bố cục YouTube Cinema Split 16:9,
          giữ 100% trọn vẹn đầu, mắt và biểu cảm diễn viên, TUYỆT ĐỐI KHÔNG CẮT CỤT MẶT.
        - Nếu nguồn là video ngang: Áp dụng Center-crop chuẩn + bottom gradient.
        """
        target_w, target_h = 1280, 720
        src_img = Image.open(source_frame_path).convert("RGBA")
        sw, sh = src_img.size
        font_path = Settings.get_font_path()
        avatar_path = str(ASSETS_DIR / "avatar.png")

        if sh > sw:
            # -------------------------------------------------------------
            # BỐ CỤC YOUTUBE CINEMA SPLIT 16:9 CHO NGUỒN PHIM DỌC
            # -------------------------------------------------------------
            # 1. Lớp nền 1280x720: Phóng ảnh gốc phủ kín 16:9 + GaussianBlur(radius=28) + phủ tối black@0.55
            scale_bg = max(target_w / sw, target_h / sh)
            bg_w, bg_h = int(sw * scale_bg), int(sh * scale_bg)
            bg_scaled = src_img.resize((bg_w, bg_h), Image.Resampling.LANCZOS)
            bg_x = (bg_w - target_w) // 2
            bg_y = (bg_h - target_h) // 2
            bg_cropped = bg_scaled.crop((bg_x, bg_y, bg_x + target_w, bg_y + target_h))
            bg_blurred = bg_cropped.filter(ImageFilter.GaussianBlur(radius=28))
            dark_overlay = Image.new("RGBA", (target_w, target_h), (0, 0, 0, 140))
            final_img = Image.alpha_composite(bg_blurred, dark_overlay)

            # 2. Lớp nhân vật chính: Scale nguyên khung hình dọc vừa khít chiều cao 720px
            # Làm mờ và tint dải chữ Trung y=780..950 trên ảnh dọc trước khi scale vào 16:9
            sub_y1 = int(sh * 0.61)
            sub_y2 = int(sh * 0.74)
            feather = 25
            src_clean = src_img.copy()
            sub_blurred = src_clean.filter(ImageFilter.GaussianBlur(radius=24))
            tint_layer = Image.new("RGBA", (sw, sh), (0, 0, 0, 115))
            sub_blurred_tinted = Image.alpha_composite(sub_blurred, tint_layer)
            sub_mask = Image.new("L", (sw, sh), 0)
            s_draw = ImageDraw.Draw(sub_mask)
            for cur_y in range(sub_y1, sub_y2):
                if cur_y < sub_y1 + feather:
                    m_alpha = int(255 * ((cur_y - sub_y1) / float(feather)))
                elif cur_y > sub_y2 - feather:
                    m_alpha = int(255 * ((sub_y2 - cur_y) / float(feather)))
                else:
                    m_alpha = 255
                s_draw.line([(0, cur_y), (sw, cur_y)], fill=m_alpha)
            src_clean.paste(sub_blurred_tinted, (0, 0), sub_mask)

            fg_h = target_h
            fg_w = int(sw * (fg_h / float(sh)))
            fg_resized = src_clean.resize((fg_w, fg_h), Image.Resampling.LANCZOS)

            # Feather viền trái để hòa vào nền tối
            fg_mask = Image.new("L", (fg_w, fg_h), 255)
            m_draw = ImageDraw.Draw(fg_mask)
            feather_w = 45
            for x in range(feather_w):
                alpha = int(255 * (x / float(feather_w)))
                m_draw.line([(x, 0), (x, fg_h)], fill=alpha)

            fg_x = target_w - fg_w - 20
            fg_y = 0
            final_img.paste(fg_resized, (fg_x, fg_y), mask=fg_mask)

            # 3. Avatar tròn 115x115 + Badge 'REVIEW PHIM' góc trên trái
            avatar_img = cls.create_circular_avatar(avatar_path, size=115)
            final_img.paste(avatar_img, (45, 40), mask=avatar_img)

            draw = ImageDraw.Draw(final_img)
            badge_w, badge_h = 230, 52
            badge_x, badge_y = 175, 70
            draw.rounded_rectangle(
                [(badge_x, badge_y), (badge_x + badge_w, badge_y + badge_h)],
                radius=14,
                fill=(210, 25, 25, 245),
                outline=(255, 215, 0, 255),
                width=3
            )
            try:
                badge_font = ImageFont.truetype(font_path, 26)
                draw.text((badge_x + 20, badge_y + 11), "REVIEW PHIM", fill=(255, 255, 255), font=badge_font)
            except Exception:
                draw.text((badge_x + 20, badge_y + 11), "REVIEW PHIM", fill=(255, 255, 255))

            # 4. Tiêu đề Tiếng Việt cực lớn ở khoảng trống bên trái (x=50, max_w=740px)
            clean_title = cls.clean_catchy_title(catchy_title)
            lines, title_font, font_sz = cls.fit_and_wrap_text(
                clean_title, font_path, max_width=720, start_font_size=62, min_font_size=38
            )
            line_h = font_sz + 14
            total_text_h = len(lines) * line_h
            base_y = 260 + max(0, (380 - total_text_h) // 2)

            for l_idx, line in enumerate(lines):
                bbox = title_font.getbbox(line)
                tw = bbox[2] - bbox[0]
                tx = 50
                ty = base_y + l_idx * line_h
                for dx in range(-5, 6):
                    for dy in range(-5, 6):
                        draw.text((tx + dx, ty + dy), line, fill=(0, 0, 0, 255), font=title_font)
                draw.text((tx, ty), line, fill=(255, 230, 10, 255), font=title_font)

        else:
            # -------------------------------------------------------------
            # BỐ CỤC CHUẨN KHI NGUỒN LÀ PHIM NGANG (16:9)
            # -------------------------------------------------------------
            target_ratio = target_w / target_h
            src_ratio = sw / sh
            if src_ratio > target_ratio:
                crop_w = int(sh * target_ratio)
                crop_h = sh
                cx = (sw - crop_w) // 2
                cy = 0
            else:
                crop_w = sw
                crop_h = int(sw / target_ratio)
                cx = 0
                cy = (sh - crop_h) // 2

            cropped = src_img.crop((cx, cy, cx + crop_w, cy + crop_h))
            final_img = cropped.resize((target_w, target_h), Image.Resampling.LANCZOS)
            final_img = cls.apply_bottom_gradient_mask(final_img, gradient_height_ratio=0.28)

            draw = ImageDraw.Draw(final_img)
            avatar_img = cls.create_circular_avatar(avatar_path, size=115)
            final_img.paste(avatar_img, (36, 36), mask=avatar_img)

            badge_w, badge_h = 230, 52
            badge_x, badge_y = 165, 68
            draw.rounded_rectangle(
                [(badge_x, badge_y), (badge_x + badge_w, badge_y + badge_h)],
                radius=14,
                fill=(210, 25, 25, 245),
                outline=(255, 215, 0, 255),
                width=3
            )
            try:
                badge_font = ImageFont.truetype(font_path, 26)
                draw.text((badge_x + 20, badge_y + 11), "REVIEW PHIM", fill=(255, 255, 255), font=badge_font)
            except Exception:
                draw.text((badge_x + 20, badge_y + 11), "REVIEW PHIM", fill=(255, 255, 255))

            clean_title = cls.clean_catchy_title(catchy_title)
            max_title_w = int(target_w * 0.88)
            lines, title_font, font_sz = cls.fit_and_wrap_text(
                clean_title, font_path, max_width=max_title_w, start_font_size=56, min_font_size=38
            )
            line_h = font_sz + 12
            total_text_h = len(lines) * line_h
            base_y = target_h - total_text_h - 26

            for l_idx, line in enumerate(lines):
                bbox = title_font.getbbox(line)
                tw = bbox[2] - bbox[0]
                tx = max(40, (target_w - tw) // 2)
                ty = base_y + l_idx * line_h
                for dx in range(-4, 5):
                    for dy in range(-4, 5):
                        draw.text((tx + dx, ty + dy), line, fill=(0, 0, 0, 255), font=title_font)
                draw.text((tx, ty), line, fill=(255, 230, 10, 255), font=title_font)

        out_rgb = final_img.convert("RGB")
        out_rgb.save(output_path, "JPEG", quality=95)
        print(f"[ThumbnailMaker] ✅ Xuất Thumbnail ngang chuẩn 16:9: {output_path}")
        return output_path

    @classmethod
    def render_vertical_thumbnail(
        cls,
        source_frame_path: str,
        catchy_title: str,
        output_path: str,
        face_cx_ratio: float = 0.5
    ) -> str:
        """
        Render Thumbnail Dọc 720x1280 (chuẩn tỷ lệ 9:16 Shorts/TikTok).
        Định vị cửa sổ crop theo tâm khuôn mặt (face-centered crop) để không bị xén mặt nhân vật.
        """
        target_w, target_h = 720, 1280
        src_img = Image.open(source_frame_path).convert("RGBA")
        sw, sh = src_img.size

        # Cắt lấy tỷ lệ 9:16
        target_ratio = target_w / target_h
        src_ratio = sw / sh

        if src_ratio > target_ratio:
            crop_w = int(sh * target_ratio)
            crop_h = sh
            # Căn cửa sổ crop theo tâm khuôn mặt
            face_cx = int(sw * max(0.05, min(0.95, face_cx_ratio)))
            cx = max(0, min(sw - crop_w, face_cx - crop_w // 2))
            cy = 0
        else:
            crop_w = sw
            crop_h = int(sw / target_ratio)
            cx = 0
            cy = max(0, min(sh - crop_h, int(sh * 0.35) - crop_h // 2))

        cropped = src_img.crop((cx, cy, cx + crop_w, cy + crop_h))
        final_img = cropped.resize((target_w, target_h), Image.Resampling.LANCZOS)
        w, h = final_img.size

        # 0. KHỐI LÀM MỜ VÀ TINT ĐEN CHUYÊN DỤNG XÓA SẠCH 100% PHỤ ĐỀ TRUNG GỐC (y=780..950)
        # Bắt buộc áp dụng GaussianBlur(radius=24) + nền tint đen rgba(0,0,0,0.45) với viền feathered 30px
        sub_y1, sub_y2 = 780, 950
        feather = 30
        sub_blurred = final_img.filter(ImageFilter.GaussianBlur(radius=24))
        tint_layer = Image.new("RGBA", (w, h), (0, 0, 0, 115))  # alpha 115 ~= 45% tint đen
        sub_blurred_tinted = Image.alpha_composite(sub_blurred, tint_layer)

        sub_mask = Image.new("L", (w, h), 0)
        s_draw = ImageDraw.Draw(sub_mask)
        for cur_y in range(sub_y1, sub_y2):
            if cur_y < sub_y1 + feather:
                m_alpha = int(255 * ((cur_y - sub_y1) / float(feather)))
            elif cur_y > sub_y2 - feather:
                m_alpha = int(255 * ((sub_y2 - cur_y) / float(feather)))
            else:
                m_alpha = 255
            s_draw.line([(0, cur_y), (w, cur_y)], fill=m_alpha)
        final_img.paste(sub_blurred_tinted, (0, 0), sub_mask)

        # Che mượt vùng đáy bằng dải GaussianBlur + Gradient tối từ y=740 xuống đáy y=1280 (42% đáy)
        y_start = 740
        grad_h = h - y_start

        # 1. Bản copy làm mờ vùng đáy
        blurred = final_img.filter(ImageFilter.GaussianBlur(radius=18))

        # 2. Feathered alpha mask cho lớp blur (alpha tăng dần từ 0 ở y=740 lên 255 ở y=840..1280)
        blur_mask = Image.new("L", (w, h), 0)
        b_draw = ImageDraw.Draw(blur_mask)
        for y in range(grad_h):
            ratio = y / float(grad_h)
            alpha = int(255 * (min(1.0, ratio * 1.8) ** 1.4))
            b_draw.line([(0, y_start + y), (w, y_start + y)], fill=alpha)
        final_img.paste(blurred, (0, 0), blur_mask)

        # 3. Lớp gradient tối (từ alpha=0 ở y=740 lên alpha=245 ở y=920..1280)
        dark_layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        k_draw = ImageDraw.Draw(dark_layer)
        for y in range(grad_h):
            ratio = y / float(grad_h)
            alpha = int(245 * (min(1.0, ratio * 1.5) ** 1.2))
            k_draw.line([(0, y_start + y), (w, y_start + y)], fill=(0, 0, 0, alpha))
        final_img.paste(dark_layer, (0, 0), dark_layer)

        draw = ImageDraw.Draw(final_img)
        font_path = Settings.get_font_path()
        avatar_path = str(ASSETS_DIR / "avatar.png")

        # 1. Chèn Avatar tròn 120x120
        avatar_img = cls.create_circular_avatar(avatar_path, size=115)
        final_img.paste(avatar_img, (32, 40), mask=avatar_img)

        # 2. Chèn Badge 'REVIEW PHIM'
        badge_w, badge_h = 230, 52
        badge_x, badge_y = 165, 70
        draw.rounded_rectangle(
            [(badge_x, badge_y), (badge_x + badge_w, badge_y + badge_h)],
            radius=14,
            fill=(210, 25, 25, 245),
            outline=(255, 215, 0, 255),
            width=3
        )
        try:
            badge_font = ImageFont.truetype(font_path, 26)
            draw.text((badge_x + 20, badge_y + 11), "REVIEW PHIM", fill=(255, 255, 255), font=badge_font)
        except Exception:
            draw.text((badge_x + 20, badge_y + 11), "REVIEW PHIM", fill=(255, 255, 255))

        # 3. Tiêu đề Tiếng Việt in hoa giật gân (auto-fit & auto-wrap với lề an toàn >= 40px)
        # Đặt tiêu đề to đậm đè chính xác lên dải y=840..1100 để triệt tiêu 100% mọi chữ Hán
        clean_title = cls.clean_catchy_title(catchy_title)
        max_title_w = int(target_w * 0.88)  # 633px (lề an toàn > 43px mỗi bên)
        lines, title_font, font_sz = cls.fit_and_wrap_text(
            clean_title, font_path, max_width=max_title_w, start_font_size=54, min_font_size=36
        )

        line_h = font_sz + 14
        total_text_h = len(lines) * line_h
        base_y = 880 if len(lines) == 1 else (920 - total_text_h // 2)

        for l_idx, line in enumerate(lines):
            bbox = title_font.getbbox(line)
            tw = bbox[2] - bbox[0]
            tx = max(40, (target_w - tw) // 2)
            ty = base_y + l_idx * line_h

            # Viền đen dày & đổ bóng cực mạnh
            for dx in range(-5, 6):
                for dy in range(-5, 6):
                    draw.text((tx + dx, ty + dy), line, fill=(0, 0, 0, 255), font=title_font)
            # Chữ vàng nghệ thuật
            draw.text((tx, ty), line, fill=(255, 230, 10, 255), font=title_font)

        out_rgb = final_img.convert("RGB")
        out_rgb.save(output_path, "JPEG", quality=95)
        print(f"[ThumbnailMaker] ✅ Xuất Thumbnail dọc chuẩn 9:16: {output_path}")
        return output_path

    @classmethod
    def generate_thumbnails(
        cls,
        video_path: str,
        catchy_title: str,
        output_vertical_path: str,
        output_horizontal_path: str,
        tmp_dir: str,
        skip_ranges: Optional[List[Dict[str, Any]]] = None
    ) -> Tuple[str, str]:
        """Tạo trọn bộ 2 phiên bản Thumbnail chất lượng cao theo từng tỷ lệ riêng biệt."""
        best_frame_path, face_cx_ratio = cls.select_best_frame_with_ai(video_path, tmp_dir, skip_ranges=skip_ranges)

        # 1. Thumbnail Dọc (Shorts/TikTok) với Face-Centered Crop
        cls.render_vertical_thumbnail(best_frame_path, catchy_title, output_vertical_path, face_cx_ratio=face_cx_ratio)

        # 2. Thumbnail Ngang (YouTube)
        cls.render_horizontal_thumbnail(best_frame_path, catchy_title, output_horizontal_path)

        return output_vertical_path, output_horizontal_path
