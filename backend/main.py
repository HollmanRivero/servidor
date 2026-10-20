import ast
import json
import os
import platform
import re
import secrets
import shutil
import signal
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

try:
    import psutil
    import uvicorn
    from dotenv import load_dotenv
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import JSONResponse, Response
    from loguru import logger
    from pydantic import BaseModel
except ModuleNotFoundError as exc:
    raise SystemExit(
        f"\nMangler Python-pakken '{exc.name}'.\n"
        "Du kjorer sannsynligvis system-python (eller sudo) i stedet for prosjektets .venv.\n\n"
        "  Du skal normalt IKKE kjore main.py selv - systemd gjor det:\n"
        "      sudo systemctl status servidor-super\n"
        "      sudo systemctl restart servidor-super\n\n"
        "  Vil du kjore den manuelt, bruk venv-python (aldri sudo):\n"
        "      cd backend && ../.venv/bin/python main.py\n"
    ) from exc

# Last inn backend/.env (GEMINI_API_KEY, DEEPSEEK_API_KEY, TECH_AI_API_KEY, API_TOKEN m.m.)
BASE_DIR = Path(__file__).resolve().parent
REPO_ROOT = BASE_DIR.parent.resolve()
OS_NAME = platform.system()  # "Linux", "Darwin" (macOS) eller "Windows"
OS_DESCRIPTION = f"{OS_NAME} ({platform.release()})"
load_dotenv(BASE_DIR / ".env", override=True)

import auth
import database

database.init_db()
auth.ensure_initial_admin()

PROJECTS_DIR = (BASE_DIR / "projects").resolve()
PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
AGENT_STATE_FILE = BASE_DIR / ".agent_state.json"

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "").strip()
TECH_AI_API_KEY = os.environ.get("TECH_AI_API_KEY", "").strip()
TECH_AI_API_URL = os.environ.get(
    "TECH_AI_API_URL",
    "https://tech-ai.tech/v1/chat/completions",
).strip()
TECH_AI_MODEL = os.environ.get("TECH_AI_MODEL", "tech-ai-monkey").strip()

# Prøves i rekkefølge. Vi unngår utgåtte modeller (som gemini-2.5-flash / gemini-2.0-flash)
# som alltid gir 404.
_DEFAULT_MODELS = "gemini-3.8-flash,gemini-3-flash-preview,gemini-flash-latest,gemini-flash-lite-latest"
_DEPRECATED_MODELS = {"gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash", "gemini-1.5-flash-8b"}
GEMINI_MODELS = [
    m.strip()
    for m in (os.environ.get("GEMINI_MODELS") or os.environ.get("GEMINI_MODEL") or _DEFAULT_MODELS).split(",")
    if m.strip() and m.strip() not in _DEPRECATED_MODELS
]
if not GEMINI_MODELS:
    GEMINI_MODELS = ["gemini-3.8-flash", "gemini-3-flash-preview", "gemini-flash-latest", "gemini-flash-lite-latest"]

# Modellen som svarte sist - prøves først neste gang (men vi låser oss ikke fast til "lite"
# hvis en sterkere modell er tilgjengelig).
_gemini_last_good: str | None = None

# Gratisnivået til Gemini er 20 forespørsler per DAG per modell
# (GenerateRequestsPerDayPerProjectPerModel-FreeTier). En modell som er kvotert ut
# gir 429 i flere timer, så vi setter den på pause i stedet for å prøve den på hver
# forespørsel. Tidsavbrudd og 5xx settes også på pause, ellers betaler hver enkelt
# samtale flere titalls sekunder på en modell som henger.
GEMINI_ATTEMPT_TIMEOUT = float(os.environ.get("GEMINI_ATTEMPT_TIMEOUT", "15"))
GEMINI_TOTAL_BUDGET = float(os.environ.get("GEMINI_TOTAL_BUDGET", "30"))
GEMINI_COOLDOWN_QUOTA = 600.0  # 429 uten oppgitt retryDelay
GEMINI_COOLDOWN_BUSY = 60.0  # 503 overbelastet
GEMINI_COOLDOWN_TIMEOUT = 1800.0  # henger / nettverksfeil
GEMINI_COOLDOWN_BROKEN = 6 * 3600.0  # 400/404 - modellen finnes ikke eller tar ikke imot kallet
GEMINI_COOLDOWN_MAX = 24 * 3600.0

AUTH_ERROR_STATUS = {401, 403}
RETRY_SAME_MODEL_STATUS = {408, 500, 502, 504}

# modell -> (monotonic-tid den kan brukes igjen, årsak)
_gemini_cooldown: dict[str, tuple[float, str]] = {}

DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-flash").strip()

# Personlighetene ligger som konstanter, slik at /chat/gemini, /chat/deepseek, /chat/techai og
# /chat/collaborate oppfører seg likt.
GEMINI_SYSTEM = (
    "Du er Gemini, en AI-assistent for Ubuntu-serveren til brukeren. "
    "Du har full lese- og skrivetilgang til serveren og prosjektfilene under projects/. "
    "Du kan kjøre shell-kommandoer, opprette, endre og lagre filer. "
    "Svar alltid på norsk. Vær kort, hjelpsom og teknisk presis."
)
DEEPSEEK_SYSTEM = (
    "Du er DeepSeek, en AI-assistent for Ubuntu-serveren. "
    "Du har full lese- og skrivetilgang til serveren og prosjektfilene under projects/. "
    "Du kan kjøre shell-kommandoer, opprette, endre og lagre filer. "
    "Svar på norsk. Vær kort, analytisk og hjelpsom."
)
TECH_AI_SYSTEM = (
    "Du er Tech-AI (tech-ai.tech), en selvlærende bygge- og kodeagent for Ubuntu-serveren. "
    "Du hjelper brukeren med å arkitektere, bygge og ferdigstille prosjekter under projects/. "
    "Du kan kjøre shell-kommandoer, opprette, endre og lagre filer. "
    "Svar på norsk. Vær løsningsorientert, konkret og teknisk presis."
)

# Delt hemmelighet. Appen sender den i headeren "X-API-Token".
# Vi støtter både API_TOKEN og ev. SECONDARY_API_TOKEN fra .env.
API_TOKEN = os.environ.get("API_TOKEN", "").strip()
SECONDARY_API_TOKEN = os.environ.get("SECONDARY_API_TOKEN", "").strip()
VALID_TOKENS = {
    t.strip()
    for t in [API_TOKEN, SECONDARY_API_TOKEN, *os.environ.get("API_TOKENS", "").split(",")]
    if t and t.strip()
}

# Hvilket grensesnitt og port serveren lytter på.
_raw_host = os.environ.get("HOST", "0.0.0.0").strip()
if _raw_host.lower().startswith("http://"):
    _raw_host = _raw_host[7:]
elif _raw_host.lower().startswith("https://"):
    _raw_host = _raw_host[8:]
if ":" in _raw_host:
    _raw_host = _raw_host.split(":")[0]
HOST = _raw_host.strip() or "0.0.0.0"
PORT = int(os.environ.get("PORT", "8000"))


def _can_bind(host: str) -> bool:
    """Kan vi lytte på denne adressen akkurat nå?"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, 0))
            return True
        except OSError:
            return False


# HOST=100.x.y.z betyr "lytt kun pa Tailscale". Er Tailscale logget ut eller
# nede, finnes ikke adressen, og uvicorn dorer med EADDRNOTAVAIL. Da starter
# tjenesten pa nytt i det uendelige og serveren er helt nede. Fall derfor
# tilbake til alle grensesnitt i stedet - en tilgjengelig server er bedre.
if not _can_bind(HOST):
    logger.warning(
        "HOST={} finnes ikke pa denne maskinen (Tailscale nede eller feil IP) - lytter pa 0.0.0.0 i stedet",
        HOST,
    )
    HOST = "0.0.0.0"

# Shell- og agent-tilgang.
AGENT_SHELL_ENABLED = os.environ.get("ENABLE_AGENT_SHELL", "1").strip().lower() in {
    "1", "true", "yes", "on",
}
AGENT_SHELL_TIMEOUT = int(os.environ.get("AGENT_SHELL_TIMEOUT", "180"))
MAX_OUTPUT_CHARS = 12000

# Disse kan nås uten token: helsesjekk + innlogging/registrering + feilrapportering fra appen.
OPEN_PATHS = {
    "/",
    "/health",
    "/favicon.ico",
    "/auth/login",
    "/auth/register",
    "/auth/forgot-password",
    "/auth/reset-password",
    "/errors/report",
}

# Tailscale auto-connect konfigurasjon (leses fra backend/.env)
TAILSCALE_AUTH_KEY = os.environ.get("TAILSCALE_AUTH_KEY", "").strip()
AUTO_CONNECT_TAILSCALE = os.environ.get("AUTO_CONNECT_TAILSCALE", "1").strip().lower() in {"1", "true", "yes", "on"}

# Meldinger brukeren skriver MENS agenten jobber. Agenten tommer denne lista i
# starten av hvert steg og vurderer oppgaven pa nytt, slik at en korreksjon
# ("nei, jeg mente...") blir tatt hensyn til i stedet for a bli ignorert.
_agent_inbox: list[str] = []
_agent_inbox_lock = threading.Lock()


def _drain_agent_inbox() -> list[str]:
    with _agent_inbox_lock:
        if not _agent_inbox:
            return []
        items = list(_agent_inbox)
        _agent_inbox.clear()
        return items


# Sanntidsstatus for kjørende agent-oppgave (slik at klienten alltid kan hente status)
_agent_lock = threading.Lock()
_agent_state: dict = {
    "running": False,
    "goal": "",
    "project": "",
    "primary": "",
    "current_step": 0,
    "max_steps": 0,
    "done": False,
    "summary": "",
    "steps": [],
    "started_at": 0.0,
    "updated_at": 0.0,
}


def _save_agent_state() -> None:
    try:
        AGENT_STATE_FILE.write_text(
            json.dumps(_agent_state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        logger.debug("Kunne ikke lagre agent_state til disk: {}", e)


def _connect_tailscale() -> None:
    """Kobler maskinen automatisk til Tailscale i bakgrunnen ved oppstart."""
    if not TAILSCALE_AUTH_KEY:
        logger.warning("Tailscale auto-connect: Ingen TAILSCALE_AUTH_KEY funnet.")
        return

    logger.info("Forsøker å koble til Tailscale...")
    cmd = ["tailscale", "up", f"--auth-key={TAILSCALE_AUTH_KEY}"]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=30)
        logger.info("Tailscale tilkoblet! Output: {}", res.stdout.strip() or "OK")
    except subprocess.CalledProcessError as e:
        err = (e.stderr or e.stdout or "").strip()
        logger.error("Tailscale up feilet (kode {}): {}", e.returncode, err)
        if "access denied" in err.lower() or "permission denied" in err.lower() or "operator" in err.lower():
            logger.warning(
                "Tips: Hvis tilgang ble nektet, gi brukeren operatørtilgang med: sudo tailscale set --operator={}",
                os.environ.get("USER", "bruker"),
            )
    except FileNotFoundError:
        logger.error("Kommandoen 'tailscale' ble ikke funnet i PATH på systemet.")
    except Exception as e:
        logger.error("Uventet feil under tilkobling til Tailscale: {}", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialiserer database og bakgrunnstjenester ved oppstart."""
    database.init_db()
    auth.ensure_initial_admin()
    if AUTO_CONNECT_TAILSCALE:
        threading.Thread(target=_connect_tailscale, daemon=True, name="tailscale-connect").start()
    yield


