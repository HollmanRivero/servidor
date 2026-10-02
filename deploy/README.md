# Sette opp Super-Server på en ny maskin

Kortversjon: kopier repoet til maskinen, kjør `bash deploy/install.sh`, skriv IP og
token inn i appen. Detaljene under.

## Forutsetninger

Maskinen som skal være server ("arbeidshesten") må ha:

- Ubuntu (eller annen Debian-basert Linux)
- Python 3.10 eller nyere – **ikke eldre**, og ikke pinn gamle `pydantic`-versjoner
- Nettverk mot resten av tailnettet
- **Tailscale** (avhengighet på Linux-serveren): `tailscale`-daemonen fra
  `curl -fsSL https://tailscale.com/install.sh | sh`.
  Dette er nettverket appen kobler seg til serveren gjennom.
  `install.sh` installerer den automatisk hvis den mangler – du trenger bare
  logge inn med `sudo tailscale up`.

## Steg for steg

### 1. Tailscale på den nye maskinen

`install.sh` installerer Tailscale selv hvis den mangler, men du kan også gjøre
det manuelt først:

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up          # åpner nettleser for innlogging
tailscale ip -4            # <- denne IP-en skal i appen
```

Logg inn med **samme konto som i appen** – da havner begge i samme tailnett.

Skriv gjerne ned IP-en med en gang. Den er `100.x.y.z`.

**Rydd opp i det gamle:** på den forrige maskinen, kjør `sudo tailscale logout`, og
fjern den gamle maskinen i [admin-konsollen](https://login.tailscale.com/admin/machines).
Da slipper du at et gammelt navn eller en gammel IP forvirrer senere.

### 2. Grunnpakker (hvis de mangler)

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip
```

### 3. Få repoet over til maskinen

Repoet ligger på GitHub, så enklest er å klone det:

```bash
git clone https://github.com/HollmanRivero/servidor.git ~/Servidor_Super
```

Alternativt med `rsync` hvis du står på en maskin som allerede har repoet:

```bash
rsync -av --exclude '.venv' --exclude 'build' --exclude '.gradle' --exclude 'backend/data' \
      ~/Dokumenter/MyGits/Servidor_Super/ bruker@ny-maskin:~/Servidor_Super/
```

Byggemappene `.venv` og `build/` og den lokale databasen (`backend/data/`) er
maskinspesifikke og skal ikke kopieres – de lages på nytt av installasjonsskriptet.

### 4. Kjør installasjonsskriptet

```bash
cd ~/Servidor_Super
bash deploy/install.sh
```

Det gjør alt:

1. lager `.venv` og installerer `backend/requirements.txt`
2. **installerer Tailscale hvis den mangler** og sjekker at den er innlogget
3. lager `backend/.env` hvis den mangler, og **genererer en ny `API_TOKEN`**
4. skriver en systemd-tjeneste `servidor-super` med riktige stier
5. starter tjenesten og setter den til å starte ved boot
6. åpner port 8000 **kun på `tailscale0`** hvis `ufw` er aktiv
7. skriver ut Tailscale-IP-en, API-tokenen og hva du skal skrive i appen

### 5. Legg inn AI-nøklene

Skriptet sier fra hvis de mangler. Lag **nye** nøkler – de gamle lå åpent lesbare
via det tidligere `/file`-hullet:

- Gemini: <https://aistudio.google.com/apikey>
- DeepSeek: <https://platform.deepseek.com/api_keys>

```bash
cd ~/Servidor_Super/backend
printf 'GEMINI_API_KEY=ditt-nye\nDEEPSEEK_API_KEY=ditt-nye\n' >> .env
sudo systemctl restart servidor-super
```

### 6. GitHub-token (repoene er private)

Repoene ligger på `HollmanRivero/servidor` og `HollmanRivero/tech-ai-monkey`, og de er
**private**. Da kan ingenting hentes anonymt — verken `git pull` på serveren eller
agentens klone-verktøy.

Lag en **fine-grained PAT** på <https://github.com/settings/personal-access-tokens/new>:

