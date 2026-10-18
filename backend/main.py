import ast
import json
import os
import re
import secrets
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path

try:
    import psutil
    import uvicorn
    from dotenv import load_dotenv
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import JSONResponse
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
    "https://tech-ai.tech/api/v1/chat/completions",
).strip()
TECH_AI_MODEL = os.environ.get("TECH_AI_MODEL", "tech-ai").strip()

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

# HTTP-koder der det er verdt å prøve på nytt på samme modell etter en kort pause.
# 404 betyr at modellen ikke finnes, så da går vi rett videre til neste modell uten å vente.
TRANSIENT_RETRY_STATUS = {408, 429, 500, 502, 503, 504}
NEXT_MODEL_STATUS = {400, 404, 408, 429, 500, 502, 503, 504}

DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat").strip()

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

# Shell- og agent-tilgang.
AGENT_SHELL_ENABLED = os.environ.get("ENABLE_AGENT_SHELL", "1").strip().lower() in {
    "1", "true", "yes", "on",
}
AGENT_SHELL_TIMEOUT = int(os.environ.get("AGENT_SHELL_TIMEOUT", "180"))
MAX_OUTPUT_CHARS = 12000

# Disse kan nås uten token: helsesjekk + innlogging/registrering + feilrapportering fra appen.
OPEN_PATHS = {"/", "/health", "/auth/login", "/auth/register", "/errors/report"}

# Tailscale auto-connect konfigurasjon (leses fra backend/.env)
TAILSCALE_AUTH_KEY = os.environ.get("TAILSCALE_AUTH_KEY", "").strip()
AUTO_CONNECT_TAILSCALE = os.environ.get("AUTO_CONNECT_TAILSCALE", "1").strip().lower() in {"1", "true", "yes", "on"}

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
        if response.status_code >= 400 and request.url.path not in {"/errors/report"}:
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
        auth.log_user_activity(request.url.path, None, client, allowed=False, blocked_reason=state.get("detail", ""))
        auth.log_error(
            error_message=state.get("detail", "Ingen tilgang"),
            source="auth",
            endpoint=f"{request.method} {request.url.path}",
            status_code=status,
            ip_address=client,
        )
        return JSONResponse(status_code=status, content={"detail": state.get("detail", "Ingen tilgang")})

    # 2) Master-token (X-API-Token)
    supplied = request.headers.get("x-api-token", "").strip()
    if _is_valid_token(supplied):
        return await _run_and_log_errors(request, call_next, None, client)

    if not VALID_TOKENS:
        return await _run_and_log_errors(request, call_next, None, client)

    logger.warning("Nektet {} {} fra {} (ugyldig token)", request.method, request.url.path, client)
    auth.log_user_activity(request.url.path, None, client, allowed=False, blocked_reason="Manglende token")
    auth.log_error(
        error_message="Token inválido o ausente",
        source="auth",
        endpoint=f"{request.method} {request.url.path}",
        status_code=401,
        ip_address=client,
    )
    return JSONResponse(status_code=401, content={"detail": "Token inválido o ausente"})


if not API_TOKEN:
    logger.warning(
        "API_TOKEN mangler i backend/.env - serveren er AAPEN for alle som naar {}:{}. "
        "Lag en token med: openssl rand -hex 32",
        HOST, PORT,
    )
else:
    logger.info("Token-beskyttelse aktiv ({} gyldige tokens konfigurert).", len(VALID_TOKENS))


def safe_path(relative: str) -> Path:
    """Løser opp en klient-oppgitt sti og nekter alt som ligger utenfor PROJECTS_DIR."""
    cleaned = (relative or "").strip()
    candidate = (PROJECTS_DIR / cleaned).resolve()
    if candidate != PROJECTS_DIR and PROJECTS_DIR not in candidate.parents:
        logger.warning("Blokkert sti utenfor projects/: {!r}", relative)
        raise HTTPException(status_code=403, detail="Acceso denegado")
    return candidate


