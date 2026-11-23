import os
import unittest
import logging
from src.security.redactor import SecretRedactingFilter, install_secret_redactor

class TestSecretRedactingFilter(unittest.TestCase):
    def test_redact_gemini_key(self):
        sample_key = "AI" + "zaSyD9876543210abcdefghijklmnopq"
        text = f"Lỗi khi gọi model: Key {sample_key} không hợp lệ"
        redacted = SecretRedactingFilter.redact_text(text)
        self.assertNotIn(sample_key, redacted)
        self.assertIn("[REDACTED]", redacted)

    def test_redact_facebook_token(self):
        sample_token = "EA" + "ABwzLxyz1234567890abcdef"
        text = f"Facebook graph api error with token {sample_token}"
        redacted = SecretRedactingFilter.redact_text(text)
        self.assertNotIn(sample_token, redacted)
        self.assertIn("[REDACTED]", redacted)

    def test_redact_m3u8_url(self):
        text = "Downloading stream from https://video.example.com/live/2026/stream.m3u8?token=xyz"
        redacted = SecretRedactingFilter.redact_text(text)
        self.assertNotIn("stream.m3u8", redacted)
        self.assertIn("[REDACTED]", redacted)

    def test_redact_telegram_bot_token(self):
        text = "Sending to https://api.telegram.org/bot123456789:ABCdefGHIjklMNOpqrSTUvwxYZ123456789/sendMessage"
        redacted = SecretRedactingFilter.redact_text(text)
        self.assertNotIn("123456789:ABCdefGHIjklMNOpqrSTUvwxYZ123456789", redacted)
        self.assertIn("[REDACTED]", redacted)

    def test_redact_env_var(self):
        os.environ["LZ_API_URL"] = "https://secret-quantum-api.internal/api.php"
        try:
            text = "Connecting to https://secret-quantum-api.internal/api.php for movies"
            redacted = SecretRedactingFilter.redact_text(text)
            self.assertNotIn("https://secret-quantum-api.internal/api.php", redacted)
            self.assertIn("[REDACTED]", redacted)
        finally:
            os.environ.pop("LZ_API_URL", None)

if __name__ == "__main__":
    unittest.main()
