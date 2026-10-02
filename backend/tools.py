"""Verktøyene agenten kan kalle, med JSON-skjemaer for tool-calling.

Hvert verktøy utfører en faktisk handling på serveren og returnerer resultatet
som en JSON-streng. Dette er laget som gjør at AI-en kan "gjøre" ting i stedet
for bare å svare med tekst.
"""
import base64
import json
import os
import re
import shutil
import subprocess

from loguru import logger

import config
import memory


# --------------------------------------------------------------------------
# Sikkerhet for shell-kommandoer
# --------------------------------------------------------------------------

# Kommandoer som aldri skal kjøres, selv om AI-en ber om det.
DENIED = [
    "rm -rf /",
    "mkfs",
    "dd if=",
    "shutdown",
    "reboot",
    "halt",
    "poweroff",
    "> /dev/sd",
    ":(){ ",
]

# Git skal aldri stoppe og spoerre om brukernavn/passord. Gjoer den det,
# henger verktoykallet til det timer ut - og AI-en tror at kloning "ikke virker".
GIT_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_ASKPASS": "/bin/true",
    "GCM_INTERACTIVE": "never",
}

# Adresser agenten faar klone fra. Lokale stier og file:// er bevisst utelatt.
CLONE_URL_PREFIXES = ("https://", "http://", "git://", "ssh://", "git@")


def _git_env() -> dict:
    """Miljo for git-kall, med GitHub-token hvis den finnes.

    Tokenen sendes som HTTP-header via GIT_CONFIG_*-variabler, ikke i URL-en og
    ikke paa kommandolinjen. Da havner den ikke i 'ps', i .git/config eller i
    loggene vaare.
    """
    env = {**os.environ, **GIT_ENV}
    if config.GITHUB_TOKEN and "GIT_CONFIG_COUNT" not in env:
        creds = base64.b64encode(
            f"x-access-token:{config.GITHUB_TOKEN}".encode("utf-8")
        ).decode("ascii")
        env.update(
            {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
                "GIT_CONFIG_VALUE_0": f"Authorization: Basic {creds}",
            }
        )
    return env


_CREDENTIALS_RE = re.compile(r"(https?://)[^/@\s]+:[^/@\s]+@")


def _redact(text: str) -> str:
    """Fjerner brukernavn:passord fra URL-er, saa tokenen ikke havner i loggen."""
    return _CREDENTIALS_RE.sub(r"\1***:***@", text or "")


def _git_auth_hint(text: str) -> str | None:
    """Forklarer git-feil som egentlig betyr 'privat repo uten tilgang'."""
    low = (text or "").lower()
    if not any(
        s in low
        for s in (
            "could not read username",
            "could not read password",
            "authentication failed",
            "repository not found",
            "permission denied (publickey)",
        )
    ):
        return None
    if config.GITHUB_TOKEN:
        return (
            "Git-tilgang feilet selv om GITHUB_TOKEN er satt: sjekk at tokenen er "
            "gyldig, ikke utlopt, og har 'Contents: Read' paa repoet."
        )
    return (
        "Repoen er privat. Sett GITHUB_TOKEN i backend/.env med en fine-grained PAT "
        "som har 'Contents: Read'."
    )


def _safe_path(relative: str):
    """Løser en sti og nekter alt utenfor prosjektmappen."""
    from pathlib import Path

    candidate = (config.PROJECTS_DIR / (relative or "")).resolve()
    if candidate != config.PROJECTS_DIR and config.PROJECTS_DIR not in candidate.parents:
        raise ValueError(f"Sti utenfor prosjektmappen er ikke tillatt: {relative!r}")
    return Path(candidate)


def _run_command(command: str, cwd: str = "", timeout: int = 60) -> dict:
    cmd = (command or "").strip()
    if not cmd:
        return {"ok": False, "error": "Tom kommando."}
    for prefix in DENIED:
        if cmd.startswith(prefix):
            return {"ok": False, "error": f"Kommandoen er blokkert av sikkerhet ({prefix!r})."}

    if timeout > 300:
        timeout = 300
    if timeout <= 0:
        timeout = 60

    base = config.PROJECTS_DIR
    if cwd:
        try:
            base = _safe_path(cwd)
        except ValueError as e:
            return {"ok": False, "error": str(e)}

    logger.info("Kjører kommando: {}", _redact(cmd))
    try:
        proc = subprocess.run(
            cmd,
            shell=True,
            cwd=str(base),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_git_env(),
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"Tidsavbrudd etter {timeout}s."}
    except Exception as e:
        return {"ok": False, "error": _redact(str(e))}

    stderr = _redact((proc.stderr or "").strip())[:2000]
    stdout = _redact((proc.stdout or "").strip())[:4000]
    result = {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout": stdout,
        "stderr": stderr,
    }
    if proc.returncode != 0:
        low = stderr.lower()
        if "not found" in low or "not recognized as an internal or external command" in low:
            result["hint"] = (
                "Kommandoen finnes ikke paa serveren. For git: sudo apt install -y git"
            )
        else:
            hint = _git_auth_hint(stderr)
            if hint:
                result["hint"] = hint
    return result


