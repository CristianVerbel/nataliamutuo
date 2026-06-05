# agent/whapi_sync.py — Sincronización forzada del historial de WhatsApp
# Mutuo Fintech S.A.S. — Origen IA

import os
import asyncio
import logging
import httpx
from datetime import datetime, timezone

logger = logging.getLogger("mutuo-bot")

WHAPI_TOKEN = os.getenv("WHAPI_TOKEN", "") or os.getenv("WHAPI_API_KEY", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "") or os.getenv("SUPABASE_ANON_KEY", "")

SB_HEADERS = {
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "apikey": SUPABASE_KEY,
    "Content-Type": "application/json",
    "Prefer": "return=representation",
}


async def fetch_all_chats(limit: int = 500) -> list[dict]:
    """Obtiene todos los chats de Whapi."""
    if not WHAPI_TOKEN:
        return []

    headers = {"Authorization": f"Bearer {WHAPI_TOKEN}"}
    url = f"https://gate.whapi.cloud/chats?count={limit}"

    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.get(url, headers=headers)
            if r.status_code == 200:
                data = r.json()
                return data.get("chats", [])
            logger.error(f"[WHAPI-SYNC] Error fetch chats: {r.status_code} {r.text[:200]}")
    except Exception as e:
        logger.error(f"[WHAPI-SYNC] Excepción chats: {e}")
    return []


async def fetch_chat_messages(chat_id: str, limit: int = 100) -> list[dict]:
    """Obtiene todos los mensajes de un chat específico."""
    if not WHAPI_TOKEN:
        return []

    headers = {"Authorization": f"Bearer {WHAPI_TOKEN}"}
    url = f"https://gate.whapi.cloud/messages/list/{chat_id}?count={limit}"

    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.get(url, headers=headers)
            if r.status_code == 200:
                data = r.json()
                return data.get("messages", [])
    except Exception as e:
        logger.warning(f"[WHAPI-SYNC] Error messages {chat_id}: {e}")
    return []


async def upsert_conversation(phone: str, prospect_name: str = "") -> str | None:
    """Crea o actualiza conversación en Supabase. Retorna el id."""
    if not SUPABASE_URL:
        return None

    # Buscar si existe
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(
                f"{SUPABASE_URL}/rest/v1/whatsapp_conversations?phone=eq.{phone}&select=id&limit=1",
                headers=SB_HEADERS,
            )
            if r.status_code == 200:
                existing = r.json()
                if existing:
                    return existing[0]["id"]

            # Crear nueva
            payload = {
                "phone": phone,
                "prospect_name": prospect_name or None,
                "status": "en_progreso",
                "messages_count": 0,
            }
            r = await c.post(
                f"{SUPABASE_URL}/rest/v1/whatsapp_conversations",
                json=payload,
                headers=SB_HEADERS,
            )
            if r.status_code in (200, 201):
                data = r.json()
                return data[0]["id"] if isinstance(data, list) else data["id"]
    except Exception as e:
        logger.warning(f"[WHAPI-SYNC] Upsert conv error {phone}: {e}")
    return None


async def message_exists(conversation_id: str, content: str, timestamp: str) -> bool:
    """Verifica si un mensaje ya existe en Supabase (para evitar duplicados)."""
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(
                f"{SUPABASE_URL}/rest/v1/whatsapp_messages"
                f"?conversation_id=eq.{conversation_id}"
                f"&created_at=eq.{timestamp}"
                f"&select=id&limit=1",
                headers=SB_HEADERS,
            )
            if r.status_code == 200:
                return len(r.json()) > 0
    except Exception:
        pass
    return False


async def insert_message(conversation_id: str, role: str, content: str, created_at: str):
    """Inserta un mensaje en Supabase."""
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            await c.post(
                f"{SUPABASE_URL}/rest/v1/whatsapp_messages",
                json={
                    "conversation_id": conversation_id,
                    "role": role,
                    "content": content,
                    "created_at": created_at,
                },
                headers=SB_HEADERS,
            )
    except Exception as e:
        logger.warning(f"[WHAPI-SYNC] Insert msg error: {e}")


