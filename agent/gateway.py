# agent/gateway.py — Cliente del bot-gateway del sistema principal
# Mutuo Fintech — Bot WhatsApp
#
# Contrato único bot ⇄ sistema: en vez de tocar tablas del sistema directo,
# el bot pide acciones con nombre al edge function `bot-gateway`, autenticado
# con el secreto compartido WHATSAPP_BOT_SECRET.
#
# Diseño de resiliencia:
#  - timeout corto (5s) + 1 reintento: una caída del sistema NO cuelga la
#    conversación.
#  - toda función devuelve None/[] en fallo: el llamador degrada con gracia
#    (responde sin datos de cuenta) en vez de romper.
#  - apagado por defecto: se activa con BOT_USE_GATEWAY=1. Sin la variable,
#    el bot sigue usando sus accesos directos actuales (rollout seguro).

import os
import logging

import httpx
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger("mutuo-bot")

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
BOT_SECRET = os.getenv("WHATSAPP_BOT_SECRET", "")
GATEWAY_URL = os.getenv("BOT_GATEWAY_URL", f"{SUPABASE_URL}/functions/v1/bot-gateway" if SUPABASE_URL else "")

TIMEOUT_S = float(os.getenv("BOT_GATEWAY_TIMEOUT", "5"))


def enabled() -> bool:
    """El gateway se usa solo si está explícitamente activado y configurado."""
    return (
        os.getenv("BOT_USE_GATEWAY", "").lower() in ("1", "true", "yes")
        and bool(GATEWAY_URL)
        and bool(BOT_SECRET)
    )


async def call(action: str, payload: dict | None = None) -> dict | list | None:
    """Llama una acción del gateway. Devuelve `data` o None si falla.

    Nunca lanza: la conversación no debe romperse porque el sistema esté caído.
    """
    if not GATEWAY_URL or not BOT_SECRET:
        return None
    body = {"action": action, "payload": payload or {}}
    headers = {"x-bot-secret": BOT_SECRET, "Content-Type": "application/json"}
    for intento in (1, 2):
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
                r = await client.post(GATEWAY_URL, json=body, headers=headers)
                if r.status_code == 200:
                    data = r.json()
                    if data.get("ok"):
                        return data.get("data")
                    logger.warning(f"[GATEWAY] {action} respondió error: {data.get('error')}")
                    return None
                logger.warning(f"[GATEWAY] {action} HTTP {r.status_code} (intento {intento})")
        except Exception as e:
            logger.warning(f"[GATEWAY] {action} falló (intento {intento}): {e}")
    return None


# ── Helpers tipados por acción ──────────────────────────────────────────────

async def affiliate_lookup(phone: str) -> dict | None:
    """Afiliación más reciente para un teléfono, o None (sin datos / sistema caído)."""
    data = await call("affiliate.lookup", {"phone": phone})
    return data if isinstance(data, dict) else None


async def affiliate_debt(affiliation_id: str) -> dict | None:
    """{transactions, total, months} de cuotas pendientes, o None."""
    data = await call("affiliate.debt", {"affiliation_id": affiliation_id})
    return data if isinstance(data, dict) else None


async def plans_list() -> list:
    data = await call("plans.list")
    return data if isinstance(data, list) else []


async def audit_log(affiliation_id: str | None, event_type: str, description: str = "", metadata: dict | None = None) -> None:
    """Auditoría fire-and-forget: no bloquea ni falla la conversación."""
    await call("audit.log", {
        "affiliation_id": affiliation_id,
        "event_type": event_type,
        "description": description,
        "metadata": metadata,
    })
