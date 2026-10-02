import json
import os
import secrets
import time
import urllib.error
import urllib.request
from pathlib import Path

import psutil
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from loguru import logger
from pydantic import BaseModel

# Nye moduler: agent med verktøy, minne, database og daglig jobb.
import agent
import auth
import database
import memory
import scheduler

# Last inn backend/.env (GEMINI_API_KEY, DEEPSEEK_API_KEY, API_TOKEN m.m.)
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

PROJECTS_DIR = (BASE_DIR / "projects").resolve()
PROJECTS_DIR.mkdir(parents=True, exist_ok=True)

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "").strip()

# Prøves i rekkefølge. "-latest"-aliasene blir ofte 503 (overbelastet), så vi faller
# videre til neste modell i lista. Overstyr med GEMINI_MODELS i .env, f.eks.:
#   GEMINI_MODELS=gemini-3-flash-preview
_DEFAULT_MODELS = "gemini-flash-latest,gemini-3-flash-preview,gemini-flash-lite-latest"
GEMINI_MODELS = [
    m.strip()
    for m in (os.environ.get("GEMINI_MODELS") or os.environ.get("GEMINI_MODEL") or _DEFAULT_MODELS).split(",")
    if m.strip()
]

# Modellen som svarte sist - prøves først neste gang, så vi slipper 503-runden.
_gemini_last_good: str | None = None

# HTTP-koder der det er verdt å prøve neste modell i stedet for å gi opp.
RETRYABLE_STATUS = {404, 408, 429, 500, 502, 503, 504}

DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat").strip()

# Personlighetene ligger som konstanter, slik at /chat/gemini, /chat/deepseek og
# /chat/collaborate oppfører seg likt.
GEMINI_SYSTEM = (
    "Du er Gemini, en AI-assistent for Ubuntu-serveren til brukeren. "
    "Svar alltid på Norsk. Vær kort, hyggelig og teknisk presis."
)
DEEPSEEK_SYSTEM = (
    "Du er DeepSeek, en AI-assistent for Ubuntu-serveren. "
    "Svar på Norsk. Vær kort og analytisk."
)

# Delt hemmelighet. Appen sender den i headeren "X-API-Token".
API_TOKEN = os.environ.get("API_TOKEN", "").strip()

# Hvilket grensesnitt og port serveren lytter på.
# Tips: sett HOST=100.x.y.z (Tailscale-adressen din) for å lytte KUN på Tailscale,
# da er serveren utilgjengelig fra LAN og internett selv om brannmuren er av.
HOST = os.environ.get("HOST", "0.0.0.0").strip()
PORT = int(os.environ.get("PORT", "8000"))

# Disse kan nås uten token: helsesjekk + innlogging/registrering.
OPEN_PATHS = {"/", "/health", "/auth/login", "/auth/register"}

app = FastAPI(title="Super-Server API", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


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
    logger.info("Token-beskyttelse aktiv ({} tegn).", len(API_TOKEN))


def safe_path(relative: str) -> Path:
    """Løser opp en klient-oppgitt sti og nekter alt som ligger utenfor PROJECTS_DIR."""
    candidate = (PROJECTS_DIR / (relative or "")).resolve()
    if candidate != PROJECTS_DIR and PROJECTS_DIR not in candidate.parents:
        logger.warning("Blokkert sti utenfor projects/: {!r}", relative)
        raise HTTPException(status_code=403, detail="Acceso denegado")
    return candidate


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
    }


@app.get("/stats")
def get_system_stats():
    memory_info = psutil.virtual_memory()
    disk_info = psutil.disk_usage('/')
    
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
        "temp_c": temp_c
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
            file_path = Path(root) / file
            try:
                mtime = file_path.stat().st_mtime
            except OSError:
                continue
            files_with_times.append({
                "path": file_path.relative_to(PROJECTS_DIR).as_posix(),
                "mtime": mtime,
            })

    # Sorter med nyeste først, ta topp 5
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


class ChatRequest(BaseModel):
    message: str = ""


def _post_json(url: str, payload: dict, headers: dict | None = None, timeout: int = 60) -> dict:
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
    """Den modellen som virket sist prøves først, så resten i konfigurert rekkefølge."""
    if _gemini_last_good:
        return [_gemini_last_good] + [m for m in GEMINI_MODELS if m != _gemini_last_good]
    return list(GEMINI_MODELS)