app = FastAPI(title="Super-Server API", version="2.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _is_valid_token(supplied: str) -> bool:
    if not supplied:
        return False
    for valid in VALID_TOKENS:
        if secrets.compare_digest(supplied.encode("utf-8"), valid.encode("utf-8")):
            return True
    return False


async def _run_and_log_errors(request: Request, call_next, current_user: dict | None, client: str):
    try:
        response = await call_next(request)
        # Bare ekte serverfeil logges som feil. 4xx er normal validering
        # (feil passord, duplikat brukernavn, utløpt kode, ukjent rute) og
        # havner i access_logs i stedet - ellers fylles feilloggen av støy.
        if response.status_code >= 500 and request.url.path not in {"/errors/report"}:
            auth.log_error(
                error_message=f"HTTP {response.status_code} på {request.method} {request.url.path}",
                source="backend",
                endpoint=f"{request.method} {request.url.path}",
                status_code=response.status_code,
                user=current_user,
                ip_address=client,
            )
        return response
    except Exception as exc:
        logger.exception("Uventet feil på {} {}: {}", request.method, request.url.path, exc)
        auth.log_error(
            error_message=str(exc),
            source="backend_exception",
            endpoint=f"{request.method} {request.url.path}",
            status_code=500,
            user=current_user,
            ip_address=client,
        )
        raise


@app.middleware("http")
async def check_token(request: Request, call_next):
    """Adgangskontroll + automatisk SQL-logging av aktivitet og feilmeldinger."""
    client = request.client.host if request.client else "?"
    if request.method == "OPTIONS" or request.url.path in OPEN_PATHS:
        return await _run_and_log_errors(request, call_next, None, client)

    current_user = None

    # 1) Sjekk Bearer JWT (fra /auth/login) så vi kan logge hvilken bruker som gjør kallet
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        token = auth_header[7:].strip()
        if _is_valid_token(token):
            return await _run_and_log_errors(request, call_next, None, client)
        state = auth.check_token(token)
        if state.get("ok"):
            current_user = state.get("user")
            if request.url.path not in {"/stats", "/health"}:
                auth.log_user_activity(request.url.path, current_user, client, allowed=True)
            return await _run_and_log_errors(request, call_next, current_user, client)
        supplied_api = request.headers.get("x-api-token", "").strip()
        if _is_valid_token(supplied_api):
            return await _run_and_log_errors(request, call_next, None, client)
        status = state.get("status", 401)
        # Avvist token er en klientfeil, ikke en serverfeil: den havner i access_logs
        # med blocked_reason, og fyller ikke feilloggen i appen.
        auth.log_user_activity(request.url.path, None, client, allowed=False, blocked_reason=state.get("detail", ""))
        return JSONResponse(status_code=status, content={"detail": state.get("detail", "Ingen tilgang")})

    # 2) Master-token (X-API-Token)
    supplied = request.headers.get("x-api-token", "").strip()
    if _is_valid_token(supplied):
        return await _run_and_log_errors(request, call_next, None, client)

    if not VALID_TOKENS:
        return await _run_and_log_errors(request, call_next, None, client)

    logger.warning("Nektet {} {} fra {} (ugyldig token)", request.method, request.url.path, client)
    auth.log_user_activity(request.url.path, None, client, allowed=False, blocked_reason="Manglende token")
    return JSONResponse(status_code=401, content={"detail": "Token inválido o ausente"})


if not API_TOKEN:
    logger.warning(
        "API_TOKEN mangler i backend/.env - serveren er AAPEN for alle som naar {}:{}. "
        "Lag en token med: openssl rand -hex 32",
        HOST, PORT,
    )
else:
    logger.info("Token-beskyttelse aktiv ({} gyldige tokens konfigurert).", len(VALID_TOKENS))


def _display_path(target: Path) -> str:
    try:
        if target == PROJECTS_DIR:
            return ""
        if PROJECTS_DIR in target.parents:
            return target.relative_to(PROJECTS_DIR).as_posix()
        if target == REPO_ROOT:
            return "servidor"
        if REPO_ROOT in target.parents:
            return f"servidor/{target.relative_to(REPO_ROOT).as_posix()}"
    except Exception:
        pass
    return target.as_posix()


def safe_path(relative: str) -> Path:
    """Løser opp en sti på serveren (støtter servidor-prosjektet, projects/, ~ og absolutte stier på Linux/Mac/Windows)."""
    cleaned = (relative or "").strip()
    if not cleaned or cleaned == ".":
        return PROJECTS_DIR

    # Støtt ~ (hjemmemappe på Linux, macOS og Windows)
    if cleaned == "~" or cleaned.startswith("~/") or cleaned.startswith("~\\"):
        return Path(os.path.expanduser(cleaned)).resolve()

    # Støtt "servidor" direkte (dette repoet: REPO_ROOT)
    norm = cleaned.replace("\\", "/").strip("/")
    if norm == "servidor":
        return REPO_ROOT
    if norm.startswith("servidor/"):
        sub = norm[len("servidor/"):]
        return (REPO_ROOT / sub).resolve()

    # Støtt absolutte stier på Linux (/...), macOS (/Users/...) og Windows (C:\...)
    p = Path(cleaned)
    if p.is_absolute():
        return p.resolve()

    # Sjekk om det er et søsterprosjekt i MyGits (f.eks. tech-ai-v1.0.0)
    for parent_candidate in (REPO_ROOT.parent, REPO_ROOT.parent.parent):
        sibling = (parent_candidate / cleaned).resolve()
        if sibling.exists() and sibling != REPO_ROOT.parent:
            return sibling

    return (PROJECTS_DIR / cleaned).resolve()


def _resolve_agent_workdir(project_str: str, fallback_project: str = "") -> tuple[Path, str, str | None]:
    """Løser opp arbeidsmappen for agenten (støtter 'servidor', absolutte stier, '~' og prosjekter i projects/)."""
    warn: str | None = None
    raw = (project_str or "").strip()
    norm = raw.replace("\\", "/").strip("/")
    for prefix in ("servidor/backend/projects/", "backend/projects/", "projects/", "./"):
        if norm.startswith(prefix):
            norm = norm[len(prefix):].strip("/")

    if not norm:
        norm = fallback_project

    try:
        workdir = safe_path(norm if norm else fallback_project)
        rel = _display_path(workdir)
        return workdir, rel, warn
    except Exception as exc:
        warn = f"Merk: Kunne ikke åpne '{project_str}' ({exc}), bruker '{fallback_project or '.'}'."
        workdir = safe_path(fallback_project)
        return workdir, _display_path(workdir), warn


def _resolve_target_in_workdir(workdir: Path, target_str: str) -> Path:
    """Løser opp en fil- eller mappesti fra agenten (relativt til workdir, eller absolutt / ~ / servidor)."""
    cleaned = (target_str or "").strip()
    if not cleaned or cleaned == ".":
        return workdir
    if cleaned == "~" or cleaned.startswith("~/") or cleaned.startswith("~\\"):
        return Path(os.path.expanduser(cleaned)).resolve()
    p = Path(cleaned)
    if p.is_absolute():
        return p.resolve()
    norm = cleaned.replace("\\", "/").strip("/")
    if norm == workdir.name and not (workdir / norm).exists():
        return workdir
    for prefix in ("servidor/backend/projects/", "backend/projects/", "projects/"):
        if norm.startswith(prefix):
            return (PROJECTS_DIR / norm[len(prefix):]).resolve()
    if norm == "servidor" and workdir != REPO_ROOT:
        return REPO_ROOT
    if norm.startswith("servidor/") and workdir != REPO_ROOT:
        return (REPO_ROOT / norm[len("servidor/"):]).resolve()
    return (workdir / cleaned).resolve()


def _sanitize_agent_command(command: str) -> tuple[str, str | None]:
    """Forhindrer at kommandoer henger på interaktiv sudo-passordprompt på Linux/macOS."""
    cmd = command.strip()
    note = None
    if os.name != "nt" and re.search(r"(^|[;&|]\s*)sudo\s+(?!-n\b)", cmd):
        cmd = re.sub(r"(^|[;&|]\s*)sudo\s+", r"\1sudo -n ", cmd)
        note = (
            "Merk: Hvis sudo krever passord, kjør uten sudo eller bruk passordfri sudo."
        )
    return cmd, note


def _run_command(command: str, workdir: Path, timeout: int) -> dict:
    """Kjører en shell-kommando på tvers av Ubuntu Linux, macOS og Windows."""
    timeout = max(5, min(int(timeout), AGENT_SHELL_TIMEOUT))
    started = time.monotonic()

    cmd, sudo_note = _sanitize_agent_command(command)

    env = os.environ.copy()
    env.update({
        "DEBIAN_FRONTEND": "noninteractive",
        "CI": "true",
        "GIT_TERMINAL_PROMPT": "0",
        "npm_config_yes": "true",
        "PIP_NO_INPUT": "1",
        "PYTHONUNBUFFERED": "1",
        "PAGER": "cat",
    })

    actual_cwd = workdir if workdir.exists() else (workdir.parent if workdir.parent.exists() else PROJECTS_DIR)

    popen_kwargs: dict = {
        "shell": True,
        "cwd": str(actual_cwd),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "env": env,
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        popen_kwargs["start_new_session"] = True

    proc = subprocess.Popen(cmd, **popen_kwargs)
    timed_out = False
    try:
        out, err = proc.communicate(timeout=timeout)
        code = proc.returncode
    except subprocess.TimeoutExpired:
        timed_out = True
        if os.name == "nt":
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True,
                    timeout=5,
                )
            except Exception:
                proc.kill()
        else:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, AttributeError):
                proc.kill()
        out, err = proc.communicate()
        code = 124

    if sudo_note and code != 0:
        err = f"{(err or '').rstrip()}\n{sudo_note}".strip()

    return {
        "exit_code": code,
        "stdout": (out or "")[:MAX_OUTPUT_CHARS],
        "stderr": (err or "")[:MAX_OUTPUT_CHARS],
        "duration_ms": int((time.monotonic() - started) * 1000),
        "timed_out": timed_out,
        "cwd": _display_path(workdir),
        "os": OS_NAME,
    }


def _require_agent_shell() -> None:
    if not AGENT_SHELL_ENABLED:
        raise HTTPException(
            status_code=403,
            detail="Shell-tilgang er av. Sett ENABLE_AGENT_SHELL=1 i backend/.env og restart tjenesten.",
        )


@app.get("/")
def read_root():
    return {
        "message": "Super-Server API v2.0 funcionando correctamente.",
        "version": "2.0",
        "os": OS_DESCRIPTION,
    }


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    """Nettleseren ber alltid om denne. Uten ruten ble hvert sidevisning en
    falsk 401 i feilloggen."""
    return Response(status_code=204)


@app.get("/health")
def health():
    """Åpne http://<server-ip>:8000/health i nettleseren for å se status."""
    return {
        "status": "ok",
        "version": "2.0",
        "os": OS_DESCRIPTION,
        "database": database.describe(),
        "listen": f"{HOST}:{PORT}",
        "auth_required": bool(VALID_TOKENS),
        "projects_dir": str(PROJECTS_DIR),
        "repo_root": str(REPO_ROOT),
        "projects_dir_exists": PROJECTS_DIR.is_dir(),
        "gemini_models": GEMINI_MODELS,
        "gemini_model_last_ok": _gemini_last_good,
        "gemini_model_cooldowns": {
            model: int(_gemini_cooldown_remaining(model))
            for model in GEMINI_MODELS
            if _gemini_cooldown_remaining(model) > 0
        },
        "gemini_key_configured": bool(GEMINI_API_KEY),
        "deepseek_key_configured": bool(DEEPSEEK_API_KEY),
        "techai_key_configured": bool(TECH_AI_API_KEY),
        "ai_providers": [p.key for p in AGENT_PROVIDERS.values() if p.configured],
        "ai_providers_paused": {
            key: int(_PROVIDER_COOLDOWN[key] - time.monotonic())
            for key in AGENT_PROVIDERS
            if not _provider_available(key)
        },
        "agent_shell_enabled": AGENT_SHELL_ENABLED,
        "tailscale_configured": bool(TAILSCALE_AUTH_KEY),
        "agent_running": _agent_state.get("running", False),
    }


