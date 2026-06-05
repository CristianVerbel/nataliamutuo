# agent/auto_assign.py — Auto-asignación de chats a asesores
# Mutuo Fintech S.A.S.

import os
import logging
import httpx

logger = logging.getLogger("mutuo-bot")

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "") or os.getenv("SUPABASE_ANON_KEY", "")
SB_HEADERS = {
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "apikey": SUPABASE_KEY,
    "Content-Type": "application/json",
}


async def get_assignment_config() -> dict | None:
    """Obtiene la configuración de auto-asignación. Retorna None si no existe la tabla."""
    return None


async def get_active_advisors() -> list[dict]:
    """Obtiene asesores activos para asignación, ordenados para round-robin."""
    if not SUPABASE_URL:
        return []
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(
                f"{SUPABASE_URL}/rest/v1/advisors?active=eq.true&select=id,nombre,apellido,last_assigned_at,current_chats&order=last_assigned_at.asc.nullsfirst",
                headers=SB_HEADERS,
            )
            if r.status_code == 200:
                return r.json()
    except Exception as e:
        logger.warning(f"[ASSIGN] Error advisors: {e}")
    return []


async def auto_assign_conversation(conversation_id: str, phone: str) -> str | None:
    """Auto-asigna una conversación nueva al siguiente asesor disponible.
    Retorna el advisor_id asignado o None si no aplica."""
    config = await get_assignment_config()

    # Usar defaults si no hay config
    if config is None:
        enabled = True
        auto_assign_new = True
        mode = "round_robin"
        max_chats = 50
    else:
        if not config.get("enabled") or not config.get("auto_assign_new"):
            return None
        mode = config.get("mode", "round_robin")
        max_chats = config.get("max_chats_per_advisor", 50)

    advisors = await get_active_advisors()
    if not advisors:
        logger.info("[ASSIGN] No hay asesores activos para asignación")
        return None

    # Filtrar asesores que no estén al tope
    available = [a for a in advisors if (a.get("current_chats") or 0) < max_chats]
    if not available:
        logger.warning("[ASSIGN] Todos los asesores al tope de chats")
        return None

    # Seleccionar asesor según modo
    if mode == "round_robin":
        # El primero es el que tiene last_assigned_at más antiguo (o null)
        selected = available[0]
    elif mode == "least_busy":
        selected = min(available, key=lambda a: a.get("current_chats") or 0)
    else:
        return None  # manual = no auto-asignar

    advisor_id = selected.get("id")
    if not advisor_id:
        return None

    try:
        async with httpx.AsyncClient(timeout=10) as c:
            # Asignar conversación
            await c.patch(
                f"{SUPABASE_URL}/rest/v1/whatsapp_conversations?id=eq.{conversation_id}",
                json={"handoff_advisor_id": advisor_id},
                headers={**SB_HEADERS, "Prefer": "return=minimal"},
            )

            # Actualizar contador del asesor
            from datetime import datetime, timezone
            await c.patch(
                f"{SUPABASE_URL}/rest/v1/advisors?id=eq.{advisor_id}",
                json={
                    "current_chats": (selected.get("current_chats") or 0) + 1,
                    "last_assigned_at": datetime.now(timezone.utc).isoformat(),
                },
                headers={**SB_HEADERS, "Prefer": "return=minimal"},
            )

        advisor_name = f"{selected.get('nombre', '')} {selected.get('apellido', '')}".strip()

        logger.info(f"[ASSIGN] {phone} → {advisor_name} ({advisor_id}) modo={mode}")
        return advisor_id

    except Exception as e:
        logger.error(f"[ASSIGN] Error asignando: {e}")
    return None
