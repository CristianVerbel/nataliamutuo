# agent/botdb.py — Base de datos PROPIA del bot (Postgres en Railway)
# Mutuo Fintech — Bot WhatsApp
#
# Memoria conversacional independiente del sistema principal: si Supabase se
# cae, el bot conserva su historial y sigue conversando. Se activa con la
# variable BOT_DATABASE_URL (referencia ${{Postgres.DATABASE_URL}} en Railway);
# sin ella, memory.py sigue usando Supabase como siempre.
#
# El esquema se crea solo al arrancar (idempotente). El sync hacia el CRM del
# sistema (crm_sync.py / whatsapp-sync) NO cambia: el admin sigue viendo los
# chats en Supabase; esta base es la fuente de verdad del BOT.

import os
import logging
from datetime import datetime, timezone

load_error: Exception | None = None
try:
    import asyncpg
except Exception as e:  # pragma: no cover - solo si falta la dependencia
    asyncpg = None
    load_error = e

logger = logging.getLogger("mutuo-bot")

BOT_DATABASE_URL = os.getenv("BOT_DATABASE_URL", "")

_pool = None

_SCHEMA = """
create table if not exists bot_conversations (
    id uuid primary key default gen_random_uuid(),
    phone text not null unique,
    prospect_name text,
    city text,
    status text default 'nuevo',
    handoff_status text default 'bot',
    last_message_at timestamptz default now(),
    created_at timestamptz default now()
);

create table if not exists bot_messages (
    id bigint generated always as identity primary key,
    conversation_id uuid not null references bot_conversations(id) on delete cascade,
    role text not null,
    content text not null,
    created_at timestamptz default now()
);

create index if not exists bot_messages_conv_created_idx
    on bot_messages (conversation_id, created_at);
"""


def enabled() -> bool:
    """True si la base propia del bot está configurada y la librería cargó."""
    return bool(BOT_DATABASE_URL) and asyncpg is not None


async def init() -> bool:
    """Crea el pool y el esquema. Devuelve True si la base quedó lista."""
    global _pool
    if not BOT_DATABASE_URL:
        return False
    if asyncpg is None:
        logger.error(f"[BOTDB] BOT_DATABASE_URL definido pero asyncpg no cargó: {load_error}")
        return False
    try:
        _pool = await asyncpg.create_pool(
            BOT_DATABASE_URL, min_size=1, max_size=5, command_timeout=10,
        )
        async with _pool.acquire() as conn:
            await conn.execute(_SCHEMA)
        logger.info("[BOTDB] Postgres propio del bot: OK (esquema listo)")
        return True
    except Exception as e:
        logger.error(f"[BOTDB] No se pudo inicializar la base propia: {e}")
        _pool = None
        return False


def ready() -> bool:
    return _pool is not None


async def get_or_create_conversation(phone: str) -> str | None:
    """Devuelve el id (uuid str) de la conversación del teléfono, creándola si falta."""
    if _pool is None:
        return None
    try:
        async with _pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                insert into bot_conversations (phone, last_message_at)
                values ($1, now())
                on conflict (phone) do update set last_message_at = now()
                returning id
                """,
                phone,
            )
            return str(row["id"]) if row else None
    except Exception as e:
        logger.error(f"[BOTDB] get_or_create_conversation falló: {e}")
        return None


async def save_message(phone: str, role: str, content: str) -> bool:
    if _pool is None:
        return False
    conv_id = await get_or_create_conversation(phone)
    if not conv_id:
        return False
    try:
        async with _pool.acquire() as conn:
            await conn.execute(
                "insert into bot_messages (conversation_id, role, content) values ($1, $2, $3)",
                conv_id, role, content,
            )
        return True
    except Exception as e:
        logger.error(f"[BOTDB] save_message falló: {e}")
        return False


async def get_history(phone: str, limit: int = 200) -> list[dict]:
    """Últimos `limit` mensajes en orden cronológico: [{role, content}, ...]."""
    if _pool is None:
        return []
    try:
        async with _pool.acquire() as conn:
            rows = await conn.fetch(
                """
                select m.role, m.content
                from bot_messages m
                join bot_conversations c on c.id = m.conversation_id
                where c.phone = $1
                order by m.created_at desc
                limit $2
                """,
                phone, limit,
            )
        result = [{"role": r["role"], "content": r["content"]} for r in rows]
        result.reverse()
        return result
    except Exception as e:
        logger.error(f"[BOTDB] get_history falló: {e}")
        return []


async def update_lead(phone: str, nombre: str | None = None, ciudad: str | None = None) -> None:
    if _pool is None or not (nombre or ciudad):
        return
    sets, args, n = [], [], 1
    if nombre:
        sets.append(f"prospect_name = ${n}"); args.append(nombre); n += 1
    if ciudad:
        sets.append(f"city = ${n}"); args.append(ciudad); n += 1
    args.append(phone)
    try:
        async with _pool.acquire() as conn:
            await conn.execute(
                f"update bot_conversations set {', '.join(sets)} where phone = ${n}",
                *args,
            )
    except Exception as e:
        logger.warning(f"[BOTDB] update_lead falló: {e}")