@app.get("/stats")
def get_system_stats():
    memory_info = psutil.virtual_memory()
    disk_root = os.path.abspath(os.sep)
    disk_info = psutil.disk_usage(disk_root)

    temp_c = 0.0
    if hasattr(psutil, "sensors_temperatures"):
        try:
            temps = psutil.sensors_temperatures()
            if temps:
                if "coretemp" in temps and temps["coretemp"]:
                    temp_c = temps["coretemp"][0].current
                else:
                    first_key = list(temps.keys())[0]
                    if temps[first_key]:
                        temp_c = temps[first_key][0].current
        except Exception:
            temp_c = 0.0

    return {
        "cpu_percent": psutil.cpu_percent(interval=0.1),
        "ram_percent": memory_info.percent,
        "ram_used_mb": memory_info.used // (1024 * 1024),
        "ram_total_mb": memory_info.total // (1024 * 1024),
        "disk_used_gb": disk_info.used // (1024**3),
        "disk_total_gb": disk_info.total // (1024**3),
        "disk_percent": disk_info.percent,
        "temp_c": temp_c,
    }


@app.get("/projects")
def list_projects(path: str = ""):
    target_path = safe_path(path)
    if not target_path.exists():
        raise HTTPException(status_code=404, detail="Ruta no encontrada")
    if not target_path.is_dir():
        raise HTTPException(status_code=400, detail="La ruta no es un directorio")

    items = []
    seen_names: set[str] = set()

    # Når man er på rot-nivå (path == ""), vis alltid selve 'servidor'-prosjektet øverst
    if not path.strip():
        items.append({
            "name": "servidor",
            "is_dir": True,
            "path": "servidor",
        })
        seen_names.add("servidor")

    for item in sorted(target_path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        if item.name.startswith(".agent_") or item.name in {".git", "__pycache__", "node_modules"}:
            continue
        if not path.strip() and item.name in seen_names:
            continue
        rel_path = item.name if not path else f"{path.rstrip('/')}/{item.name}"
        items.append({
            "name": item.name,
            "is_dir": item.is_dir(),
            "path": rel_path,
        })
    return {"items": items}


@app.get("/recent_changes")
def recent_changes():
    """Finner de filene som er endret sist i projects-mappen."""
    files_with_times = []
    for root, _dirs, files in os.walk(PROJECTS_DIR):
        for file in files:
            if file.startswith(".agent_"):
                continue
            file_path = Path(root) / file
            try:
                mtime = file_path.stat().st_mtime
            except OSError:
                continue
            files_with_times.append({
                "path": file_path.relative_to(PROJECTS_DIR).as_posix(),
                "mtime": mtime,
            })

    files_with_times.sort(key=lambda f: f["mtime"], reverse=True)
    return {"recent": files_with_times[:5]}


@app.get("/file")
def read_file(path: str):
    target_path = safe_path(path)
    if not target_path.is_file():
        raise HTTPException(status_code=404, detail="Archivo no encontrado")
    try:
        content = target_path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        raise HTTPException(status_code=400, detail="El archivo no se pudo leer") from e
    return {"content": content}


class SaveFileRequest(BaseModel):
    content: str = ""


@app.post("/file")
def save_file(path: str, data: SaveFileRequest):
    target_path = safe_path(path)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(data.content, encoding="utf-8")
    logger.info("Lagret {}", target_path)
    return {"message": "Guardado"}


# ---------------------------------------------------------------------------
# Prosjekter og shell
# ---------------------------------------------------------------------------


class ShellRequest(BaseModel):
    command: str = ""
    cwd: str = ""
    timeout: int = 120


@app.post("/shell")
def run_shell(body: ShellRequest):
    """Kjører en shell-kommando i valgt mappe på serveren (Linux, macOS og Windows)."""
    _require_agent_shell()
    command = body.command.strip()
    if not command:
        raise HTTPException(status_code=400, detail="Tom kommando")

    workdir, where, _warn = _resolve_agent_workdir(body.cwd, fallback_project="")
    workdir.mkdir(parents=True, exist_ok=True)
    if not workdir.is_dir():
        raise HTTPException(status_code=400, detail="cwd er ikke en mappe")

    logger.info("Shell i {}: {}", where or ".", command[:200])
    return {"command": command, **_run_command(command, workdir, body.timeout)}


class CreateProjectRequest(BaseModel):
    name: str = ""
    git: bool = True
    readme: bool = True


@app.post("/projects/create")
def create_project(body: CreateProjectRequest):
    """Lager en ny prosjektmappe under backend/projects/."""
    name = body.name.strip().strip("/")
    if not name:
        raise HTTPException(status_code=400, detail="Prosjektnavn mangler")

    target = safe_path(name)
    if target == PROJECTS_DIR:
        raise HTTPException(status_code=400, detail="Ugyldig prosjektnavn")
    if target.exists():
        raise HTTPException(status_code=409, detail="Prosjektet finnes allerede")

    target.mkdir(parents=True)
    files = []
    if body.readme:
        (target / "README.md").write_text(f"# {name}\n", encoding="utf-8")
        files.append("README.md")
    if body.git:
        _run_command("git init -q && git add -A && git commit -qm init || true", target, 60)

    rel = target.relative_to(PROJECTS_DIR).as_posix()
    logger.info("Opprettet prosjekt {}", rel)
    return {"name": name, "path": rel, "files": files}


class ChatRequest(BaseModel):
    message: str = ""


def _post_json(url: str, payload: dict, headers: dict | None = None, timeout: int = 40) -> dict:
    """Sender JSON med POST og returnerer svaret som dict."""
    request_obj = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    with urllib.request.urlopen(request_obj, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _error_detail(exc: Exception) -> str:
    """Henter feiltekst fra et HTTPError-svar uten å eksponere API-nøkkelen i URL-en."""
    if isinstance(exc, urllib.error.HTTPError):
        return auth.redact_secrets(f"HTTP {exc.code}: {_http_body(exc)[:300]}")
    return auth.redact_secrets(str(exc))


def _http_body(exc: urllib.error.HTTPError) -> str:
    """Leser feilresponsen. Kan bare kalles én gang per HTTPError."""
    try:
        return exc.read().decode("utf-8", errors="replace")
    except Exception:
        return ""


def _parse_retry_delay(body: str, default: float) -> float:
    """Henter RetryInfo.retryDelay (f.eks. "7710s") fra en Google-feilrespons.
    Ved kvotetomming oppgir API-et selv hvor lenge modellen er ubrukelig."""
    try:
        details = json.loads(body).get("error", {}).get("details") or []
    except Exception:
        return default
    for item in details:
        if not isinstance(item, dict) or not str(item.get("@type", "")).endswith("RetryInfo"):
            continue
        try:
            return min(GEMINI_COOLDOWN_MAX, max(1.0, float(str(item.get("retryDelay", "")).rstrip("s"))))
        except ValueError:
            return default
    return default


def _gemini_cooldown_remaining(model: str) -> float:
    """Sekunder til modellen kan prøves igjen. 0 = klar nå."""
    entry = _gemini_cooldown.get(model)
    if not entry:
        return 0.0
    remaining = entry[0] - time.monotonic()
    if remaining <= 0:
        _gemini_cooldown.pop(model, None)
        return 0.0
    return remaining


def _set_gemini_cooldown(model: str, seconds: float, reason: str) -> None:
    seconds = min(GEMINI_COOLDOWN_MAX, max(1.0, seconds))
    _gemini_cooldown[model] = (time.monotonic() + seconds, reason)
    logger.warning("Gemini-modellen {} settes på pause i {}s ({})", model, int(seconds), reason)


def _gemini_model_order() -> list[str]:
    """Foretrekker den modellen som virket sist, men unngår å låse seg fast til 'lite'
    slik at de sterkere modellene alltid prøves først."""
    if _gemini_last_good and "lite" not in _gemini_last_good:
        return [_gemini_last_good] + [m for m in GEMINI_MODELS if m != _gemini_last_good]
    return list(GEMINI_MODELS)


def _ask_gemini(prompt: str, json_mode: bool = False) -> tuple[str, str]:
    """Sender prompt til Gemini med modell-fallback. Returnerer (svar, modell)."""
    global _gemini_last_good

    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY mangler i backend/.env")

    gen_config: dict = {
        "temperature": 0.2 if json_mode else 0.5,
        "maxOutputTokens": 8192,
    }
    if json_mode:
        gen_config["responseMimeType"] = "application/json"

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": gen_config,
    }
    problems: list[str] = []
    paused: list[str] = []
    deadline = time.monotonic() + GEMINI_TOTAL_BUDGET

    for model in _gemini_model_order():
        wait = _gemini_cooldown_remaining(model)
        if wait > 0:
            paused.append(f"{model} (pause {int(wait)}s)")
            continue

        remaining = deadline - time.monotonic()
        if remaining <= 2:
            problems.append("tidsbudsjettet for Gemini er brukt opp")
            break

        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}"
            f":generateContent?key={GEMINI_API_KEY}"
        )
        payload_to_send = payload
        result = None
        for attempt in range(2):
            try:
                result = _post_json(
                    url,
                    payload_to_send,
                    timeout=min(GEMINI_ATTEMPT_TIMEOUT, max(3.0, deadline - time.monotonic())),
                )
                break
            except urllib.error.HTTPError as e:
                body = _http_body(e)
                detail = f"HTTP {e.code}: {body[:300]}"
                problems.append(f"{model}: {detail}")
                if e.code in AUTH_ERROR_STATUS:
                    raise RuntimeError(
                        f"Gemini avviser API-nøkkelen (HTTP {e.code}). "
                        f"Sjekk GEMINI_API_KEY i backend/.env. {detail}"
                    ) from e
                # Hvis en modell ikke støtter responseMimeType, prøv samme modell uten
                if e.code == 400 and json_mode and "responseMimeType" in payload_to_send.get("generationConfig", {}):
                    payload_to_send = {
                        "contents": payload["contents"],
                        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 8192},
                    }
                    continue
                if e.code == 429:
                    _set_gemini_cooldown(
                        model, _parse_retry_delay(body, GEMINI_COOLDOWN_QUOTA), "kvote brukt opp"
                    )
                elif e.code == 503:
                    _set_gemini_cooldown(model, GEMINI_COOLDOWN_BUSY, "overbelastet")
                elif e.code in (400, 404):
                    _set_gemini_cooldown(model, GEMINI_COOLDOWN_BROKEN, f"HTTP {e.code}")
                elif e.code in RETRY_SAME_MODEL_STATUS and attempt == 0:
                    logger.warning("Gemini-modellen {} svarte {} - prøver igjen om 0.8s", model, e.code)
                    time.sleep(0.8)
                    continue
                break
            except Exception as e:
                problems.append(f"{model}: {_error_detail(e)}")
                _set_gemini_cooldown(model, GEMINI_COOLDOWN_TIMEOUT, "tidsavbrudd/tilkobling")
                break

        if not result:
            continue

        candidates = result.get("candidates") or []
        if not candidates:
            problems.append(f"{model}: tomt svar (blokkert av sikkerhetsfilter?)")
            continue
        try:
            parts = candidates[0]["content"]["parts"]
            text_chunks = [p["text"] for p in parts if isinstance(p, dict) and "text" in p]
            text = "\n".join(text_chunks).strip()
            if not text:
                raise KeyError("Ingen tekst i parts")
        except (KeyError, IndexError, TypeError):
            problems.append(f"{model}: uventet svarstruktur")
            continue

        if model != _gemini_last_good:
            logger.info("Gemini svarte med modell {}", model)
        _gemini_last_good = model
        return text, model

    summary = " | ".join(problems[:3])
    if paused:
        summary = (summary + " | " if summary else "") + "på pause: " + ", ".join(paused)
    raise RuntimeError("Ingen Gemini-modell svarte. " + summary)


