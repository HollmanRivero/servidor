-- ================================================================
-- SUPER-SERVER CUSTOMER & SUBSCRIPTION DATABASE SCHEMA
-- Model: 30 EUR / Year Subscription + Absolute Admin Ban Power
-- Designed for: Vercel Postgres / Neon / PostgreSQL
-- ================================================================

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Trengs for crypt()/gen_salt() i admin_queries.sql (bcrypt-hash fra SQL).
-- Python-koden bruker bcrypt selv og er kompatibel med disse hash-ene.
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- 1. USERS TABLE
CREATE TABLE IF NOT EXISTS users (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    username VARCHAR(50) UNIQUE NOT NULL,
    email VARCHAR(255) UNIQUE,
    password_hash VARCHAR(255) NOT NULL,
    role VARCHAR(20) DEFAULT 'user',               -- 'admin' (you) or 'user'
    is_vip BOOLEAN DEFAULT FALSE,                 -- VIP / close friends (free pass bypass)
    is_banned BOOLEAN DEFAULT FALSE,              -- THE BAN HAMMER: immediate lockout
    ban_reason TEXT,                              -- e.g. "Disturbed my peace at 3 AM"
    friend_notes TEXT,                            -- e.g. "Childhood friend, allowed always"
    banned_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- 2. ANNUAL SUBSCRIPTIONS TABLE (30 € / Year)
CREATE TABLE IF NOT EXISTS subscriptions (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID UNIQUE NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    plan_name VARCHAR(50) DEFAULT 'Annual Peace Pass',
    price_eur NUMERIC(6, 2) DEFAULT 30.00,
    status VARCHAR(20) DEFAULT 'pending',          -- 'active', 'expired', 'pending', 'banned'
    started_at TIMESTAMP WITH TIME ZONE,
    expires_at TIMESTAMP WITH TIME ZONE,          -- Set to NOW() + INTERVAL '1 year' when activated
    payment_method VARCHAR(50),                   -- 'revolut', 'vipps', 'stripe', 'cash', 'crypto'
    notes TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- 3. AUDIT & USAGE LOG
CREATE TABLE IF NOT EXISTS access_logs (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    endpoint VARCHAR(100) NOT NULL,
    allowed BOOLEAN DEFAULT TRUE,
    blocked_reason VARCHAR(100),                  -- 'banned', 'expired_subscription', 'invalid_token'
    ip_address VARCHAR(45),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- 4. PERFORMANCE INDEXES
CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);
CREATE INDEX IF NOT EXISTS idx_subs_user_id ON subscriptions(user_id);
CREATE INDEX IF NOT EXISTS idx_subs_expires_at ON subscriptions(expires_at);
CREATE INDEX IF NOT EXISTS idx_access_logs_user_id ON access_logs(user_id);

-- ================================================================
-- 5. AGENT-MINNE & JOBBER
--    Brukes av backend/agent.py, memory.py og tools.py.
--    NB: backend/database.py lager disse automatisk ved oppstart,
--    så dette avsnittet er kun for manuell opprettelse i Vercel.
-- ================================================================

CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    title TEXT DEFAULT 'Ny samtale',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS memory (
    key TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    kind TEXT DEFAULT 'note',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT DEFAULT '',
    status TEXT DEFAULT 'pending',
    result TEXT DEFAULT '',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id);
CREATE INDEX IF NOT EXISTS idx_memory_kind ON memory(kind);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
