# config/products.py — Catálogo de planes Mutuo Plan Exequial (Origen IA)
# Mutuo Fintech S.A.S.
#
# FUENTE ÚNICA DE VERDAD: la tabla `plans` de Supabase. Los precios, cupos
# (titular + beneficiarios), coberturas y beneficios se cargan EN VIVO desde la
# base de datos (cache de 5 min). Aquí NO se hardcodea ningún número de plan,
# para que nunca se desfase respecto a Mi Cuenta, la web ni el bot principal.
#
# Lo único estático es la PRESENTACIÓN del bot (imagen y palabras gatillo),
# que no son datos del plan. Las imágenes se sirven desde Supabase Storage.

import os
import time
import logging
import httpx
from collections.abc import Mapping

logger = logging.getLogger("origen-ai")

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
STORAGE_BASE = f"{SUPABASE_URL}/storage/v1/object/public/plan-images" if SUPABASE_URL else ""

# Orden de escalamiento (de más barato a más caro). Son identificadores fijos,
# no números de plan.
PLAN_ORDER = ["familia_esencial", "familia_plus", "familia_total"]

# Presentación del bot por plan_key de la BD → imagen + palabras gatillo.
# NO contiene precios, cupos ni coberturas (eso sale siempre de `plans`).
_PRESENTACION = {
    "esencial": {
        "key": "familia_esencial",
        "tipo": "basico",
        "orden": 1,
        "imagen": f"{STORAGE_BASE}/exequial_familia_feed_1080x1350.png",
        "triggers": [
            "barato", "económico", "basico", "esencial", "sencillo",
            "lo más barato", "precio bajo", "algo básico",
        ],
    },
    "plus": {
        "key": "familia_plus",
        "tipo": "intermedio",
        "orden": 2,
        "imagen": f"{STORAGE_BASE}/exequial_tranquilidad_feed_1080x1350.png",
        "triggers": [
            "plus", "intermedio", "padres", "suegros", "sin limite de edad",
            "familiar mayor", "persona mayor", "adulto mayor", "abuela", "abuelo",
            "mama", "papa", "edad",
        ],
    },
    "total": {
        "key": "familia_total",
        "tipo": "premium",
        "orden": 3,
        "imagen": f"{STORAGE_BASE}/exequial_completo_feed_1080x1350.png",
        "triggers": [
            "total", "completo", "premium", "mejor", "todo incluido",
            "mascota", "perro", "gato", "golden", "descuento",
            "lo mejor", "el mas completo", "todos los beneficios",
        ],
    },
}


def _fmt_cop(n) -> str:
    """Formatea un número como precio COP: 25000 → "$25.000"."""
    try:
        return f"${int(n or 0):,.0f}".replace(",", ".")
    except (TypeError, ValueError):
        return "$0"


def _plan_key(p: dict) -> str:
    """Deriva la clave de presentación (esencial/plus/total) desde la fila de BD."""
    pk = (p.get("plan_key") or "").lower()
    if pk in _PRESENTACION:
        return pk
    name = (p.get("name") or "").lower()
    if "total" in name:
        return "total"
    if "plus" in name:
        return "plus"
    return "esencial"


def _cobertura(p: dict) -> str:
    """Texto "Titular + N beneficiarios" generado desde los cupos reales del plan."""
    max_b = p.get("max_beneficiarios") or 0
    sin_lim = p.get("beneficiarios_sin_limite_edad") or 0
    edad = p.get("edad_maxima_beneficiarios") or 69

    pars = p.get("parentescos_beneficiarios")
    parentesco_txt = (
        f"con parentesco ({', '.join(pars)})"
        if isinstance(pars, list) and pars
        else "sin importar parentesco"
    )
    txt = f"Titular + {max_b} beneficiarios ({parentesco_txt}, menores de {edad} años)"

    if sin_lim > 0:
        sl = p.get("parentescos_sin_limite")
        sl_txt = f" ({', '.join(sl)})" if isinstance(sl, list) and sl else ""
        txt += f" + {sin_lim} sin límite de edad{sl_txt}"
    return txt


def _incluye(p: dict) -> list:
    """Lista de beneficios desde el JSON `features` del plan (sin los cupos, que ya
    van en 'cobertura')."""
    feats = p.get("features")
    if not isinstance(feats, dict):
        return []
    skip = {
        "titular", "beneficiarios", "beneficiario_sin_limite",
        "beneficiarios_sin_limite", "beneficiarios_adicionales",
    }
    return [str(v) for k, v in feats.items() if k not in skip and v and v is not True]


