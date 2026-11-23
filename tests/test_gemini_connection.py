import os
import sys
sys.path.insert(0, ".")
import json
from config.settings import Settings
from google import genai
from google.genai import types

def test_connection():
    api_key = Settings.GEMINI_API_KEYS[0] if Settings.GEMINI_API_KEYS else None
    if not api_key:
        print("NO API KEY")
        return
    client = genai.Client(api_key=api_key)
    
    # List models
    models_list = []
    for m in client.models.list():
        models_list.append(m.name)
    print("Found total models:", len(models_list))
    
    # Find working flash model
    candidates = [
        "gemini-3.5-flash",
        "gemini-3-flash-preview",
        "gemini-3.1-flash-lite",
        "gemini-3.7-flash",
        "gemini-3.8-flash",
        "gemini-flash-latest"
    ]
    working_model = None
    for cand in candidates:
        try:
            resp = client.models.generate_content(
                model=cand,
                contents="Hello, reply with JSON: {\"status\": \"ok\", \"model\": \"" + cand + "\"}",
                config=types.GenerateContentConfig(response_mime_type="application/json")
            )
            print(f"Model {cand} SUCCESS: {resp.text.strip()}")
            working_model = cand
            break
        except Exception as e:
            print(f"Model {cand} failed: {e}")
            
    print("SELECTED WORKING MODEL:", working_model)

if __name__ == "__main__":
    test_connection()
