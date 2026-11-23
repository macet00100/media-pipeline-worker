import os
import sys
sys.path.insert(0, ".")
import time
import subprocess
from config.settings import Settings
from google import genai
from google.genai import types

def test_file_api():
    api_key = Settings.GEMINI_API_KEYS[0]
    client = genai.Client(api_key=api_key)
    
    # Create a 5-second 360p test video with test audio
    test_vid = "test_sample_vid.mp4"
    cmd = [
        "ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=10",
        "-f", "lavfi", "-i", "sine=frequency=1000",
        "-t", "5", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
        test_vid
    ]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    
    try:
        print("Uploading test video to Gemini File API...")
        f = client.files.upload(file=test_vid)
        print("Uploaded file name:", f.name, "state:", f.state)
        
        while f.state == "PROCESSING":
            time.sleep(2)
            f = client.files.get(name=f.name)
            print("File state:", f.state)
            
        print("Generating multimodal content...")
        resp = client.models.generate_content(
            model="gemini-3.5-flash",
            contents=[
                f,
                "Describe this video briefly and return JSON: {\"description\": \"...\", \"has_audio\": true/false}"
            ],
            config=types.GenerateContentConfig(response_mime_type="application/json")
        )
        print("Multimodal response:", resp.text)
        
        # Cleanup
        client.files.delete(name=f.name)
        print("Cleaned up Gemini file.")
    finally:
        if os.path.exists(test_vid):
            os.remove(test_vid)

if __name__ == "__main__":
    test_file_api()
