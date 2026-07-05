#!/usr/bin/env python3
"""Copia el historial conversacional desde Supabase a la base PROPIA del bot.

Uso (una sola vez, tras crear la Postgres del bot en Railway):

    BOT_DATABASE_URL=... SUPABASE_URL=... SUPABASE_SERVICE_ROLE_KEY=... \
        python scripts/migrate_memory_to_botdb.py

Se puede correr desde la consola del servicio en Railway (las tres variables ya
están inyectadas ahí) con:  python scripts/migrate_memory_to_botdb.py

Es idempotente: las conversaciones se upsert-ean por teléfono y los mensajes
solo se insertan si la conversación del bot está vacía (no duplica al re-correr).
"""

import asyncio
import os
import sys

import asyncpg
import httpx

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = (
    os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    or os.getenv("SUPABASE_KEY", "")
    or os.getenv("SUPABASE_ANON_KEY", "")
)
BOT_DATABASE_URL = os.getenv("BOT_DATABASE_URL", "")

HEADERS = {
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "apikey": SUPABASE_KEY,
}

PAGE = 1000

from datetime import datetime  # noqa: E402


def _ts(v):
    """ISO string de PostgREST -> datetime (asyncpg exige datetime, no str)."""
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except Exception:
        return None


async def fetch_all(client: httpx.AsyncClient, path: str) -> list[dict]:
    """Pagina PostgREST completo (corta en PAGE filas por request)."""
    rows: list[dict] = []
    offset = 0
    while True:
        r = await client.get(
            f"{SUPABASE_URL}/rest/v1/{path}&limit={PAGE}&offset={offset}",
            headers=HEADERS,
            timeout=60,
        )
        r.raise_for_status()
        batch = r.json()
        rows.extend(batch)
        if len(batch) < PAGE:
            return rows
        offset += PAGE


async def main() -> int:
    if not (SUPABASE_URL and SUPABASE_KEY and BOT_DATABASE_URL):
        print("Faltan SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY / BOT_DATABASE_URL")
        return 1

    # Asegurar esquema del bot
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from agent import botdb  # noqa: E402

    if not await botdb.init():
        print("No se pudo inicializar la base propia del bot")
        return 1

    pool = await asyncpg.create_pool(BOT_DATABASE_URL, min_size=1, max_size=3)

    async with httpx.AsyncClient() as client:
        convs = await fetch_all(
            client,
            "whatsapp_conversations?select=id,phone,phone_number,prospect_name,city,status,last_message_at&order=id",
        )
        print(f"Conversaciones en Supabase: {len(convs)}")

        migrated_convs = 0
        migrated_msgs = 0
        skipped = 0

        for c in convs:
            raw_phone = c.get("phone") or c.get("phone_number") or ""
            digits = "".join(ch for ch in str(raw_phone) if ch.isdigit())
            if len(digits) == 10:
                digits = "57" + digits
            if len(digits) < 10:
                skipped += 1
                continue

            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    insert into bot_conversations (phone, prospect_name, city, status, last_message_at)
                    values ($1, $2, $3, coalesce($4, 'nuevo'), coalesce($5::timestamptz, now()))
                    on conflict (phone) do update
                        set prospect_name = coalesce(excluded.prospect_name, bot_conversations.prospect_name),
                            city = coalesce(excluded.city, bot_conversations.city)
                    returning id,
                        (select count(*) from bot_messages where conversation_id = bot_conversations.id) as msg_count
                    """,
                    digits,
                    c.get("prospect_name"),
                    c.get("city"),
                    c.get("status"),
                    _ts(c.get("last_message_at")),
                )
                bot_conv_id = row["id"]
                if row["msg_count"] and int(row["msg_count"]) > 0:
                    continue  # ya migrada — idempotencia

            msgs = await fetch_all(
                client,
                f"whatsapp_messages?conversation_id=eq.{c['id']}"
                "&select=role,direction,message_text,content,created_at&order=created_at",
            )
            if not msgs:
                migrated_convs += 1
                continue

            records = []
            for m in msgs:
                text = m.get("message_text") or m.get("content") or ""
                if not text:
                    continue
                role = m.get("role") or (
                    "user" if m.get("direction") == "inbound" else "assistant"
                )
                records.append((bot_conv_id, role, text, _ts(m.get("created_at"))))

            if records:
                async with pool.acquire() as conn:
                    await conn.executemany(
                        """
                        insert into bot_messages (conversation_id, role, content, created_at)
                        values ($1, $2, $3, coalesce($4::timestamptz, now()))
                        """,
                        records,
                    )
                migrated_msgs += len(records)
            migrated_convs += 1
            if migrated_convs % 100 == 0:
                print(f"  ... {migrated_convs} conversaciones, {migrated_msgs} mensajes")

    await pool.close()
    print(
        f"LISTO: {migrated_convs} conversaciones migradas, {migrated_msgs} mensajes copiados, {skipped} sin teléfono válido"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
