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

# Nye moduler: agent med verktøy, minne, database og daglig jobb.
import agent
import auth
import database
import memory
import scheduler

# Last inn backend/.env (GEMINI_API_KEY, DEEPSEEK_API_KEY, API_TOKEN m.m.)
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env", override=True)

PROJECTS_DIR = (BASE_DIR / "projects").resolve()
PROJECTS_DIR.mkdir(parents=True, exist_ok=True)

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

# Disse kan nås uten token: helsesjekk + innlogging/registrering.
OPEN_PATHS = {"/", "/health", "/auth/login", "/auth/register"}

# Tailscale auto-connect konfigurasjon (leses fra backend/.env)
TAILSCALE_AUTH_KEY = os.environ.get("TAILSCALE_AUTH_KEY", "").strip()
AUTO_CONNECT_TAILSCALE = os.environ.get("AUTO_CONNECT_TAILSCALE", "1").strip().lower() in {"1", "true", "yes", "on"}


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
    """Kjører Tailscale-tilkoblingen asynkront i bakgrunnen ved oppstart for ikke å blokkere serveren."""
    if AUTO_CONNECT_TAILSCALE:
        threading.Thread(target=_connect_tailscale, daemon=True, name="tailscale-connect").start()
    yield


app = FastAPI(title="Super-Server API", version="1.1.0", lifespan=lifespan)
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


@app.middleware("http")
async def check_token(request: Request, call_next):
    """Adgangskontroll: master-token (X-API-Token) ELLER JWT fra /auth/login."""
    if request.method == "OPTIONS" or request.url.path in OPEN_PATHS:
        return await call_next(request)

    client = request.client.host if request.client else "?"

    # 1) Master-token: den delte hemmeligheten appen sender i X-API-Token.
    supplied = request.headers.get("x-api-token", "").strip()
    if API_TOKEN and supplied and secrets.compare_digest(supplied.encode("utf-8"), API_TOKEN.encode("utf-8")):
        return await call_next(request)

    # 2) JWT (Bearer) fra /auth/login - eller master-token sendt som Bearer.
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        token = auth_header[7:].strip()
        if API_TOKEN and token and secrets.compare_digest(token.encode("utf-8"), API_TOKEN.encode("utf-8")):
            return await call_next(request)
        state = auth.check_token(token)
        if state.get("ok"):
            return await call_next(request)
        status = state.get("status", 401)
        return JSONResponse(status_code=status, content={"detail": state.get("detail", "Ingen tilgang")})

    if not API_TOKEN:
        # Ingen token konfigurert - åpen server (advart ved oppstart).
        return await call_next(request)

    logger.warning("Nektet {} {} fra {} (manglende/ugyldig token)", request.method, request.url.path, client)
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
    return {"message": "Super-Server API funcionando correctamente."}


