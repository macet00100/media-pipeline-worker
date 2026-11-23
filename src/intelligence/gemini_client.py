import os
import time
import json
import re
import subprocess
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
from PIL import Image

from config.settings import Settings
from google import genai
from google.genai import types

class GeminiClient:
    """
    Trình kết nối Gemini API trung tâm hỗ trợ:
    1. Kiểm tra kết nối thật và tự động phát hiện model Flash khả dụng mới nhất.
    2. Phân tích video/audio đa phương thức (Multimodal File API) đọc hiểu nội dung thật.
    3. Nhận diện mở đầu, kết thúc, quảng cáo chèn giữa phim (Opening/Ending/Ads).
    4. Thẩm định và chọn khung hình Thumbnail có khuôn mặt biểu cảm kịch tính nhất.
    5. Tuyệt đối KHÔNG sử dụng kịch bản mẫu hay cơ chế giả mạo.
    """

    _cached_client: Optional[genai.Client] = None
    _cached_model: Optional[str] = None

    @classmethod
    def get_client_and_model(cls) -> Tuple[genai.Client, str]:
        """Khởi tạo client và tự động dò model Flash hoạt động thực tế."""
        if cls._cached_client and cls._cached_model:
            return cls._cached_client, cls._cached_model

        api_keys = Settings.GEMINI_API_KEYS
        if not api_keys:
            raise RuntimeError("LỖI: Chưa cấu hình GEMINI_API_KEY trong .env! Vui lòng cấu hình key hợp lệ.")

        last_error = None
        for key_idx, key in enumerate(api_keys):
            try:
                client = genai.Client(
                    api_key=key,
                    http_options=types.HttpOptions(
                        api_version="v1beta",
                        headers={"x-goog-api-key": key},
                        timeout=60000
                    )
                )
                # Dò danh sách model khả dụng từ Google AI Studio
                candidates = [
                    "gemini-3.1-flash-lite",
                    "gemini-3.5-flash-lite",
                    "gemini-flash-lite-latest",
                    "gemini-3.5-flash",
                    "gemini-3-flash-preview",
                    "gemini-3.7-flash",
                    "gemini-3.8-flash"
                ]

                working_model = None
                for cand in candidates:
                    try:
                        resp = client.models.generate_content(
                            model=cand,
                            contents="Ping. Reply with 1 word: OK",
                            config=types.GenerateContentConfig(temperature=0.1)
                        )
                        if resp.text and "OK" in resp.text.upper():
                            working_model = cand
                            break
                    except Exception as e_cand:
                        err_msg = str(e_cand).lower()
                        print(f"[GeminiClient] Thử model {cand} chưa thành công: {err_msg[:80]}...")
                        last_error = e_cand
                        continue

                if working_model:
                    cls._cached_client = client
                    cls._cached_model = working_model
                    print(f"======================================================================")
                    print(f"[GeminiClient] Kết nối Gemini API thành công (Key #{key_idx+1})")
                    print(f"[GeminiClient] Model đang hoạt động: {working_model}")
                    print(f"======================================================================")
                    return client, working_model

            except Exception as e_key:
                last_error = e_key

        raise RuntimeError(f"LỖI: Không thể kết nối bất kỳ model Gemini Flash nào! Chi tiết lỗi: {last_error}")

    @classmethod
    def make_draft_video(cls, source_video: str, output_draft: str, duration_sec: Optional[float] = None) -> str:
        """
        Nén bản nháp siêu nhẹ của video (360p, 2fps, audio 64k mono)
        để gửi nhanh qua Gemini Multimodal File API.
        """
        cmd = [
            "ffmpeg", "-y",
            "-i", source_video
        ]
        if duration_sec:
            cmd.extend(["-t", str(duration_sec)])

        cmd.extend([
            "-vf", "scale=-2:360,fps=2",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "32",
            "-c:a", "aac", "-b:a", "64k", "-ac", "1",
            output_draft
        ])
        res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if res.returncode != 0 or not os.path.exists(output_draft):
            raise RuntimeError(f"Lỗi tạo bản nháp video cho Gemini Multimodal: {source_video}")

        size_mb = os.path.getsize(output_draft) / (1024 * 1024)
        print(f"[GeminiClient] Đã tạo bản nháp nhẹ: {size_mb:.2f} MB ({output_draft})")
        return output_draft

    @classmethod
    def analyze_movie_multimodal(
        cls,
        source_video: str,
        drama_title: str,
        tmp_dir: str,
        episode_index: int = 1,
        total_episodes: int = 1,
        previous_summary: str = "",
        target_duration_sec: Optional[float] = None,
        is_first: Optional[bool] = None,
        is_last: Optional[bool] = None
    ) -> Dict[str, Any]:
        """
        Gửi bản nháp video qua Gemini File API để AI trực tiếp xem hình, nghe thoại
        và đọc phụ đề tiếng Trung gốc của phim.
        Hỗ trợ điều phối mạch truyện liền mạch (Seamless Story Flow) và Dynamic Budget cho Review Trọn Bộ:
        - Phần 1: Có lời chào thương hiệu mở đầu.
        - Phần 2..N: Vào thẳng mạch phim, không lặp lại tình tiết các phần trước, không chào lại.
        - Phần N (cuối): Có lời kết trọn bộ, tuyệt đối không nói 'hẹn tập tiếp theo'.
        """
        client, model_name = cls.get_client_and_model()

        draft_video = os.path.join(tmp_dir, f"gemini_multimodal_draft_ep{episode_index}.mp4")
        cls.make_draft_video(source_video, draft_video)

        # Lấy thời lượng thực tế của source_video
        dur_cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", source_video]
        try:
            r_dur = subprocess.run(dur_cmd, capture_output=True, text=True, timeout=10)
            v_dur = float(r_dur.stdout.strip() or 600.0)
        except Exception:
            v_dur = 600.0
        ending_hint = max(30.0, v_dur - 50.0)

        # Tính toán ngân sách từ ngữ và số phân đoạn theo Dynamic Duration Budget
        if target_duration_sec and target_duration_sec > 10.0:
            target_words = int(target_duration_sec * 2.7)
            min_blocks = max(4, min(25, int(target_duration_sec / 35.0)))
            max_blocks = max(min_blocks + 2, min(32, int(target_duration_sec / 20.0)))
            budget_instruction = (
                f"- ĐẶT MỤC TIÊU NÉN CHUẨN TOÀN BỘ: Phần này cần được nén thành khoảng {target_duration_sec:.1f} giây video review "
                f"(toàn bộ kịch bản phần này có khoảng {target_words} từ tiếng Việt, chia thành {min_blocks} đến {max_blocks} phân đoạn narration_blocks)."
            )
        else:
            budget_instruction = (
                "- ĐẶT MỤC TIÊU NÉN CHUẨN 35% – 45%: BẮT BUỘC chia phần phim thành đúng 10 ĐẾN 14 PHÂN ĐOẠN REVIEW CHI TIẾT "
                "(narration_blocks), tổng thời lượng review đạt khoảng 3.5 đến 4.5 phút (tương đương toàn bộ kịch bản có từ 550 đến 700 từ)."
            )

        # Hướng dẫn mạch truyện theo vị trí tập/chương
        is_first_part = (episode_index == 1) if is_first is None else is_first
        is_last_part = (episode_index == total_episodes) if is_last is None else is_last

        if is_first_part:
            flow_instruction = (
                "- LỜI MỞ ĐẦU THƯƠNG HIỆU: Vì đây là phần mở đầu của video Review Trọn Bộ, ở đầu phân đoạn 1 BẮT BUỘC có lời chào: "
                "'Chào mừng quý khán giả đến với Cool Mèo Phim.'"
            )
        else:
            flow_instruction = (
                f"- MẠCH TRUYỆN LIỀN MẠCH (Phần {episode_index}/{total_episodes}): "
                f"Tóm tắt các phần trước: '{previous_summary[:300] if previous_summary else 'Tiếp nối biến cố trước'}'."
                "\nTUYỆT ĐỐI KHÔNG chào hỏi lại, KHÔNG nhắc tên kênh hay kêu gọi like/sub. Vào thẳng ngay tình tiết mới để nối tiếp các phần trước một cách liền mạch!"
            )

        if is_last_part:
            ending_instruction = (
                "- LỜI KẾT TRỌN BỘ: Vì đây là phần kết thúc toàn bộ phim, ở cuối phân đoạn cuối cùng (trước Outro) BẮT BUỘC có lời kết: "
                "'Hãy bấm like và đăng ký kênh Cool Mèo Phim để đón xem những bộ phim hấp dẫn tiếp theo nhé.' "
                "TUYỆT ĐỐI KHÔNG NÓI 'hẹn gặp lại ở tập tiếp theo' vì video này là trọn bộ cả bộ phim đã xong!"
            )
        else:
            ending_instruction = (
                "- CẤM TUYỆT ĐỐI CÂU KẾT LỬNG / NGẮT MẠCH: TUYỆT ĐỐI CẤM các câu ngắt mạch như 'Hãy cùng theo dõi diễn biến tiếp theo của bộ phim', "
                "'tập sau sẽ rõ', 'chuyện gì sẽ xảy ra tiếp theo hãy cùng đón xem'. "
                "Vì toàn bộ các phần sẽ được ghép nối liền mạch thành 1 video duy nhất, câu cuối của phần này PHẢI dẫn thẳng tự nhiên sang diễn biến tiếp theo!"
            )

        print(f"[GeminiClient] Đang tải bản nháp video tập {episode_index}/{total_episodes} lên Gemini File API...")
        uploaded_file = client.files.upload(file=draft_video)
        print(f"[GeminiClient] File ID: {uploaded_file.name}. Đang đợi xử lý...")

        try:
            # Chờ file sẵn sàng
            while uploaded_file.state == types.FileState.PROCESSING:
                time.sleep(2)
                uploaded_file = client.files.get(name=uploaded_file.name)

            if uploaded_file.state != types.FileState.ACTIVE:
                raise RuntimeError(f"Lỗi tải video lên Gemini: File ở trạng thái {uploaded_file.state}")

            print(f"[GeminiClient] File đã sẵn sàng. Gemini ({model_name}) đang xem phim & phân tích kịch bản...")

            prompt = f"""
Bạn là Tổng Biên Tập Review Phim kiêm Chuyên gia Hậu kỳ Video chuyên nghiệp của kênh Cool Mèo Phim (@CoolMeoPhim).
Bạn vừa nhận được video bản nháp của tập phim có tên gốc là: '{drama_title}' (Tập {episode_index}/{total_episodes}).
THÔNG SỐ QUAN TRỌNG:
- Video này có tổng thời lượng chính xác là {v_dur:.1f} giây ({v_dur/60:.1f} phút).
- MỌI mốc thời gian trong JSON BẮT BUỘC nằm trong khoảng [0.0, {v_dur:.1f}]. CẤM TUYỆT ĐỐI bịa mốc thời gian vượt quá {v_dur:.1f}s!

HÃY XEM KỸ TỪNG GIÂY CỦA VIDEO, NGHE THOẠI TIẾNG TRUNG VÀ ĐỌC PHỤ ĐỀ TRUNG GỐC ĐỂ THỰC HIỆN CÁC NHIỆM VỤ SAU:

1. DỊCH TIÊU ĐỀ & TIÊU ĐỀ THUMBNAIL:
   - vietnamese_title: Dịch tên phim '{drama_title}' sang Tiếng Việt chuẩn xác, hấp dẫn, đậm chất phim ảnh (ví dụ: 'Song Thù Cung Lan Mộng'). XÓA MỌI HẬU TỐ '- Tập 1'.
   - thumbnail_title: 3 ĐẾN 5 TỪ IN HOA GIẬT GÂN, ĐÚNG CHÍNH TẢ TIẾNG VIỆT 100% (ví dụ: 'HAI CHỊ EM XUYÊN KHÔNG', 'SỰ THẬT KINH HOÀNG'). TUYỆT ĐỐI KHÔNG VIẾT SAI CHÍNH TẢ!
   - TUYỆT ĐỐI 100% TIẾNG VIỆT, KHÔNG CHỨA BẤT KỲ KÝ TỰ CHỮ HÁN NÀO.

2. PHÁT HIỆN ĐOẠN CẦN CẮT BỎ (skip_ranges):
   - Đánh dấu chính xác mốc thời gian [start_sec, end_sec] của:
     a) Opening Credits (đoạn nhạc dạo mở đầu, giới thiệu diễn viên/đạo diễn, thường từ 0s đến 35–45s).
     b) Ending Credits (nhạc kết, danh đề chạy chữ ở cuối tập, thường khoảng 45-60s cuối video từ giây {ending_hint:.1f} đến {v_dur:.1f}). Ghi chính xác mốc này để cắt bỏ hoàn toàn!
     c) MÀN HÌNH CHUYỂN TẬP / TITLE CARDS: Các đoạn màn hình đen hoặc bảng chữ ghi số tập (như '第一集', '第二集', '第1集', 'Tập 1', 'Tập 2'...) xuất hiện ở đầu hoặc giữa phim. BẮT BUỘC đánh dấu chính xác vào skip_ranges với reason 'title_card' để cắt bỏ triệt để!
     d) MỌI ĐOẠN QUẢNG CÁO CHÈN GIỮA PHIM (quảng cáo đồ ăn bim bim, app, web cờ bạc, mã QR hoặc cảnh diễn viên hiện đại lạc quẻ).

3. SỔ NHỚ CỐT TRUYỆN (character_glossary & rolling_summary):
   - Nhận diện đúng các nhân vật chính xuất hiện trong tập phim, chuyển tên tiếng Trung sang Tên Hán Việt chuẩn xác.
   - rolling_summary: Tóm tắt diễn biến thật của tập phim theo góc nhìn NGHIÊM TÚC, kịch tính, hấp dẫn. TUYỆT ĐỐI CẤM dùng từ lóng cợt nhả cho các tình tiết tai nạn, đau thương, cái chết!

4. KỊCH BẢN REVIEW TIẾNG DẪN HÌNH CHI TIẾT:
   {budget_instruction}
   {flow_instruction}
   {ending_instruction}
   - Mỗi phân đoạn review khoảng 35 đến 50 từ tiếng Việt, kể chi tiết diễn biến, lời thoại kịch tính, hành động và cảm xúc nhân vật trong từng phân cảnh cụ thể.
   - BẮT BUỘC KHỚP CẢNH 1-1: Mỗi phân đoạn phải có `source_clip_ranges` là khoảng thời gian [start, end] trong video gốc của chính cảnh đó. CẤM TUYỆT ĐỐI lấy hình ảnh rơi vào đoạn Ending Credits ({ending_hint:.1f}s đến {v_dur:.1f}s) hoặc đoạn Title Cards!
   - Lời đọc 100% bằng Tiếng Việt chuẩn ngữ pháp, xưng hô ngôi thứ ba, KHÔNG chứa chữ Hán, KHÔNG lặp câu vô nghĩa.
   - CHUẨN PHÁT THANH TRUYỀN HÌNH: Dùng câu văn giàu cảm xúc, đúng ngữ pháp. Tránh dùng các từ nhạy cảm dễ bị hệ thống giọng đọc chặn như 'kết thúc cuộc đời', 'đâm nát', 'chém giết' (thay bằng 'qua đời', 'va chạm mạnh', 'hư hỏng nặng', 'nguy cấp').

Trả về duy nhất định dạng JSON chuẩn:
{{
  "vietnamese_title": "Tên tiếng Việt chuẩn",
  "thumbnail_title": "3 ĐẾN 5 TỪ IN HOA GIẬT GÂN",
  "rolling_summary": "Tóm tắt cốt truyện tập này theo văn phong nghiêm túc...",
  "character_glossary": {{
    "Tên_Gốc_Trung": "Tên_Hán_Việt"
  }},
  "skip_ranges": [
    {{"start_sec": 0.0, "end_sec": 35.0, "reason": "opening_credits"}},
    {{"start_sec": {ending_hint:.1f}, "end_sec": {v_dur:.1f}, "reason": "ending_credits"}}
  ],
  "narration_blocks": [
    {{
      "block_index": 1,
      "source_clip_ranges": [{{"start": 35.0, "end": 75.0}}],
      "script": "Lời bình review chi tiết phân đoạn 1..."
    }}
  ]
}}
"""
            candidate_models = [model_name, "gemini-3.1-flash-lite", "gemini-3.5-flash-lite", "gemini-flash-lite-latest", "gemini-3.5-flash", "gemini-3-flash-preview"]
            seen = set()
            models_to_try = [m for m in candidate_models if not (m in seen or seen.add(m))]

            resp = None
            last_err = None
            for m_try in models_to_try:
                for attempt in range(3):
                    try:
                        print(f"[GeminiClient] Gọi model {m_try} xem phim & phân tích (Lần thử #{attempt+1})...")
                        resp = client.models.generate_content(
                            model=m_try,
                            contents=[
                                uploaded_file,
                                prompt
                            ],
                            config=types.GenerateContentConfig(
                                response_mime_type="application/json",
                                temperature=0.4
                            )
                        )
                        if resp and resp.text:
                            break
                    except Exception as e_gen:
                        last_err = e_gen
                        err_str = str(e_gen).lower()
                        print(f"[GeminiClient] Model {m_try} gặp lỗi: {e_gen}")
                        if "503" in err_str or "unavailable" in err_str or "high demand" in err_str:
                            time.sleep(3)
                            continue
                        else:
                            break
                if resp and resp.text:
                    break

            if not resp or not resp.text:
                raise RuntimeError(f"LỖI: Gọi Gemini phân tích video thất bại sau khi thử các model: {last_err}")

            text = resp.text.strip()
            if text.startswith("```json"):
                text = text[7:]
            if text.startswith("```"):
                text = text[3:]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()

            data = json.loads(text)
            if isinstance(data, list):
                if len(data) > 0 and isinstance(data[0], dict):
                    if "vietnamese_title" in data[0] or "narration_blocks" in data[0]:
                        data = data[0]
                    else:
                        data = {
                            "vietnamese_title": drama_title,
                            "thumbnail_title": "SỰ THẬT KINH HOÀNG",
                            "narration_blocks": data
                        }
                else:
                    data = {"narration_blocks": []}

            print(f"[GeminiClient] ✅ Phân tích phim thành công! Tiêu đề Việt: {data.get('vietnamese_title')}")
            print(f"[GeminiClient] Phát hiện {len(data.get('skip_ranges', []))} đoạn quảng cáo/credits cần cắt bỏ.")
            print(f"[GeminiClient] Biên soạn {len(data.get('narration_blocks', []))} khối Tiếng Dẫn Hình.")
            return data

        finally:
            try:
                client.files.delete(name=uploaded_file.name)
                print(f"[GeminiClient] Đã dọn dẹp file tạm trên Gemini: {uploaded_file.name}")
            except Exception:
                pass
            if os.path.exists(draft_video):
                try: os.remove(draft_video)
                except Exception: pass

    @classmethod
    def select_best_thumbnail_frame(cls, candidate_frame_paths: List[str]) -> Tuple[int, str, float]:
        """
        Dùng Gemini Vision để thẩm định 6-8 khung hình ứng viên,
        bắt buộc chọn 1 khung hình có khuôn mặt nhân vật chính rõ nét, biểu cảm kịch tính nhất.
        Đồng thời ước lượng chính xác tọa độ tâm khuôn mặt (face_cx_ratio) để crop ảnh dọc 9:16 chuẩn xác.
        """
        if not candidate_frame_paths:
            raise RuntimeError("Danh sách khung hình ứng viên rỗng!")

        client, model_name = cls.get_client_and_model()

        images = []
        prompt_parts = [
            "Bạn là chuyên gia thiết kế Thumbnail YouTube triệu view cho kênh phim. "
            "Dưới đây là các khung hình được trích xuất từ các cảnh sạch của phim. "
            "Hãy đánh giá từng ảnh và CHỌN DUY NHẤT 1 KHUNG HÌNH TỐT NHẤT theo tiêu chí:\n"
            "1. Có khuôn mặt nhân vật chính rõ nét, ánh mắt và biểu cảm kịch tính (tức giận, đau đớn, kinh ngạc, sắc lẹm).\n"
            "2. Tuyệt đối KHÔNG chọn cảnh chụp lưng, chụp chân, váy, cảnh mờ hoặc cảnh không có người.\n"
            "3. ƯỚC LƯỢNG TỌA ĐỘ TÂM KHUÔN MẶT CHÍNH (theo chiều ngang từ 0.0 mép trái đến 1.0 mép phải, ví dụ 0.5 là chính giữa, 0.7 là lệch sang phải).\n"
            "4. Trả về JSON: {\"selected_index\": <chỉ số từ 0 đến N-1>, \"reason\": \"lý do chọn\", \"face_cx_ratio\": <float từ 0.1 đến 0.9>}\n"
        ]

        for idx, p in enumerate(candidate_frame_paths):
            try:
                img = Image.open(p)
                prompt_parts.append(f"Khung hình chỉ số {idx}:")
                prompt_parts.append(img)
            except Exception as e:
                print(f"[GeminiClient] Lỗi đọc ảnh {p}: {e}")

        candidate_models = [model_name, "gemini-3.1-flash-lite", "gemini-3.5-flash-lite", "gemini-flash-lite-latest", "gemini-3.5-flash", "gemini-3-flash-preview"]
        seen = set()
        models_to_try = [m for m in candidate_models if not (m in seen or seen.add(m))]

        resp = None
        for m_try in models_to_try:
            try:
                resp = client.models.generate_content(
                    model=m_try,
                    contents=prompt_parts,
                    config=types.GenerateContentConfig(response_mime_type="application/json")
                )
                if resp and resp.text:
                    break
            except Exception as e_vis:
                print(f"[GeminiClient] Gemini Vision {m_try} lỗi: {e_vis}")
                time.sleep(2)

        try:
            res_json = json.loads(resp.text.strip())
            sel_idx = int(res_json.get("selected_index", 0))
            face_ratio = float(res_json.get("face_cx_ratio", 0.5))
            face_ratio = max(0.1, min(0.9, face_ratio))
            if 0 <= sel_idx < len(candidate_frame_paths):
                print(f"[GeminiClient] Đã chọn khung hình #{sel_idx} làm Thumbnail (face_cx_ratio={face_ratio:.2f}): {res_json.get('reason')}")
                return sel_idx, candidate_frame_paths[sel_idx], face_ratio
        except Exception:
            pass

        return 0, candidate_frame_paths[0], 0.5
