# Sette opp Super-Server på en ny maskin

Kortversjon: kopier repoet til maskinen, kjør `bash deploy/install.sh`, skriv IP og
token inn i appen. Detaljene under.

## Forutsetninger

Maskinen som skal være server ("arbeidshesten") må ha:

- Ubuntu (eller annen Debian-basert Linux)
- Python 3.10 eller nyere – **ikke eldre**, og ikke pinn gamle `pydantic`-versjoner
- Nettverk mot resten av tailnettet
<<<<<<< HEAD
=======
- **Tailscale** (avhengighet på Linux-serveren): `tailscale`-daemonen fra
  `curl -fsSL https://tailscale.com/install.sh | sh`.
  Dette er nettverket appen kobler seg til serveren gjennom.
  `install.sh` installerer den automatisk hvis den mangler – du trenger bare
  logge inn med `sudo tailscale up`.
>>>>>>> 465ea2342a392fafbd129a4f7e728ec8af8b9e62

## Steg for steg

### 1. Tailscale på den nye maskinen

<<<<<<< HEAD
=======
`install.sh` installerer Tailscale selv hvis den mangler, men du kan også gjøre
det manuelt først:

>>>>>>> 465ea2342a392fafbd129a4f7e728ec8af8b9e62
```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up          # åpner nettleser for innlogging
tailscale ip -4            # <- denne IP-en skal i appen
```

<<<<<<< HEAD
=======
Logg inn med **samme konto som i appen** – da havner begge i samme tailnett.

>>>>>>> 465ea2342a392fafbd129a4f7e728ec8af8b9e62
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

<<<<<<< HEAD
Repoet er ikke på GitHub, så bruk `rsync` fra maskinen du står på nå:

```bash
rsync -av --exclude '.venv' --exclude 'build' --exclude '.gradle' \
      ~/Dokumenter/MyGits/Servidor_Super/ bruker@ny-maskin:~/Servidor_Super/
```

Byggemappene `.venv` og `build/` er maskinspesifikke og skal ikke kopieres –
de lages på nytt av installasjonsskriptet.

Alternativt kan du `git init` i repoet og pushe til et privat repo. Det gir deg
i tillegg sporbarhet, som er verdt mye når du lurer på hva som har endret seg.
=======
Repoet ligger på GitHub, så enklest er å klone det:

```bash
git clone https://github.com/Salazar-Rivero-Smart-Things/servidor.git ~/Servidor_Super
```

Alternativt med `rsync` hvis du står på en maskin som allerede har repoet:

```bash
rsync -av --exclude '.venv' --exclude 'build' --exclude '.gradle' --exclude 'backend/data' \
      ~/Dokumenter/MyGits/Servidor_Super/ bruker@ny-maskin:~/Servidor_Super/
```

Byggemappene `.venv` og `build/` og den lokale databasen (`backend/data/`) er
maskinspesifikke og skal ikke kopieres – de lages på nytt av installasjonsskriptet.
>>>>>>> 465ea2342a392fafbd129a4f7e728ec8af8b9e62

### 4. Kjør installasjonsskriptet

```bash
cd ~/Servidor_Super
bash deploy/install.sh
```

Det gjør alt:

1. lager `.venv` og installerer `backend/requirements.txt`
<<<<<<< HEAD
2. lager `backend/.env` hvis den mangler, og **genererer en ny `API_TOKEN`**
3. skriver en systemd-tjeneste `servidor-super` med riktige stier
4. starter tjenesten og setter den til å starte ved boot
5. åpner port 8000 **kun på `tailscale0`** hvis `ufw` er aktiv
6. skriver ut Tailscale-IP-en, API-tokenen og hva du skal skrive i appen
=======
2. **installerer Tailscale hvis den mangler** og sjekker at den er innlogget
3. lager `backend/.env` hvis den mangler, og **genererer en ny `API_TOKEN`**
4. skriver en systemd-tjeneste `servidor-super` med riktige stier
5. starter tjenesten og setter den til å starte ved boot
6. åpner port 8000 **kun på `tailscale0`** hvis `ufw` er aktiv
7. skriver ut Tailscale-IP-en, API-tokenen og hva du skal skrive i appen
>>>>>>> 465ea2342a392fafbd129a4f7e728ec8af8b9e62

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

### 6. Verifiser

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

### 7. Appen

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
