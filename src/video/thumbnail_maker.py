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
    def get_display_font_path(cls) -> str:
        """Ưu tiên font Montserrat-BlackItalic hoặc BeVietnamPro-BlackItalic cho tiêu đề display."""
        candidates = [
            ASSETS_DIR / "fonts" / "Montserrat-BlackItalic.ttf",
            ASSETS_DIR / "fonts" / "BeVietnamPro-BlackItalic.ttf",
            ASSETS_DIR / "fonts" / "BeVietnamPro-ExtraBoldItalic.ttf",
            ASSETS_DIR / "fonts" / "Oswald-Bold.ttf"
        ]
        for c in candidates:
            if c.exists():
                return str(c)
        return Settings.get_font_path()

    @classmethod
    def parse_layers_from_title(cls, catchy_title: str) -> Dict[str, str]:
        """Tự động phân tách tiêu đề thành 3 tầng nếu không có sẵn layers từ AI."""
        clean = cls.clean_catchy_title(catchy_title)
        words = clean.split()
        if len(words) <= 2:
            return {
                "line1_red_brush": "TRỌN BỘ FULL",
                "line2_big_yellow": clean,
                "line3_yellow_block": "KỊCH TÍNH NGHẸT THỞ!"
            }
        elif len(words) == 3:
            return {
                "line1_red_brush": words[0],
                "line2_big_yellow": words[1],
                "line3_yellow_block": words[2] + " ĐỈNH CAO!"
            }
        else:
            mid = (len(words) + 1) // 2
            return {
                "line1_red_brush": words[0],
                "line2_big_yellow": " ".join(words[1:mid]),
                "line3_yellow_block": " ".join(words[mid:]) + "!"
            }

    @classmethod
    def render_horizontal_thumbnail(
        cls,
        source_frame_path: str,
        catchy_title: str,
        output_path: str,
        face_cx_ratio: float = 0.5,
        layers: Optional[Dict[str, str]] = None,
        bg_image: Optional[Image.Image] = None
    ) -> str:
        """
        Render Thumbnail Ngang 1280x720 (chuẩn tỷ lệ 16:9).
        - Nền: Ảnh AI điện ảnh 16:9 (hoặc Cinematic Color Grading fallback).
        - Bên phải (x=52%..97%): Khối chữ 3 tầng nghiêng +5.5° (bay lên bên phải).
        - Góc dưới trái: Huy hiệu FULL đỏ viền vàng sang trọng.
        - Góc trên trái: Avatar tròn viền vàng tinh tế.
        """
        target_w, target_h = 1280, 720
        scale = 2
        sw, sh = target_w * scale, target_h * scale  # 2560 x 1440
        max_block_w = int(580 * scale)  # Tối đa 580px tại 1x (1160px tại 2x) trên nửa phải

        # 1. Chuẩn bị ảnh nền 2560x1440
        if bg_image:
            final_bg = bg_image.convert("RGBA").resize((sw, sh), Image.Resampling.LANCZOS)
        else:
            final_bg = GeminiClient.generate_cinematic_thumbnail_bg(
                story_bible={},
                reference_frame_path=source_frame_path,
                aspect_ratio="16:9",
                face_cx_ratio=face_cx_ratio
            ).resize((sw, sh), Image.Resampling.LANCZOS)

        font_path = cls.get_display_font_path()
        avatar_path = str(ASSETS_DIR / "avatar.png")
        kit_dir = ASSETS_DIR / "thumb_kit"
        brush_red_path = str(kit_dir / "brush_red.png")
        brush_yellow_path = str(kit_dir / "brush_yellow.png")
        badge_full_path = str(kit_dir / "badge_full.png")

        # 2. Chuẩn bị nội dung 3 tầng
        if not layers:
            layers = cls.parse_layers_from_title(catchy_title)

        l1_text = layers.get("line1_red_brush", "TRỌN BỘ FULL").upper().strip()
        l2_text = layers.get("line2_big_yellow", "VẢ MẶT").upper().strip()
        l3_text = layers.get("line3_yellow_block", "CẢ NHÀ QUỲ XIN!").upper().strip()

        # Canvas tạm chứa khối chữ unrotated
        bw = max_block_w
        bh = int(600 * scale)
        block = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))

        # --- TẦNG 1: Vệt cọ đỏ + chữ trắng in nghiêng ---
        f1_size = 54 * scale
        f1 = ImageFont.truetype(font_path, f1_size)
        b1_bbox = f1.getbbox(l1_text)
        t1_w = b1_bbox[2] - b1_bbox[0]
        br1_w = int(t1_w * 1.25) + int(50 * scale)
        while (br1_w > bw - int(40 * scale)) and f1_size > 28 * scale:
            f1_size -= 2 * scale
            f1 = ImageFont.truetype(font_path, f1_size)
            b1_bbox = f1.getbbox(l1_text)
            t1_w = b1_bbox[2] - b1_bbox[0]
            br1_w = int(t1_w * 1.25) + int(50 * scale)

        t1_h = b1_bbox[3] - b1_bbox[1]
        br1_w = max(br1_w, int(350 * scale))
        br1_h = int(125 * scale)

        if os.path.exists(brush_red_path):
            brush_red = Image.open(brush_red_path).convert("RGBA").resize((br1_w, br1_h), Image.Resampling.LANCZOS)
        else:
            brush_red = Image.new("RGBA", (br1_w, br1_h), (210, 20, 30, 240))

        t1_canvas = Image.new("RGBA", (br1_w, br1_h), (0, 0, 0, 0))
        t1_canvas.paste(brush_red, (0, 0), brush_red)
        t1_draw = ImageDraw.Draw(t1_canvas)
        tx1 = (br1_w - t1_w) // 2 - b1_bbox[0]
        ty1 = (br1_h - t1_h) // 2 - b1_bbox[1] - int(4 * scale)
        t1_draw.text((tx1, ty1), l1_text, font=f1, fill=(255, 255, 255, 255),
                     stroke_width=int(4 * scale), stroke_fill=(15, 15, 15, 255))

        p1_x = max(0, (bw - br1_w) // 2)
        p1_y = int(20 * scale)
        block.paste(t1_canvas, (p1_x, p1_y), t1_canvas)

        # --- TẦNG 2: Chữ vàng chanh cực lớn + viền đen siêu dày ---
        f2_size = 95 * scale
        f2 = ImageFont.truetype(font_path, f2_size)
        b2_bbox = f2.getbbox(l2_text)
        t2_w = b2_bbox[2] - b2_bbox[0]
        while (t2_w + int(30 * scale) > bw - int(30 * scale)) and f2_size > 45 * scale:
            f2_size -= 2 * scale
            f2 = ImageFont.truetype(font_path, f2_size)
            b2_bbox = f2.getbbox(l2_text)
            t2_w = b2_bbox[2] - b2_bbox[0]
        t2_h = b2_bbox[3] - b2_bbox[1]

        t2_canvas = Image.new("RGBA", (bw, t2_h + int(50 * scale)), (0, 0, 0, 0))
        t2_draw = ImageDraw.Draw(t2_canvas)
        tx2 = (bw - t2_w) // 2 - b2_bbox[0]
        ty2 = int(12 * scale) - b2_bbox[1]

        # Đổ bóng đen sâu
        for s_dx, s_dy in [(int(6 * scale), int(8 * scale)), (int(10 * scale), int(12 * scale))]:
            t2_draw.text((tx2 + s_dx, ty2 + s_dy), l2_text, font=f2, fill=(0, 0, 0, 180),
                         stroke_width=int(12 * scale), stroke_fill=(0, 0, 0, 180))
        # Viền đen dày + màu vàng chanh rực rỡ
        t2_draw.text((tx2, ty2), l2_text, font=f2, fill=(255, 240, 0, 255),
                     stroke_width=int(10 * scale), stroke_fill=(10, 10, 10, 255))

        p2_y = p1_y + br1_h - int(10 * scale)
        block.paste(t2_canvas, (0, p2_y), t2_canvas)

        # --- TẦNG 3: Mảng cọ vàng rực + chữ đỏ thẫm viền trắng ---
        words3 = l3_text.split()
        if len(words3) >= 4 or len(l3_text) > 16:
            mid = (len(words3) + 1) // 2
            l3_lines = [" ".join(words3[:mid]), " ".join(words3[mid:])]
        else:
            l3_lines = [l3_text]

        f3_size = (54 if len(l3_lines) == 1 else 44) * scale
        f3 = ImageFont.truetype(font_path, f3_size)
        max_w3 = max([f3.getbbox(ln)[2] - f3.getbbox(ln)[0] for ln in l3_lines])
        while (max_w3 + int(60 * scale) > bw - int(30 * scale)) and f3_size > 26 * scale:
            f3_size -= 2 * scale
            f3 = ImageFont.truetype(font_path, f3_size)
            max_w3 = max([f3.getbbox(ln)[2] - f3.getbbox(ln)[0] for ln in l3_lines])

        line_h3 = int(f3_size * 1.15)
        total_h3 = len(l3_lines) * line_h3

        br3_w = min(bw - int(20 * scale), max(int(max_w3 * 1.25) + int(60 * scale), int(420 * scale)))
        br3_h = int((140 if len(l3_lines) == 1 else 210) * scale)

        if os.path.exists(brush_yellow_path):
            brush_yellow = Image.open(brush_yellow_path).convert("RGBA").resize((br3_w, br3_h), Image.Resampling.LANCZOS)
        else:
            brush_yellow = Image.new("RGBA", (br3_w, br3_h), (255, 200, 10, 240))

        t3_canvas = Image.new("RGBA", (br3_w, br3_h), (0, 0, 0, 0))
        t3_canvas.paste(brush_yellow, (0, 0), brush_yellow)
        t3_draw = ImageDraw.Draw(t3_canvas)

        start_y3 = (br3_h - total_h3) // 2
        for idx_l, line_str in enumerate(l3_lines):
            bb = f3.getbbox(line_str)
            lw = bb[2] - bb[0]
            lx = (br3_w - lw) // 2 - bb[0]
            ly = start_y3 + idx_l * line_h3 - bb[1]
            t3_draw.text((lx + int(3 * scale), ly + int(4 * scale)), line_str, font=f3, fill=(0, 0, 0, 160),
                         stroke_width=int(6 * scale), stroke_fill=(0, 0, 0, 160))
            t3_draw.text((lx, ly), line_str, font=f3, fill=(160, 0, 20, 255),
                         stroke_width=int(6 * scale), stroke_fill=(255, 255, 255, 255))

        p3_x = max(0, (bw - br3_w) // 2)
        p3_y = p2_y + t2_h + int(24 * scale)
        block.paste(t3_canvas, (p3_x, p3_y), t3_canvas)

        # 3. Cắt gọn vùng chữ và nghiêng +5.5° ngược chiều kim đồng hồ (bay lên bên phải)
        bbox = block.getbbox()
        if bbox:
            block = block.crop(bbox)

        rotated_block = block.rotate(5.5, resample=Image.Resampling.BICUBIC, expand=True)
        # Khóa chống tràn tuyệt đối 100%
        if rotated_block.width > max_block_w:
            factor = max_block_w / float(rotated_block.width)
            new_sz = (max_block_w, int(rotated_block.height * factor))
            rotated_block = rotated_block.resize(new_sz, Image.Resampling.LANCZOS)

        rb_w, rb_h = rotated_block.size
        paste_x = sw - rb_w - int(40 * scale)
        paste_y = (sh - rb_h) // 2 - int(10 * scale)
        final_bg.paste(rotated_block, (paste_x, paste_y), rotated_block)

        # 4. Huy hiệu FULL (Góc dưới trái)
        if os.path.exists(badge_full_path):
            badge = Image.open(badge_full_path).convert("RGBA")
            badge_w, badge_h = int(250 * scale), int(115 * scale)
            badge = badge.resize((badge_w, badge_h), Image.Resampling.LANCZOS)
            final_bg.paste(badge, (int(45 * scale), sh - badge_h - int(40 * scale)), badge)

        # 5. Avatar (Góc trên trái)
        if os.path.exists(avatar_path):
            av = Image.open(avatar_path).convert("RGBA")
            av_sz = int(95 * scale)
            av = av.resize((av_sz, av_sz), Image.Resampling.LANCZOS)
            av_mask = Image.new("L", (av_sz, av_sz), 0)
            ImageDraw.Draw(av_mask).ellipse((0, 0, av_sz, av_sz), fill=255)
            av_circ = Image.new("RGBA", (av_sz, av_sz), (0, 0, 0, 0))
            av_circ.paste(av, (0, 0), av_mask)
            ImageDraw.Draw(av_circ).ellipse((2, 2, av_sz - 3, av_sz - 3), outline=(255, 215, 0, 255), width=int(3 * scale))
            final_bg.paste(av_circ, (int(45 * scale), int(45 * scale)), av_circ)

        # 6. Downscale LANCZOS về kích thước chuẩn 1280x720
        out_rgb = final_bg.resize((target_w, target_h), Image.Resampling.LANCZOS).convert("RGB")
        out_rgb.save(output_path, "JPEG", quality=95)
        print(f"[ThumbnailMaker] ✅ Xuất Thumbnail ngang chuẩn 16:9 (3 tầng cọ sơn): {output_path}")
        return output_path

    @classmethod
    def render_vertical_thumbnail(
        cls,
        source_frame_path: str,
        catchy_title: str,
        output_path: str,
        face_cx_ratio: float = 0.5,
        layers: Optional[Dict[str, str]] = None,
        bg_image: Optional[Image.Image] = None
    ) -> str:
        """
        Render Thumbnail Dọc 720x1280 (chuẩn tỷ lệ 9:16 Shorts/TikTok).
        - Nền: Ảnh AI điện ảnh 9:16 (hoặc Cinematic Color Grading fallback).
        - Phía dưới (y=62%..93%): Khối chữ 3 tầng nghiêng +5.5° căn giữa cân đối.
        - Góc trên: Avatar tròn + Huy hiệu FULL sang trọng.
        """
        target_w, target_h = 720, 1280
        scale = 2
        sw, sh = target_w * scale, target_h * scale  # 1440 x 2560
        max_block_w = int(600 * scale)  # Tối đa 600px tại 1x (1200px tại 2x = 83.3% bề rộng)

        # 1. Chuẩn bị ảnh nền 1440x2560
        if bg_image:
            final_bg = bg_image.convert("RGBA").resize((sw, sh), Image.Resampling.LANCZOS)
        else:
            final_bg = GeminiClient.generate_cinematic_thumbnail_bg(
                story_bible={},
                reference_frame_path=source_frame_path,
                aspect_ratio="9:16",
                face_cx_ratio=face_cx_ratio
            ).resize((sw, sh), Image.Resampling.LANCZOS)

        # 1b. Xóa sạch 100% vệt chữ Hán y=760..980px (59%..77% chiều cao) với feather 35px
        blur_y1 = int(760 * scale)
        blur_y2 = int(980 * scale)
        feather = int(35 * scale)
        sub_blurred = final_bg.filter(ImageFilter.GaussianBlur(radius=int(26 * scale)))
        tint_layer = Image.new("RGBA", (sw, sh), (0, 0, 0, int(255 * 0.38)))
        sub_blurred_tinted = Image.alpha_composite(sub_blurred, tint_layer)
        sub_mask = Image.new("L", (sw, sh), 0)
        s_draw = ImageDraw.Draw(sub_mask)
        for cur_y in range(blur_y1, blur_y2):
            if cur_y < blur_y1 + feather:
                m_alpha = int(255 * ((cur_y - blur_y1) / float(feather)))
            elif cur_y > blur_y2 - feather:
                m_alpha = int(255 * ((blur_y2 - cur_y) / float(feather)))
            else:
                m_alpha = 255
            s_draw.line([(0, cur_y), (sw, cur_y)], fill=m_alpha)
        final_bg.paste(sub_blurred_tinted, (0, 0), sub_mask)

        font_path = cls.get_display_font_path()
        avatar_path = str(ASSETS_DIR / "avatar.png")
        kit_dir = ASSETS_DIR / "thumb_kit"
        brush_red_path = str(kit_dir / "brush_red.png")
        brush_yellow_path = str(kit_dir / "brush_yellow.png")
        badge_full_path = str(kit_dir / "badge_full.png")

        # 2. Chuẩn bị nội dung 3 tầng
        if not layers:
            layers = cls.parse_layers_from_title(catchy_title)

        l1_text = layers.get("line1_red_brush", "TRỌN BỘ FULL").upper().strip()
        l2_text = layers.get("line2_big_yellow", "VẢ MẶT").upper().strip()
        l3_text = layers.get("line3_yellow_block", "CẢ NHÀ QUỲ XIN!").upper().strip()

        # Canvas tạm chứa khối chữ unrotated
        bw = max_block_w
        bh = int(550 * scale)
        block = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))

        # --- TẦNG 1: Vệt cọ đỏ + chữ trắng ---
        f1_size = 50 * scale
        f1 = ImageFont.truetype(font_path, f1_size)
        b1_bbox = f1.getbbox(l1_text)
        t1_w = b1_bbox[2] - b1_bbox[0]
        br1_w = int(t1_w * 1.25) + int(45 * scale)
        while (br1_w > bw - int(40 * scale)) and f1_size > 26 * scale:
            f1_size -= 2 * scale
            f1 = ImageFont.truetype(font_path, f1_size)
            b1_bbox = f1.getbbox(l1_text)
            t1_w = b1_bbox[2] - b1_bbox[0]
            br1_w = int(t1_w * 1.25) + int(45 * scale)

        t1_h = b1_bbox[3] - b1_bbox[1]
        br1_w = max(br1_w, int(320 * scale))
        br1_h = int(120 * scale)

        if os.path.exists(brush_red_path):
            brush_red = Image.open(brush_red_path).convert("RGBA").resize((br1_w, br1_h), Image.Resampling.LANCZOS)
        else:
            brush_red = Image.new("RGBA", (br1_w, br1_h), (210, 20, 30, 240))

        t1_canvas = Image.new("RGBA", (br1_w, br1_h), (0, 0, 0, 0))
        t1_canvas.paste(brush_red, (0, 0), brush_red)
        t1_draw = ImageDraw.Draw(t1_canvas)
        tx1 = (br1_w - t1_w) // 2 - b1_bbox[0]
        ty1 = (br1_h - t1_h) // 2 - b1_bbox[1] - int(4 * scale)
        t1_draw.text((tx1, ty1), l1_text, font=f1, fill=(255, 255, 255, 255),
                     stroke_width=int(4 * scale), stroke_fill=(15, 15, 15, 255))

        p1_x = (bw - br1_w) // 2
        p1_y = int(15 * scale)
        block.paste(t1_canvas, (p1_x, p1_y), t1_canvas)

        # --- TẦNG 2: Chữ vàng chanh cực lớn ---
        f2_size = 85 * scale
        f2 = ImageFont.truetype(font_path, f2_size)
        b2_bbox = f2.getbbox(l2_text)
        t2_w = b2_bbox[2] - b2_bbox[0]
        while (t2_w + int(30 * scale) > bw - int(30 * scale)) and f2_size > 42 * scale:
            f2_size -= 2 * scale
            f2 = ImageFont.truetype(font_path, f2_size)
            b2_bbox = f2.getbbox(l2_text)
            t2_w = b2_bbox[2] - b2_bbox[0]
        t2_h = b2_bbox[3] - b2_bbox[1]

        t2_canvas = Image.new("RGBA", (bw, t2_h + int(50 * scale)), (0, 0, 0, 0))
        t2_draw = ImageDraw.Draw(t2_canvas)
        tx2 = (bw - t2_w) // 2 - b2_bbox[0]
        ty2 = int(10 * scale) - b2_bbox[1]
        for s_dx, s_dy in [(int(6 * scale), int(8 * scale)), (int(10 * scale), int(12 * scale))]:
            t2_draw.text((tx2 + s_dx, ty2 + s_dy), l2_text, font=f2, fill=(0, 0, 0, 180),
                         stroke_width=int(12 * scale), stroke_fill=(0, 0, 0, 180))
        t2_draw.text((tx2, ty2), l2_text, font=f2, fill=(255, 240, 0, 255),
                     stroke_width=int(10 * scale), stroke_fill=(10, 10, 10, 255))

        p2_y = p1_y + br1_h - int(10 * scale)
        block.paste(t2_canvas, (0, p2_y), t2_canvas)

        # --- TẦNG 3: Mảng cọ vàng rực + chữ đỏ thẫm viền trắng ---
        words3 = l3_text.split()
        if len(words3) >= 4 or len(l3_text) > 16:
            mid = (len(words3) + 1) // 2
            l3_lines = [" ".join(words3[:mid]), " ".join(words3[mid:])]
        else:
            l3_lines = [l3_text]

        f3_size = (48 if len(l3_lines) == 1 else 40) * scale
        f3 = ImageFont.truetype(font_path, f3_size)
        max_w3 = max([f3.getbbox(ln)[2] - f3.getbbox(ln)[0] for ln in l3_lines])
        while (max_w3 + int(50 * scale) > bw - int(30 * scale)) and f3_size > 24 * scale:
            f3_size -= 2 * scale
            f3 = ImageFont.truetype(font_path, f3_size)
            max_w3 = max([f3.getbbox(ln)[2] - f3.getbbox(ln)[0] for ln in l3_lines])

        line_h3 = int(f3_size * 1.15)
        total_h3 = len(l3_lines) * line_h3

        br3_w = min(bw - int(20 * scale), max(int(max_w3 * 1.25) + int(50 * scale), int(380 * scale)))
        br3_h = int((130 if len(l3_lines) == 1 else 195) * scale)

        if os.path.exists(brush_yellow_path):
            brush_yellow = Image.open(brush_yellow_path).convert("RGBA").resize((br3_w, br3_h), Image.Resampling.LANCZOS)
        else:
            brush_yellow = Image.new("RGBA", (br3_w, br3_h), (255, 200, 10, 240))

        t3_canvas = Image.new("RGBA", (br3_w, br3_h), (0, 0, 0, 0))
        t3_canvas.paste(brush_yellow, (0, 0), brush_yellow)
        t3_draw = ImageDraw.Draw(t3_canvas)

        start_y3 = (br3_h - total_h3) // 2
        for idx_l, line_str in enumerate(l3_lines):
            bb = f3.getbbox(line_str)
            lw = bb[2] - bb[0]
            lx = (br3_w - lw) // 2 - bb[0]
            ly = start_y3 + idx_l * line_h3 - bb[1]
            t3_draw.text((lx + int(3 * scale), ly + int(4 * scale)), line_str, font=f3, fill=(0, 0, 0, 160),
                         stroke_width=int(6 * scale), stroke_fill=(0, 0, 0, 160))
            t3_draw.text((lx, ly), line_str, font=f3, fill=(160, 0, 20, 255),
                         stroke_width=int(6 * scale), stroke_fill=(255, 255, 255, 255))

        p3_x = (bw - br3_w) // 2
        p3_y = p2_y + t2_h + int(20 * scale)
        block.paste(t3_canvas, (p3_x, p3_y), t3_canvas)

        # 3. Cắt gọn vùng chữ và nghiêng +5.5° ngược chiều kim đồng hồ (bay lên bên phải)
        bbox = block.getbbox()
        if bbox:
            block = block.crop(bbox)

        rotated_block = block.rotate(5.5, resample=Image.Resampling.BICUBIC, expand=True)
        # Khóa chống tràn tuyệt đối 100%
        if rotated_block.width > max_block_w:
            factor = max_block_w / float(rotated_block.width)
            new_sz = (max_block_w, int(rotated_block.height * factor))
            rotated_block = rotated_block.resize(new_sz, Image.Resampling.LANCZOS)

        rb_w, rb_h = rotated_block.size
        paste_x = (sw - rb_w) // 2
        paste_y = sh - rb_h - int(70 * scale)
        final_bg.paste(rotated_block, (paste_x, paste_y), rotated_block)

        # 4. Avatar (Góc trên trái)
        if os.path.exists(avatar_path):
            av = Image.open(avatar_path).convert("RGBA")
            av_sz = int(85 * scale)
            av = av.resize((av_sz, av_sz), Image.Resampling.LANCZOS)
            av_mask = Image.new("L", (av_sz, av_sz), 0)
            ImageDraw.Draw(av_mask).ellipse((0, 0, av_sz, av_sz), fill=255)
            av_circ = Image.new("RGBA", (av_sz, av_sz), (0, 0, 0, 0))
            av_circ.paste(av, (0, 0), av_mask)
            ImageDraw.Draw(av_circ).ellipse((2, 2, av_sz - 3, av_sz - 3), outline=(255, 215, 0, 255), width=int(3 * scale))
            final_bg.paste(av_circ, (int(35 * scale), int(45 * scale)), av_circ)

        # 5. Huy hiệu FULL (Góc trên phải)
        if os.path.exists(badge_full_path):
            badge = Image.open(badge_full_path).convert("RGBA")
            badge_w, badge_h = int(210 * scale), int(95 * scale)
            badge = badge.resize((badge_w, badge_h), Image.Resampling.LANCZOS)
            final_bg.paste(badge, (sw - badge_w - int(35 * scale), int(45 * scale)), badge)

        # 6. Downscale LANCZOS về kích thước chuẩn 720x1280
        out_rgb = final_bg.resize((target_w, target_h), Image.Resampling.LANCZOS).convert("RGB")
        out_rgb.save(output_path, "JPEG", quality=95)
        print(f"[ThumbnailMaker] ✅ Xuất Thumbnail dọc chuẩn 9:16 (3 tầng cọ sơn): {output_path}")
        return output_path

    @classmethod
    def generate_thumbnails(
        cls,
        video_path: str,
        catchy_title: str,
        output_vertical_path: str,
        output_horizontal_path: str,
        tmp_dir: str,
        skip_ranges: Optional[List[Dict[str, Any]]] = None,
        story_bible: Optional[Dict[str, Any]] = None,
        layers: Optional[Dict[str, str]] = None
    ) -> Tuple[str, str]:
        """Tạo trọn bộ 2 phiên bản Thumbnail chất lượng cao theo từng tỷ lệ riêng biệt."""
        best_frame_path, face_cx_ratio = cls.select_best_frame_with_ai(video_path, tmp_dir, skip_ranges=skip_ranges)

        # Sinh ảnh nền điện ảnh bằng AI (kèm fallback Cinematic Color Grading)
        bg_h = None
        bg_v = None
        if story_bible:
            try:
                bg_h = GeminiClient.generate_cinematic_thumbnail_bg(
                    story_bible=story_bible,
                    reference_frame_path=best_frame_path,
                    aspect_ratio="16:9",
                    face_cx_ratio=face_cx_ratio
                )
                bg_v = GeminiClient.generate_cinematic_thumbnail_bg(
                    story_bible=story_bible,
                    reference_frame_path=best_frame_path,
                    aspect_ratio="9:16",
                    face_cx_ratio=face_cx_ratio
                )
            except Exception as e_bg:
                print(f"[ThumbnailMaker] Lỗi sinh background: {e_bg}")

        # 1. Thumbnail Dọc (Shorts/TikTok) với Face-Centered Crop & 3 Tầng Vệt Cọ
        cls.render_vertical_thumbnail(
            best_frame_path,
            catchy_title,
            output_vertical_path,
            face_cx_ratio=face_cx_ratio,
            layers=layers,
            bg_image=bg_v
        )

        # 2. Thumbnail Ngang (YouTube) với Bố cục Điện ảnh Split 16:9 & 3 Tầng Vệt Cọ
        cls.render_horizontal_thumbnail(
            best_frame_path,
            catchy_title,
            output_horizontal_path,
            face_cx_ratio=face_cx_ratio,
            layers=layers,
            bg_image=bg_h
        )

        # 3. Ghi metadata text thumbnail nếu thư mục chưa có (phục vụ QA Gate đối chiếu trực tiếp)
        out_dir = Path(output_vertical_path).parent
        meta_file = out_dir / "thumbnail_metadata.json"
        if not meta_file.exists():
            try:
                g = (story_bible.get("genre") if story_bible else "") or "modern"
                actual_layers = layers or cls.parse_layers_from_title(catchy_title)
                import json
                with open(meta_file, "w", encoding="utf-8") as f:
                    json.dump({
                        "genre": g,
                        "line1_red_brush": actual_layers.get("line1_red_brush", "TRỌN BỘ FULL"),
                        "line2_big_yellow": actual_layers.get("line2_big_yellow", catchy_title),
                        "line3_yellow_block": actual_layers.get("line3_yellow_block", "KỊCH TÍNH NGHẸT THỞ!")
                    }, f, ensure_ascii=False, indent=2)
            except Exception:
                pass

        return output_vertical_path, output_horizontal_path
