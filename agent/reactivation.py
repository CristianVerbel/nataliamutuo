# agent/reactivation.py — Reactivación de leads inactivos
# Mutuo Fintech S.A.S. — Origen IA
#
# Contacta leads que no respondieron después de X horas.
# Solo en horario hábil: L-V 7am-7pm, Sáb 7am-4pm, nunca Domingo.

import os
import asyncio
import logging
from datetime import datetime, timezone, timedelta

import httpx

logger = logging.getLogger("mutuo-bot")

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "") or os.getenv("SUPABASE_ANON_KEY", "")
COL_TZ = timezone(timedelta(hours=-5))

SB_HEADERS = {
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "apikey": SUPABASE_KEY,
    "Content-Type": "application/json",
}

# Config
REACTIVATION_DELAY_HOURS = 24  # Reactivar si no responden en 24h
MAX_REACTIVATIONS = 2  # Máximo 2 intentos de reactivación
BATCH_SIZE = 10  # Leads por ciclo


def is_working_hours() -> bool:
    """Verifica horario hábil: L-V 7am-7pm, Sáb 7am-4pm, nunca Domingo."""
    now = datetime.now(COL_TZ)
    weekday = now.weekday()  # 0=Lunes, 6=Domingo
    hour = now.hour

    if weekday == 6:  # Domingo
        return False
    if weekday == 5:  # Sábado
        return 7 <= hour < 16
    # Lunes a Viernes
    return 7 <= hour < 19


async def fetch_inactive_leads() -> list[dict]:
    """Busca leads que no han respondido en las últimas REACTIVATION_DELAY_HOURS horas."""
    if not SUPABASE_URL:
        return []

    # Usar sufijo 'Z' en vez de '+00:00': el '+' sin URL-encodear rompe el
    # filtro de PostgREST (lo interpreta como espacio → error 22007).
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=REACTIVATION_DELAY_HOURS)).isoformat().replace("+00:00", "Z")

    try:
        async with httpx.AsyncClient(timeout=15) as c:
            # Leads en_progreso o caliente que no tienen actividad reciente
            # y que NO están excluidos, ganados, perdidos o ya en gestión humana
            r = await c.get(
                f"{SUPABASE_URL}/rest/v1/whatsapp_conversations"
                f"?status=in.(en_progreso,caliente,no_responde)"
                f"&last_message_at=lt.{cutoff}"
                f"&handoff_status=eq.bot"
                f"&select=id,phone,prospect_name,city,messages_count,last_message_at,notes,interest"
                f"&order=last_message_at.asc"
                f"&limit={BATCH_SIZE}",
                headers=SB_HEADERS,
            )
            if r.status_code == 200:
                leads = r.json()
                # Filtrar los que ya tuvieron demasiados reintentos
                leads = [l for l in leads if _get_reactivation_count(l) < MAX_REACTIVATIONS]
                # Excluir clientes que YA están afiliados: a un afiliado no se le
                # ofrece el plan de prospección (genera confusión y desconfianza).
                leads = await _excluir_afiliados(c, leads)
                return leads
            logger.error(f"[REACTIVATION] Fetch error: {r.status_code}")
    except Exception as e:
        logger.error(f"[REACTIVATION] Error: {e}")
    return []


def _phone_variants(phone: str) -> list[str]:
    """Variantes de un teléfono para cruzar contra b2c_affiliations.

    Las afiliaciones guardan el número en formatos distintos (con/sin '57',
    con/sin '+'). Generamos las variantes más comunes para no fallar el match.
    """
    digits = "".join(ch for ch in (phone or "") if ch.isdigit())
    local = digits[-10:]
    return list({digits, local, f"57{local}", f"+57{local}"})