def _read_file(path: str) -> dict:
    try:
        p = _safe_path(path)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    if not p.is_file():
        return {"ok": False, "error": "Filen finnes ikke."}
    try:
        return {"ok": True, "content": p.read_text(encoding="utf-8")[:8000]}
    except UnicodeDecodeError:
        return {"ok": False, "error": "Filen er ikke UTF-8 tekst."}


def _write_file(path: str, content: str) -> dict:
    try:
        p = _safe_path(path)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content or "", encoding="utf-8")
    return {"ok": True, "message": f"Lagret {p}"}


def _clone_repo(url: str, name: str = "", branch: str = "") -> dict:
    """Kloner et git-repo inn i prosjektmappen - aldri utenfor."""
    url = (url or "").strip()
    if not url:
        return {"ok": False, "error": "Mangler 'url'."}
    if not url.startswith(CLONE_URL_PREFIXES):
        return {
            "ok": False,
            "error": "Bare http(s)://, git://, ssh:// eller git@-adresser er tillatt.",
        }
    if shutil.which("git") is None:
        return {"ok": False, "error": "git er ikke installert. Kjoer: sudo apt install -y git"}

    folder = (name or "").strip() or url.rstrip("/").split("/")[-1]
    if folder.endswith(".git"):
        folder = folder[:-4]
    if not folder or folder in {".", ".."}:
        return {"ok": False, "error": f"Kunne ikke utlede mappenavn fra {url!r}. Oppgi 'name'."}

    try:
        target = _safe_path(folder)
    except ValueError as e:
        return {"ok": False, "error": str(e)}

    if target.exists() and any(target.iterdir()):
        return {"ok": False, "error": f"'{folder}' finnes allerede og er ikke tom."}

    args = ["git", "clone", "--depth", "1", "--single-branch"]
    if branch:
        args += ["--branch", branch]
    args += ["--", url, str(target)]

    logger.info("Kloner {} -> {}", _redact(url), target)
    try:
        proc = subprocess.run(
            args,
            cwd=str(config.PROJECTS_DIR),
            capture_output=True,
            text=True,
            timeout=180,
            env=_git_env(),
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "Tidsavbrudd etter 180s. Er serveren paa nett?"}
    except PermissionError as e:
        return {"ok": False, "error": f"Ingen skriverett i prosjektmappen: {e}"}
    except Exception as e:
        return {"ok": False, "error": _redact(str(e))}

    if proc.returncode != 0:
        err = _redact((proc.stderr or proc.stdout or "").strip())[:1000] or "git clone feilet."
        result = {"ok": False, "returncode": proc.returncode, "error": err}
        hint = _git_auth_hint(err)
        if hint:
            result["hint"] = hint
        return result

    files = sorted(p.name + ("/" if p.is_dir() else "") for p in target.iterdir())[:25]
    return {
        "ok": True,
        "path": folder,
        "message": f"Klonet til prosjektmappen/{folder}",
        "files": files,
    }


def _list_projects(path: str = "") -> dict:
    try:
        p = _safe_path(path)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    if not p.is_dir():
        return {"ok": False, "error": "Ikke en mappe."}
    items = []
    for child in sorted(p.iterdir(), key=lambda c: (not c.is_dir(), c.name.lower()))[:200]:
        items.append({"name": child.name, "is_dir": child.is_dir()})
    return {"ok": True, "items": items}


def _get_stats() -> dict:
    import psutil

    mem = psutil.virtual_memory()
    return {
        "ok": True,
        "cpu_percent": psutil.cpu_percent(interval=0.1),
        "ram_percent": mem.percent,
        "ram_used_mb": mem.used // (1024 * 1024),
        "disk_percent": psutil.disk_usage("/").percent,
    }


# --------------------------------------------------------------------------
# Utfører et verktøy
# --------------------------------------------------------------------------

def execute_tool(name: str, arguments: dict) -> str:
    """Kjører et verktøy og returnerer resultatet som en JSON-streng."""
    try:
        args = arguments or {}
        if name == "run_command":
            res = _run_command(
                args.get("command", ""),
                args.get("cwd", ""),
                int(args.get("timeout_seconds") or 60),
            )
        elif name == "read_file":
            res = _read_file(args.get("path", ""))
        elif name == "write_file":
            res = _write_file(args.get("path", ""), args.get("content", ""))
        elif name == "list_projects":
            res = _list_projects(args.get("path", ""))
        elif name == "clone_repo":
            res = _clone_repo(
                args.get("url", ""), args.get("name", ""), args.get("branch", "")
            )
        elif name == "get_stats":
            res = _get_stats()
        elif name == "remember":
            memory.remember(args.get("key", ""), args.get("content", ""), args.get("kind", "note"))
            res = {"ok": True, "message": f"Lagret '{args.get('key')}' i minnet."}
        elif name == "recall":
            res = {"items": memory.recall(args.get("query"))}
        elif name == "create_task":
            tid = memory.create_task(args.get("title", ""), args.get("description", ""))
            res = {"ok": True, "task_id": tid}
        elif name == "list_tasks":
            res = {"items": memory.list_tasks()}
        elif name == "complete_task":
            memory.update_task(args.get("task_id", ""), args.get("status", "done"), args.get("result", ""))
            res = {"ok": True}
        else:
            res = {"ok": False, "error": f"Ukjent verktøy: {name}"}
        return json.dumps(res, ensure_ascii=False, default=str)
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)