async def sync_all_history() -> dict:
    """
    Sincroniza TODOS los chats y mensajes de Whapi con Supabase.
    Útil después de una reconexión para recuperar mensajes perdidos.
    """
    if not WHAPI_TOKEN:
        return {"error": "WHAPI_TOKEN no configurado"}

    stats = {
        "chats_processed": 0,
        "chats_created": 0,
        "messages_synced": 0,
        "messages_skipped": 0,
        "errors": 0,
    }

    logger.info("[WHAPI-SYNC] Iniciando sincronización completa...")
    chats = await fetch_all_chats(limit=500)
    logger.info(f"[WHAPI-SYNC] {len(chats)} chats encontrados")

    for chat in chats:
        try:
            chat_id = chat.get("id", "")
            if not chat_id or "@g.us" in chat_id:  # Skip grupos
                continue

            phone = chat_id.replace("@s.whatsapp.net", "").replace("+", "")
            name = chat.get("name") or ""

            # Crear/obtener conversación
            conv_id = await upsert_conversation(phone, name)
            if not conv_id:
                stats["errors"] += 1
                continue

            # Traer mensajes
            messages = await fetch_chat_messages(chat_id, limit=100)

            for msg in messages:
                try:
                    msg_type = msg.get("type", "text")
                    body = ""
                    if msg_type == "text":
                        body = msg.get("text", {}).get("body", "")
                    elif msg_type in ("audio", "voice", "ptt"):
                        body = "[Audio]"
                    elif msg_type == "image":
                        body = "[Imagen]"
                    elif msg_type == "video":
                        body = "[Video]"
                    elif msg_type == "document":
                        body = "[Documento]"
                    else:
                        continue  # Skip otros tipos

                    if not body:
                        continue

                    from_me = msg.get("from_me", False)
                    role = "assistant" if from_me else "user"

                    # Timestamp
                    ts = msg.get("timestamp")
                    if ts:
                        created_at = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
                    else:
                        created_at = datetime.now(timezone.utc).isoformat()

                    # Evitar duplicados
                    if await message_exists(conv_id, body, created_at):
                        stats["messages_skipped"] += 1
                        continue

                    await insert_message(conv_id, role, body, created_at)
                    stats["messages_synced"] += 1
                except Exception as e:
                    logger.warning(f"[WHAPI-SYNC] Error msg: {e}")
                    stats["errors"] += 1

            stats["chats_processed"] += 1

            # Pequeña pausa para no abrumar Supabase
            await asyncio.sleep(0.1)

        except Exception as e:
            logger.error(f"[WHAPI-SYNC] Error chat: {e}")
            stats["errors"] += 1

    logger.info(f"[WHAPI-SYNC] Completado: {stats}")
    return stats


async def check_whapi_health() -> dict:
    """Verifica estado de Whapi. Retorna estado y detalles."""
    if not WHAPI_TOKEN:
        return {"status": "error", "healthy": False, "message": "WHAPI_TOKEN no configurado"}

    headers = {"Authorization": f"Bearer {WHAPI_TOKEN}"}

    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get("https://gate.whapi.cloud/health", headers=headers)
            if r.status_code == 200:
                data = r.json()
                health_status = data.get("status", {}).get("text", "unknown")

                # AUTH (conectado) = sano
                if health_status.lower() == "auth":
                    return {
                        "status": "ok",
                        "healthy": True,
                        "message": "Whapi conectado y funcionando",
                        "details": data,
                    }
                # INIT, LOADING, LAUNCH = cargando
                elif health_status.lower() in ("init", "loading", "launch"):
                    return {
                        "status": "loading",
                        "healthy": False,
                        "message": f"Whapi cargando (estado: {health_status})",
                        "details": data,
                    }
                # QR = desconectado, requiere escanear QR
                elif health_status.lower() == "qr":
                    return {
                        "status": "disconnected",
                        "healthy": False,
                        "message": "⚠️ WhatsApp desconectado — requiere escanear código QR",
                        "details": data,
                    }
                # BAN, FAILED, EXPIRED = bloqueado
                elif health_status.lower() in ("ban", "failed", "expired"):
                    return {
                        "status": "blocked",
                        "healthy": False,
                        "message": "🚫 WhatsApp bloqueado o suspendido por Meta",
                        "details": data,
                    }
                else:
                    return {
                        "status": "unknown",
                        "healthy": False,
                        "message": f"Estado desconocido: {health_status}",
                        "details": data,
                    }
            elif r.status_code == 401:
                return {
                    "status": "auth_error",
                    "healthy": False,
                    "message": "Token de Whapi inválido o expirado",
                }
            elif r.status_code == 429:
                return {
                    "status": "rate_limited",
                    "healthy": False,
                    "message": "Whapi rate limited — demasiadas peticiones",
                }
            else:
                return {
                    "status": "error",
                    "healthy": False,
                    "message": f"Whapi error HTTP {r.status_code}",
                }
    except httpx.TimeoutException:
        return {
            "status": "timeout",
            "healthy": False,
            "message": "Whapi no responde (timeout)",
        }
    except Exception as e:
        return {
            "status": "error",
            "healthy": False,
            "message": f"Error de red: {str(e)[:100]}",
        }