def _ask_deepseek(prompt: str, json_mode: bool = False) -> tuple[str, str]:
    """Sender prompt til DeepSeek. Returnerer (svar, modell)."""
    if not DEEPSEEK_API_KEY:
        raise RuntimeError("DEEPSEEK_API_KEY mangler i backend/.env")

    url = "https://api.deepseek.com/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
    }
    sys_msg = (
        "Du er en autonom kodeagent på en Ubuntu-server. Svar kun med gyldig JSON."
        if json_mode
        else DEEPSEEK_SYSTEM
    )
    payload: dict = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": sys_msg},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 4096,
        "temperature": 0.2 if json_mode else 0.5,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    last_err: Exception | None = None
    for attempt in range(2):
        try:
            result = _post_json(url, payload, headers, timeout=45)
            choice = result["choices"][0]
            content = (choice.get("message", {}).get("content") or "").strip()
            if content:
                return content, DEEPSEEK_MODEL
            # deepseek-flash er en resonnerende modell: skjulte resonnement-tokens
            # teller mot max_tokens, så et lavt budsjett gir tomt svar.
            last_err = RuntimeError(
                f"DeepSeek ga tomt svar (finish_reason={choice.get('finish_reason')})"
            )
            payload["max_tokens"] = min(int(payload.get("max_tokens", 4096)) * 2, 16384)
            logger.warning(
                "DeepSeek svarte tomt ({}), prøver igjen med max_tokens={}",
                choice.get("finish_reason"),
                payload["max_tokens"],
            )
        except Exception as e:
            last_err = e
            if json_mode and "response_format" in payload:
                payload.pop("response_format", None)
            if attempt == 0:
                logger.warning("DeepSeek feilet på forsøk 1 ({}), prøver igjen...", _error_detail(e))
                time.sleep(1.0)
    raise RuntimeError(f"DeepSeek feilet: {_error_detail(last_err) if last_err else 'ukjent feil'}")


def _ask_techai(prompt: str, json_mode: bool = False) -> tuple[str, str]:
    """Sender prompt til Tech-AI (tech-ai.tech). Støtter både OpenAI-kompatibelt
    endepunkt og enkelt JSON-format ({message/prompt} -> {reply/response})."""
    if not TECH_AI_API_KEY and not os.environ.get("TECH_AI_API_URL"):
        raise RuntimeError("TECH_AI_API_KEY / TECH_AI_API_URL mangler i backend/.env")

    # tech-ai.tech ligger bak Cloudflare. Uten en nettleserlik User-Agent
    # svarer Cloudflare "error code: 1010" (403) for alle POST-kall, sa
    # Tech-AI virket aldri fra serveren. Authorization: Bearer er headeren
    # API-et faktisk godtar (X-API-Key gir 401 Invalid API key).
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
    }
    if TECH_AI_API_KEY:
        headers["Authorization"] = f"Bearer {TECH_AI_API_KEY}"
        headers["X-API-Key"] = TECH_AI_API_KEY
        headers["x-api-key"] = TECH_AI_API_KEY

    sys_msg = (
        "Du er Tech-AI kodeagent på en Ubuntu-server. Svar kun med gyldig JSON, uten markdown."
        if json_mode
        else TECH_AI_SYSTEM
    )
    # Tech-AI (tech-ai-monkey) er en agent. System-rollen, response_format,
    # max_tokens og de gamle message/prompt-feltene gjør at API-et henger.
    # Én user-melding med en enkel OpenAI-kompatibel payload svarer på noen sekunder.
    payload: dict = {
        "model": TECH_AI_MODEL,
        "messages": [
            {"role": "user", "content": f"{sys_msg}\n\nBruker: {prompt}\nTech-AI:"},
        ],
        "stream": False,
    }

    # api.tech-ai.tech finnes ikke (DNS), så den er fjernet. Rekkefolgen under
    # er de url-ene som faktisk svarer på tech-ai.tech.
    candidate_urls = [
        TECH_AI_API_URL,
        "https://tech-ai.tech/v1/chat/completions",
        "https://tech-ai.tech/api/chat",
    ]
    seen_urls: list[str] = []
    for u in candidate_urls:
        if u and u not in seen_urls:
            seen_urls.append(u)

    problems: list[str] = []
    for url in seen_urls:
        try:
            result = _post_json(url, payload, headers, timeout=90)
            if "choices" in result and result["choices"]:
                msg = result["choices"][0].get("message") or {}
                text = msg.get("content") or result["choices"][0].get("text") or ""
                if text:
                    return str(text), TECH_AI_MODEL
            for key in ("reply", "response", "text", "answer", "content", "output"):
                if key in result and isinstance(result[key], str) and result[key].strip():
                    return result[key].strip(), TECH_AI_MODEL
        except Exception as e:
            # Ta vare pa alle feilene: den siste var ofte en DNS-feil fra en
            # dod kandidat-URL og skjulte den ekte arsaken (f.eks. HTTP 502).
            problems.append(f"{url.split('//')[-1]}: {_error_detail(e)}")
            continue

    _mark_provider_down("techai", problems[0] if problems else "ukjent feil")
    raise RuntimeError("Tech-AI svarte ikke. " + " | ".join(problems[:3]))


@app.post("/chat/gemini")
def chat_gemini(body: ChatRequest):
    user_message = body.message.strip()
    if not user_message:
        return {"reply": "Hei! Jeg er Gemini. Hva kan jeg hjelpe deg med?"}
    # Hvis serveren er satt opp med Tech-AI i stedet for Gemini (f.eks. hos en venn),
    # rutes /chat/gemini sømløst til Tech-AI!
    if not GEMINI_API_KEY and TECH_AI_API_KEY:
        return chat_techai(body)
    if not GEMINI_API_KEY:
        auth.log_error("GEMINI_API_KEY mangler i backend/.env", source="ai_gemini", endpoint="POST /chat/gemini")
        return {"reply": "Gemini Feil: GEMINI_API_KEY mangler i backend/.env"}

    try:
        text, _model = _ask_gemini(f"{GEMINI_SYSTEM}\n\nBruker: {user_message}\nGemini:")
        return {"reply": text}
    except Exception as e:
        logger.error("Gemini feilet: {}", e)
        auth.log_error(str(e), source="ai_gemini", endpoint="POST /chat/gemini")
        if DEEPSEEK_API_KEY:
            try:
                text, _model = _ask_deepseek(user_message)
                return {"reply": f"(Svar via DeepSeek-fallback fordi Gemini var opptatt)\n\n{text}"}
            except Exception:
                pass
        return {"reply": f"Gemini Feil: {e}"}


@app.post("/chat/techai")
def chat_techai(body: ChatRequest):
    user_message = body.message.strip()
    if not user_message:
        return {"reply": "Hei! Jeg er Tech-AI. Hva skal vi bygge i dag?"}

    # Er Tech-AI kjent nede (502/kvote), hopp over det direkte kallet og gå rett
    # på fallback. Ellers venter hver forespørsel på en tjeneste som er nede.
    if (TECH_AI_API_KEY or os.environ.get("TECH_AI_API_URL")) and _provider_available("techai"):
        try:
            text, _model = _ask_techai(user_message)
            return {"reply": text}
        except Exception as e:
            logger.warning("Tech-AI direkte kall feilet ({}), bruker fallback-motor...", _error_detail(e))

    try:
        if DEEPSEEK_API_KEY:
            text, _model = _ask_deepseek(f"{TECH_AI_SYSTEM}\n\nBruker: {user_message}")
            return {"reply": f"(Svar via DeepSeek-fallback fordi Tech-AI var utilgjengelig)\n\n{text}"}
        if GEMINI_API_KEY:
            text, _model = _ask_gemini(f"{TECH_AI_SYSTEM}\n\nBruker: {user_message}\nTech-AI:")
            return {"reply": text}
        auth.log_error("TECH_AI_API_KEY mangler i backend/.env", source="ai_techai", endpoint="POST /chat/techai")
        return {"reply": "Tech-AI Feil: Legg inn TECH_AI_API_KEY i backend/.env"}
    except Exception as e:
        logger.error("Tech-AI feilet: {}", e)
        auth.log_error(_error_detail(e), source="ai_techai", endpoint="POST /chat/techai")
        return {"reply": f"Tech-AI Feil: {_error_detail(e)}"}


@app.post("/chat/deepseek")
def chat_deepseek(body: ChatRequest):
    user_message = body.message.strip()
    if not user_message:
        return {"reply": "Hei! Jeg er DeepSeek. Hva kan jeg hjelpe deg med?"}
    if not DEEPSEEK_API_KEY:
        auth.log_error("DEEPSEEK_API_KEY mangler i backend/.env", source="ai_deepseek", endpoint="POST /chat/deepseek")
        return {"reply": "DeepSeek Feil: DEEPSEEK_API_KEY mangler i backend/.env"}

    try:
        text, _model = _ask_deepseek(user_message)
        return {"reply": text}
    except Exception as e:
        logger.error("DeepSeek feilet: {}", e)
        auth.log_error(_error_detail(e), source="ai_deepseek", endpoint="POST /chat/deepseek")
        if GEMINI_API_KEY:
            try:
                text, _model = _ask_gemini(f"{GEMINI_SYSTEM}\n\nBruker: {user_message}\nGemini:")
                return {"reply": f"(Svar via Gemini-fallback fordi DeepSeek var opptatt)\n\n{text}"}
            except Exception:
                pass
        return {"reply": f"DeepSeek Feil: {_error_detail(e)}"}


@app.get("/chat/providers")
def chat_providers():
    """Lister alle AI-agentene som er registrert i AGENT_PROVIDERS.
    Appen kan bruke denne til å bygge fanene sine selv."""
    return {
        "providers": [provider.describe() for provider in AGENT_PROVIDERS.values()],
        "configured": [p.key for p in AGENT_PROVIDERS.values() if p.configured],
    }


@app.post("/chat/provider/{key}")
def chat_provider(key: str, body: ChatRequest):
    """Generisk inngang for alle agenter i AGENT_PROVIDERS - også de du legger til
    selv. /chat/gemini, /chat/deepseek og /chat/techai finnes fortsatt uendret."""
    provider = AGENT_PROVIDERS.get((key or "").strip().lower())
    if not provider:
        raise HTTPException(
            status_code=404,
            detail=f"Ukjent AI-agent '{key}'. Se GET /chat/providers.",
        )

    user_message = body.message.strip()
    if not user_message:
        return {"reply": f"Hei! Jeg er {provider.label}. Hva kan jeg hjelpe deg med?", "provider": provider.key}
    if not provider.configured:
        auth.log_error(
            f"{provider.env_var} mangler i backend/.env",
            source=f"ai_{provider.key}",
            endpoint=f"POST /chat/provider/{provider.key}",
        )
        return {
            "reply": f"{provider.label} Feil: {provider.env_var} mangler i backend/.env",
            "provider": provider.key,
        }

    try:
        text, model = _ask_agent(provider.key, user_message)
        return {"reply": text, "provider": provider.key, "model": model}
    except Exception as e:
        logger.error("{} feilet: {}", provider.label, e)
        auth.log_error(
            _error_detail(e),
            source=f"ai_{provider.key}",
            endpoint=f"POST /chat/provider/{provider.key}",
        )
        return {"reply": f"{provider.label} Feil: {_error_detail(e)}", "provider": provider.key}


