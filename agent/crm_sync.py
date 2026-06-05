# agent/crm_sync.py — Sincronización en tiempo real con whatsapp_conversations / whatsapp_messages
# Cada mensaje entrante/saliente se refleja en Supabase para el módulo CRM de ventas.mutuo.la

import os
import logging
import httpx
from datetime import datetime, timezone

logger = logging.getLogger("mutuo-bot")

SB_URL = os.getenv("SUPABASE_URL", "")
SB_KEY = (os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_KEY") or os.getenv("SUPABASE_ANON_KEY", ""))

_HEADERS = lambda: {
    "Authorization": f"Bearer {SB_KEY}",
    "apikey": SB_KEY,
    "Content-Type": "application/json",
    "Prefer": "return=representation",
}


def _norm(phone: str) -> str:
    raw = phone.split("@")[0].replace("+", "").replace(" ", "").replace("-", "")
    local = raw[-10:] if len(raw) >= 10 else raw
    return f"57{local}"


async def get_or_create_conversation(phone: str, prospect_name: str = None) -> str | None:
    """Devuelve el ID de la conversación en whatsapp_conversations, creándola si no existe."""
    if not SB_URL or not SB_KEY:
        return None
    phone_norm = _norm(phone)
    try:
        async with httpx.AsyncClient(timeout=10) as http:
            # Buscar existente
            r = await http.get(
                f"{SB_URL}/rest/v1/whatsapp_conversations?phone=eq.{phone_norm}&select=id&limit=1",
                headers={**_HEADERS(), "Prefer": ""},
            )
            if r.status_code == 200 and r.json():
                return r.json()[0]["id"]

            # Crear nueva
            payload = {
                "phone": phone_norm,
                "status": "nuevo",
                "handoff_status": "bot",
                "last_message_at": datetime.now(timezone.utc).isoformat(),
            }
            if prospect_name:
                payload["prospect_name"] = prospect_name

            r = await http.post(
                f"{SB_URL}/rest/v1/whatsapp_conversations",
                headers=_HEADERS(),
                json=payload,
            )
            if r.status_code in (200, 201):
                data = r.json()
                conv_id = data[0]["id"] if isinstance(data, list) else data.get("id")
                logger.info(f"[CRM] Conversación creada {conv_id} para {phone_norm}")
                return conv_id
            else:
                logger.error(f"[CRM] Error creando conversación: {r.status_code} {r.text[:400]}")
                return None
    except Exception as e:
        logger.error(f"[CRM] get_or_create_conversation error: {e}", exc_info=True)
        raise  # re-raise so the test endpoint can capture it
    return None


async def save_message(conv_id: str, role: str, content: str, ts: datetime = None) -> None:
    """Guarda un mensaje en whatsapp_messages."""
    if not SB_URL or not SB_KEY or not conv_id:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as http:
            await http.post(
                f"{SB_URL}/rest/v1/whatsapp_messages",
                headers={**_HEADERS(), "Prefer": "return=minimal"},
                json={
                    "conversation_id": conv_id,
                    "role": role,
                    "content": content,
                    "created_at": (ts or datetime.now(timezone.utc)).isoformat(),
                },
            )
    except Exception as e:
        logger.warning(f"[CRM] save_message error: {e}")


async def update_conversation(conv_id: str, **fields) -> None:
    """Actualiza campos de la conversación (prospect_name, city, status, last_message_at, etc.)."""
    if not SB_URL or not SB_KEY or not conv_id:
        return
    fields.setdefault("last_message_at", datetime.now(timezone.utc).isoformat())
    try:
        async with httpx.AsyncClient(timeout=10) as http:
            await http.patch(
                f"{SB_URL}/rest/v1/whatsapp_conversations?id=eq.{conv_id}",
                headers={**_HEADERS(), "Prefer": "return=minimal"},
                json=fields,
            )
    except Exception as e:
        logger.warning(f"[CRM] update_conversation error: {e}")


async def sync_inbound(phone: str, content: str, prospect_name: str = None,
                       city: str = None, department: str = None,
                       interest: str = None, disc_profile: str = None,
                       current_operator: str = None, ts: datetime = None) -> str | None:
    """Sincroniza un mensaje entrante del cliente. Retorna conv_id."""
    conv_id = await get_or_create_conversation(phone, prospect_name)
    if not conv_id:
        return None
    await save_message(conv_id, "user", content, ts)
    updates = {"last_message_at": (ts or datetime.now(timezone.utc)).isoformat()}
    if prospect_name:
        updates["prospect_name"] = prospect_name
    if city:
        updates["city"] = city
    if department:
        updates["department"] = department
    if interest:
        updates["interest"] = interest
    if disc_profile:
        updates["disc_profile"] = disc_profile
    if current_operator:
        updates["current_operator"] = current_operator
    await update_conversation(conv_id, **updates)
    return conv_id


async def sync_outbound(phone: str, content: str, conv_id: str = None, ts: datetime = None) -> None:
    """Sincroniza un mensaje saliente del bot."""
    if not conv_id:
        conv_id = await get_or_create_conversation(phone)
    if conv_id:
        await save_message(conv_id, "assistant", content, ts)
        await update_conversation(conv_id, last_message_at=(ts or datetime.now(timezone.utc)).isoformat())
