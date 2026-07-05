# agent/memory.py — Memoria de conversaciones
# Desarrollado por Catalitico LLC para Mutuo Fintech S.A.S.
#
# Modo dual:
#  - Con BOT_DATABASE_URL (Postgres propia del bot en Railway): la memoria vive
#    en la base del bot → independiente del sistema principal. Si Supabase se
#    cae, el bot no pierde historial ni deja de conversar.
#  - Sin BOT_DATABASE_URL: comportamiento original (Supabase del sistema).
# En ambos modos se mantiene el respaldo en RAM.

import os
import logging
import httpx
from datetime import datetime
from collections import defaultdict
from dotenv import load_dotenv

from agent import botdb

load_dotenv()
logger = logging.getLogger("mutuo-bot")

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "") or os.getenv("SUPABASE_KEY", "") or os.getenv("SUPABASE_ANON_KEY", "")

HEADERS = {
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "apikey": SUPABASE_KEY,
    "Content-Type": "application/json",
    "Prefer": "return=minimal",
}

# In-memory fallback: si Supabase falla, al menos mantenemos historial en RAM
_local_history: dict[str, list[dict]] = defaultdict(list)
MAX_LOCAL_HISTORY = 300


def _normalize_phone(phone: str) -> str:
    clean = phone.split("@")[0].replace("+", "").replace(" ", "").replace("-", "")
    if len(clean) == 10:
        clean = "57" + clean
    return clean


async def inicializar_db():
    # Base propia del bot primero (independencia del sistema principal).
    if botdb.enabled():
        if await botdb.init():
            logger.info("Memoria: usando Postgres PROPIA del bot (BOT_DATABASE_URL)")
            return
        logger.error("Memoria: BOT_DATABASE_URL definido pero la base no inició — uso Supabase")

    if not SUPABASE_URL or not SUPABASE_KEY:
        logger.error("MEMORIA: SUPABASE_URL o SUPABASE_KEY no configurados")
        return
    # Test connection
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(
                f"{SUPABASE_URL}/rest/v1/whatsapp_conversations?select=id&limit=1",
                headers={**HEADERS, "Prefer": ""},
            )
            if r.status_code == 200:
                logger.info(f"Memoria: Supabase OK (KEY={SUPABASE_KEY[:8]}...)")
            else:
                logger.error(f"Memoria: Supabase FALLO status={r.status_code} body={r.text[:200]}")
    except Exception as e:
        logger.error(f"Memoria: Supabase conexion FALLIDA: {e}")


async def guardar_mensaje(telefono: str, role: str, content: str):
    phone = _normalize_phone(telefono)

    # Siempre guardar en local (RAM) como respaldo
    _local_history[phone].append({"role": role, "content": content})
    if len(_local_history[phone]) > MAX_LOCAL_HISTORY:
        _local_history[phone] = _local_history[phone][-MAX_LOCAL_HISTORY:]

    # Base propia del bot: fuente de verdad de la memoria conversacional.
    if botdb.ready():
        if await botdb.save_message(phone, role, content):
            return
        logger.warning(f"[MEMORIA] botdb no guardó; intento Supabase para {phone}")

    if not SUPABASE_URL or not SUPABASE_KEY:
        return

    conv_id = await _get_or_create_conversation(phone)
    if not conv_id:
        logger.warning(f"[MEMORIA] No se pudo obtener/crear conversacion para {phone}")
        return

    try:
        direction = "inbound" if role == "user" else "outbound"
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                f"{SUPABASE_URL}/rest/v1/whatsapp_messages",
                headers=HEADERS,
                json={
                    "conversation_id": conv_id,
                    "role": role,
                    "direction": direction,
                    "message_text": content,
                    "content": content,
                },
            )
            if r.status_code not in (200, 201):
                logger.error(f"[MEMORIA] Error guardando mensaje: status={r.status_code} body={r.text[:200]}")
            else:
                logger.info(f"[MEMORIA] Mensaje guardado OK para {phone} role={role}")
    except Exception as e:
        logger.error(f"[MEMORIA] Excepcion guardando mensaje: {e}")