# ---------------------------------------------------------------------------
# Samarbeid mellom de to agentene
# ---------------------------------------------------------------------------


class CollaborateRequest(BaseModel):
    message: str = ""
    primary: str = "gemini"  # "gemini", "deepseek" eller "techai"


@dataclass(frozen=True)
class AgentProvider:
    """Én AI-agent tilgjengelig i /chat.

    For å legge til en ny agent: skriv en `_ask_dinagent(prompt, json_mode)` som
    returnerer `(tekst, modellnavn)`, og legg inn en oppføring i AGENT_PROVIDERS.
    Da får den /chat/provider/<key>, oppføring i /chat/providers og fallback
    automatisk. Se docs/README.md.
    """

    key: str
    label: str
    env_var: str
    api_key: str
    system_prompt: str
    ask: Callable[[str, bool], tuple[str, str]]
    aliases: tuple[str, ...]
    fallbacks: tuple[str, ...]

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def describe(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "env_var": self.env_var,
            "configured": self.configured,
            "aliases": list(self.aliases),
            "fallbacks": list(self.fallbacks),
        }


# Rekkefølgen her bestemmer rekkefølgen i /chat/providers. `fallbacks` er
# rekkefølgen agenten prøver hvis den selv feiler (kvote, overbelastning, nede).
AGENT_PROVIDERS: dict[str, AgentProvider] = {
    provider.key: provider
    for provider in (
        AgentProvider(
            key="gemini",
            label="Gemini",
            env_var="GEMINI_API_KEY",
            api_key=GEMINI_API_KEY,
            system_prompt=GEMINI_SYSTEM,
            ask=_ask_gemini,
            aliases=("gemini", "gemi"),
            fallbacks=("techai", "deepseek"),
        ),
        AgentProvider(
            key="deepseek",
            label="DeepSeek",
            env_var="DEEPSEEK_API_KEY",
            api_key=DEEPSEEK_API_KEY,
            system_prompt=DEEPSEEK_SYSTEM,
            ask=_ask_deepseek,
            aliases=("deepseek", "deep"),
            fallbacks=("techai", "gemini"),
        ),
        AgentProvider(
            key="techai",
            label="Tech-AI",
            env_var="TECH_AI_API_KEY",
            api_key=TECH_AI_API_KEY,
            system_prompt=TECH_AI_SYSTEM,
            ask=_ask_techai,
            aliases=("techai", "tech-ai", "tech"),
            fallbacks=("deepseek", "gemini"),
        ),
    )
}


# Nar en leverandor feiler (kvote, 502, tidsavbrudd) skal vi ikke prove den pa
# nytt for hver eneste foresporsel. Da venter brukeren pa dode tjenester hver gang.
_PROVIDER_COOLDOWN: dict[str, float] = {}
PROVIDER_COOLDOWN_SECONDS = float(os.environ.get("PROVIDER_COOLDOWN_SECONDS", "300"))


def _provider_available(key: str) -> bool:
    return _PROVIDER_COOLDOWN.get(key, 0.0) <= time.monotonic()


def _mark_provider_down(key: str, reason: object) -> None:
    _PROVIDER_COOLDOWN[key] = time.monotonic() + PROVIDER_COOLDOWN_SECONDS
    logger.warning("{} settes pa pause i {}s ({})", key, int(PROVIDER_COOLDOWN_SECONDS), str(reason)[:120])


def _first_available_provider(preferred: str) -> str:
    """Bruk den foretrukne agenten hvis den virker, ellers en som virker na."""
    if preferred in AGENT_PROVIDERS and _provider_available(preferred):
        return preferred
    for key, provider in AGENT_PROVIDERS.items():
        if provider.configured and _provider_available(key):
            return key
    return preferred


def _normalize_agent(name: str) -> str:
    low = (name or "").strip().lower()
    for provider in AGENT_PROVIDERS.values():
        if any(low.startswith(alias) for alias in provider.aliases):
            return provider.key
    if not GEMINI_API_KEY and TECH_AI_API_KEY:
        return "techai"
    return "gemini"


def _agent_label(key: str) -> str:
    provider = AGENT_PROVIDERS.get(key)
    return provider.label if provider else AGENT_PROVIDERS["gemini"].label


def _system_prompt(key: str) -> str:
    provider = AGENT_PROVIDERS.get(key)
    return provider.system_prompt if provider else AGENT_PROVIDERS["gemini"].system_prompt


def _ask_agent(key: str, prompt: str, json_mode: bool = False) -> tuple[str, str]:
    """Sender prompt til valgt agent. Hvis hovedagenten er utilgjengelig/overbelastet,
    faller den automatisk tilbake til en av de andre konfigurerte AI-agentene slik at
    oppgaver aldri stopper!"""
    primary = AGENT_PROVIDERS.get(key) or AGENT_PROVIDERS["gemini"]
    order = [primary.key] + list(primary.fallbacks)
    problems: list[str] = []

    for name in order:
        provider = AGENT_PROVIDERS.get(name)
        if not provider or not provider.configured:
            continue
        if not _provider_available(name):
            problems.append(f"{provider.label}: pa pause")
            continue
        try:
            return provider.ask(prompt, json_mode)
        except Exception as err:
            problems.append(f"{provider.label}: {str(err)[:100]}")
            _mark_provider_down(name, err)
            logger.warning("{} feilet ({}), prover neste agent", provider.label, str(err)[:160])

    raise RuntimeError("Ingen AI-agent svarte. " + " | ".join(problems[:4]))


def _server_context() -> str:
    """Sanntidsstatus fra serveren, som begge agentene får se før de svarer."""
    recent: list[tuple[float, str]] = []
    for root, _dirs, files in os.walk(PROJECTS_DIR):
        for name in files:
            if name.startswith(".agent_"):
                continue
            file_path = Path(root) / name
            try:
                recent.append((file_path.stat().st_mtime, file_path.relative_to(PROJECTS_DIR).as_posix()))
            except OSError:
                continue
    recent.sort(reverse=True)

    if recent:
        files_txt = "\n".join(
            f"  - {name} (endret {time.strftime('%Y-%m-%d %H:%M', time.localtime(m))})"
            for m, name in recent[:5]
        )
    else:
        files_txt = "  (ingen filer i prosjektmappen)"

    mem = psutil.virtual_memory()
    return (
        "SANNTIDSSTATUS FRA SERVEREN:\n"
        f"- CPU: {psutil.cpu_percent(interval=0.1)}%\n"
        f"- RAM: {mem.used // (1024 * 1024)} MB av {mem.total // (1024 * 1024)} MB ({mem.percent}%)\n"
        f"- Filene som er endret sist:\n{files_txt}"
    )


def _review_prompt(reviewer: str, author: str, question: str, answer: str, context: str) -> str:
    return (
        f"Du er {reviewer}. {author} har svart brukeren, og jobben din er å kontrollere svaret.\n\n"
        f"{context}\n\n"
        f"Brukerens spørsmål:\n{question}\n\n"
        f"Svaret fra {author}:\n{answer}\n\n"
        "Svar kort på norsk i dette formatet:\n"
        "Linje 1: skriv nøyaktig 'VURDERING: OK' hvis svaret er riktig og trygt, eller "
        "'VURDERING: FEIL' hvis noe er galt, mangler eller kan skade serveren.\n"
        "Linje 2 og utover: 1-3 setninger med begrunnelse. Er det feil, skriv den konkrete rettelsen."
    )


def _parse_verdict(review_text: str) -> str:
    stripped = (review_text or "").strip()
    if not stripped:
        return "OK"
    first_line = stripped.splitlines()[0].upper()
    return "FEIL" if "FEIL" in first_line else "OK"


@app.post("/chat/collaborate")
def chat_collaborate(body: CollaborateRequest):
    """Den valgte agenten svarer, den andre kontrollerer - og retter hvis noe er galt."""
    question = body.message.strip()
    primary_key = _normalize_agent(body.primary)
    reviewer_key = "deepseek" if primary_key == "gemini" else "gemini"
    author = _agent_label(primary_key)
    reviewer = _agent_label(reviewer_key)

    if not question:
        return {
            "question": "",
            "primary": {
                "agent": author,
                "model": None,
                "reply": f"Hei! Jeg er {author}. Hva kan jeg hjelpe deg med?",
            },
            "review": {
                "agent": reviewer,
                "model": None,
                "verdict": "OK",
                "reply": "Ingen oppgave å kontrollere ennå.",
            },
            "final": None,
        }

    context = _server_context()

    # 1) Den valgte agenten svarer brukeren
    try:
        primary_reply, primary_model = _ask_agent(
            primary_key,
            f"{_system_prompt(primary_key)}\n\n{context}\n\nBruker: {question}\n{author}:",
        )
    except Exception as e:
        logger.error("{} svarte ikke i samarbeidet: {}", author, e)
        return {
            "question": question,
            "primary": {"agent": author, "model": None, "reply": f"{author} Feil: {e}"},
            "review": {
                "agent": reviewer,
                "model": None,
                "verdict": "AVBRUTT",
                "reply": f"Ingenting å kontrollere - {author} svarte ikke.",
            },
            "final": None,
        }

    # 2) Den andre agenten kontrollerer svaret
    try:
        review_reply, review_model = _ask_agent(
            reviewer_key,
            _review_prompt(reviewer, author, question, primary_reply, context),
        )
    except Exception as e:
        logger.error("{} kunne ikke kontrollere: {}", reviewer, e)
        return {
            "question": question,
            "primary": {"agent": author, "model": primary_model, "reply": primary_reply},
            "review": {"agent": reviewer, "model": None, "verdict": "AVBRUTT", "reply": f"Kontrollen feilet: {e}"},
            "final": None,
        }

    verdict = _parse_verdict(review_reply)

    # 3) Fant kontrolløren en feil, får forfatteren rette opp
    final = None
    if verdict == "FEIL":
        try:
            fix_prompt = (
                f"{_system_prompt(primary_key)}\n\n{context}\n\n"
                f"Brukerens spørsmål:\n{question}\n\n"
                f"Ditt opprinnelige svar:\n{primary_reply}\n\n"
                f"{reviewer} har kontrollert svaret og mener det er feil:\n{review_reply}\n\n"
                "Gi et kort, korrigert svar til brukeren. Ikke gjenta hele analysen."
            )
            final_reply, final_model = _ask_agent(primary_key, fix_prompt)
            final = {"agent": author, "model": final_model, "reply": final_reply}
        except Exception as e:
            logger.error("{} kunne ikke rette opp: {}", author, e)

    return {
        "question": question,
        "primary": {"agent": author, "model": primary_model, "reply": primary_reply},
        "review": {"agent": reviewer, "model": review_model, "verdict": verdict, "reply": review_reply},
        "final": final,
    }


# ---------------------------------------------------------------------------
# Autonom kode-agent: skriver filer, kjører kommandoer og gir ALDRI opp før ferdig
# ---------------------------------------------------------------------------