def _beneficios_extra(p: dict) -> list:
    """Diferenciadores del plan, derivados de columnas reales de la BD."""
    extra = []
    sin_lim = p.get("beneficiarios_sin_limite_edad") or 0
    if sin_lim > 0:
        sl = p.get("parentescos_sin_limite")
        sl_txt = f" ({', '.join(sl)})" if isinstance(sl, list) and sl else ""
        extra.append(f"{sin_lim} beneficiario(s) sin límite de edad{sl_txt}")
    if p.get("includes_pet") and (p.get("pet_count_included") or 0) > 0:
        extra.append(f"{p.get('pet_count_included')} mascota incluida (máx {p.get('pet_max_age', 5)} años)")
    if p.get("includes_golden_offers"):
        extra.append("Tarjeta Golden Offers")
    if p.get("includes_exhumacion"):
        extra.append("Exhumación incluida")
    if p.get("includes_columbario"):
        extra.append("Columbario incluido")
    return extra


def _argumento(p: dict, cobertura: str) -> str:
    """Argumento de venta generado con los datos reales del plan."""
    total = 1 + (p.get("max_beneficiarios") or 0) + (p.get("beneficiarios_sin_limite_edad") or 0)
    return (
        f"{p.get('name')}: protección exequial para {total} personas "
        f"({cobertura}). {_fmt_cop(p.get('price'))}/mes con cobertura nacional Los Olivos."
    )


def _build_paquetes() -> dict:
    """Carga los planes activos desde la tabla `plans` y arma el catálogo del bot."""
    sb_url = os.getenv("SUPABASE_URL", "")
    sb_key = (
        os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
        or os.getenv("SUPABASE_KEY", "")
        or os.getenv("SUPABASE_ANON_KEY", "")
    )
    if not sb_url or not sb_key:
        logger.error("products: faltan credenciales Supabase; no se pueden cargar planes")
        return {}

    try:
        r = httpx.get(
            f"{sb_url}/rest/v1/plans?is_active=eq.true&order=display_order&select=*",
            headers={"Authorization": f"Bearer {sb_key}", "apikey": sb_key},
            timeout=10,
        )
        if r.status_code != 200:
            logger.error(f"products: error cargando planes status={r.status_code}")
            return {}

        paquetes = {}
        for p in r.json():
            pk = _plan_key(p)
            pres = _PRESENTACION.get(pk, {})
            cobertura = _cobertura(p)
            key = pres.get("key", f"familia_{pk}")
            paquetes[key] = {
                "nombre": p.get("name"),
                "tipo": pres.get("tipo", ""),
                "precio": _fmt_cop(p.get("price")),
                "precio_num": int(p.get("price") or 0),
                "orden": pres.get("orden", p.get("display_order", 99)),
                "cobertura": cobertura,
                "incluye": _incluye(p),
                "beneficios_extra": _beneficios_extra(p),
                "golden_offers": bool(p.get("includes_golden_offers")),
                "exhumacion": bool(p.get("includes_exhumacion")),
                "columbario": bool(p.get("includes_columbario")),
                "adicional_persona": int(p.get("adicional_persona_price") or 0),
                "imagen": pres.get("imagen", ""),
                "argumento": _argumento(p, cobertura),
                "triggers": pres.get("triggers", []),
            }
        logger.info(f"products: planes cargados desde BD: {len(paquetes)}")
        return paquetes
    except Exception as e:  # noqa: BLE001
        logger.error(f"products: excepción cargando planes: {e}")
        return {}


class _LazyPaquetes(Mapping):
    """Catálogo de planes cargado en vivo desde la tabla `plans` (cache 5 min).

    Se comporta como un dict para los consumidores: PAQUETES["familia_plus"],
    PAQUETES.get(...), PAQUETES.items(), `key in PAQUETES`, etc.
    """

    _cache: dict = {}
    _ts: float = 0.0

    def _data(self) -> dict:
        now = time.time()
        if type(self)._cache and (now - type(self)._ts) < 300:
            return type(self)._cache
        fresh = _build_paquetes()
        if fresh:
            type(self)._cache = fresh
            type(self)._ts = now
        return type(self)._cache

    def __getitem__(self, k):
        return self._data()[k]

    def __iter__(self):
        return iter(self._data())

    def __len__(self):
        return len(self._data())


PAQUETES = _LazyPaquetes()


def planes_resumen() -> str:
    """Resumen de planes en texto para los system prompts (datos reales de `plans`)."""
    planes = sorted(PAQUETES.values(), key=lambda x: x.get("orden", 99))
    lines = []
    for p in planes:
        extras = "; ".join(p.get("beneficios_extra", []))
        extra_txt = f" — {extras}" if extras else ""
        lines.append(f"- {p.get('nombre')} {p.get('precio')}/mes: {p.get('cobertura', '')}{extra_txt}")
    return "\n".join(lines)


def adicional_resumen() -> str:
    """Texto del adicional por persona, con el precio real de la BD."""
    precios = [p.get("adicional_persona", 0) for p in PAQUETES.values() if p.get("adicional_persona")]
    precio = max(precios) if precios else 0
    return f"{_fmt_cop(precio)}/mes por persona extra (menor de 69 años, cualquier parentesco)"
