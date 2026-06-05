# agent/sync.py — Sincronizacion con Supabase (Mutuo backend)
# Envia cada mensaje y datos de prospecto a Supabase en tiempo real

import os
import logging
import httpx
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger("mutuo-bot")

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "")
BOT_SECRET = os.getenv("WHATSAPP_BOT_SECRET", "")


def _get_sync_url() -> str:
    """Construye la URL de la Edge Function whatsapp-sync."""
    return f"{SUPABASE_URL}/functions/v1/whatsapp-sync"


def _get_headers() -> dict:
    """Headers para llamar a la Edge Function."""
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {SUPABASE_ANON_KEY}",
        "x-bot-secret": BOT_SECRET,
    }


async def sync_message(
    phone: str,
    role: str,
    content: str,
    prospect_name: str = None,
    city: str = None,
    department: str = None,
    current_operator: str = None,
    interest: str = None,
    disc_profile: str = None,
):
    """Envia un mensaje a Supabase para trazabilidad."""
    if not SUPABASE_URL or not SUPABASE_ANON_KEY:
        logger.debug("Supabase no configurado, sync desactivado")
        return

    payload = {
        "action": "sync_message",
        "data": {
            "phone": phone,
            "role": role,
            "content": content,
        },
    }
    # Agregar datos opcionales del prospecto
    if prospect_name:
        payload["data"]["prospect_name"] = prospect_name
    if city:
        payload["data"]["city"] = city
    if department:
        payload["data"]["department"] = department
    if current_operator:
        payload["data"]["current_operator"] = current_operator
    if interest:
        payload["data"]["interest"] = interest
    if disc_profile:
        payload["data"]["disc_profile"] = disc_profile

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(_get_sync_url(), json=payload, headers=_get_headers())
            if r.status_code != 200:
                logger.warning(f"Sync error: {r.status_code} — {r.text}")
            else:
                logger.debug(f"Synced message for {phone}")
    except Exception as e:
        logger.warning(f"Sync failed (non-blocking): {e}")


async def update_conversation_status(
    phone: str,
    status: str,
    prospect_name: str = None,
    city: str = None,
    interest: str = None,
    notes: str = None,
):
    """Actualiza el estado de una conversacion en Supabase."""
    if not SUPABASE_URL or not SUPABASE_ANON_KEY:
        return

    payload = {
        "action": "update_status",
        "data": {
            "phone": phone,
            "status": status,
        },
    }
    if prospect_name:
        payload["data"]["prospect_name"] = prospect_name
    if city:
        payload["data"]["city"] = city
    if interest:
        payload["data"]["interest"] = interest
    if notes:
        payload["data"]["notes"] = notes

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(_get_sync_url(), json=payload, headers=_get_headers())
    except Exception as e:
        logger.warning(f"Status update sync failed: {e}")