# --------------------------------------------------------------------------
# Verktøy-skjemaer for tool-calling
# --------------------------------------------------------------------------

TOOLS = [
    {
        "name": "run_command",
        "description": (
            "Kjør en shell-kommando på Ubuntu-serveren og få stdout/stderr/returncode tilbake. "
            "Bruk denne for å faktisk utføre oppgaver på serveren. "
            "Skal du klone et git-repo: bruk clone_repo i stedet."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell-kommandoen som skal kjøres."},
                "cwd": {"type": "string", "description": "Arbeidsmappe (relativt til prosjektmappen). Valgfri."},
                "timeout_seconds": {"type": "integer", "description": "Tidsavbrudd i sekunder. Standard 60."},
            },
            "required": ["command"],
        },
    },
    {
        "name": "read_file",
        "description": "Les innholdet av en tekstfil i prosjektmappen.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Sti relativt til prosjektmappen."}},
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Opprett eller overskriv en fil i prosjektmappen.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Sti relativt til prosjektmappen."},
                "content": {"type": "string", "description": "Innholdet som skal skrives."},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "list_projects",
        "description": "List filer og mapper i prosjektmappen (eller en undermappe).",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Undermappe. Tom = rot."}},
        },
    },
    {
        "name": "clone_repo",
        "description": (
            "Klon et git-repo ned i prosjektmappen (backend/projects/<navn>). "
            "Bruk denne i stedet for 'git clone' via run_command - den er raskere, "
            "henger aldri paa passordsporsmaal, og havner alltid riktig sted."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "Repo-adresse, f.eks. https://github.com/bruker/repo.git",
                },
                "name": {
                    "type": "string",
                    "description": "Mappenavn under prosjektmappen. Tom = utledes fra url.",
                },
                "branch": {
                    "type": "string",
                    "description": "Grenen som skal hentes. Tom = repoenes standardgren.",
                },
            },
            "required": ["url"],
        },
    },
    {
        "name": "get_stats",
        "description": "Hent serverens status: CPU, RAM og diskbruk.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "remember",
        "description": "Lagre et faktum, en preferanse eller et notat i langtidsminnet.",
        "parameters": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "Kort nøkkel, f.eks. 'bruker_preferanse'."},
                "content": {"type": "string", "description": "Innholdet som skal huskes."},
                "kind": {"type": "string", "description": "fact, note eller preference. Standard note."},
            },
            "required": ["key", "content"],
        },
    },
    {
        "name": "recall",
        "description": "Søk i langtidsminnet etter tidligere lagrede fakta/notater.",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "Søkeord. Tom = siste notater."}},
        },
    },
    {
        "name": "create_task",
        "description": "Opprett en ny jobb i jobb-oversikten.",
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Kort tittel på jobben."},
                "description": {"type": "string", "description": "Detaljer om jobben."},
            },
            "required": ["title"],
        },
    },
    {
        "name": "list_tasks",
        "description": "Vis alle jobber og deres status.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "complete_task",
        "description": "Marker en jobb som ferdig eller feilet.",
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "ID-en fra create_task/list_tasks."},
                "status": {"type": "string", "description": "done eller failed. Standard done."},
                "result": {"type": "string", "description": "Kort oppsummering av resultatet."},
            },
            "required": ["task_id"],
        },
    },
]


def _upper(schema):
    """Gjør OpenAI-stil typeskjema om til Geminis STORE BOKSTAVER."""
    if isinstance(schema, dict):
        return {k: _upper(v) for k, v in schema.items()}
    if isinstance(schema, list):
        return [_upper(v) for v in schema]
    if isinstance(schema, str):
        return {
            "object": "OBJECT",
            "string": "STRING",
            "array": "ARRAY",
            "integer": "INTEGER",
            "number": "NUMBER",
            "boolean": "BOOLEAN",
        }.get(schema, schema)
    return schema


def openai_tools():
    """DeepSeek / OpenAI-format: [{type: 'function', function: {...}}]."""
    return [{"type": "function", "function": t} for t in TOOLS]


def gemini_function_declarations():
    """Gemini-format: liste av {name, description, parameters}."""
    return [
        {"name": t["name"], "description": t["description"], "parameters": _upper(t["parameters"])}
        for t in TOOLS
    ]