async def _excluir_afiliados(c: httpx.AsyncClient, leads: list[dict]) -> list[dict]:
    """Quita de la lista de reactivación los teléfonos que ya tienen una
    afiliación activa en b2c_affiliations. A un cliente afiliado no se le debe
    enviar el copy de prospección ('¿pudiste pensar en el plan...?')."""
    if not leads:
        return leads

    variantes: set[str] = set()
    for l in leads:
        variantes.update(_phone_variants(l.get("phone", "")))
    variantes = {v for v in variantes if v}
    if not variantes:
        return leads

    try:
        or_filter = ",".join(f"phone.eq.{v}" for v in variantes)
        r = await c.get(
            f"{SUPABASE_URL}/rest/v1/b2c_affiliations"
            f"?or=({or_filter})"
            f"&is_active=eq.true"
            f"&select=phone",
            headers=SB_HEADERS,
        )
        if r.status_code != 200:
            logger.warning(f"[REACTIVATION] No se pudo verificar afiliados: {r.status_code}")
            return leads

        afiliados: set[str] = set()
        for row in r.json():
            for v in _phone_variants(row.get("phone", "")):
                afiliados.add(v)

        filtrados = [
            l for l in leads
            if not (set(_phone_variants(l.get("phone", ""))) & afiliados)
        ]
        excluidos = len(leads) - len(filtrados)
        if excluidos:
            logger.info(f"[REACTIVATION] {excluidos} lead(s) omitidos por ser afiliados activos")
        return filtrados
    except Exception as e:
        logger.warning(f"[REACTIVATION] Error verificando afiliados: {e}")
        return leads


def _get_reactivation_count(lead: dict) -> int:
    """Cuenta cuántas reactivaciones ha tenido un lead (guardado en notes)."""
    notes = lead.get("notes") or ""
    return notes.count("[REACTIVADO]")


def build_reactivation_message(lead: dict, attempt: int) -> str:
    """Genera el mensaje de reactivación personalizado según el perfil del lead."""
    nombre = ""
    if lead.get("prospect_name"):
        nombre = lead["prospect_name"].split()[0].title()

    saludo = f"{nombre}, " if nombre else ""
    plan = lead.get("interest", "")
    plan_txt = f"el {plan}" if plan else "el plan de protección familiar"

    if attempt == 0:
        return (
            f"Hola {saludo}¿pudiste pensar en {plan_txt}? "
            f"Sigo disponible para ayudarte cuando quieras 😊"
        )
    else:
        return (
            f"{saludo}no quiero interrumpirte. ¿Prefieres que un asesor te contacte para explicarte mejor el plan de Mutuo? "
            f"¿Te llamo o prefieres que no te contactemos más?"
        )


async def mark_reactivated(lead_id: str, notes: str):
    """Marca el lead como reactivado."""
    new_notes = (notes or "") + f" [REACTIVADO:{datetime.now(COL_TZ).strftime('%d/%m %H:%M')}]"
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            await c.patch(
                f"{SUPABASE_URL}/rest/v1/whatsapp_conversations?id=eq.{lead_id}",
                json={
                    "notes": new_notes,
                    "last_message_at": datetime.now(timezone.utc).isoformat(),
                },
                headers=SB_HEADERS,
            )
    except Exception as e:
        logger.warning(f"[REACTIVATION] Mark error: {e}")


async def mark_excluded(phone: str):
    """Marca un lead como excluido (solicitó no ser contactado)."""
    phone_clean = phone.replace("+", "").replace(" ", "")
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            await c.patch(
                f"{SUPABASE_URL}/rest/v1/whatsapp_conversations?phone=eq.{phone_clean}",
                json={
                    "status": "descartado",
                    "handoff_status": "lost",
                    "bot_paused": True,
                    "notes": f"[EXCLUSION:{datetime.now(COL_TZ).strftime('%d/%m/%Y %H:%M')}] Cliente solicitó no ser contactado",
                },
                headers=SB_HEADERS,
            )
            logger.info(f"[EXCLUSION] {phone_clean} marcado como excluido")
    except Exception as e:
        logger.error(f"[EXCLUSION] Error: {e}")


