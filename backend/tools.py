"""Verktøyene agenten kan kalle, med JSON-skjemaer for tool-calling.

Hvert verktøy utfører en faktisk handling på serveren og returnerer resultatet
som en JSON-streng. Dette er laget som gjør at AI-en kan "gjøre" ting i stedet
for bare å svare med tekst.
"""
import json
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

    logger.info("Kjører kommando: {}", cmd)
    try:
        proc = subprocess.run(
            cmd, shell=True, cwd=str(base), capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"Tidsavbrudd etter {timeout}s."}
    except Exception as e:
        return {"ok": False, "error": str(e)}

    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout": (proc.stdout or "").strip()[:4000],
        "stderr": (proc.stderr or "").strip()[:2000],
    }


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
            "Bruk denne for å faktisk utføre oppgaver på serveren."
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
