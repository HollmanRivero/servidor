-- ================================================================
-- ADMIN CHEAT-SHEET  --  hvordan du bruker denne filen
-- ================================================================
--
-- HVILKEN DATABASE? Serveren bruker SQLite lokalt med mindre DATABASE_URL er
-- satt i backend/.env (da brukes Vercel Postgres i stedet).
--
--   uten DATABASE_URL ->  backend/data/memory.db   (SQLite, dette er standard)
--   med DATABASE_URL  ->  Postgres (delen nederst i filen)
--
-- SLIK KJORER DU MOT SQLITE:
--
--   cd backend
--   sqlite3 data/memory.db                       # apne shell
--   sqlite> .tables                              # se tabellene
--   sqlite> UPDATE users SET role='admin' WHERE username='dittnavn';
--   sqlite> .quit
--
--   eller uten shell, rett fra kommandolinjen:
--   sqlite3 data/memory.db "SELECT username, role FROM users;"
--
--   Er ikke sqlite3 installert:  sudo apt install -y sqlite3
--
-- TIPS: skriv alltid WHERE pa UPDATE/DELETE. Uten WHERE treffer det alle rader.
-- Ta en kopi forst hvis du er usikker:  cp data/memory.db data/memory.db.bak
--
-- Passord kan du ikke lese ut (de er hashet). Glemt passord?
--   ../.venv/bin/python reset_password.py --user <navn>              (engangskode)
--   ../.venv/bin/python reset_password.py --user <navn> --set-passord <nytt>
--   ../.venv/bin/python reset_password.py --list                     (alle brukere)
-- ================================================================


-- ================================================================
-- DEL 1: SQLITE (det serveren faktisk kjorer)
-- ================================================================

-- 1. GJOR DEG SELV TIL ADMIN
UPDATE users
SET role = 'admin', is_banned = 0
WHERE username = 'dittnavn';

-- 2. GI EN VENN TILGANG I ETT AR (nar han har betalt 30 EUR)
--    SQLite har ikke INTERVAL/NOW+1ar, sa datoen skrives som tekst (ISO).
UPDATE subscriptions
SET status = 'active',
    started_at = datetime('now'),
    expires_at = datetime('now', '+1 year'),
    notes = 'Betalt 30 EUR for 1 ar'
WHERE user_id = (SELECT id FROM users WHERE username = 'venn');

--    Har vennen ingen subscriptions-rad enna, lag den:
INSERT INTO subscriptions (id, user_id, plan_name, price_eur, status, started_at, expires_at, payment_method)
SELECT lower(hex(randomblob(16))), id, 'Friend Pass', 30.00, 'active',
       datetime('now'), datetime('now', '+1 year'), 'revolut'
FROM users
WHERE username = 'venn'
  AND NOT EXISTS (SELECT 1 FROM subscriptions s WHERE s.user_id = users.id);

-- 3. BAN HAMMER (umiddelbar utestengelse)
UPDATE users
SET is_banned = 1,
    ban_reason = 'Forstyrret freden. Tenk deg om.',
    banned_at = datetime('now')
WHERE username = 'lovbryter';

UPDATE subscriptions
SET status = 'banned'
WHERE user_id = (SELECT id FROM users WHERE username = 'lovbryter');

-- 4. TILGI OG SLIPP INN IGJEN
UPDATE users
SET is_banned = 0, ban_reason = NULL, banned_at = NULL
WHERE username = 'lovbryter';

-- 5. ALLE BRUKERE: HVEM HAR BETALT, HVEM ER AKTIV, HVEM ER BANNET
SELECT
    u.username,
    u.role,
    CASE
        WHEN u.is_banned = 1 THEN 'BANNET (' || COALESCE(u.ban_reason, 'ingen grunn') || ')'
        WHEN u.role = 'admin' THEN 'ADMIN (permanent)'
        WHEN s.expires_at > datetime('now') THEN 'AKTIV til ' || substr(s.expires_at, 1, 10)
        WHEN s.expires_at IS NULL THEN 'VENTER PA BETALING'
        ELSE 'UTLOPT'
    END AS status,
    s.payment_method,
    u.created_at
FROM users u
LEFT JOIN subscriptions s ON u.id = s.user_id
ORDER BY u.created_at DESC;

-- 6. HVEM LOGGER INN OG NAR
SELECT username, endpoint, allowed, ip_address, created_at
FROM access_logs
ORDER BY created_at DESC
LIMIT 30;

-- 7. HVEM HAR BEDT OM NYTT PASSORD (se etter /auth/forgot-password)
SELECT username, ip_address, created_at
FROM access_logs
WHERE endpoint = '/auth/forgot-password'
ORDER BY created_at DESC
LIMIT 20;

-- 8. UBEHANDLEDE FEIL FRA APPEN OG SERVEREN
SELECT created_at, source, endpoint, status_code, substr(error_message, 1, 120) AS melding
FROM error_logs
WHERE resolved = 0
ORDER BY created_at DESC
LIMIT 20;

-- 9. MAKS EN ADMIN (rydd bort utilsiktede admin-roller)
UPDATE users SET role = 'user'
WHERE role = 'admin'
  AND username <> 'dittnavn';

-- 10. SLETT EN BRUKER HELT
DELETE FROM subscriptions WHERE user_id = (SELECT id FROM users WHERE username = 'venn');
DELETE FROM password_resets WHERE username = 'venn';
DELETE FROM users WHERE username = 'venn';


-- ================================================================
-- DEL 2: POSTGRES (bare hvis DATABASE_URL er satt i backend/.env)
-- ================================================================
-- Kjør med:  psql "$DATABASE_URL" -f admin_queries.sql
-- eller lim inn enkeltspørringer i Neon/Vercel-konsollen.
-- ================================================================

-- Gjør deg selv til admin:
-- UPDATE users SET role = 'admin', is_banned = FALSE WHERE username = 'dittnavn';

-- Gi en venn et år:
-- INSERT INTO subscriptions (user_id, status, started_at, expires_at, payment_method, notes)
-- VALUES (
--     (SELECT id FROM users WHERE username = 'venn'),
--     'active', NOW(), NOW() + INTERVAL '1 year', 'revolut', 'Betalt 30 EUR'
-- )
-- ON CONFLICT (user_id) DO UPDATE
-- SET status = 'active', started_at = NOW(), expires_at = NOW() + INTERVAL '1 year';

-- Ban:
-- UPDATE users SET is_banned = TRUE, ban_reason = 'Freden ble forstyrret', banned_at = NOW()
-- WHERE username = 'lovbryter';
-- UPDATE subscriptions SET status = 'banned'
-- WHERE user_id = (SELECT id FROM users WHERE username = 'lovbryter');

-- Tilgi:
-- UPDATE users SET is_banned = FALSE, ban_reason = NULL, banned_at = NULL
-- WHERE username = 'lovbryter';

-- Alle brukere med status:
-- SELECT u.username, u.role,
--        CASE WHEN u.is_banned THEN 'BANNET'
--             WHEN u.role = 'admin' THEN 'ADMIN'
--             WHEN s.expires_at > NOW() THEN 'AKTIV til ' || TO_CHAR(s.expires_at, 'YYYY-MM-DD')
--             ELSE 'UTLOPT' END AS status,
--        s.payment_method, u.created_at
-- FROM users u LEFT JOIN subscriptions s ON u.id = s.user_id
-- ORDER BY u.created_at DESC;
