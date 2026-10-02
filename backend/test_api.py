import os
import json
import urllib.request

try:
    with open('.env', 'r') as f:
        for line in f:
            if '=' in line:
                key, val = line.strip().split('=', 1)
                os.environ[key] = val
except Exception:
    pass

api_key = os.environ.get("DEEPSEEK_API_KEY")
if not api_key:
    print("API-nøkkel mangler!")
    exit(1)

url = "https://api.deepseek.com/chat/completions"
headers = {
    "Content-Type": "application/json",
    "Authorization": f"Bearer {api_key}"
}
data = {
    "model": "deepseek-chat",
    "messages": [
        {"role": "user", "content": "Svar kun med ett ord: Hei!"}
    ]
}

req = urllib.request.Request(url, data=json.dumps(data).encode("utf-8"), headers=headers, method="POST")

try:
    with urllib.request.urlopen(req) as response:
        result = json.loads(response.read().decode("utf-8"))
        print("\n=== SUKSESS! ===")
        print("Svar fra DeepSeek:", result["choices"][0]["message"]["content"])
        print("================\n")
except Exception as e:
    print("\n=== FEIL! ===")
    print("Klarte ikke koble til:", str(e))
    try:
        print(e.read().decode('utf-8'))
    except:
        pass
    print("================\n")