- *Repository access*: velg `servidor` (og `tech-ai-monkey` hvis agenten skal klone den)
- *Permissions* → *Repository permissions* → **Contents: Read-only**
- Sett gjerne en utløpsdato

Legg den i `.env` og kjør installasjonsskriptet på nytt:

```bash
cd ~/Servidor_Super
printf 'GITHUB_TOKEN=ditt-token-her\n' >> backend/.env
bash deploy/install.sh      # sjekker tokenen og setter opp git for deg
sudo systemctl restart servidor-super
```

`install.sh` legger tokenen i `~/.gitconfig` (via `url.…insteadOf`), slik at vanlig
`git pull` i repoet virker uten passordspørsmål. Agenten bruker samme token, men
sender den som HTTP-header fra `backend/.env` — den havner aldri i kommandolinjen,
i `.git/config` eller i loggene.

Agenten kan klone repoer med verktøyet `clone_repo`. Klonene havner i
`backend/projects/<navn>` og kan ikke skrive noe sted utenfor den mappen.

### 7. Verifiser

```bash
systemctl status servidor-super
curl -s localhost:8000/health | python3 -m json.tool
```

`/health` skal svare noe slikt:

```json
{
  "status": "ok",
  "listen": "0.0.0.0:8000",
  "auth_required": true,
  "gemini_models": ["gemini-flash-latest", "gemini-3-flash-preview", "..."],
  "gemini_model_last_ok": null,
  "gemini_key_configured": true,
  "deepseek_key_configured": true
}
```

Sjekk at `auth_required` er `true` og begge `*_key_configured` er `true`.

### 8. Appen

Bygg APK-en (se `INTERN-NOTAT.md` hvis den finnes, ellers under), og installer.
I appen, trykk tannhjulet og fyll inn:

- **URL del Servidor**: `http://<tailscale-ip>:8000`
- **X-API-Token**: verdien fra `backend/.env` (`grep API_TOKEN backend/.env`)

IP-en er hardkodet som *standard* i `RetrofitClient`, men alt du skriver i
innstillingene lagres og overstyrer den. **Du trenger altså ikke bygge appen
på nytt bare fordi IP-en har endret seg.**

## Hvis serveren er en virtuell maskin

Alt over fungerer likt i en VM – Tailscale, systemd og Python bryr seg ikke om det er
bare metal eller virtualisert. Men noen VM-spesifikke ting er verdt å sjekke:

- **Er VM-en klonet fra den gamle serveren?** Da har den med seg gammel
  Tailscale-identitet og gammelt `backend/.env` med de kompromitterte nøklene:
  ```bash
  sudo tailscale logout && sudo tailscale up   # tvinger ny node-identitet
  ```
  Sjekk `backend/.env` – finnes den fra før, må nøklene byttes. Installasjonsskriptet
  ser bare at feltet er utfylt, ikke om verdien er ny.

- **`/stats` viser VM-ens ressurser**, ikke vertsmaskinens. Står det 4 GB når
  arbeidshesten har 32, er det VM-konfigurasjonen som er for liten – ikke serveren.

- **Ta et snapshot** når installasjonen og nøklene virker. Det er backupen din, og det
  lar deg rulle tilbake til et *fungerende* oppsett neste gang noe endrer seg.

- **Klokka må være riktig.** Pauses eller skrus VM-en av, kan klokka drifte, og da
  feiler HTTPS-kallene mot Gemini og DeepSeek med kryptiske feil:
  ```bash
  timedatectl                                  # sjekk "System clock synchronized"
  sudo apt install -y systemd-timesyncd        # hvis den drifter
  ```

- **NAT er ikke et problem.** VirtualBox/VMware/UTM med NAT blokkerer innkommende
  trafikk, men Tailscale er laget nettopp for det. Det holder at VM-en har utgående nett.

- **Bygg ikke Android-appen i VM-en.** Den trenger Android SDK + JDK. Gjør det på
  vertsmaskinen der `~/Android/Sdk` allerede finnes.

- **Snapshots og systemd:** ruller du tilbake til et snapshot, kommer tjenesten tilbake
  slik den var da – inkludert en eventuelt gammel versjon av koden. Det er den ene
  mekanismen som faktisk kan forklare at filer «endrer seg tilbake» av seg selv.