def _ask_gemini(prompt: str) -> tuple[str, str]:
    """Sender prompt til Gemini med modell-fallback. Returnerer (svar, modell)."""
    global _gemini_last_good

    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    problems: list[str] = []
    for model in _gemini_model_order():
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}"
            f":generateContent?key={GEMINI_API_KEY}"
        )
        try:
            result = _post_json(url, payload)
        except urllib.error.HTTPError as e:
            problems.append(f"{model}: {_error_detail(e)}")
            if e.code in RETRYABLE_STATUS:
                # F.eks. 503 "high demand" - prøv neste modell i stedet for å gi opp
                logger.warning("Gemini-modellen {} svarte {} - prøver neste", model, e.code)
                continue
            break
        except Exception as e:
            problems.append(f"{model}: {_error_detail(e)}")
            logger.warning("Gemini-kall mot {} feilet: {}", model, e)
            continue

        candidates = result.get("candidates") or []
        if not candidates:
            problems.append(f"{model}: tomt svar (blokkert av sikkerhetsfilter?)")
            continue
        try:
            text = candidates[0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError, TypeError):
            problems.append(f"{model}: uventet svarstruktur")
            continue

        if model != _gemini_last_good:
            logger.info("Gemini svarte med modell {}", model)
        _gemini_last_good = model
        return text, model

    raise RuntimeError("Ingen Gemini-modell svarte. " + " | ".join(problems[:3]))


def _ask_deepseek(prompt: str) -> tuple[str, str]:
    """Sender prompt til DeepSeek. Returnerer (svar, modell)."""
    url = "https://api.deepseek.com/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
    }
    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": DEEPSEEK_SYSTEM},
            {"role": "user", "content": prompt},
        ],
    }
    result = _post_json(url, payload, headers)
    return result["choices"][0]["message"]["content"], DEEPSEEK_MODEL


@app.post("/chat/gemini")
def chat_gemini(body: ChatRequest):
    user_message = body.message.strip()
    if not user_message:
        return {"reply": "Hei! Jeg er Gemini. Hva kan jeg hjelpe deg med?"}
    if not GEMINI_API_KEY:
        return {"reply": "Gemini Feil: GEMINI_API_KEY mangler i backend/.env"}

    try:
        text, _model = _ask_gemini(f"{GEMINI_SYSTEM}\n\nBruker: {user_message}\nGemini:")
        return {"reply": text}
    except Exception as e:
        logger.error("Gemini feilet: {}", e)
        return {"reply": f"Gemini Feil: {e}"}


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
        return {"reply": f"DeepSeek Feil: {_error_detail(e)}"}


# ---------------------------------------------------------------------------
# Samarbeid mellom de to agentene
#
# Dette er et EGET lag ved siden av /chat/gemini og /chat/deepseek, som begge
# er uendret. Flyten er: den valgte agenten svarer brukeren, den andre
# kontrollerer svaret, og finner den en feil får den første rette opp.
# ---------------------------------------------------------------------------


class CollaborateRequest(BaseModel):
    message: str = ""
    primary: str = "gemini"  # "gemini" eller "deepseek" - hvem som svarer brukeren


def _normalize_agent(name: str) -> str:
    return "deepseek" if (name or "").strip().lower().startswith("deep") else "gemini"


def _agent_label(key: str) -> str:
    return "DeepSeek" if key == "deepseek" else "Gemini"


def _system_prompt(key: str) -> str:
    return DEEPSEEK_SYSTEM if key == "deepseek" else GEMINI_SYSTEM


def _ask_agent(key: str, prompt: str) -> tuple[str, str]:
    """Sender prompt til valgt agent. Returnerer (svar, modell)."""
    if key == "deepseek":
        return _ask_deepseek(prompt)
    return _ask_gemini(prompt)


def _server_context() -> str:
    """Sanntidsstatus fra serveren, som begge agentene får se før de svarer."""
    recent: list[tuple[float, str]] = []
    for root, _dirs, files in os.walk(PROJECTS_DIR):
        for name in files:
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
    # Kan overstyres med HOST/PORT i .env eller som miljøvariabler.
    # F.eks. HOST=100.x.y.z for å lytte kun på Tailscale-adressen.
    logger.info("Super-Server lytter på http://{}:{}", HOST, PORT)
    uvicorn.run(app, host=HOST, port=PORT)
