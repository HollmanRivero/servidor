"""Agent-loopen: modellen kaller verktøy til oppgaven faktisk er løst.

Flyten for hver brukerforespørsel:
    1. Bygg samtalen (systemprompt + historikk + brukerens melding).
    2. Send til modellen MED verktøy.
    3. Hvis modellen svarer med et verktøykall: utfør det, legg resultatet
       tilbake i samtalen og gjenta.
    4. Når modellen svarer med ren tekst: det er sluttsvaret.
"""
import json
import urllib.error
import urllib.request

from loguru import logger

import config
import memory
import tools

GEMINI_URL_TMPL = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}"
    ":generateContent?key={key}"
)
DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"

RETRYABLE = {404, 408, 429, 500, 502, 503, 504}

_gemini_last_good = None


def _post_json(url: str, payload: dict, headers: dict | None = None, timeout: int = 120) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _err(exc: Exception) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        return f"HTTP {exc.code}: {body[:300]}"
    return str(exc)


def _gemini_order() -> list[str]:
    if _gemini_last_good:
        return [_gemini_last_good] + [m for m in config.GEMINI_MODELS if m != _gemini_last_good]
    return list(config.GEMINI_MODELS)


def _gemini_call(payload: dict) -> tuple[dict, str]:
    """Sender et ferdig payload til Gemini med modell-fallback."""
    global _gemini_last_good
    problems: list[str] = []
    for model in _gemini_order():
        url = GEMINI_URL_TMPL.format(model=model, key=config.GEMINI_API_KEY)
        try:
            result = _post_json(url, payload)
        except urllib.error.HTTPError as e:
            problems.append(f"{model}: {_err(e)}")
            if e.code in RETRYABLE:
                continue
            break
        except Exception as e:
            problems.append(f"{model}: {_err(e)}")
            continue

        candidates = result.get("candidates") or []
        if not candidates:
            problems.append(f"{model}: tomt svar")
            continue
        _gemini_last_good = model
        return result, model

    raise RuntimeError("Ingen Gemini-modell svarte. " + " | ".join(problems[:3]))


def _deepseek_call(payload: dict) -> tuple[dict, str]:
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {config.DEEPSEEK_API_KEY}",
    }
    result = _post_json(DEEPSEEK_URL, payload, headers)
    return result, config.DEEPSEEK_MODEL


# --------------------------------------------------------------------------
# Agent-loop: Gemini
# --------------------------------------------------------------------------

def _agent_gemini(user_text: str, history: list[dict], trace: list[dict]) -> dict:
    contents: list[dict] = []
    for h in history:
        role = "user" if h.get("role") == "user" else "model"
        contents.append({"role": role, "parts": [{"text": h.get("content", "")}]})
    contents.append({"role": "user", "parts": [{"text": user_text}]})

    decls = tools.gemini_function_declarations()

    for _ in range(config.MAX_AGENT_STEPS):
        payload = {
            "systemInstruction": {"parts": [{"text": config.AGENT_SYSTEM}]},
            "contents": contents,
            "tools": [{"functionDeclarations": decls}],
            "toolConfig": {"functionCallingConfig": {"mode": "AUTO"}},
        }
        result, model = _gemini_call(payload)
        cand = result["candidates"][0]
        parts = cand.get("content", {}).get("parts", []) or []

        calls = [p.get("functionCall") for p in parts if p.get("functionCall")]
        if not calls:
            text = "".join(p.get("text", "") for p in parts if p.get("text"))
            return {"reply": text or "(tomt svar)", "model": model, "tool_calls": trace}

        contents.append({"role": "model", "parts": parts})
        responses = []
        for call in calls:
            name = call.get("name", "")
            args = call.get("args") or {}
            result_str = tools.execute_tool(name, args)
            trace.append({"tool": name, "arguments": args, "result": result_str[:500]})
            responses.append(
                {"functionResponse": {"name": name, "response": {"result": result_str}}}
            )
        contents.append({"role": "tool", "parts": responses})

    return {
        "reply": f"Agenten brukte {config.MAX_AGENT_STEPS} verktøysteg uten å avslutte.",
        "model": None,
        "tool_calls": trace,
    }


# --------------------------------------------------------------------------
# Agent-loop: DeepSeek
# --------------------------------------------------------------------------

def _agent_deepseek(user_text: str, history: list[dict], trace: list[dict]) -> dict:
    messages: list[dict] = [{"role": "system", "content": config.AGENT_SYSTEM}]
    for h in history:
        role = h.get("role")
        if role in ("user", "assistant"):
            messages.append({"role": role, "content": h.get("content", "")})
    messages.append({"role": "user", "content": user_text})

    for _ in range(config.MAX_AGENT_STEPS):
        payload = {
            "model": config.DEEPSEEK_MODEL,
            "messages": messages,
            "tools": tools.openai_tools(),
        }
        result, model = _deepseek_call(payload)
        msg = result["choices"][0]["message"]
        tool_calls = msg.get("tool_calls") or []

        if not tool_calls:
            return {
                "reply": msg.get("content") or "(tomt svar)",
                "model": model,
                "tool_calls": trace,
            }

        messages.append(
            {"role": "assistant", "content": msg.get("content"), "tool_calls": tool_calls}
        )
        for tc in tool_calls:
            fn = tc.get("function", {})
            name = fn.get("name", "")
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            result_str = tools.execute_tool(name, args)
            trace.append({"tool": name, "arguments": args, "result": result_str[:500]})
            messages.append(
                {"role": "tool", "tool_call_id": tc.get("id", ""), "content": result_str}
            )

    return {
        "reply": f"Agenten brukte {config.MAX_AGENT_STEPS} verktøysteg uten å avslutte.",
        "model": None,
        "tool_calls": trace,
    }


# --------------------------------------------------------------------------
# Offentlig inngang
# --------------------------------------------------------------------------

def run(provider: str, message: str, conversation_id: str | None = None) -> dict:
    """Kjører agenten med minne. Returnerer svar + hvilke verktøy som ble brukt."""
    provider = "deepseek" if (provider or "").strip().lower().startswith("deep") else "gemini"

    conv_id = memory.ensure_conversation(conversation_id)
    hist = memory.history(conv_id, limit=20)

    if provider == "deepseek" and not config.DEEPSEEK_API_KEY:
        return {
            "conversation_id": conv_id,
            "reply": "DeepSeek Feil: DEEPSEEK_API_KEY mangler i backend/.env",
            "model": None,
            "provider": "deepseek",
            "tool_calls": [],
        }
    if provider == "gemini" and not config.GEMINI_API_KEY:
        return {
            "conversation_id": conv_id,
            "reply": "Gemini Feil: GEMINI_API_KEY mangler i backend/.env",
            "model": None,
            "provider": "gemini",
            "tool_calls": [],
        }

    trace: list[dict] = []
    try:
        if provider == "deepseek":
            out = _agent_deepseek(message, hist, trace)
        else:
            out = _agent_gemini(message, hist, trace)
    except Exception as e:
        logger.error("Agent ({}) feilet: {}", provider, e)
        out = {"reply": f"Agent Feil: {_err(e)}", "model": None, "tool_calls": trace}

    memory.append_message(conv_id, "user", message)
    memory.append_message(conv_id, "assistant", out.get("reply", ""))

    return {
        "conversation_id": conv_id,
        "reply": out.get("reply", ""),
        "model": out.get("model"),
        "provider": provider,
        "tool_calls": out.get("tool_calls", []),
    }