## Oppdatere en server som allerede kjører

Gjør dette på serveren hver gang koden har endret seg:

```bash
cd ~/Servidor_Super
git pull                                   # hent nyeste kode
.venv/bin/pip install -r backend/requirements.txt   # nye/endrede pakker
sudo systemctl restart servidor-super      # start på nytt
systemctl status servidor-super            # skal stå "active (running)"
journalctl -u servidor-super -n 30         # se feil hvis den ikke starter
```

Verifiser at den svarer:

```bash
curl -s localhost:8000/health | python3 -m json.tool
```

### Database: SQLite eller PostgreSQL/Neon

Serveren trenger **ingen** ekstern database for å kjøre – uten `DATABASE_URL` bruker
den en lokal SQLite-fil i `backend/data/memory.db`. Registrering, login og abonnement
fungerer da også, men dataene ligger kun på den maskinen.

Vil du ha PostgreSQL (Neon/Vercel), legg linjen under i `backend/.env` på serveren og
kjør `sudo systemctl restart servidor-super`:

```
DATABASE_URL=postgresql://bruker:passord@vert/db?sslmode=require
```

Første oppstart oppretter tabellene automatisk. Vil du opprette dem manuelt i Neon,
kjør innholdet i `backend/schema.sql` i Neon-konsollen først. Nøkkelen ligger kun i
`.env` – legg den aldri i Git.

Etter at `DATABASE_URL` er lagt inn, kan du verifisere koblingen:

```bash
cd ~/Servidor_Super/backend
../.venv/bin/python check_db.py
```

Skriptet viser vertsnavnet (aldri passordet), lister tabellene og sier hvor mange
brukere som finnes. Skriv `python check_db.py` i stedet hvis venv-en er aktivert.
Tabellen `users` skal alltid være der; er den borte, lager neste oppstart den på nytt.

### Sjekk at auth faktisk virker

```bash
curl -s -X POST localhost:8000/auth/register \
  -H "Content-Type: application/json" \
  -d '{"username":"testbruker","password":"hemmelig123"}'
```

- `{"id":...,"username":"testbruker","role":"admin"}` → alt OK (første bruker blir admin)
- `409` → brukernavnet finnes allerede
- `500` → databasen feiler; se `journalctl -u servidor-super -n 30`

## Feilsøking

| Symptom | Sjekk |
|---|---|
| Appen sier «Error de conexión» | Kjører tjenesten? `systemctl status servidor-super` |
| Appen sier «Token rechazado (401)» | `API_TOKEN` i `.env` og i appen er ulike |
| Ingenting fra telefonen | `tailscale status` – er begge maskinene i samme tailnett? |
| `No matching distribution found for pydantic-core` | Du har pinnet en gammel `pydantic`. Bruk `>=` i `requirements.txt` |
| `ModuleNotFoundError: No module named 'flask'` | Gammel kode. `main.py` skal være FastAPI |
| Gemini svarer «ingen modell svarte» | Sjekk `gemini_models` i `/health`; `*-latest`-aliasene blir ofte 503 |
| Alt feiler etter en kodeendring | `journalctl -u servidor-super -n 40` |

## Daglig bruk

```bash
sudo systemctl restart servidor-super     # etter endring i .env eller koden
journalctl -u servidor-super -f           # se loggen live
sudo systemctl stop servidor-super        # stoppe
```

## Sikkerhet

- Serveren har **ingen brukerinnlogging** – kun den delte `API_TOKEN`.
  Alle som har tailnett-tilgang *og* tokenen kan lese og skrive filer i
  `backend/projects/` og bruke begge AI-nøklene på din regning.
- Bind eventuelt til Tailscale-adressen i stedet for alle grensesnitt:
  legg `HOST=100.x.y.z` i `.env`. Da er serveren usynlig fra LAN selv uten brannmur.
- `/file` og `/projects` er sperret til `backend/projects/`. Legg ikke `.env`
  eller annet sensitivt inn dit.
