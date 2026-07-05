# agent/catalog.py — Catálogo de planes EN VIVO desde el sistema
# Mutuo Fintech — Bot WhatsApp
#
# Fuente única de verdad para precios/planes: la tabla `plans` del sistema.
# NADA de precios hardcodeados: si el admin cambia una tarifa, el bot la refleja.
# Cachea unos minutos para no golpear la BD en cada mensaje. Si no puede leer el
# sistema, NO inventa un precio: devuelve None y el llamador degrada con honestidad.

import os
import time
import logging

import httpx
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger("mutuo-bot")

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = (
    os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    or os.getenv("SUPABASE_KEY", "")
    or os.getenv("SUPABASE_ANON_KEY", "")
)

_TTL = 300  # 5 min
_cache: dict = {"data": None, "ts": 0.0}


def _norm(s: str | None) -> str:
    return (s or "").lower().replace("familia", "").replace("plan", "").strip()


async def get_plans() -> list[dict]:
    """Planes activos del sistema (cacheado). Vía gateway si está activo, si no REST."""
    now = time.time()
    if _cache["data"] is not None and (now - _cache["ts"]) < _TTL:
        return _cache["data"]

    plans: list[dict] = []
    # Preferir el gateway (contrato bot ⇄ sistema) si está encendido.
    try:
        from agent import gateway
        if gateway.enabled():
            data = await gateway.plans_list()
            if isinstance(data, list):
                plans = data
    except Exception as e:
        logger.debug(f"[CATALOG] gateway no disponible: {e}")

    if not plans and SUPABASE_URL and SUPABASE_KEY:
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                r = await client.get(
                    f"{SUPABASE_URL}/rest/v1/plans"
                    f"?is_active=eq.true&select=name,plan_key,price,continuation_price&order=price",
                    headers={"Authorization": f"Bearer {SUPABASE_KEY}", "apikey": SUPABASE_KEY},
                )
                if r.status_code == 200:
                    plans = r.json()
        except Exception as e:
            logger.warning(f"[CATALOG] fetch de planes falló: {e}")

    if plans:
        _cache["data"] = plans
        _cache["ts"] = now
        return plans
    # Si falló pero teníamos cache viejo, mejor eso que nada (sigue siendo del sistema).
    return _cache["data"] or []


async def _find(plan_name_or_key: str) -> dict | None:
    if not plan_name_or_key:
        return None
    key = _norm(plan_name_or_key)
    for p in await get_plans():
        if _norm(p.get("plan_key")) == key or _norm(p.get("name")) == key:
            return p
    return None


async def price_for(plan_name_or_key: str) -> int | None:
    """Precio REAL del sistema para un plan. None si no se puede determinar (nunca inventa)."""
    p = await _find(plan_name_or_key)
    if not p:
        return None
    try:
        return int(float(p.get("price")))
    except (TypeError, ValueError):
        return None


async def plan_name_for(plan_name_or_key: str) -> str | None:
    p = await _find(plan_name_or_key)
    return p.get("name") if p else None
