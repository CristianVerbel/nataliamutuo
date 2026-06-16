# agent/bot_preferences.py — Preferencias de interacción del bot por afiliado
# Mutuo Fintech S.A.S. — Habeas Data
#
# Lee public.bot_interaction_settings para decidir si el bot puede interactuar
# con un teléfono en una categoría dada (cartera, inbound, soporte_cuenta,
# servicio_cliente). Política fail-open: ante cualquier error, falta de
# configuración o ausencia de registro, se devuelve True para NO silenciar el
# bot ni frenar la cartera por un dato faltante. El afiliado ya autorizó el
# contacto al afiliarse; estas preferencias permiten revocar/ajustar por canal.

import os
import time
import logging

import httpx

logger = logging.getLogger("origen-ai")

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = (
    os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    or os.getenv("SUPABASE_KEY", "")
    or os.getenv("SUPABASE_ANON_KEY", "")
)

# Categorías (coinciden con las columnas allow_* de la tabla)
CATEGORY_CARTERA = "cartera"
CATEGORY_INBOUND = "inbound"
CATEGORY_SOPORTE = "soporte_cuenta"
CATEGORY_SERVICIO = "servicio_cliente"

# Acciones del LLM que cuentan como "soporte de cuenta"
ACCOUNT_SUPPORT_ACTIONS = {
    "CONSULTAR_ESTADO",
    "PAGO_ANTICIPADO",
    "REENVIAR_RECIBO",
    "CREAR_TICKET_CANCELACION",
    "CONSULTAR_RADICADO",
    "CONSULTAR_POR_CEDULA",
    "ACTUALIZAR_BENEFICIARIOS",
}

# Cache simple con TTL para no consultar Supabase en cada mensaje
_cache: dict[str, tuple[float, dict | None]] = {}
_CACHE_TTL = float(os.getenv("BOT_PREFS_CACHE_TTL", "60"))


def _digits(phone: str) -> str:
    return "".join(ch for ch in (phone or "") if ch.isdigit())


def _phone_variants(phone: str) -> set[str]:
    """Variantes del teléfono con/sin prefijo de país 57."""
    d = _digits(phone)
    out = {d}
    if len(d) == 12 and d.startswith("57"):
        out.add(d[2:])
    elif len(d) == 10:
        out.add("57" + d)
    out.discard("")
    return out


def invalidate_cache(phone: str | None = None) -> None:
    """Invalida el cache (todo o un teléfono). Útil al cambiar preferencias."""
    if phone is None:
        _cache.clear()
        return
    for v in _phone_variants(phone):
        _cache.pop(v, None)


async def _fetch_settings(phone: str) -> dict | None:
    """Devuelve la fila de preferencias del teléfono, o None si no existe."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return None

    variants = _phone_variants(phone)
    if not variants:
        return None
    cache_key = _digits(phone)

    cached = _cache.get(cache_key)
    if cached and cached[0] > time.time():
        return cached[1]

    or_clause = ",".join(f"phone.eq.{v}" for v in variants)
    url = (
        f"{SUPABASE_URL}/rest/v1/bot_interaction_settings"
        f"?or=({or_clause})"
        f"&select=bot_enabled,allow_cartera,allow_inbound,allow_soporte_cuenta,allow_servicio_cliente"
        f"&order=updated_at.desc&limit=1"
    )
    headers = {"Authorization": f"Bearer {SUPABASE_KEY}", "apikey": SUPABASE_KEY}
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(url, headers=headers)
            row = r.json()[0] if r.status_code == 200 and r.json() else None
    except Exception as e:
        logger.warning(f"[BOT_PREFS] error consultando preferencias de {phone} (fail-open): {e}")
        # No cacheamos errores para reintentar pronto
        return None

    _cache[cache_key] = (time.time() + _CACHE_TTL, row)
    return row


async def can_interact(phone: str, category: str) -> bool:
    """True si el bot puede interactuar con `phone` en `category`.

    Fail-open: sin configuración/registro o ante error → True.
    """
    row = await _fetch_settings(phone)
    if not row:
        return True
    if row.get("bot_enabled") is False:
        return False
    column = f"allow_{category}"
    value = row.get(column)
    # Categoría desconocida o columna ausente → respetar el maestro
    if value is None:
        return row.get("bot_enabled", True)
    return bool(value)