async def obtener_historial(telefono: str, limite: int = 200) -> list[dict]:
    phone = _normalize_phone(telefono)

    # Base propia del bot primero.
    if botdb.ready():
        hist = await botdb.get_history(phone, limite)
        if hist:
            logger.info(f"[MEMORIA] Historial botdb: {len(hist)} mensajes para {phone}")
            return hist
        # Sin historial en botdb: caer a RAM (no a Supabase — botdb es la fuente).
        local = _local_history.get(phone, [])
        if local:
            return local[-limite:]
        return []

    # Intentar Supabase primero
    if SUPABASE_URL and SUPABASE_KEY:
        conv_id = await _get_conversation_id(phone)
        if conv_id:
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    r = await client.get(
                        f"{SUPABASE_URL}/rest/v1/whatsapp_messages"
                        f"?conversation_id=eq.{conv_id}"
                        f"&select=role,message_text,content,direction"
                        f"&order=created_at.desc"
                        f"&limit={limite}",
                        headers={**HEADERS, "Prefer": ""},
                    )
                    if r.status_code == 200:
                        msgs = r.json()
                        if msgs:
                            msgs.reverse()
                            result = []
                            for m in msgs:
                                text = m.get("message_text") or m.get("content") or ""
                                role_val = m.get("role") or ("user" if m.get("direction") == "inbound" else "assistant")
                                if text:
                                    result.append({"role": role_val, "content": text})
                            logger.info(f"[MEMORIA] Historial Supabase: {len(result)} mensajes para {phone}")
                            return result
                        else:
                            logger.info(f"[MEMORIA] Supabase vacio para {phone}, conv_id={conv_id}")
                    else:
                        logger.error(f"[MEMORIA] Error obteniendo historial: status={r.status_code}")
            except Exception as e:
                logger.error(f"[MEMORIA] Excepcion obteniendo historial: {e}")

    # Fallback: historial local en RAM
    local = _local_history.get(phone, [])
    if local:
        logger.info(f"[MEMORIA] Usando historial LOCAL: {len(local)} mensajes para {phone}")
        return local[-limite:]

    logger.info(f"[MEMORIA] Sin historial para {phone}")
    return []


async def guardar_lead(telefono: str, nombre: str = None, ciudad: str = None, interes: str = None):
    if botdb.ready():
        await botdb.update_lead(_normalize_phone(telefono), nombre, ciudad)
        return
    if not SUPABASE_URL or not SUPABASE_KEY:
        return
    try:
        update = {}
        if nombre:
            update["prospect_name"] = nombre
            update["contact_name"] = nombre
        if ciudad:
            update["city"] = ciudad
        if update:
            phone = _normalize_phone(telefono)
            async with httpx.AsyncClient(timeout=10) as client:
                await client.patch(
                    f"{SUPABASE_URL}/rest/v1/whatsapp_conversations?phone=eq.{phone}",
                    headers=HEADERS,
                    json=update,
                )
    except Exception as e:
        logger.warning(f"Error guardando lead: {e}")


async def obtener_leads(estado: str = None) -> list[dict]:
    return []


async def asegurar_historial_en_supabase(telefono: str) -> str | None:
    """Garantiza que el historial quede en Supabase al momento de afiliar.

    Crea la conversación si no existe y, si Supabase no tiene mensajes (porque la
    sincronización en vivo falló o el chat solo quedó en RAM), vuelca el historial
    local. Así el perfil del afiliado en el admin SIEMPRE muestra la conversación.
    Devuelve el conversation_id.
    """
    if not SUPABASE_URL or not SUPABASE_KEY:
        return None
    phone = _normalize_phone(telefono)
    conv_id = await _get_or_create_conversation(phone)
    if not conv_id:
        logger.warning(f"[MEMORIA] asegurar_historial: sin conv_id para {phone}")
        return None

    # ¿Ya hay mensajes en Supabase? Si sí, no duplicamos.
    existing = 0
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"{SUPABASE_URL}/rest/v1/whatsapp_messages"
                f"?conversation_id=eq.{conv_id}&select=id",
                headers={**HEADERS, "Prefer": "count=exact", "Range": "0-0"},
            )
            cr = r.headers.get("content-range", "")
            tail = cr.split("/")[-1] if "/" in cr else ""
            existing = int(tail) if tail.isdigit() else 0
    except Exception as e:
        logger.warning(f"[MEMORIA] conteo de mensajes falló: {e}")

    if existing > 0:
        return conv_id

    # Volcar el historial que tengamos (base propia del bot, o RAM) a Supabase,
    # para que el perfil del afiliado en el admin muestre la conversación.
    local: list[dict] = []
    if botdb.ready():
        local = await botdb.get_history(phone, 300)
    if not local:
        local = _local_history.get(phone, [])
    if not local:
        return conv_id
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            for m in local:
                role = m.get("role")
                content = m.get("content")
                if not content:
                    continue
                await client.post(
                    f"{SUPABASE_URL}/rest/v1/whatsapp_messages",
                    headers=HEADERS,
                    json={
                        "conversation_id": conv_id,
                        "role": role,
                        "direction": "inbound" if role == "user" else "outbound",
                        "message_text": content,
                        "content": content,
                    },
                )
        logger.info(f"[MEMORIA] Historial RAM volcado a Supabase ({len(local)} msgs) para {phone}")
    except Exception as e:
        logger.warning(f"[MEMORIA] volcado de historial falló: {e}")

    return conv_id


