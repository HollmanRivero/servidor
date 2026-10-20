"""Felles konfigurasjon og miljøvariabler for Super-Server.

Lastes fra backend/.env. Importeres av alle nye moduler (database, tools,
agent, scheduler). Verdiene her er de samme som main.py bruker.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

# Prosjektmappen agenten får lese/skrive i. Samme sti som main.py bruker.
PROJECTS_DIR = (BASE_DIR / "projects").resolve()
PROJECTS_DIR.mkdir(parents=True, exist_ok=True)

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "").strip()
TECH_AI_API_KEY = os.environ.get("TECH_AI_API_KEY", "").strip()
TECH_AI_API_URL = os.environ.get(
    "TECH_AI_API_URL",
    "https://tech-ai.tech/v1/chat/completions",
).strip()
TECH_AI_MODEL = os.environ.get("TECH_AI_MODEL", "tech-ai-monkey").strip()
API_TOKEN = os.environ.get("API_TOKEN", "").strip()

# GitHub-token, slik at agenten kan klone PRIVATE repoer fra serveren.
# Fine-grained PAT med "Contents: Read" paa repoet holder. Ligger kun i .env.
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()

HOST = os.environ.get("HOST", "0.0.0.0").strip()
PORT = int(os.environ.get("PORT", "8000"))

DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-flash").strip()

# Rekkefølge modellene prøves i. "-latest"-aliasene blir ofte 503 (overbelastet),
# så vi faller videre. Overstyr med GEMINI_MODELS i .env.
_DEFAULT_MODELS = "gemini-flash-latest,gemini-3-flash-preview,gemini-flash-lite-latest"
GEMINI_MODELS = [
    m.strip()
    for m in (os.environ.get("GEMINI_MODELS") or os.environ.get("GEMINI_MODEL") or _DEFAULT_MODELS).split(",")
    if m.strip()
]

# Når den daglige agentjobben kjører (HH:MM, lokal tid på serveren).
DAILY_JOB_TIME = os.environ.get("DAILY_JOB_TIME", "08:00").strip()

# Maks antall verktøy-steg i agent-loopen før den gir opp.
MAX_AGENT_STEPS = int(os.environ.get("MAX_AGENT_STEPS", "6"))

GEMINI_SYSTEM = (
    "Du er Gemini, en AI-assistent for Ubuntu-serveren til brukeren. "
    "Svar alltid på Norsk. Vær kort, hyggelig og teknisk presis."
)
DEEPSEEK_SYSTEM = (
    "Du er DeepSeek, en AI-assistent for Ubuntu-serveren. "
    "Svar på Norsk. Vær kort og analytisk."
)

AGENT_SYSTEM = (
    "Du er en praktisk AI-agent som styrer brukerens Ubuntu-server. "
    "Du har verktøy og SKAL bruke dem for å faktisk utføre oppgavene, ikke bare forklare. "
    "Når brukeren ber deg gjøre noe: velg riktig verktøy, utfør handlingen, "
    "og rapporter kort hva du faktisk gjorde og hva resultatet ble. "
    "Lagre viktige fakta og preferanser i minnet med remember-verktøyet. "
    "Bruk create_task/complete_task for å holde oversikt over lengre jobber. "
    "Svar alltid på norsk, kort og konkret."
)
