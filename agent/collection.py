# agent/collection.py — Cron de cobro embebido en el bot
# Mutuo Fintech — Origen IA
#
# CONTEXTO: el cobro automático (cartera) vivía en un cron de pg_cron dentro de
# Supabase que dispara las edge functions. Ese cron es frágil: si la base pierde
# el secreto del Vault, o el job se desprograma, el cobro deja de salir SIN error
# visible. El bot, en cambio, es un proceso que permanece vivo (ya corre la
# reactivación y los reportes desde aquí).
#
# Este módulo replica ese cron DENTRO del bot: una vez al día, en horario hábil
# de Colombia, invoca las edge functions de cobro con el service-role key (el
# mismo Bearer que usaba pg_cron). Las edge functions ya deduplican (no reenvían
# si ya cobraron en las últimas 24h o en el mismo tramo de mora), así que correr
# esto AUNQUE el pg_cron reviva NO genera cobros duplicados.

import os
import asyncio
import logging
from datetime import datetime, timezone, timedelta

import httpx

logger = logging.getLogger("mutuo-bot")

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "") or os.getenv("SUPABASE_KEY", "")
COL_TZ = timezone(timedelta(hours=-5))

# Hora (Colombia) a la que dispara el cobro diario. Configurable por si se quiere
# mover sin redeploy de código.
COLLECTION_HOUR = int(os.getenv("COLLECTION_HOUR", "9"))

# Edge functions de cobro a disparar, en orden. Cada una es idempotente.
#   - send-cartera-collection: cobro principal sobre payment_transactions
#     (agrupa por afiliado, manda el total con un link). Envía directo por Whapi.
#   - run-daily-whatsapp-collection: cobro sobre b2c_affiliations según reglas
#     editables (collection_settings + plantillas).
#   - process-whatsapp-collection-queue: drena la cola whatsapp_collection_log
#     (los cobros que quedaron 'pending' mientras Whapi estuvo caído).
COLLECTION_FUNCTIONS = [
    "send-cartera-collection",
    "run-daily-whatsapp-collection",
    "process-whatsapp-collection-queue",
]


def is_working_day() -> bool:
    """No cobrar los domingos (weekday 6)."""
    return datetime.now(COL_TZ).weekday() != 6


async def _invoke(func: str) -> dict:
    """Invoca una edge function de Supabase con el service-role key."""
    url = f"{SUPABASE_URL}/functions/v1/{func}"
    headers = {
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "apikey": SUPABASE_KEY,
        "Content-Type": "application/json",
    }
    try:
        # El cobro recorre toda la cartera: damos margen amplio de timeout.
        async with httpx.AsyncClient(timeout=300) as c:
            r = await c.post(url, headers=headers, json={})
            body = r.text[:500]
            if r.status_code == 200:
                logger.info(f"[COBRO] {func} OK: {body}")
                return {"func": func, "ok": True, "status": r.status_code}
            logger.error(f"[COBRO] {func} {r.status_code}: {body}")
            return {"func": func, "ok": False, "status": r.status_code, "body": body}
    except Exception as e:
        logger.error(f"[COBRO] {func} excepción: {e}")
        return {"func": func, "ok": False, "error": str(e)}


async def run_collection_now() -> list[dict]:
    """Dispara todas las edge functions de cobro una vez. Reutilizable desde un
    endpoint manual (/admin/run-collection) además del loop diario."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        logger.error("[COBRO] Falta SUPABASE_URL o SUPABASE_SERVICE_ROLE_KEY — no se puede cobrar")
        return [{"ok": False, "error": "supabase_no_configurado"}]

    results = []
    for func in COLLECTION_FUNCTIONS:
        results.append(await _invoke(func))
    return results


async def collection_loop():
    """Cron de cobro embebido. Dispara una vez al día a COLLECTION_HOUR (COL),
    en día hábil. Sobrevive errores: un fallo puntual no mata el loop."""
    logger.info(f"[COBRO] Scheduler iniciado (dispara {COLLECTION_HOUR}:00 COL, L-S)")

    if not SUPABASE_URL or not SUPABASE_KEY:
        logger.error("[COBRO] Sin SUPABASE_URL/SERVICE_ROLE_KEY — scheduler inactivo")
        return

    last_run_date = None  # fecha (COL) de la última corrida, para disparar 1×/día

    while True:
        try:
            now = datetime.now(COL_TZ)
            today = now.date()

            # Dispara una vez al día dentro de la franja [COLLECTION_HOUR, 19h).
            # Usamos un rango (no la hora exacta) para que un reinicio tardío del
            # bot igual cobre el mismo día, sin enviar fuera de horario.
            if (
                COLLECTION_HOUR <= now.hour < 19
                and last_run_date != today
                and is_working_day()
            ):
                logger.info(f"[COBRO] Disparando cobro diario ({today})")
                await run_collection_now()
                last_run_date = today

        except Exception as e:
            logger.error(f"[COBRO] Error en loop: {e}")

        # Revisar cada 10 min: suficiente para no perder la ventana de la hora.
        await asyncio.sleep(600)