class AgentTaskRequest(BaseModel):
    goal: str = ""
    project: str = ""
    primary: str = ""
    agent: str = ""
    review_with_other: bool = True
    max_steps: int = 30


AGENT_INSTRUCTIONS = f"""Du er en utholdende, autonom kode- og serveragent med FULL KONTROLL over serveren ({OS_DESCRIPTION}).
Du jobber i en løkke og SKAL FULLFØRE hele oppgaven før du setter "done": true.
ALDRI gi opp midtveis, ALDRI stopp etter bare å ha opprettet en mappe eller én fil, og ALDRI sett "done": true hvis siste kommando feilet!

VIKTIGE REGLER:
1. Du har full tilgang til å opprette, lese, redigere, flytte, kopiere og slette mapper og filer på hele serveren (relativt til valgt prosjektmappe, i 'servidor', i '~', eller via absolutte stier på Linux, macOS og Windows).
2. Tilpass shell-kommandoer til operativsystemet på serveren ({OS_DESCRIPTION}): bruk Bash/POSIX på Linux og macOS, og CMD/PowerShell-kompatible kommandoer på Windows.
3. Svar KUN med ett gyldig JSON-objekt per steg (ingen markdown-kodeblokk rundt, ingen ekstra tekst).
4. Svar KORT. Hele svaret må være under ca. 1200 tegn. Skal du skrive en stor fil, skriv
   den i biter: første del med "write_file", resten med "append_file" i neste steg.
   Ellers blir JSON-en avkuttet og steget mislykkes.

JSON-format (velg nøyaktig ett skjema per steg):
- Skrive eller overskrive fil:
  {{"thought": "kort forklaring", "action": "write_file", "path": "sti/til/fil.ext", "content": "full filtekst", "done": false}}
- Redigere del av eksisterende fil (søk og erstatt):
  {{"thought": "kort forklaring", "action": "edit_file", "path": "sti/til/fil.ext", "old_text": "eksisterende tekst", "new_text": "ny tekst", "done": false}}
- Legge til tekst på slutten av en fil:
  {{"thought": "kort forklaring", "action": "append_file", "path": "sti/til/fil.ext", "content": "tekst som legges til", "done": false}}
- Lese eksisterende fil:
  {{"thought": "kort forklaring", "action": "read_file", "path": "sti/til/fil.ext", "done": false}}
- Liste filer og mapper:
  {{"thought": "kort forklaring", "action": "list_files", "path": "", "done": false}}
- Opprette ny mappe (mkdir):
  {{"thought": "kort forklaring", "action": "mkdir", "path": "ny/mappe", "done": false}}
- Slette fil eller mappe (bruk "path": "." hvis selve den valgte prosjektmappen skal slettes):
  {{"thought": "kort forklaring", "action": "delete_path", "path": "sti/som/skal/slettes", "done": false}}
- Flytte eller gi nytt navn til fil/mappe:
  {{"thought": "kort forklaring", "action": "move_path", "path": "fra/sti", "dest": "til/sti", "done": false}}
- Kopiere fil eller mappe:
  {{"thought": "kort forklaring", "action": "copy_path", "path": "fra/sti", "dest": "til/sti", "done": false}}
- Kjøre en hvilken som helst terminal/shell-kommando på serveren:
  {{"thought": "kort forklaring", "action": "run_command", "command": "kommando her", "done": false}}
- Når HELE oppgaven er utført, verifisert og testet uten feil:
  {{"thought": "alt er testet og verifisert", "action": "finish", "summary": "kort oppsummering på norsk av alt som ble gjort", "done": true}}
"""


def _extract_json_object(raw: str) -> dict | None:
    """Robust JSON-uttrekk som tåler markdown-blokker, ekstra tekst og enkle feil."""
    if not raw:
        return None
    cleaned = raw.strip()

    if "```" in cleaned:
        fenced = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned, flags=re.DOTALL)
        for block in fenced:
            try:
                obj = json.loads(block)
                if isinstance(obj, dict):
                    return obj
            except Exception:
                pass

    try:
        obj = json.loads(cleaned)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end > start:
        candidate = cleaned[start : end + 1]
        for attempt_str in (
            candidate,
            re.sub(r",\s*([}\]])", r"\1", candidate),
        ):
            try:
                obj = json.loads(attempt_str)
                if isinstance(obj, dict):
                    return obj
            except Exception:
                pass
        try:
            obj = ast.literal_eval(candidate)
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass

    return None


def _project_tree_snapshot(workdir: Path, max_entries: int = 60) -> str:
    if not workdir.exists():
        return "(mappen finnes ikke / er slettet)"
    entries: list[str] = []
    for root, dirs, files in os.walk(workdir):
        dirs[:] = [d for d in dirs if d not in {".git", "node_modules", ".venv", "venv", "__pycache__"}]
        rel_root = Path(root).relative_to(workdir).as_posix()
        prefix = "" if rel_root == "." else f"{rel_root}/"
        for d in sorted(dirs):
            entries.append(f"{prefix}{d}/")
        for f in sorted(files):
            if f.startswith(".agent_"):
                continue
            entries.append(f"{prefix}{f}")
        if len(entries) >= max_entries:
            break
    if not entries:
        return "(tom prosjektmappe)"
    return "\n".join(entries[:max_entries])


