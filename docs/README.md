# AI agents in the servidor app

How the AI agents are implemented, how the Android app talks to them, and how to
add or replace an AI of your own.

This repo ships three agents (Gemini, DeepSeek, Tech-AI) wired into one server.
The "clean" template repo ships the same server **without** agent entries, so you
can add exactly the AI you use. See [Two repositories](#two-repositories).

---

## 1. The idea in one paragraph

The Android app is a thin client. It sends `{"message": "..."}` and renders
`{"reply": "..."}`. **All model calls happen on the server**, which is why you can
change AI providers without rebuilding the app. The server keeps the API keys,
picks a model, retries and falls back, and returns plain text.

```
Android app  ──POST /chat/deepseek──►  FastAPI (backend/main.py)
   (Kotlin or WebView)                       │
   X-API-Token: <API_TOKEN>                  ├─ _ask_deepseek() ──► api.deepseek.com
                                             ├─ _ask_gemini()   ──► generativelanguage...
                                             └─ _ask_techai()   ──► tech-ai.tech
                                                    │
                                             AGENT_PROVIDERS registry
                                             (who exists, who is configured,
                                              who to fall back to)
```

---

## 2. The three built-in agents

| key | label | env var | endpoint | default model |
|---|---|---|---|---|
| `gemini` | Gemini | `GEMINI_API_KEY` | `POST /chat/gemini` | first working of `GEMINI_MODELS` |
| `deepseek` | DeepSeek | `DEEPSEEK_API_KEY` | `POST /chat/deepseek` | `DEEPSEEK_MODEL` (`deepseek-flash`) |
| `techai` | Tech-AI | `TECH_AI_API_KEY` | `POST /chat/techai` | `TECH_AI_MODEL` (`tech-ai-monkey`) |

Each one has a system prompt constant near the top of `backend/main.py`:

```python
GEMINI_SYSTEM   = "Du er Gemini, en AI-assistent for Ubuntu-serveren ..."
DEEPSEEK_SYSTEM = "Du er DeepSeek, en AI-assistent for Ubuntu-serveren ..."
TECH_AI_SYSTEM  = "Du er Tech-AI (tech-ai.tech), en selvlærende bygge- og kodeagent ..."
```

Edit those strings to change an agent's personality. They are prepended to every
request, so the model always knows it is talking to a server agent with file and
shell access.

### The per-provider call function

Every agent is one function with the same signature:

```python
def _ask_gemini(prompt: str, json_mode: bool = False) -> tuple[str, str]:
    ...
    return text, model_name        # (svaret, hvilken modell som svarte)
```

`json_mode=True` is used by the agent/collaborate paths, which need a strict JSON
answer. Normal chat uses `json_mode=False`.

**Gemini** (`_ask_gemini`) is the interesting one: `GEMINI_MODELS` is a fallback
list. It tries each model in order until one answers. If a model returns
`429` (quota), `503` (overloaded), `404` or a timeout, the model is put on
**cooldown** and skipped for the next requests instead of being retried:

```python
_gemini_cooldown[model] = (time.monotonic() + seconds, reason)
```

Cooldown length comes from Google's own `RetryInfo.retryDelay` on a 429, so the
server stops hammering a model that is dead until tomorrow. You can see the state
in `/health` → `gemini_model_cooldowns`.

> **Free tier reality check.** Gemini's free tier is **20 requests per day per
> model** (`GenerateRequestsPerDayPerProjectPerModel-FreeTier`). With four models
> that is ~80 requests/day total. When a model is out of quota it answers `429`
> for hours — the key is still valid, the quota is not. That is why the app can
> work in the morning and stop in the afternoon. Options: enable billing on the
> key, add another key/project, or let the fallback chain use DeepSeek.

**DeepSeek** (`_ask_deepseek`) calls `https://api.deepseek.com/chat/completions`.
`deepseek-flash` is a reasoning model: its hidden reasoning tokens count against
`max_tokens`, so a small budget produces an **empty** `content` with
`finish_reason: "length"`. The code detects that and retries once with a doubled
budget.

**Tech-AI** (`_ask_techai`) speaks OpenAI format at `TECH_AI_API_URL`. It sends a
single `user` message with the system prompt inlined, because the system role and
`response_format` made that API hang.

---

## 3. The provider registry

`AGENT_PROVIDERS` in `backend/main.py` is the single place that lists the agents:

```python
@dataclass(frozen=True)
class AgentProvider:
    key: str                                   # "gemini"
    label: str                                 # "Gemini"  (shown in the app)
    env_var: str                               # "GEMINI_API_KEY"
    api_key: str                               # the loaded value
    system_prompt: str                         # GEMINI_SYSTEM
    ask: Callable[[str, bool], tuple[str, str]] # _ask_gemini
    aliases: tuple[str, ...]                   # ("gemini", "gemi")
    fallbacks: tuple[str, ...]                 # ("techai", "deepseek")
```

The registry drives four things:

1. **Name resolution** — `_normalize_agent("Deep")` → `"deepseek"`; anything
   unknown falls back to Gemini (or Tech-AI if no Gemini key is set).
2. **Labels and prompts** — `_agent_label()` and `_system_prompt()` read from the
   registry, so a new agent needs no extra `if` branches.
3. **Fallback** — `_ask_agent()` tries the primary agent, then each entry in its
   `fallbacks`, skipping agents that are not configured. A task never dies just
   because one provider is down.
4. **Discovery** — `GET /chat/providers` and `/health` → `ai_providers`.

---

## 4. Add your own AI (the whole procedure)

Total: one function, one registry entry, one env var. No app rebuild.

### Step 1 — add the key to `.env`

```bash
MYAI_API_KEY=...
# MYAI_MODEL=my-model
```

Also add the same lines to `backend/.env.example` (or `deploy/config.local.example`)
so the next person knows the variable exists. Never commit `.env`, it is
gitignored.

### Step 2 — read it near the other keys in `backend/main.py`

```python
MYAI_API_KEY = os.environ.get("MYAI_API_KEY", "").strip()
MYAI_MODEL = os.environ.get("MYAI_MODEL", "my-model").strip()
```

### Step 3 — write the call function

Any provider that speaks the OpenAI chat-completions format needs only this,
because `_post_json` already exists:

```python
def _ask_myai(prompt: str, json_mode: bool = False) -> tuple[str, str]:
    """Sender prompt til MyAI. Returnerer (svar, modell)."""
    if not MYAI_API_KEY:
        raise RuntimeError("MYAI_API_KEY mangler i backend/.env")

    payload: dict = {
        "model": MYAI_MODEL,
        "messages": [
            {"role": "system", "content": MYAI_SYSTEM},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 4096,
        "temperature": 0.2 if json_mode else 0.5,
    }
    result = _post_json(
        "https://api.myai.example/v1/chat/completions",
        payload,
        {"Authorization": f"Bearer {MYAI_API_KEY}"},
        timeout=45,
    )
    text = (result["choices"][0]["message"].get("content") or "").strip()
    if not text:
        raise RuntimeError("MyAI ga tomt svar")
    return text, MYAI_MODEL
```

Rules that keep the rest of the system working:

- Return a **tuple** `(text, model)`, never a bare string.
- **Raise** on failure (do not return an error string). The exception is what
  triggers the fallback chain and the error log.
- Keep `json_mode` in the signature even if you ignore it, otherwise the
  agent/collaborate paths break.
- Add a system prompt constant next to the others, e.g. `MYAI_SYSTEM`.

### Step 4 — register it

```python
AGENT_PROVIDERS: dict[str, AgentProvider] = {
    provider.key: provider
    for provider in (
        ...,
        AgentProvider(
            key="myai",
            label="MyAI",
            env_var="MYAI_API_KEY",
            api_key=MYAI_API_KEY,
            system_prompt=MYAI_SYSTEM,
            ask=_ask_myai,
            aliases=("myai", "my-ai"),
            fallbacks=("deepseek", "gemini"),
        ),
    )
}
```

That is it. You now have:

- `POST /chat/provider/myai` with the same `{"message": "..."}` contract
- an entry in `GET /chat/providers`
- `myai` in `/health` → `ai_providers`
- automatic fallback to whichever agents you listed

### Step 5 (optional) — a dedicated route

The generic route is enough for most people. If you want
`POST /chat/myai` to look exactly like the other three, copy the body of
`chat_deepseek` and swap the function call. It is ~15 lines including the
fallback.

### Step 6 — restart the server

`.env` is read **once at import**. After any change to `.env` or `main.py`:

```bash
sudo systemctl restart servidor-super
curl -s localhost:8000/chat/providers -H "X-API-Token: $API_TOKEN"
```

If `/health` shows `"deepseek_key_configured": false` while the key is in `.env`,
you forgot this step.

---

## 5. Remove the built-in agents (the clean template)

For the repo where people bring their own AI:

1. Delete the `AgentProvider(...)` entries from `AGENT_PROVIDERS` (keep the
   dataclass, `_normalize_agent`, `_agent_label`, `_system_prompt`,
   `_ask_agent`).
2. Delete the `_ask_gemini`, `_ask_deepseek`, `_ask_techai` functions and their
   system prompt constants.
3. Delete the routes `/chat/gemini`, `/chat/deepseek`, `/chat/techai` and the
   model/cooldown constants they use.
4. Delete the matching keys from `.env.example`.
5. Keep `GET /chat/providers`, `POST /chat/provider/{key}` and the `_ask_*`
   convention — those are the extensibility surface.

Add a note in the template README pointing at section 4 of this file. People
then implement exactly one `_ask_<name>` function.

**One caveat when removing agents:** `/chat/collaborate` and `/chat/agent` call
`_ask_agent(primary)`, which falls back through the registry. With only one
provider registered there is no fallback, which is fine, but make sure the
`primary` default in `CollaborateRequest` names a provider that still exists.

---

## 6. API reference

All routes require the shared secret when `API_TOKEN` is set (the app sends it
as the `X-API-Token` header). Request body is always `{"message": "..."}`.

| method | path | purpose |
|---|---|---|
| `POST` | `/chat/gemini` | ask Gemini directly |
| `POST` | `/chat/deepseek` | ask DeepSeek directly |
| `POST` | `/chat/techai` | ask Tech-AI directly |
| `POST` | `/chat/provider/{key}` | ask any registered agent (generic) |
| `GET` | `/chat/providers` | list agents, `configured` flags, fallback order |
| `POST` | `/chat/collaborate` | one agent writes, another reviews |
| `POST` | `/chat/agent` | autonomous tool-using loop (`backend/agent.py`) |
| `GET` | `/health` | status, `gemini_models`, `gemini_model_cooldowns`, `ai_providers` |

`/chat/gemini` answers with a `"(Svar via DeepSeek-fallback ...)"` prefix when it
had to use another provider, so the user always knows who actually answered.

Discover the agents:

```bash
curl -s localhost:8000/chat/providers -H "X-API-Token: $API_TOKEN"
```

```json
{
  "providers": [
    {"key": "gemini", "label": "Gemini", "env_var": "GEMINI_API_KEY",
     "configured": true, "aliases": ["gemini", "gemi"],
     "fallbacks": ["techai", "deepseek"]}
  ],
  "configured": ["gemini", "deepseek", "techai"]
}
```

Ask one:

```bash
curl -s -X POST localhost:8000/chat/provider/deepseek \
  -H "Content-Type: application/json" \
  -H "X-API-Token: $API_TOKEN" \
  -d '{"message": "Si hei på norsk"}'
```

---

## 7. Resilience: what happens when an AI is down

The chain for `/chat/gemini` when Gemini is out of quota:

1. `_ask_gemini` tries `gemini-3.8-flash` → `429` → cooldown for the
   `retryDelay` Google reports (hours), move on.
2. Next model in `GEMINI_MODELS`, e.g. `gemini-3-flash-preview` → `429` →
   cooldown, move on.
3. A model that hangs costs at most `GEMINI_ATTEMPT_TIMEOUT` (15 s) and is then
   benched for 30 min, so it cannot slow down every later request.
4. If all models are benched or the 30 s total budget runs out, `_ask_gemini`
   raises.
5. `/chat/gemini` catches it and asks DeepSeek, returning the fallback prefix.

Tunables (environment variables):

| var | default | meaning |
|---|---|---|
| `GEMINI_MODELS` | `gemini-3.8-flash,gemini-3-flash-preview,gemini-flash-latest,gemini-flash-lite-latest` | fallback order |
| `GEMINI_ATTEMPT_TIMEOUT` | `15` | seconds per model attempt |
| `GEMINI_TOTAL_BUDGET` | `30` | seconds for the whole Gemini attempt |
| `DEEPSEEK_MODEL` | `deepseek-flash` | `deepseek-v4-pro` also exists |
| `TECH_AI_MODEL` | `tech-ai-monkey` | |

Everything is logged (`journalctl -u servidor-super -f`) and failures are stored
in the `error_logs` table, visible with
`cd backend && ../.venv/bin/python check_errors.py`.

---

## 8. The app side

Two clients exist in this repo and both call the same routes:

| client | path | package | notes |
|---|---|---|---|
| native | `android_app/` | `com.pepito.servidor.gemini` | Kotlin + Retrofit, `chat/gemini`, `chat/deepseek`, `chat/techai`, `chat/collaborate`, `chat/agent` |
| WebView | `mobile/` (`app/src/main/assets/www/index.html`) | `com.superserver.app` | HTML/JS, same routes |

Base URL is the **Tailscale IP** of the server, e.g.
`http://100.x.y.z:8000/`, set in `ServerApi.kt` (`BASE_URL`) and in the WebView
settings screen. The server must listen on `0.0.0.0` and be reachable over
Tailscale; `HOST=100.x.y.z` in `.env` restricts it to Tailscale only.

### Do I need to rebuild the APK?

**Only if you change app code.** Adding, swapping or removing a server-side agent
does not change the request or response shape, so an installed APK keeps working.

Rebuild when you:

- change the base URL, the token, or the routes the app calls
- add a new tab for a provider
- change how a reply is rendered

The fully dynamic version (app builds its tabs from `GET /chat/providers`) is not
implemented in the app yet — the endpoint is there so it is a small change:
call `/chat/providers`, create one tab per entry where `configured` is true, and
route each tab to `/chat/provider/{key}`.

---

## 9. Two repositories

| repo | contains | audience |
|---|---|---|
| personal (`Hollman`) | Gemini + DeepSeek + Tech-AI, all keys wired | you |
| clean template | the same server, no `AgentProvider` entries | everyone else |

Keep the clean template in sync with the shared parts — `database.py`,
`auth.py`, `tools.py`, `agent.py`, `memory.py`, the `/chat/provider/{key}`
convention and this document. When you add an agent to the personal repo, add the
*pattern* (section 4) to the template, not the vendor.

---

## 10. Troubleshooting

| symptom | cause | fix |
|---|---|---|
| `"deepseek_key_configured": false` but the key is in `.env` | server started before the edit | `sudo systemctl restart servidor-super` |
| Gemini says "ingen modell svarte" | all models in `gemini_model_cooldowns` | wait for the reset, or add billing / another key |
| `HTTP 429 ... exceeded your current quota` | free tier: 20 req/day/model | billing, new key, or let it fall back to DeepSeek |
| `Gemini avviser API-nøkkelen (HTTP 401)` | wrong or revoked key | regenerate at <https://aistudio.google.com/apikey> |
| Empty replies from DeepSeek | reasoning tokens ate `max_tokens` | already handled; keep `max_tokens` generous |
| `401` from the app | app `API_TOKEN` ≠ server `API_TOKEN` | update the app's token |
| New agent answers `... key mangler` | env var name mismatch | `env_var` in the registry must equal the key in `.env` |
| App shows connection error | Tailscale down / wrong IP | `tailscale status`, check `ServerApi.kt` |

---

## 11. Security notes

- `.env` holds every key and is **gitignored**. Never commit it, never paste it
  into an issue.
- `API_TOKEN` is a shared secret, not user auth — anyone with it can run shell
  commands through the agent. Treat it like a password and keep the server on
  Tailscale rather than the open internet.
- Gemini's key is sent as a query parameter because that is what the endpoint
  requires; `x-goog-api-key` also works if you prefer it out of the URL.
- Rotate any key that has been pasted into a chat, a log or a screenshot.
