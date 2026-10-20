# Servidor Super

Servidor doméstico con dos asistentes de IA (**Gemini** + **DeepSeek**) y una app Android
para controlarlo desde el móvil. El servidor corre en una máquina Ubuntu y **solo es
accesible a través de Tailscale** — nada queda expuesto a internet.

```
┌──────────────┐   Tailscale (100.x.y.z)   ┌─────────────────────┐
│  App Android │ ────────────────────────► │  FastAPI :8000      │
│  (Compose)   │   header: X-API-Token     │  backend/main.py    │
└──────────────┘                           └──────────┬──────────┘
                                                      │
                                       ┌──────────────┴──────────────┐
                                       ▼                             ▼
                               Gemini API (Google)          DeepSeek API
```

---

## Índice

- [Qué hay en el repo](#qué-hay-en-el-repo)
- [Parte 1 — Instalar el servidor (Ubuntu Server)](#parte-1--instalar-el-servidor-ubuntu-server)
- [Parte 2 — Compilar e instalar la app Android](#parte-2--compilar-e-instalar-la-app-android)
- [Parte 3 — Conectar el móvil](#parte-3--conectar-el-móvil)
- [Referencia de la API](#referencia-de-la-api)
- [Uso diario](#uso-diario)
- [Seguridad](#seguridad)
- [Solución de problemas](#solución-de-problemas)
- [Roadmap](#roadmap)

---

## Qué hay en el repo

| Ruta | Qué es |
|---|---|
| `backend/main.py` | La API FastAPI completa (stats, ficheros, chat con IA) |
| `backend/requirements.txt` | Dependencias de Python |
| `backend/.env.example` | **Plantilla** de configuración. El `.env` real no se sube al repo |
| `backend/schema.sql` | Esquema de base de datos para el sistema de suscripciones (fase 1) |
| `backend/admin_queries.sql` | Comandos SQL de administración (añadir amigos, banear…) |
| `backend/NOTES.txt` | Notas de trabajo y plan del proyecto |
| `android_app/` | App Android (Jetpack Compose + Retrofit) |
| `deploy/install.sh` | Instalador automático del servidor en Ubuntu |
| `deploy/README.md` | Guía detallada de despliegue (incluye notas sobre VMs) |

### Requisitos

**Servidor:**
- Ubuntu (o cualquier Linux basado en Debian)
- Python 3.10 o superior (probado con 3.14)
- **Tailscale** (cliente oficial, `tailscale` en PATH) — *dependencia crítica: es la red por la que la app se conecta al servidor*. `install.sh` la instala sola si falta.
- Cuenta de Tailscale (la app y el servidor deben estar en el mismo *tailnet*)

**App Android / móvil:**
- Android Studio, o JDK 17+ y el Android SDK
- Android 7.0 (API 24) o superior
- **App de Tailscale** en el móvil (Play Store), con la misma cuenta que el servidor — *dependencia crítica para llegar al servidor desde fuera*

---

## Parte 1 — Instalar el servidor (Ubuntu Server)

Todo esto se ejecuta **en la máquina servidor**, sin interfaz gráfica.

### 1.1 Instalar Tailscale

`sudo tailscale up` a secas **no funciona** en un servidor sin navegador: se queda
esperando un login que no puede abrir. Usa una *auth key*:

```bash
curl -fsSL https://tailscale.com/install.sh | sh
```

Genera una auth key en <https://login.tailscale.com/admin/settings/keys> y luego:

```bash
sudo tailscale up --authkey=tskey-auth-XXXXXXXX --hostname=servidor-super
```

Alternativa sin auth key: ejecuta `sudo tailscale up`, copia la URL
`https://login.tailscale.com/a/...` que imprime, ábrela desde el móvil e introduce el código.

Guarda la IP del servidor, la vas a necesitar en la app:

```bash
tailscale ip -4      # -> 100.x.y.z
```

> El móvil debe iniciar sesión en Tailscale con **la misma cuenta**. Ambos quedan en el
> mismo *tailnet* y se ven como si estuvieran en la misma red local.

### 1.2 Instalar el proyecto

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git

git clone https://github.com/HollmanRivero/servidor.git ~/Servidor_Super
cd ~/Servidor_Super
```

### 1.3 Configurar las claves

El `.env` **no está en el repo** (contiene secretos). Créalo desde la plantilla:

```bash
cp backend/.env.example backend/.env
nano backend/.env
```

Rellena estos valores:

| Variable | Dónde se consigue |
|---|---|
| `API_TOKEN` | Tú la inventas: `openssl rand -hex 32` |
| `GEMINI_API_KEY` | <https://aistudio.google.com/apikey> |
| `DEEPSEEK_API_KEY` | <https://platform.deepseek.com/api_keys> |
| `TECH_AI_API_KEY` | <https://tech-ai.tech/dashboard/api-keys> — clave `tai_…` (opcional, usa el modelo `tech-ai-monkey`) |
| `GITHUB_TOKEN` | PAT con **Contents: Read** — solo si el agente va a clonar los repos privados |

`API_TOKEN` es la clave compartida entre el móvil y el servidor: la app la envía en la
cabecera `X-API-Token` en cada petición. **Debe ser idéntica en los dos lados.**

> Opcional pero recomendado: añade `HOST=100.x.y.z` (tu IP de Tailscale) para que el
> servidor escuche **solo** por Tailscale y sea invisible desde la LAN.

### 1.4 Ejecutar el instalador

```bash
bash deploy/install.sh
```

Es idempotente (se puede repetir sin romper nada). Hace:

1. Crea el entorno virtual `.venv` e instala `backend/requirements.txt`
2. Genera un `API_TOKEN` si falta en el `.env` (solo si la línea está vacía)
3. Crea el servicio systemd `servidor-super` con las rutas correctas
4. Lo arranca y lo activa al inicio del sistema
5. Abre el puerto 8000 **solo en `tailscale0`** si `ufw` está activo
6. Imprime la IP de Tailscale y el token

### 1.5 Verificar

```bash
systemctl status servidor-super
curl -s localhost:8000/health | python3 -m json.tool
```

`/health` debe responder algo así — comprueba `auth_required` y los dos `*_key_configured`:

```json
{
  "status": "ok",
  "listen": "0.0.0.0:8000",
  "auth_required": true,
  "gemini_key_configured": true,
  "deepseek_key_configured": true
}
```

Apunta el token, lo necesitas para la app:

```bash
grep API_TOKEN backend/.env
```

---

## Parte 2 — Compilar e instalar la app Android

Esto se hace en tu PC de desarrollo (o en Android Studio), **no** en el servidor.

```bash
cd android_app

# Si no usas Android Studio, apunta el SDK a mano:
echo "sdk.dir=$HOME/Android/Sdk" > local.properties

./gradlew assembleDebug
```

El APK queda en `android_app/app/build/outputs/apk/debug/app-debug.apk`.

Instálalo en el móvil por USB:

```bash
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

O copia el `.apk` al móvil y ábrelo (tendrás que permitir "instalar apps de origen
desconocido").

**Detalles técnicos:** `applicationId = com.pepito.servidor.gemini`, `minSdk = 24`,
`targetSdk = 36`, Kotlin 2.2.10, AGP 9.3.3, Compose BOM 2026.02.01.
`android:usesCleartextTraffic="true"` está activado porque el tráfico va por HTTP dentro
de la red de Tailscale (que ya está cifrada extremo a extremo).

---

## Parte 3 — Conectar el móvil

1. Instala **Tailscale** en el móvil (Play Store) e inicia sesión con la misma cuenta.
2. Comprueba la conexión desde el navegador del móvil:

   ```
   http://100.x.y.z:8000/health
   ```

   Debe responder `{"status":"ok", ...}`. Si no responde, mira la sección de problemas.

3. Abre la app, toca el **engranaje** (arriba a la derecha) y rellena:
   - **URL del Servidor**: `http://100.x.y.z:8000`
   - **X-API-Token**: el valor de `grep API_TOKEN backend/.env`

Los ajustes se guardan en `SharedPreferences`, así que **si cambia la IP no hay que
recompilar la app**: basta con editar la URL en el engranaje.

---

## Referencia de la API

Todas las rutas menos `/` y `/health` exigen autenticación mediante la cabecera
`X-API-Token: <API_TOKEN>` (también vale `Authorization: Bearer <API_TOKEN>`).
Sin token válido responden `401`.

| Método | Ruta | Qué hace |
|---|---|---|
| `GET` | `/` | Comprobación básica. **Abierta** |
| `GET` | `/health` | Estado, modelos disponibles y si las claves están configuradas. **Abierta** |
| `GET` | `/stats` | CPU y RAM del servidor |
| `GET` | `/projects?path=` | Lista el contenido de `backend/projects/` |
| `GET` | `/recent_changes` | Los 5 ficheros modificados más recientemente |
| `GET` | `/file?path=` | Lee un fichero de texto |
| `POST` | `/file?path=` | Guarda un fichero |
| `POST` | `/chat/gemini` | Pregunta a Gemini. Body: `{"message": "..."}` |
| `POST` | `/chat/deepseek` | Pregunta a DeepSeek. Body: `{"message": "..."}` |
| `POST` | `/chat/collaborate` | Un agente responde y el otro revisa. Body: `{"message": "...", "primary": "gemini"}` |

Ejemplo:

```bash
curl -s -X POST http://localhost:8000/chat/gemini \
  -H "Content-Type: application/json" \
  -H "X-API-Token: $(grep API_TOKEN backend/.env | cut -d= -f2)" \
  -d '{"message": "Hola"}'
```

> `/file` y `/projects` están enjaulados en `backend/projects/`: cualquier ruta que
> intente salir de ahí devuelve `403`. **No metas nunca el `.env` ni nada sensible
> dentro de `backend/projects/`.**

---

## Uso diario

```bash
sudo systemctl restart servidor-super     # tras cambiar .env o el código
sudo systemctl stop servidor-super        # parar
sudo systemctl status servidor-super      # ver estado
journalctl -u servidor-super -f           # ver logs en directo
journalctl -u servidor-super -n 40        # últimas 40 líneas
```

Para actualizar el código en el servidor:

```bash
cd ~/Servidor_Super
git pull
sudo systemctl restart servidor-super
```

---

## Seguridad

- El servidor **no tiene login de usuario**: la única barrera es el `API_TOKEN` compartido.
  Quien tenga acceso al tailnet **y** el token puede leer y escribir en
  `backend/projects/` y gastar tus créditos de Gemini y DeepSeek.
- El token y las claves de IA **nunca** deben estar en el repo. `.env` está en
  `.gitignore`. `.env.example` sí se publica: déjalo siempre con valores vacíos.
- Opcional: `HOST=100.x.y.z` en `.env` limita el servidor a la interfaz de Tailscale.
- Si alguna clave se filtra: rótala y reinicia el servicio.

---

## Solución de problemas

| Síntoma | Causa probable / solución |
|---|---|
| `tailscale status` dice `NeedsLogin` | El nodo no está autenticado. `sudo tailscale up --authkey=...` |
| La app dice «Error de conexión» | El servicio no corre: `systemctl status servidor-super` |
| La app dice «Token rechazado (401)» | El `API_TOKEN` del `.env` y el del engranaje no coinciden |
| Nada responde desde el móvil | `tailscale status`: ¿ambas máquinas en el mismo tailnet? |
| Gemini responde «ningún modelo respondió» | Mira `gemini_models` en `/health`. Los alias `*-latest` dan 503 a menudo; el servidor ya prueba el siguiente de la lista |
| `No matching distribution found for pydantic-core` | Estás fijando una versión vieja de `pydantic`. En `requirements.txt` usa `>=` |
| `ModuleNotFoundError: No module named 'flask'` | Código antiguo: `main.py` es FastAPI |
| Todo falla tras un cambio de código | `journalctl -u servidor-super -n 40` |
| Cambió algo «solo» tras reiniciar | ¿Restauraste un snapshot de la VM? Eso revierte también la versión del código |

**Si el servidor es una máquina virtual:** comprueba que la hora esté sincronizada
(`timedatectl`), porque con el reloj desviado las llamadas HTTPS a Gemini y DeepSeek
fallan con errores crípticos:

```bash
sudo apt install -y systemd-timesyncd
```

---

## Roadmap

**Fase 1 — Autenticación y suscripciones** (`backend/schema.sql`, `admin_queries.sql`)
- [ ] Conectar a Vercel Postgres con `DATABASE_URL`
- [ ] `POST /auth/login` → token JWT
- [ ] Middleware: rechazar baneados (`403`) y suscripciones caducadas (`402`)
- [ ] Modelo «Peace Pass»: 30 €/año, más acceso VIP gratuito para amigos

**Fase 2 — App Android**
- [ ] `LoginScreen` con usuario y contraseña
- [ ] Guardar la sesión y saltar el login si ya está iniciada
- [ ] Mostrar el usuario conectado y botón de cerrar sesión
- [ ] Mensajes claros para `403` (baneado) y `402` (cuota agotada)

Detalles completos en `backend/NOTES.txt`.