@app.get("/chat/agent/status")
def chat_agent_status():
    """Returnerer sanntidsstatus for pågående eller siste agent-kjøring."""
    if not _agent_state.get("steps") and AGENT_STATE_FILE.is_file():
        try:
            return json.loads(AGENT_STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return _agent_state


class AgentMessageRequest(BaseModel):
    message: str


@app.post("/chat/agent/message")
def chat_agent_message(body: AgentMessageRequest):
    """Legger en melding i ko til en agent som jobber. Agenten leser den i
    starten av neste steg og vurderer situasjonen pa nytt."""
    text = (body.message or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Meldingen er tom")
    if not _agent_state.get("running"):
        return {
            "queued": 0,
            "running": False,
            "note": "Agenten kjorer ikke na. Send meldingen som en vanlig oppgave i stedet.",
        }
    with _agent_inbox_lock:
        _agent_inbox.append(text)
        queued = len(_agent_inbox)
    logger.info("Melding fra bruker lagt i ko ({} tegn, {} i ko)", len(text), queued)
    return {
        "queued": queued,
        "running": True,
        "note": "Agenten leser meldingen naar dette steget er ferdig.",
    }


@app.post("/chat/agent")
def chat_agent(body: AgentTaskRequest):
    """Autonom kodeagent som jobber steg-for-steg helt til oppgaven er ferdigstilt."""
    _require_agent_shell()
    goal = body.goal.strip()
    if not goal:
        raise HTTPException(status_code=400, detail="Mål (goal) mangler")

    default_primary = body.primary or body.agent or "gemini"
    chosen_agent = _first_available_provider(_normalize_agent(default_primary))
    primary_key = _normalize_agent(chosen_agent)
    reviewer_key = "deepseek" if primary_key == "gemini" else "gemini"
    author = _agent_label(primary_key)
    reviewer = _agent_label(reviewer_key)

    max_steps = max(5, min(int(body.max_steps or 30), 60))

    raw_project = body.project.strip().strip("/")
    if not raw_project:
        slug = re.sub(r"[^a-z0-9_-]+", "-", goal.lower())[:32].strip("-") or "prosjekt"
        raw_project = slug

    workdir, rel_project, init_warn = _resolve_agent_workdir(raw_project, fallback_project="prosjekt")
    if not any(w in goal.lower() for w in ("slett", "delete", "fjern", "remove")):
        workdir.mkdir(parents=True, exist_ok=True)

    steps_log: list[dict] = []
    history_notes: list[str] = []
    if init_warn:
        history_notes.append(init_warn)

    with _agent_inbox_lock:
        _agent_inbox.clear()

    with _agent_lock:
        _agent_state.update({
            "running": True,
            "goal": goal,
            "project": rel_project,
            "primary": author,
            "current_step": 0,
            "max_steps": max_steps,
            "done": False,
            "summary": "Agenten jobber...",
            "steps": steps_log,
            "user_messages": int(_agent_state.get("user_messages", 0)),
            "started_at": time.time(),
            "updated_at": time.time(),
        })
        _save_agent_state()

    done = False
    summary = ""
    last_model: str | None = None
    last_step_failed = False
    consecutive_parse_errors = 0

    try:
        for step_num in range(1, max_steps + 1):
            tree_now = _project_tree_snapshot(workdir)

            # Har brukeren skrevet noe mens vi jobbet? Da er det viktigere enn
            # historikken, og agenten skal vurdere om planen maa justeres.
            user_updates = _drain_agent_inbox()
            if user_updates:
                history_notes.append("NY MELDING FRA BRUKEREN UNDERVEIS: " + " / ".join(user_updates))
                with _agent_lock:
                    _agent_state["user_messages"] = int(_agent_state.get("user_messages", 0)) + len(user_updates)
                    _agent_state["updated_at"] = time.time()

            user_updates_block = ""
            if user_updates:
                user_updates_block = (
                    "VIKTIG - NYE MELDINGER FRA BRUKEREN MENS DU JOBBET:\n"
                    + "\n".join(f"- {u}" for u in user_updates)
                    + "\nVurder om dette endrer oppgaven. Har brukeren rettet seg selv eller "
                    "ombestemt seg, juster planen - eller avslutt med done=true og forklar hvorfor.\n\n"
                )

            recent_history = "\n\n".join(history_notes[-10:]) if history_notes else "(ingen steg utført ennå)"

            prompt = (
                f"{AGENT_INSTRUCTIONS}\n\n"
                f"OPERATIVSYSTEM: {OS_DESCRIPTION}\n"
                f"AKTIV ARBEIDSMAPPE: '{workdir}' (visningsnavn: '{rel_project or '.'}')\n"
                f"FILER I ARBEIDSMAPPEN NÅ:\n{tree_now}\n\n"
                f"BRUKERENS MÅL:\n{goal}\n\n"
                f"HISTORIKK FRA TIDLIGERE STEG:\n{recent_history}\n\n"
                f"{user_updates_block}"
                f"Dette er steg {step_num} av {max_steps}. "
                "Velg neste handling som bringer oppgaven i mål, og svar KUN med gyldig JSON:"
            )

            try:
                raw_reply, last_model = _ask_agent(primary_key, prompt, json_mode=True)
            except Exception as e:
                logger.error("Agent-kall feilet i steg {}: {}", step_num, e)
                err_txt = f"AI-kall feilet midlertidig ({e}) - prøver igjen i neste steg."
                history_notes.append(f"Steg {step_num}: {err_txt}")
                time.sleep(1.5)
                continue

            action_obj = _extract_json_object(raw_reply)
            if not action_obj:
                consecutive_parse_errors += 1
                truncated = raw_reply.count("{") > raw_reply.count("}")
                logger.warning(
                    "Ugyldig JSON fra {} i steg {} ({}): {!r}",
                    author,
                    step_num,
                    "AVKUTTET av token-grensen" if truncated else "ugyldig",
                    auth.redact_secrets(raw_reply[:200]),
                )
                if truncated:
                    # Modellen traff maks output-tokens midt i en streng. Da maa
                    # neste forsok vaere kortere, ellers gjentar det seg i det
                    # uendelige. Vi reparerer ikke JSON-en: en halv 'content'
                    # ville skrevet en halv fil.
                    history_notes.append(
                        f"Steg {step_num}: Svaret ditt ble AVKUTTET fordi det ble for langt. "
                        "Svar KUN med ett JSON-objekt, hold 'content' under 1200 tegn, "
                        "og bruk handlingen append_file for resten av filen."
                    )
                else:
                    history_notes.append(
                        f"Steg {step_num}: Ditt forrige svar var ikke gyldig JSON. "
                        "Du MÅ svare med KUN ett JSON-objekt med feltene thought, action og done."
                    )
                if consecutive_parse_errors >= 4:
                    primary_key = reviewer_key
                continue

            consecutive_parse_errors = 0
            thought = str(action_obj.get("thought") or "").strip()
            action = str(action_obj.get("action") or "").strip().lower()
            wants_done = bool(action_obj.get("done"))

            if not action and "command" in action_obj:
                action = "run_command"
            elif not action and "old_text" in action_obj and "new_text" in action_obj:
                action = "edit_file"
            elif not action and "content" in action_obj and "path" in action_obj:
                action = "write_file"
            elif not action and wants_done:
                action = "finish"

            step_entry: dict = {
                "step": step_num,
                "action": action or "thinking",
                "thought": thought,
                "target": "",
                "command": "",
                "result": "",
                "stdout": "",
                "stderr": "",
                "ok": True,
            }

            all_file_actions = {
                "write_file",
                "edit_file",
                "append_file",
                "read_file",
                "list_files",
                "mkdir",
                "delete_path",
                "move_path",
                "copy_path",
                "run_command",
            }

            if action == "finish" or (wants_done and action not in all_file_actions):
                # Brukeren kan ha skrevet noe mens vi jobbet. Da skal vi IKKE
                # avslutte: les meldingen, vurder situasjonen og fortsett hvis
                # den betyr at noe skal gjøres annerledes.
                late_messages = _drain_agent_inbox()
                if late_messages:
                    with _agent_lock:
                        _agent_state["user_messages"] = int(_agent_state.get("user_messages", 0)) + len(late_messages)
                    late_txt = " / ".join(late_messages)
                    msg = (
                        "Finish avvist: brukeren sendte en melding mens du jobbet: "
                        f"«{late_txt}». Les den, vurder om oppgaven skal justeres, og fortsett."
                    )
                    step_entry["action"] = "continue"
                    step_entry["result"] = msg
                    step_entry["ok"] = False
                    steps_log.append(step_entry)
                    history_notes.append(f"Steg {step_num}: {msg}")
                    continue

                if last_step_failed and step_num < max_steps - 1:
                    msg = (
                        "Avvist finish: Forrige steg feilet. Rett opp feilen og verifiser at alt "
                        "fungerer uten feil før du avslutter!"
                    )
                    step_entry["action"] = "continue"
                    step_entry["result"] = msg
                    step_entry["ok"] = False
                    steps_log.append(step_entry)
                    history_notes.append(f"Steg {step_num}: {msg}")
                    last_step_failed = False
                    continue

                done = True
                summary = str(action_obj.get("summary") or thought or "Oppgaven er fullført.").strip()
                step_entry["action"] = "finish"
                step_entry["result"] = summary
                steps_log.append(step_entry)
                break

            elif action == "write_file":
                rel_file = str(action_obj.get("path") or "").strip()
                content = str(action_obj.get("content") or "")
                step_entry["target"] = rel_file
                step_entry["command"] = f"write_file {rel_file}"
                if not rel_file:
                    step_entry["ok"] = False
                    step_entry["result"] = "Filsti ('path') mangler."
                    step_entry["stderr"] = step_entry["result"]
                    last_step_failed = True
                else:
                    try:
                        target_file = _resolve_target_in_workdir(workdir, rel_file)
                        target_file.parent.mkdir(parents=True, exist_ok=True)
                        target_file.write_text(content, encoding="utf-8")
                        step_entry["result"] = f"Skrev {len(content)} tegn til {target_file}"
                        step_entry["stdout"] = step_entry["result"]
                        last_step_failed = False
                    except Exception as e:
                        step_entry["ok"] = False
                        step_entry["result"] = f"Kunne ikke skrive fil '{rel_file}': {e}"
                        step_entry["stderr"] = step_entry["result"]
                        last_step_failed = True

            elif action == "edit_file":
                rel_file = str(action_obj.get("path") or "").strip()
                old_text = str(action_obj.get("old_text") or "")
                new_text = str(action_obj.get("new_text") or "")
                step_entry["target"] = rel_file
                step_entry["command"] = f"edit_file {rel_file}"
                try:
                    target_file = _resolve_target_in_workdir(workdir, rel_file)
                    if not target_file.is_file():
                        raise FileNotFoundError(f"Filen '{rel_file}' finnes ikke")
                    existing = target_file.read_text(encoding="utf-8", errors="replace")
                    if old_text and old_text not in existing:
                        raise ValueError("Fant ikke 'old_text' i filen")
                    updated = existing.replace(old_text, new_text, 1) if old_text else (existing + "\n" + new_text)
                    target_file.write_text(updated, encoding="utf-8")
                    step_entry["result"] = f"Oppdaterte {target_file}"
                    step_entry["stdout"] = step_entry["result"]
                    last_step_failed = False
                except Exception as e:
                    step_entry["ok"] = False
                    step_entry["result"] = f"Kunne ikke redigere '{rel_file}': {e}"
                    step_entry["stderr"] = step_entry["result"]
                    last_step_failed = True

            elif action == "append_file":
                rel_file = str(action_obj.get("path") or "").strip()
                content = str(action_obj.get("content") or "")
                step_entry["target"] = rel_file
                step_entry["command"] = f"append_file {rel_file}"
                try:
                    target_file = _resolve_target_in_workdir(workdir, rel_file)
                    target_file.parent.mkdir(parents=True, exist_ok=True)
                    with target_file.open("a", encoding="utf-8") as f:
                        f.write(content)
                    step_entry["result"] = f"La til {len(content)} tegn i {target_file}"
                    step_entry["stdout"] = step_entry["result"]
                    last_step_failed = False
                except Exception as e:
                    step_entry["ok"] = False
                    step_entry["result"] = f"Kunne ikke legge til i '{rel_file}': {e}"
                    step_entry["stderr"] = step_entry["result"]
                    last_step_failed = True

            elif action == "read_file":
                rel_file = str(action_obj.get("path") or "").strip()
                step_entry["target"] = rel_file
                step_entry["command"] = f"read_file {rel_file}"
                target_file = _resolve_target_in_workdir(workdir, rel_file)
                if not rel_file or not target_file.is_file():
                    step_entry["ok"] = False
                    step_entry["result"] = f"Filen '{rel_file}' finnes ikke ({target_file})."
                    step_entry["stderr"] = step_entry["result"]
                    last_step_failed = True
                else:
                    txt = target_file.read_text(encoding="utf-8", errors="replace")[:MAX_OUTPUT_CHARS]
                    step_entry["result"] = txt
                    step_entry["stdout"] = txt
                    last_step_failed = False

            elif action == "list_files":
                rel_sub = str(action_obj.get("path") or "").strip()
                step_entry["target"] = rel_sub or "."
                step_entry["command"] = f"list_files {rel_sub or '.'}"
                target_dir = _resolve_target_in_workdir(workdir, rel_sub)
                if not target_dir.is_dir():
                    step_entry["ok"] = False
                    step_entry["result"] = f"Mappen '{rel_sub}' finnes ikke ({target_dir})."
                    step_entry["stderr"] = step_entry["result"]
                    last_step_failed = True
                else:
                    snap = _project_tree_snapshot(target_dir)
                    step_entry["result"] = snap
                    step_entry["stdout"] = snap
                    last_step_failed = False

            elif action in {"mkdir", "create_dir"}:
                rel_sub = str(action_obj.get("path") or "").strip()
                step_entry["target"] = rel_sub
                step_entry["command"] = f"mkdir {rel_sub}"
                try:
                    target_dir = _resolve_target_in_workdir(workdir, rel_sub)
                    target_dir.mkdir(parents=True, exist_ok=True)
                    step_entry["result"] = f"Opprettet mappe {target_dir}"
                    step_entry["stdout"] = step_entry["result"]
                    last_step_failed = False
                except Exception as e:
                    step_entry["ok"] = False
                    step_entry["result"] = f"Kunne ikke opprette mappe '{rel_sub}': {e}"
                    step_entry["stderr"] = step_entry["result"]
                    last_step_failed = True

            elif action in {"delete_path", "rm", "delete_file"}:
                rel_sub = str(action_obj.get("path") or "").strip()
                step_entry["target"] = rel_sub
                step_entry["command"] = f"delete_path {rel_sub}"
                try:
                    target_p = _resolve_target_in_workdir(workdir, rel_sub)
                    if not target_p.exists():
                        raise FileNotFoundError(f"'{rel_sub}' finnes ikke")
                    if target_p.is_dir():
                        shutil.rmtree(target_p)
                    else:
                        target_p.unlink()
                    step_entry["result"] = f"Slettet {target_p}"
                    step_entry["stdout"] = step_entry["result"]
                    last_step_failed = False
                except Exception as e:
                    step_entry["ok"] = False
                    step_entry["result"] = f"Kunne ikke slette '{rel_sub}': {e}"
                    step_entry["stderr"] = step_entry["result"]
                    last_step_failed = True

            elif action in {"move_path", "mv", "rename"}:
                src_str = str(action_obj.get("path") or action_obj.get("src") or "").strip()
                dst_str = str(action_obj.get("dest") or action_obj.get("to") or "").strip()
                step_entry["target"] = f"{src_str} -> {dst_str}"
                step_entry["command"] = f"move_path {src_str} -> {dst_str}"
                try:
                    src_p = _resolve_target_in_workdir(workdir, src_str)
                    dst_p = _resolve_target_in_workdir(workdir, dst_str)
                    dst_p.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(src_p), str(dst_p))
                    step_entry["result"] = f"Flyttet {src_p} til {dst_p}"
                    step_entry["stdout"] = step_entry["result"]
                    last_step_failed = False
                except Exception as e:
                    step_entry["ok"] = False
                    step_entry["result"] = f"Kunne ikke flytte '{src_str}' til '{dst_str}': {e}"
                    step_entry["stderr"] = step_entry["result"]
                    last_step_failed = True

            elif action in {"copy_path", "cp"}:
                src_str = str(action_obj.get("path") or action_obj.get("src") or "").strip()
                dst_str = str(action_obj.get("dest") or action_obj.get("to") or "").strip()
                step_entry["target"] = f"{src_str} -> {dst_str}"
                step_entry["command"] = f"copy_path {src_str} -> {dst_str}"
                try:
                    src_p = _resolve_target_in_workdir(workdir, src_str)
                    dst_p = _resolve_target_in_workdir(workdir, dst_str)
                    dst_p.parent.mkdir(parents=True, exist_ok=True)
                    if src_p.is_dir():
                        shutil.copytree(src_p, dst_p, dirs_exist_ok=True)
                    else:
                        shutil.copy2(src_p, dst_p)
                    step_entry["result"] = f"Kopierte {src_p} til {dst_p}"
                    step_entry["stdout"] = step_entry["result"]
                    last_step_failed = False
                except Exception as e:
                    step_entry["ok"] = False
                    step_entry["result"] = f"Kunne ikke kopiere '{src_str}' til '{dst_str}': {e}"
                    step_entry["stderr"] = step_entry["result"]
                    last_step_failed = True

            elif action == "run_command":
                cmd = str(action_obj.get("command") or "").strip()
                step_entry["target"] = cmd
                step_entry["command"] = cmd
                if not cmd:
                    step_entry["ok"] = False
                    step_entry["result"] = "Tom kommando."
                    step_entry["stderr"] = "Tom kommando."
                    last_step_failed = True
                else:
                    cmd_res = _run_command(cmd, workdir, AGENT_SHELL_TIMEOUT)
                    code = cmd_res["exit_code"]
                    out = cmd_res["stdout"].strip()
                    err = cmd_res["stderr"].strip()
                    combined = f"exit_code={code}"
                    if out:
                        combined += f"\nSTDOUT:\n{out}"
                    if err:
                        combined += f"\nSTDERR:\n{err}"
                    step_entry["result"] = combined
                    step_entry["stdout"] = out
                    step_entry["stderr"] = err
                    step_entry["ok"] = code == 0
                    last_step_failed = code != 0
            else:
                step_entry["ok"] = False
                step_entry["result"] = (
                    f"Ukjent action '{action}'. Gyldige actions er: "
                    "write_file, edit_file, append_file, read_file, list_files, mkdir, delete_path, move_path, copy_path, run_command, finish."
                )
                last_step_failed = True

            steps_log.append(step_entry)
            history_notes.append(
                f"Steg {step_num} ({step_entry['action']} {step_entry['target']}): "
                f"{'OK' if step_entry['ok'] else 'FEIL'}\n"
                f"Tanke: {thought}\n"
                f"Resultat: {step_entry['result'][:1500]}"
            )

            with _agent_lock:
                _agent_state.update({
                    "current_step": step_num,
                    "steps": steps_log,
                    "user_messages": int(_agent_state.get("user_messages", 0)),
                    "updated_at": time.time(),
                })
                _save_agent_state()

            if wants_done and not last_step_failed and action in all_file_actions:
                done = True
                summary = str(action_obj.get("summary") or thought or "Oppgaven er fullført.").strip()
                break

        if not summary:
            if done:
                summary = "Oppgaven er fullført."
            else:
                summary = (
                    f"Agenten kjørte {len(steps_log)} steg i '{rel_project}'. "
                    "Trykk fortsett eller kjør på nytt for å bygge videre på samme prosjekt."
                )

        review_obj = None
        if body.review_with_other and steps_log:
            try:
                tree_after = _project_tree_snapshot(workdir)
                steps_brief = "\n".join(
                    f"- Steg {s['step']} ({s['action']} {s['target']}): {'OK' if s['ok'] else 'FEIL'}"
                    for s in steps_log[-12:]
                )
                rev_prompt = (
                    f"Du er {reviewer}. {author} har kjørt en autonom kodeagent i prosjektet '{rel_project}'.\n"
                    f"Mål: {goal}\n"
                    f"Filer i prosjektet nå:\n{tree_after}\n\n"
                    f"Utførte steg:\n{steps_brief}\n\n"
                    f"Oppsummering fra {author}: {summary}\n\n"
                    "Svar kort på norsk:\n"
                    "Linje 1: 'VURDERING: OK' hvis oppgaven ser løst ut, eller 'VURDERING: FEIL' hvis noe viktig mangler.\n"
                    "Linje 2+: Kort tilbakemelding (1-3 setninger)."
                )
                rev_reply, rev_model = _ask_agent(reviewer_key, rev_prompt)
                review_obj = {
                    "agent": reviewer,
                    "model": rev_model,
                    "verdict": _parse_verdict(rev_reply),
                    "reply": rev_reply,
                }
            except Exception as e:
                logger.warning("Kontroll-agent feilet: {}", e)

        with _agent_lock:
            _agent_state.update({
                "running": False,
                "done": done,
                "summary": summary,
                "steps": steps_log,
                "user_messages": int(_agent_state.get("user_messages", 0)),
                "updated_at": time.time(),
            })
            _save_agent_state()

        # Meldinger som kom for sent til a bli lest i et steg skal ikke forsvinne
        # i stillhet - de returneres sammen med svaret.
        leftover = _drain_agent_inbox()
        pending_note = ""
        if leftover:
            pending_note = (
                " Du sendte også en melding helt til slutt som jeg ikke rakk å behandle: "
                + " / ".join(leftover)
            )
            summary = (summary or "") + pending_note

        return {
            "goal": goal,
            "project": rel_project,
            "primary": author,
            "model": last_model,
            "done": done,
            "summary": summary,
            "answer": summary,
            "steps": steps_log,
            "user_messages": int(_agent_state.get("user_messages", 0)),
            "pending_messages": leftover,
            "review": review_obj,
        }
    except Exception as exc:
        logger.exception("Uventet feil i chat_agent: {}", exc)
        with _agent_lock:
            _agent_state.update({
                "running": False,
                "done": False,
                "summary": f"Avbrutt av feil: {exc}",
                "updated_at": time.time(),
            })
            _save_agent_state()
        return {
            "goal": goal,
            "project": rel_project,
            "primary": author,
            "model": last_model,
            "done": False,
            "summary": f"Agenten møtte en feil underveis ({exc}), men har lagret {len(steps_log)} steg.",
            "answer": f"Agenten møtte en feil underveis ({exc}), men har lagret {len(steps_log)} steg.",
            "steps": steps_log,
            "user_messages": int(_agent_state.get("user_messages", 0)),
            "review": None,
        }


# ---------------------------------------------------------------------------
# Lokal SQL Bruker-autentisering og brukeroversikt (/auth/* og /admin/*)
# ---------------------------------------------------------------------------


class RegisterRequest(BaseModel):
    username: str
    password: str
    email: str | None = None


class LoginRequest(BaseModel):
    username: str
    password: str


@app.post("/auth/register")
def auth_register(body: RegisterRequest, request: Request):
    """Registrerer en ny bruker i databasen."""
    username = body.username.strip()
    if not username or len(body.password) < 4:
        raise HTTPException(status_code=400, detail="Brukernavn påkrevd og passord minst 4 tegn.")
    try:
        user = auth.create_user(username, body.password, body.email)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    client_ip = request.client.host if request.client else ""
    auth.log_user_activity("/auth/register", user, client_ip, allowed=True)
    return {"id": user["id"], "username": user["username"], "role": user["role"]}


@app.post("/auth/login")
def auth_login(body: LoginRequest, request: Request):
    """Logger inn bruker mot lokal SQL-database og returnerer JWT-token."""
    client_ip = request.client.host if request.client else ""
    user = auth.login(body.username.strip(), body.password, ip_address=client_ip)
    if not user:
        auth.log_user_activity("/auth/login", None, client_ip, allowed=False, blocked_reason=f"Feil innlogging for {body.username}")
        raise HTTPException(status_code=401, detail="Feil brukernavn eller passord.")
    if bool(user.get("is_banned")):
        auth.log_user_activity("/auth/login", user, client_ip, allowed=False, blocked_reason="Bannet")
        raise HTTPException(status_code=403, detail=f"Bannet: {user.get('ban_reason') or 'ingen grunn'}")
    state, reason = auth.access_state(user)
    auth.log_user_activity("/auth/login", user, client_ip, allowed=True)
    return {
        "token": auth.create_token(user),
        "user": {
            "id": user["id"],
            "username": user["username"],
            "role": user["role"],
            "is_vip": bool(user.get("is_vip")),
        },
        "access": state,
        "reason": reason,
    }


@app.get("/auth/me")
def auth_me(request: Request):
    """Returnerer informasjon om den innloggede brukeren."""
    auth_header = request.headers.get("authorization", "")
    if not auth_header.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Ingen token.")
    try:
        payload = auth.decode_token(auth_header[7:].strip())
    except Exception as e:
        raise HTTPException(status_code=401, detail="Ugyldig eller utløpt token.") from e
    user = auth.get_user_by_id(payload.get("sub"))
    if not user:
        raise HTTPException(status_code=404, detail="Brukeren finnes ikke.")
    state, reason = auth.access_state(user)
    return {
        "id": user["id"],
        "username": user["username"],
        "role": user["role"],
        "is_vip": bool(user.get("is_vip")),
        "access": state,
        "reason": reason,
    }


class ForgotPasswordRequest(BaseModel):
    username: str


class ResetPasswordRequest(BaseModel):
    username: str
    code: str
    new_password: str


@app.post("/auth/forgot-password")
def auth_forgot_password(body: ForgotPasswordRequest, request: Request):
    """Åpen rute: brukeren ber om hjelp. Vi sier aldri om brukernavnet finnes,
    slik at ruten ikke kan brukes til å kartlegge kontoer.

    Koden lages av administratoren (backend/reset_password.py) og gis til
    brukeren utenfor appen. Serveren har ingen e-postutsending."""
    username = body.username.strip()
    client_ip = request.client.host if request.client else ""
    user = auth.get_user_by_username(username) if username else None
    auth.log_user_activity(
        "/auth/forgot-password",
        user or {"id": None, "username": username or "(tomt)"},
        client_ip,
        allowed=True,
    )
    if user:
        logger.warning("Passordtilbakestilling forespurt for '{}' fra {}", user["username"], client_ip)
    return {
        "message": (
            "Engangskoden lages på serveren av administratoren. Kjør fra repo-roten:\n"
            ".venv/bin/python backend/reset_password.py --user \"<brukernavn>\"\n"
            "Du får 6 siffer som er gyldige i 30 minutter."
        )
    }


@app.post("/auth/reset-password")
def auth_reset_password(body: ResetPasswordRequest):
    """Bytter passord med engangskode fra administratoren."""
    if len(body.new_password or "") < 4:
        raise HTTPException(status_code=400, detail="Nytt passord må ha minst 4 tegn.")
    if not auth.redeem_reset_code(body.username.strip(), body.code, body.new_password):
        raise HTTPException(
            status_code=400,
            detail="Feil eller utløpt kode. Be administratoren om en ny engangskode.",
        )
    return {"ok": True, "message": "Passordet er endret. Du kan logge inn nå."}


@app.get("/admin/users")
def admin_list_users():
    """Returnerer alle registrerte brukere og innsamlet informasjon fra lokal SQL-database."""
    return {"users": auth.list_all_users()}


class ErrorReportRequest(BaseModel):
    error_message: str
    source: str = "app"
    endpoint: str = ""
    status_code: int = 0
    details: str = ""
    username: str = ""


class ResolveErrorRequest(BaseModel):
    error_id: str = "all"


@app.post("/errors/report")
def report_error(body: ErrorReportRequest, request: Request):
    """Mottar feilmeldinger fra mobilappen eller eksterne servere og lagrer dem i lokal SQL."""
    client_ip = request.client.host if request.client else ""
    saved = auth.log_error(
        error_message=body.error_message,
        source=body.source or "app",
        endpoint=body.endpoint,
        status_code=body.status_code,
        details=body.details,
        username=body.username,
        ip_address=client_ip,
    )
    return {"ok": True, "error": saved}


@app.get("/errors")
def get_errors(only_unresolved: bool = False, limit: int = 50):
    """Henter lagrede feilmeldinger fra lokal SQL-database slik at man kan sjekke dem når som helst."""
    items = auth.list_error_logs(only_unresolved=only_unresolved, limit=limit)
    unresolved = [i for i in items if not i.get("resolved")]
    return {
        "unresolved_count": len(unresolved),
        "total_returned": len(items),
        "errors": items,
    }


@app.post("/errors/resolve")
def resolve_errors(body: ResolveErrorRequest):
    """Markerer feilmeldinger i SQL som løst etter at de er fikset."""
    auth.mark_errors_resolved(body.error_id)
    return {"ok": True}


if __name__ == "__main__":
    logger.info("Super-Server lytter på http://{}:{}", HOST, PORT)
    uvicorn.run(app, host=HOST, port=PORT)



