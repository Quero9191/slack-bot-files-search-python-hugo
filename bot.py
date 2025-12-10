import os
from pathlib import Path

from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from openai import OpenAI

# 1) Cargar .env desde la misma carpeta que este archivo
env_path = Path(__file__).with_name(".env")
load_dotenv(env_path)

# 2) Leer variables (usando getenv para no petar con KeyError)
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SLACK_APP_TOKEN = os.getenv("SLACK_APP_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

print("DEBUG SLACK_BOT_TOKEN presente?:", SLACK_BOT_TOKEN is not None)
print("DEBUG SLACK_APP_TOKEN presente?:", SLACK_APP_TOKEN is not None)
print("DEBUG OPENAI_API_KEY presente?:", OPENAI_API_KEY is not None)

missing = []
if SLACK_BOT_TOKEN is None:
    missing.append("SLACK_BOT_TOKEN")
if SLACK_APP_TOKEN is None:
    missing.append("SLACK_APP_TOKEN")
if OPENAI_API_KEY is None:
    missing.append("OPENAI_API_KEY")

if missing:
    raise RuntimeError(
        f"Faltan variables en .env: {', '.join(missing)}. "
        f"Revisa que el archivo .env esté en la MISMA carpeta que bot.py "
        f"y que los nombres coincidan."
    )

app = App(token=SLACK_BOT_TOKEN)
client = OpenAI(api_key=OPENAI_API_KEY)

SYSTEM_PROMPT = (
    "You are a helpful assistant. "
    "Always answer in the SAME language the user used (English or Spanish). "
    "Keep answers clear and reasonably short."
)

def ask_llm(text: str) -> str:
    r = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        temperature=0.6,
    )
    return r.choices[0].message.content.strip()

@app.event("message")
def handle_message(event, say):
    if event.get("bot_id") or event.get("subtype"):
        return
    text = (event.get("text") or "").strip()
    if not text:
        return
    say(ask_llm(text))

if __name__ == "__main__":
    SocketModeHandler(app, SLACK_APP_TOKEN).start()