@app.get("/health")
def health():
    """Åpne http://<server-ip>:8000/health i nettleseren for å se status."""
    return {
        "status": "ok",
        "listen": f"{HOST}:{PORT}",
        "auth_required": bool(API_TOKEN),
        "projects_dir": str(PROJECTS_DIR),
        "projects_dir_exists": PROJECTS_DIR.is_dir(),
        "gemini_models": GEMINI_MODELS,
        "gemini_model_last_ok": _gemini_last_good,
        "gemini_key_configured": bool(GEMINI_API_KEY),
        "deepseek_key_configured": bool(DEEPSEEK_API_KEY),
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

    headers = {"Content-Type": "application/json"}
    if TECH_AI_API_KEY:
        headers["Authorization"] = f"Bearer {TECH_AI_API_KEY}"
        headers["X-API-Key"] = TECH_AI_API_KEY

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

    result = _post_json(TECH_AI_API_URL, payload, headers, timeout=45)
    if "choices" in result and result["choices"]:
        msg = result["choices"][0].get("message") or {}
        text = msg.get("content") or result["choices"][0].get("text") or ""
        return str(text), TECH_AI_MODEL
    for key in ("reply", "response", "text", "answer", "content"):
        if key in result and isinstance(result[key], str):
            return result[key], TECH_AI_MODEL
    raise RuntimeError(f"Uventet svar fra Tech-AI: {str(result)[:200]}")


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
        return {"reply": "Gemini Feil: GEMINI_API_KEY mangler i backend/.env"}

    try:
        text, _model = _ask_gemini(f"{GEMINI_SYSTEM}\n\nBruker: {user_message}\nGemini:")
        return {"reply": text}
    except Exception as e:
        logger.error("Gemini feilet: {}", e)
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

    try:
        if TECH_AI_API_KEY or os.environ.get("TECH_AI_API_URL"):
            text, _model = _ask_techai(user_message)
            return {"reply": text}
        if GEMINI_API_KEY:
            text, _model = _ask_gemini(f"{TECH_AI_SYSTEM}\n\nBruker: {user_message}\nTech-AI:")
            return {"reply": text}
        if DEEPSEEK_API_KEY:
            text, _model = _ask_deepseek(f"{TECH_AI_SYSTEM}\n\nBruker: {user_message}")
            return {"reply": text}
        return {"reply": "Tech-AI Feil: Legg inn TECH_AI_API_KEY i backend/.env"}
    except Exception as e:
        logger.error("Tech-AI feilet: {}", e)
        return {"reply": f"Tech-AI Feil: {_error_detail(e)}"}


@app.post("/chat/deepseek")
def chat_deepseek(body: ChatRequest):
    user_message = body.message.strip()
    if not user_message:
        return {"reply": "Hei! Jeg er DeepSeek. Hva kan jeg hjelpe deg med?"}
    if not DEEPSEEK_API_KEY:
        return {"reply": "DeepSeek Feil: DEEPSEEK_API_KEY mangler i backend/.env"}

    try:
        text, _model = _ask_deepseek(user_message)
        return {"reply": text}
    except Exception as e:
        logger.error("DeepSeek feilet: {}", e)
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
# Autentisering (Fase 1: bcrypt + JWT)
# ---------------------------------------------------------------------------


class RegisterRequest(BaseModel):
    username: str
    password: str
    email: str | None = None


class LoginRequest(BaseModel):
    username: str
    password: str


@app.post("/auth/register")
def auth_register(body: RegisterRequest):
    """Registrerer en bruker. Den aller første brukeren blir automatisk admin."""
    username = body.username.strip()
    if not username or len(body.password) < 6:
        raise HTTPException(status_code=400, detail="Brukernavn påkrevd og passord minst 6 tegn.")
    try:
        role = "admin" if not auth.any_admin() else "user"
        user = auth.create_user(username, body.password, body.email, role=role)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {"id": user["id"], "username": user["username"], "role": user["role"]}


@app.post("/auth/login")
def auth_login(body: LoginRequest):
    """Logger inn og returnerer et JWT som brukes i Authorization: Bearer <token>."""
    user = auth.login(body.username.strip(), body.password)
    if not user:
        raise HTTPException(status_code=401, detail="Feil brukernavn eller passord.")
    if bool(user.get("is_banned")):
        raise HTTPException(status_code=403, detail=f"Bannet: {user.get('ban_reason') or 'ingen grunn'}")
    state, reason = auth.access_state(user)
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
    """Returnerer informasjon om den innloggede brukeren (krever Bearer JWT)."""
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


# ---------------------------------------------------------------------------
# Agent med verktøy + minne + jobber
#
# Dette er laget som gir AI-en faktiske evner: kjøre kommandoer, lese/skrive
# filer, huske ting og holde oversikt over jobber. Appen bruker /agent i
# stedet for de rene /chat/*-endepunktene når den skal "gjøre" noe.
# ---------------------------------------------------------------------------


class AgentRequest(BaseModel):
    message: str = ""
    provider: str = "gemini"       # "gemini" eller "deepseek"
    conversation_id: str | None = None


@app.post("/agent")
def agent_chat(body: AgentRequest):
    """Agent-loopen: AI-en velger verktøy, utfører dem og svarer med hva den gjorde."""
    if not body.message.strip():
        return {
            "conversation_id": body.conversation_id,
            "reply": "Hei! Hva vil du at jeg skal gjøre på serveren?",
            "model": None,
            "provider": body.provider,
            "tool_calls": [],
        }
    return agent.run(body.provider, body.message, body.conversation_id)


@app.get("/tasks")
def tasks_list():
    """Alle jobbene agenten har opprettet, nyeste først."""
    return {"items": memory.list_tasks()}


@app.get("/memory")
def memory_list(query: str = ""):
    """Fakta og notater agenten har lagret. Bruk ?query= for å søke."""
    return {"items": memory.recall(query or None)}


@app.post("/run_daily")
def run_daily_now():
    """Kjører den daglige vedlikeholdsjobben med en gang (for testing)."""
    return scheduler.run_daily_job()


@app.on_event("startup")
def _on_startup():
    database.init_db()
    scheduler.start_scheduler()


if __name__ == "__main__":
    logger.info("Super-Server lytter på http://{}:{}", HOST, PORT)
    uvicorn.run(app, host=HOST, port=PORT)