async def _get_conversation_id(phone: str) -> str | None:
    digits = _normalize_phone(phone)
    # La BD puede tener el teléfono con o sin prefijo '+'. Buscamos ambos.
    with_plus = f"%2B{digits}"  # URL-encoded '+'
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"{SUPABASE_URL}/rest/v1/whatsapp_conversations"
                f"?or=(phone.eq.{digits},phone.eq.+{digits},phone_number.eq.{digits},phone_number.eq.+{digits})"
                f"&select=id"
                # CRÍTICO: si hay conversaciones duplicadas para el mismo teléfono,
                # SIEMPRE devolvemos la misma (la de actividad más reciente). Sin este
                # orden, PostgREST devuelve una fila arbitraria y el bot termina leyendo
                # el historial de una conversación y escribiendo en otra → "pierde la
                # memoria" a mitad de la afiliación. El orden hace que lectura y
                # escritura coincidan SIEMPRE en la misma fila canónica.
                f"&order=last_message_at.desc.nullslast"
                f"&limit=1",
                headers={**HEADERS, "Prefer": ""},
            )
            if r.status_code == 200:
                data = r.json()
                if data:
                    return data[0]["id"]
    except Exception as e:
        logger.warning(f"Error buscando conversation: {e}")
    return None


async def _get_or_create_conversation(phone: str) -> str | None:
    conv_id = await _get_conversation_id(phone)
    digits = _normalize_phone(phone)
    # Usar formato con '+' para coincidir con lo que guarda el webhook de Whapi
    phone_with_plus = f"+{digits}"

    if conv_id:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.patch(
                    f"{SUPABASE_URL}/rest/v1/whatsapp_conversations"
                    f"?or=(phone.eq.{digits},phone.eq.+{digits},phone_number.eq.{digits},phone_number.eq.+{digits})",
                    headers=HEADERS,
                    json={"last_message_at": datetime.utcnow().isoformat()},
                )
        except Exception as e:
            logger.debug(f"Error actualizando last_message_at: {e}")
        return conv_id

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                f"{SUPABASE_URL}/rest/v1/whatsapp_conversations",
                headers={**HEADERS, "Prefer": "return=representation"},
                json={
                    "phone": phone_with_plus,
                    "phone_number": phone_with_plus,
                    "status": "nuevo",
                    "handoff_status": "bot",
                    "last_message_at": datetime.utcnow().isoformat(),
                },
            )
            if r.status_code in (200, 201):
                data = r.json()
                conv_id = data[0]["id"] if isinstance(data, list) else data.get("id")
                logger.info(f"[MEMORIA] Conversacion creada para {digits}: {conv_id}")
                return conv_id
            elif r.status_code == 409:
                # El índice único anti-duplicados ya tiene una conversación para
                # este teléfono (carrera entre dos mensajes entrantes). NO es error:
                # re-buscamos la fila existente en vez de crear un duplicado.
                logger.info(f"[MEMORIA] Conversacion ya existe (carrera) para {digits}; re-buscando")
                return await _get_conversation_id(phone)
            else:
                logger.error(f"[MEMORIA] Error creando conversacion: status={r.status_code} body={r.text[:200]}")
    except Exception as e:
        logger.error(f"[MEMORIA] Excepcion creando conversacion: {e}")

    return None