def _resolve_agent_workdir(project_str: str, fallback_project: str = "") -> tuple[Path, str, str | None]:
    """Sikrer at agenten aldri krasjer hele oppgaven med HTTP 403 dersom modellen
    foreslår '..' eller en sti utenfor projects/. Returnerer (workdir, rel_project, warning)."""
    raw = (project_str or "").strip().strip("/")
    # Fjern eventuelle innledende "projects/" eller "backend/projects/" hvis modellen inkluderer det
    for prefix in ("backend/projects/", "projects/", "./"):
        if raw.startswith(prefix):
            raw = raw[len(prefix):].strip("/")

    if not raw or raw in {".", ".."} or raw.startswith("../") or "/.." in raw:
        warn = None
        if raw in {".."} or raw.startswith("../") or "/.." in raw:
            warn = (
                f"Merk: Stien '{project_str}' peker utenfor projects/ og ble automatisk "
                f"korrigert til '{fallback_project or '.'}' inne i backend/projects/."
            )
            logger.warning("Agent foreslo ugyldig project={!r}, korrigerer til {!r}", project_str, fallback_project)
        raw = fallback_project

    try:
        workdir = safe_path(raw)
        rel = workdir.relative_to(PROJECTS_DIR).as_posix() if workdir != PROJECTS_DIR else ""
        return workdir, rel, warn
    except HTTPException:
        warn = (
            f"Merk: Stien '{project_str}' er utenfor backend/projects/ og er ikke tillatt. "
            f"Bruker '{fallback_project or '.'}' i stedet."
        )
        try:
            workdir = safe_path(fallback_project)
            rel = workdir.relative_to(PROJECTS_DIR).as_posix() if workdir != PROJECTS_DIR else ""
        except HTTPException:
            workdir = PROJECTS_DIR
            rel = ""
        return workdir, rel, warn


def _sanitize_agent_command(command: str) -> tuple[str, str | None]:
    """Forhindrer at kommandoer henger på interaktiv sudo eller passord-prompt."""
    cmd = command.strip()
    note = None
    # Hvis agenten prøver å bruke sudo, legg til -n så den aldri henger, eller fjern sudo
    # på vanlige filkommandoer inne i projects/.
    if re.search(r"(^|[;&|]\s*)sudo\s+(?!-n\b)", cmd):
        cmd = re.sub(r"(^|[;&|]\s*)sudo\s+", r"\1sudo -n ", cmd)
        note = (
            "Merk: 'sudo' uten passord er ikke tilgjengelig for agenten. "
            "Bruk lokale verktøy/pakker uten sudo (f.eks. python3 -m venv, pip, npm lokalt)."
        )
    return cmd, note


def _run_command(command: str, workdir: Path, timeout: int) -> dict:
    """Kjører en shell-kommando ikke-interaktivt med cwd inne i projects/.
    Dreper hele prosessgruppen ved timeout, og kapper output."""
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

    proc = subprocess.Popen(
        cmd,
        shell=True,
        cwd=str(workdir),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        start_new_session=True,
    )
    timed_out = False
    try:
        out, err = proc.communicate(timeout=timeout)
        code = proc.returncode
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
        out, err = proc.communicate()
        code = 124

    if sudo_note and code != 0:
        err = f"{(err or '').rstrip()}\n{sudo_note}".strip()

    rel = workdir.relative_to(PROJECTS_DIR).as_posix() if workdir != PROJECTS_DIR else ""
    return {
        "exit_code": code,
        "stdout": (out or "")[:MAX_OUTPUT_CHARS],
        "stderr": (err or "")[:MAX_OUTPUT_CHARS],
        "duration_ms": int((time.monotonic() - started) * 1000),
        "timed_out": timed_out,
        "cwd": rel,
    }


def _require_agent_shell() -> None:
    if not AGENT_SHELL_ENABLED:
        raise HTTPException(
            status_code=403,
            detail="Shell-tilgang er av. Sett ENABLE_AGENT_SHELL=1 i backend/.env og restart tjenesten.",
        )


@app.get("/")
def read_root():
    return {"message": "Super-Server API v2.0 funcionando correctamente.", "version": "2.0"}