async def classify_no_responde():
    """Clasifica como 'no_responde' las conversaciones en_progreso donde:
    - last_message_at > 24h ago
    - El último mensaje fue del bot (role=assistant) — el cliente no respondió
    - handoff_status = 'bot' (no tocar las gestionadas por humanos)
    """
    if not SUPABASE_URL:
        return

    # Sufijo 'Z' para evitar el '+' sin encodear en la URL (PostgREST 22007).
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat().replace("+00:00", "Z")

    try:
        async with httpx.AsyncClient(timeout=15) as c:
            # Fetch en_progreso conversations older than 24h managed by bot
            r = await c.get(
                f"{SUPABASE_URL}/rest/v1/whatsapp_conversations"
                f"?status=eq.en_progreso"
                f"&last_message_at=lt.{cutoff}"
                f"&handoff_status=eq.bot"
                f"&select=id,phone,prospect_name"
                f"&limit=100",
                headers=SB_HEADERS,
            )
            if r.status_code != 200:
                logger.error(f"[NO_RESPONDE] Fetch conversations error: {r.status_code}")
                return

            candidates = r.json()
            if not candidates:
                logger.debug("[NO_RESPONDE] No hay candidatos para clasificar")
                return

            classified = 0
            for conv in candidates:
                # Check that the last message was from the bot (assistant)
                msg_r = await c.get(
                    f"{SUPABASE_URL}/rest/v1/whatsapp_messages"
                    f"?conversation_id=eq.{conv['id']}"
                    f"&select=role"
                    f"&order=created_at.desc"
                    f"&limit=1",
                    headers=SB_HEADERS,
                )
                if msg_r.status_code != 200:
                    continue

                last_msgs = msg_r.json()
                if not last_msgs or last_msgs[0].get("role") != "assistant":
                    continue  # Last message was from user, skip

                # Update status to no_responde
                await c.patch(
                    f"{SUPABASE_URL}/rest/v1/whatsapp_conversations?id=eq.{conv['id']}",
                    json={"status": "no_responde"},
                    headers={**SB_HEADERS, "Prefer": "return=minimal"},
                )
                classified += 1

            if classified > 0:
                logger.info(f"[NO_RESPONDE] {classified} conversaciones clasificadas como no_responde")
            else:
                logger.debug("[NO_RESPONDE] Ninguna conversación clasificada")

    except Exception as e:
        logger.error(f"[NO_RESPONDE] Error: {e}")


async def reactivation_loop(proveedor, crm):
    """Loop de reactivación de leads inactivos. Corre cada 2 horas."""
    logger.info("[REACTIVATION] Scheduler iniciado")

    while True:
        try:
            if is_working_hours():
                leads = await fetch_inactive_leads()
                if leads:
                    logger.info(f"[REACTIVATION] {len(leads)} leads para reactivar")

                for lead in leads:
                    attempt = _get_reactivation_count(lead)
                    phone = lead.get("phone", "").replace("+", "").replace(" ", "")
                    if not phone:
                        continue

                    msg = build_reactivation_message(lead, attempt)

                    # Enviar mensaje
                    ok = await proveedor.enviar_mensaje(phone, msg)
                    if ok:
                        # Registrar en CRM
                        await crm.sync_message(phone, "assistant", msg, {
                            "prospect_name": lead.get("prospect_name"),
                            "city": lead.get("city"),
                        })
                        await mark_reactivated(lead["id"], lead.get("notes", ""))
                        logger.info(f"[REACTIVATION] Enviado a {phone} (intento {attempt + 1})")

                    # Delay entre mensajes (45-60s)
                    await asyncio.sleep(50)

                if not leads:
                    logger.info("[REACTIVATION] Sin leads para reactivar")

                # ── Auto-classify "no_responde" after 24h ──
                await classify_no_responde()

            else:
                logger.debug("[REACTIVATION] Fuera de horario hábil")

        except Exception as e:
            logger.error(f"[REACTIVATION] Error en loop: {e}")

        # Esperar 1 hora
        await asyncio.sleep(3600)


# NOTA: Se removió `reactivate_stale_human_conversations` deliberadamente.
# El bot NUNCA debe retomar automáticamente una conversación marcada como
# human_active. Solo retoma cuando el asesor pulsa explícitamente
# "Reactivar Bot" en la UI (que escribe handoff_status="bot").