@app.get("/health")
def health():
    """Åpne http://<server-ip>:8000/health i nettleseren for å se status."""
    return {
        "status": "ok",
        "version": "2.0",
        "listen": f"{HOST}:{PORT}",
        "auth_required": bool(VALID_TOKENS),
        "projects_dir": str(PROJECTS_DIR),
        "projects_dir_exists": PROJECTS_DIR.is_dir(),
        "gemini_models": GEMINI_MODELS,
        "gemini_model_last_ok": _gemini_last_good,
        "gemini_key_configured": bool(GEMINI_API_KEY),
        "deepseek_key_configured": bool(DEEPSEEK_API_KEY),
        "techai_key_configured": bool(TECH_AI_API_KEY),
        "agent_shell_enabled": AGENT_SHELL_ENABLED,
        "tailscale_configured": bool(TAILSCALE_AUTH_KEY),
        "agent_running": _agent_state.get("running", False),
    }


@app.get("/stats")
def get_system_stats():
    memory_info = psutil.virtual_memory()
    disk_info = psutil.disk_usage("/")

    temp_c = 0.0
    if hasattr(psutil, "sensors_temperatures"):
        temps = psutil.sensors_temperatures()
        if temps:
            if "coretemp" in temps and temps["coretemp"]:
                temp_c = temps["coretemp"][0].current
            else:
                first_key = list(temps.keys())[0]
                if temps[first_key]:
                    temp_c = temps[first_key][0].current

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
    for item in sorted(target_path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        if item.name.startswith(".agent_"):
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
        content = target_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="El archivo no es texto UTF-8")
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
    """Kjører en shell-kommando inne i backend/projects/."""
    _require_agent_shell()
    command = body.command.strip()
    if not command:
        raise HTTPException(status_code=400, detail="Tom kommando")

    workdir = safe_path(body.cwd)
    workdir.mkdir(parents=True, exist_ok=True)
    if not workdir.is_dir():
        raise HTTPException(status_code=400, detail="cwd er ikke en mappe")

    where = workdir.relative_to(PROJECTS_DIR).as_posix() or "."
    logger.info("Shell i projects/{}: {}", where, command[:200])
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
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        return f"HTTP {exc.code}: {body[:300]}"
    return str(exc)


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

    for model in _gemini_model_order():
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}"
            f":generateContent?key={GEMINI_API_KEY}"
        )
        result = None
        for attempt in range(2):
            try:
                result = _post_json(url, payload, timeout=35)
                break
            except urllib.error.HTTPError as e:
                detail = _error_detail(e)
                problems.append(f"{model} (forsøk {attempt + 1}): {detail}")
                # Hvis en modell ikke støtter responseMimeType, prøv uten
                if e.code == 400 and json_mode and "responseMimeType" in payload.get("generationConfig", {}):
                    fallback_payload = {
                        "contents": [{"parts": [{"text": prompt}]}],
                        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 8192},
                    }
                    try:
                        result = _post_json(url, fallback_payload, timeout=35)
                        break
                    except Exception as inner_e:
                        problems.append(f"{model} (uten mime): {_error_detail(inner_e)}")
                        break
                if e.code in TRANSIENT_RETRY_STATUS and attempt == 0:
                    logger.warning("Gemini-modellen {} svarte {} - venter 1.2s og prøver igjen", model, e.code)
                    time.sleep(1.2)
                    continue
                if e.code in NEXT_MODEL_STATUS:
                    logger.warning("Gemini-modellen {} svarte {} - prøver neste modell", model, e.code)
                break
            except Exception as e:
                problems.append(f"{model}: {_error_detail(e)}")
                logger.warning("Gemini-kall mot {} feilet: {}", model, e)
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

    raise RuntimeError("Ingen Gemini-modell svarte. " + " | ".join(problems[:4]))


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
            return result["choices"][0]["message"]["content"], DEEPSEEK_MODEL
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

    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Super-Server/2.0",
    }
    if TECH_AI_API_KEY:
        headers["Authorization"] = f"Bearer {TECH_AI_API_KEY}"
        headers["X-API-Key"] = TECH_AI_API_KEY
        headers["x-api-key"] = TECH_AI_API_KEY

    sys_msg = (
        "Du er Tech-AI kodeagent på en Ubuntu-server. Svar kun med gyldig JSON."
        if json_mode
        else TECH_AI_SYSTEM
    )
    payload: dict = {
        "model": TECH_AI_MODEL,
        "messages": [
            {"role": "system", "content": sys_msg},
            {"role": "user", "content": prompt},
        ],
        "message": prompt,
        "prompt": prompt,
        "max_tokens": 4096,
        "temperature": 0.2 if json_mode else 0.5,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    candidate_urls = [
        TECH_AI_API_URL,
        "https://tech-ai.tech/api/chat",
        "https://tech-ai.tech/v1/chat/completions",
        "https://api.tech-ai.tech/v1/chat/completions",
    ]
    seen_urls: list[str] = []
    for u in candidate_urls:
        if u and u not in seen_urls:
            seen_urls.append(u)

    last_err: Exception | None = None
    for url in seen_urls:
        try:
            result = _post_json(url, payload, headers, timeout=30)
            if "choices" in result and result["choices"]:
                msg = result["choices"][0].get("message") or {}
                text = msg.get("content") or result["choices"][0].get("text") or ""
                if text:
                    return str(text), TECH_AI_MODEL
            for key in ("reply", "response", "text", "answer", "content", "output"):
                if key in result and isinstance(result[key], str) and result[key].strip():
                    return result[key].strip(), TECH_AI_MODEL
        except Exception as e:
            last_err = e
            continue

    raise RuntimeError(f"Tech-AI endepunkt svarte ikke ({_error_detail(last_err) if last_err else 'ukjent feil'})")


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

    if TECH_AI_API_KEY or os.environ.get("TECH_AI_API_URL"):
        try:
            text, _model = _ask_techai(user_message)
            return {"reply": text}
        except Exception as e:
            logger.warning("Tech-AI direkte kall feilet ({}), bruker fallback-motor...", _error_detail(e))

    try:
        if DEEPSEEK_API_KEY:
            text, _model = _ask_deepseek(f"{TECH_AI_SYSTEM}\n\nBruker: {user_message}")
            return {"reply": text}
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


# ---------------------------------------------------------------------------
# Samarbeid mellom de to agentene
# ---------------------------------------------------------------------------


class CollaborateRequest(BaseModel):
    message: str = ""
    primary: str = "gemini"  # "gemini", "deepseek" eller "techai"


def _normalize_agent(name: str) -> str:
    low = (name or "").strip().lower()
    if low.startswith("deep"):
        return "deepseek"
    if low.startswith("tech"):
        return "techai"
    if not GEMINI_API_KEY and TECH_AI_API_KEY:
        return "techai"
    return "gemini"


def _agent_label(key: str) -> str:
    if key == "deepseek":
        return "DeepSeek"
    if key == "techai":
        return "Tech-AI"
    return "Gemini"


def _system_prompt(key: str) -> str:
    if key == "deepseek":
        return DEEPSEEK_SYSTEM
    if key == "techai":
        return TECH_AI_SYSTEM
    return GEMINI_SYSTEM


def _ask_agent(key: str, prompt: str, json_mode: bool = False) -> tuple[str, str]:
    """Sender prompt til valgt agent. Hvis hovedagenten er utilgjengelig/overbelastet,
    faller den automatisk tilbake til en av de andre konfigurerte AI-agentene slik at
    oppgaver aldri stopper!"""
    if key == "deepseek":
        try:
            return _ask_deepseek(prompt, json_mode=json_mode)
        except Exception as primary_err:
            if TECH_AI_API_KEY:
                logger.warning("DeepSeek feilet ({}), faller tilbake til Tech-AI...", primary_err)
                return _ask_techai(prompt, json_mode=json_mode)
            if GEMINI_API_KEY:
                logger.warning("DeepSeek feilet ({}), faller tilbake til Gemini...", primary_err)
                return _ask_gemini(prompt, json_mode=json_mode)
            raise
    elif key == "techai":
        try:
            return _ask_techai(prompt, json_mode=json_mode)
        except Exception as primary_err:
            if DEEPSEEK_API_KEY:
                logger.warning("Tech-AI feilet ({}), faller tilbake til DeepSeek...", primary_err)
                return _ask_deepseek(prompt, json_mode=json_mode)
            if GEMINI_API_KEY:
                logger.warning("Tech-AI feilet ({}), faller tilbake til Gemini...", primary_err)
                return _ask_gemini(prompt, json_mode=json_mode)
            raise
    else:
        try:
            return _ask_gemini(prompt, json_mode=json_mode)
        except Exception as primary_err:
            if TECH_AI_API_KEY:
                logger.warning("Gemini feilet ({}), faller tilbake til Tech-AI...", primary_err)
                return _ask_techai(prompt, json_mode=json_mode)
            if DEEPSEEK_API_KEY:
                logger.warning("Gemini feilet ({}), faller tilbake til DeepSeek...", primary_err)
                return _ask_deepseek(prompt, json_mode=json_mode)
            raise


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


AGENT_INSTRUCTIONS = """Du er en utholdende, autonom kodeagent på brukerens Ubuntu-server.
Du jobber i en løkke og SKAL FULLFØRE hele oppgaven før du setter "done": true.
ALDRI gi opp midtveis, ALDRI stopp etter bare å ha opprettet en mappe eller én fil, og ALDRI sett "done": true hvis siste kommando feilet!

VIKTIGE REGLER:
1. Alle stier ("path" og "project") er ALLTID relative til `backend/projects/`. Bruk ALDRI `..` eller stier utenfor prosjektmappen.
2. Ikke bruk interaktive kommandoer eller `sudo` (bruk `python3 -m venv`, lokale filer og porter > 1024).
3. Svar KUN med ett gyldig JSON-objekt (ingen markdown-kodeblokk rundt, ingen ekstra tekst).

JSON-format (velg nøyaktig ett skjema per steg):
- Skrive eller oppdatere fil:
  {"thought": "kort forklaring", "action": "write_file", "path": "filnavn.ext", "content": "full filtekst", "done": false}
- Lese eksisterende fil:
  {"thought": "kort forklaring", "action": "read_file", "path": "filnavn.ext", "done": false}
- Liste filer i mappe:
  {"thought": "kort forklaring", "action": "list_files", "path": "", "done": false}
- Kjøre en shell-kommando (f.eks. opprette venv, installere pakker, kjøre tester, git init/commit):
  {"thought": "kort forklaring", "action": "run_command", "command": "kommando her", "done": false}
- Når HELE oppgaven er bygget, verifisert og testet uten feil:
  {"thought": "alt er testet og verifisert", "action": "finish", "summary": "kort oppsummering på norsk av alt som ble laget og testet", "done": true}
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
        return "(mappen finnes ikke ennå)"
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


@app.post("/chat/agent")
def chat_agent(body: AgentTaskRequest):
    """Autonom kodeagent som jobber steg-for-steg helt til oppgaven er ferdigstilt."""
    _require_agent_shell()
    goal = body.goal.strip()
    if not goal:
        raise HTTPException(status_code=400, detail="Mål (goal) mangler")

    chosen_agent = body.primary or body.agent or "gemini"
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
    workdir.mkdir(parents=True, exist_ok=True)

    steps_log: list[dict] = []
    history_notes: list[str] = []
    if init_warn:
        history_notes.append(init_warn)

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
            recent_history = "\n\n".join(history_notes[-10:]) if history_notes else "(ingen steg utført ennå)"

            prompt = (
                f"{AGENT_INSTRUCTIONS}\n\n"
                f"PROSJEKTMAPPE (relativ til backend/projects/): '{rel_project or '.'}'\n"
                f"FILER I PROSJEKTET NÅ:\n{tree_now}\n\n"
                f"BRUKERENS MÅL:\n{goal}\n\n"
                f"HISTORIKK FRA TIDLIGERE STEG:\n{recent_history}\n\n"
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
                logger.warning("Ugyldig JSON fra {} i steg {}: {!r}", author, step_num, raw_reply[:200])
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

            if action == "finish" or (wants_done and action not in {"write_file", "run_command", "read_file", "list_files"}):
                if last_step_failed and step_num < max_steps - 1:
                    msg = (
                        "Avvist finish: Forrige steg feilet. Rett opp feilen og verifiser at koden "
                        "kjører uten feil før du avslutter!"
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
                rel_file = str(action_obj.get("path") or "").strip().lstrip("/")
                content = str(action_obj.get("content") or "")
                step_entry["target"] = rel_file
                step_entry["command"] = f"write_file {rel_file}"
                if not rel_file or ".." in Path(rel_file).parts:
                    step_entry["ok"] = False
                    step_entry["result"] = f"Ugyldig filsti '{rel_file}'. Bruk en relativ sti inne i prosjektmappen."
                    step_entry["stderr"] = step_entry["result"]
                    last_step_failed = True
                else:
                    target_file = (workdir / rel_file).resolve()
                    if workdir != target_file and workdir not in target_file.parents and PROJECTS_DIR not in target_file.parents:
                        step_entry["ok"] = False
                        step_entry["result"] = f"Stien '{rel_file}' peker utenfor prosjektmappen."
                        step_entry["stderr"] = step_entry["result"]
                        last_step_failed = True
                    else:
                        target_file.parent.mkdir(parents=True, exist_ok=True)
                        target_file.write_text(content, encoding="utf-8")
                        step_entry["result"] = f"Skrev {len(content)} tegn til {rel_file}"
                        step_entry["stdout"] = step_entry["result"]
                        last_step_failed = False

            elif action == "read_file":
                rel_file = str(action_obj.get("path") or "").strip().lstrip("/")
                step_entry["target"] = rel_file
                step_entry["command"] = f"read_file {rel_file}"
                target_file = (workdir / rel_file).resolve()
                if not rel_file or not target_file.is_file() or (workdir not in target_file.parents and target_file != workdir):
                    step_entry["ok"] = False
                    step_entry["result"] = f"Filen '{rel_file}' finnes ikke."
                    step_entry["stderr"] = step_entry["result"]
                    last_step_failed = True
                else:
                    txt = target_file.read_text(encoding="utf-8", errors="replace")[:MAX_OUTPUT_CHARS]
                    step_entry["result"] = txt
                    step_entry["stdout"] = txt
                    last_step_failed = False

            elif action == "list_files":
                rel_sub = str(action_obj.get("path") or "").strip().lstrip("/")
                step_entry["target"] = rel_sub or "."
                step_entry["command"] = f"list_files {rel_sub or '.'}"
                target_dir = (workdir / rel_sub).resolve()
                if not target_dir.is_dir() or (workdir != target_dir and workdir not in target_dir.parents):
                    step_entry["ok"] = False
                    step_entry["result"] = f"Mappen '{rel_sub}' finnes ikke."
                    step_entry["stderr"] = step_entry["result"]
                    last_step_failed = True
                else:
                    snap = _project_tree_snapshot(target_dir)
                    step_entry["result"] = snap
                    step_entry["stdout"] = snap
                    last_step_failed = False

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
                    "write_file, read_file, list_files, run_command, finish."
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
                    "updated_at": time.time(),
                })
                _save_agent_state()

            if wants_done and not last_step_failed and action in {"write_file", "run_command"}:
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
                "updated_at": time.time(),
            })
            _save_agent_state()

        return {
            "goal": goal,
            "project": rel_project,
            "primary": author,
            "model": last_model,
            "done": done,
            "summary": summary,
            "answer": summary,
            "steps": steps_log,
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
        raise HTTPException(status_code=409, detail=str(e))
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
    except Exception:
        raise HTTPException(status_code=401, detail="Ugyldig eller utløpt token.")
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



