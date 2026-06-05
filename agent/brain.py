# agent/brain.py — Cerebro del agente de IA
# Desarrollado por Catalitico LLC para Mutuo Fintech S.A.S.

import os
import yaml
import logging
import httpx
from anthropic import AsyncAnthropic
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger("mutuo-bot")

client = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

_kb_cache: str | None = None
_kb_cache_ts: float = 0


def cargar_config_prompts() -> dict:
    for path in ["config/prompts.yaml", "whatsapp-bot/config/prompts.yaml"]:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        except FileNotFoundError:
            continue
    logger.error("prompts.yaml no encontrado")
    return {}


def cargar_system_prompt() -> str:
    config = cargar_config_prompts()
    return config.get("system_prompt", "Eres Natalia, asesora de Mutuo.")


async def cargar_knowledge_base() -> str:
    global _kb_cache, _kb_cache_ts
    import time

    now = time.time()
    if _kb_cache and (now - _kb_cache_ts) < 300:
        return _kb_cache

    sb_url = os.getenv("SUPABASE_URL", "")
    sb_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "") or os.getenv("SUPABASE_KEY", "") or os.getenv("SUPABASE_ANON_KEY", "")
    if not sb_url or not sb_key:
        logger.warning("SUPABASE no disponible para KB")
        return ""

    headers = {"Authorization": f"Bearer {sb_key}", "apikey": sb_key}
    try:
        async with httpx.AsyncClient(timeout=10) as http:
            r = await http.get(
                f"{sb_url}/rest/v1/ai_knowledge_base?is_active=eq.true"
                f"&category=not.in.(plan,benefit)"
                f"&order=priority.desc&select=category,topic,content",
                headers=headers,
            )
            if r.status_code == 200:
                sections = r.json()
                kb = "\n\n".join(
                    f"## {s.get('topic', s.get('category', 'Info'))}\n{s['content']}"
                    for s in sections if s.get("content")
                )
                _kb_cache = kb
                _kb_cache_ts = now
                logger.info(f"KB cargada: {len(sections)} secciones, {len(kb)} chars")
                return kb
            else:
                logger.error(f"KB error: status={r.status_code}")
    except Exception as e:
        logger.error(f"Error cargando KB: {e}")

    return _kb_cache or ""


_plans_cache: str | None = None
_plans_raw_cache: list[dict] = []
_plans_cache_ts: float = 0


async def cargar_planes_db() -> str:
    global _plans_cache, _plans_raw_cache, _plans_cache_ts
    import time

    now = time.time()
    if _plans_cache and (now - _plans_cache_ts) < 300:
        return _plans_cache

    sb_url = os.getenv("SUPABASE_URL", "")
    sb_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "") or os.getenv("SUPABASE_KEY", "") or os.getenv("SUPABASE_ANON_KEY", "")
    if not sb_url or not sb_key:
        return ""

    try:
        async with httpx.AsyncClient(timeout=10) as http:
            r = await http.get(
                f"{sb_url}/rest/v1/plans?is_active=eq.true&order=display_order&select=*",
                headers={"Authorization": f"Bearer {sb_key}", "apikey": sb_key},
            )
            if r.status_code == 200:
                planes = r.json()
                _plans_raw_cache = planes
                lines = ["## PLANES ACTIVOS (datos reales del sistema, NO inventar)"]
                for p in planes:
                    total = 1 + (p.get("max_beneficiarios") or 0) + (p.get("beneficiarios_sin_limite_edad") or 0)
                    titular_max = p.get("titular_edad_maxima", 69)
                    benef_max = p.get("edad_maxima_beneficiarios", 69)
                    line = f"\n### {p['name']} — ${p['price']:,.0f}/mes"
                    line += f"\n- Titular: edad permitida {p.get('titular_edad_minima', 18)} a {titular_max} anos (DATO REAL — no inventes otro limite)"
                    line += f"\n- Titular + {p.get('max_beneficiarios', 5)} beneficiarios (edad permitida hasta {benef_max} anos)"
                    if p.get("beneficiarios_sin_limite_edad", 0) > 0:
                        pars = p.get("parentescos_sin_limite", [])
                        pars_str = ", ".join(pars) if isinstance(pars, list) and pars else "padres, suegros, conyuge"
                        line += f"\n- + {p['beneficiarios_sin_limite_edad']} beneficiario(s) SIN LIMITE DE EDAD ({pars_str})"
                    if p.get("includes_pet"):
                        line += f"\n- {p.get('pet_count_included', 1)} mascota INCLUIDA (max {p.get('pet_max_age', 5)} anos)"
                    if p.get("includes_golden_offers"):
                        line += "\n- Tarjeta Golden Offers con descuentos exclusivos"
                    line += f"\n- Total: {total} personas cubiertas"
                    line += f"\n- Carencia: {p.get('carencia_dias', 90)} dias"
                    if p.get("permite_adicionales_persona"):
                        line += f"\n- Persona adicional: ${p.get('adicional_persona_price', 9900):,.0f}/mes"
                    if p.get("permite_adicionales_mascota"):
                        line += f"\n- Mascota adicional: ${p.get('adicional_mascota_price', 15000):,.0f}/mes"

                    # Coberturas detalladas del plan (features JSONB)
                    features = p.get("features")
                    if features and isinstance(features, dict):
                        line += "\n\nCOBERTURAS DETALLADAS:"
                        for key, val in features.items():
                            if val is not None and val is not False:
                                line += f"\n  - {key.replace('_', ' ')}: {val}"

                    # Servicios operativos
                    line += "\n\nSERVICIOS OPERATIVOS:"
                    line += f"\n  - Sala de velacion: hasta {p.get('sala_velacion_horas', 24)} horas"
                    line += f"\n  - Cofre: hasta {p.get('cofre_referencias', 4)} referencias disponibles"
                    line += f"\n  - Traslado terrestre: hasta {p.get('traslado_km', 300)} km"
                    if p.get("includes_vehiculo_funerario") is not False:
                        line += "\n  - Vehiculo funerario: incluido"
                    if p.get("includes_flores") is not False:
                        line += "\n  - Ofrenda floral: incluida"
                    if p.get("includes_documentos") is not False:
                        line += "\n  - Tramitacion de documentos: incluida"
                    if p.get("includes_kit_recordatorio") is not False:
                        line += "\n  - Kit de recordatorio: incluido"
                    if p.get("includes_videohomenaje") is not False:
                        line += "\n  - Video homenaje: incluido"
                    line += f"\n  - Cobertura: {p.get('cobertura_geografica', 'Nacional')}"

                    lines.append(line)
                _plans_cache = "\n".join(lines)
                _plans_cache_ts = now
                logger.info(f"Planes cargados: {len(planes)}")
                return _plans_cache
            else:
                logger.error(f"Planes error: status={r.status_code}")
    except Exception as e:
        logger.error(f"Error cargando planes: {e}")

    return _plans_cache or ""


async def planes_raw() -> list[dict]:
    """Devuelve los planes activos como lista de dicts (para lógica determinística)."""
    if not _plans_raw_cache:
        await cargar_planes_db()
    return list(_plans_raw_cache)


def _evaluar_planes_para_familia(
    edad_titular: int | None,
    edades_familia: list[int],
    tiene_mascota: bool,
    planes: list[dict],
) -> str | None:
    """
    Decide determinísticamente qué planes cubren a esta familia y cuál es el
    mínimo viable, sin dejar la decisión al LLM. Devuelve un bloque de texto
    para inyectar al system prompt, o None si no hay datos suficientes.

    Reglas (todas vienen de la tabla `plans`):
      - titular_edad ≤ titular_edad_maxima
      - familiares con edad > edad_maxima_beneficiarios ≤ beneficiarios_sin_limite_edad
      - total personas ≤ 1 + max_beneficiarios + beneficiarios_sin_limite_edad
      - si tiene_mascota y el plan no la incluye → "cabe pero suma adicional_mascota_price"
    """
    if not planes:
        return None
    if edad_titular is None and not edades_familia:
        return None

    planes_ordenados = sorted(planes, key=lambda p: p.get("price") or 0)
    evaluaciones = []
    plan_minimo_viable: dict | None = None

    for p in planes_ordenados:
        nombre = p.get("name") or p.get("plan_key") or "?"
        precio = p.get("price") or 0
        titular_max = p.get("titular_edad_maxima") or 69
        titular_min = p.get("titular_edad_minima") or 18
        benef_max = p.get("edad_maxima_beneficiarios") or 69
        max_benef = p.get("max_beneficiarios") or 0
        cupos_sin_limite = p.get("beneficiarios_sin_limite_edad") or 0
        incluye_mascota = bool(p.get("includes_pet"))
        precio_mascota_extra = p.get("adicional_mascota_price") or 15000

        problemas: list[str] = []
        if edad_titular is not None:
            if edad_titular > titular_max:
                problemas.append(f"titular tiene {edad_titular} y el límite del plan es {titular_max}")
            elif edad_titular < titular_min:
                problemas.append(f"titular tiene {edad_titular} y el mínimo del plan es {titular_min}")

        sobre_limite = [e for e in edades_familia if e > benef_max]
        if len(sobre_limite) > cupos_sin_limite:
            problemas.append(
                f"{len(sobre_limite)} familiar(es) superan {benef_max} años "
                f"y el plan solo tiene {cupos_sin_limite} cupo(s) sin límite de edad"
            )

        total_personas = (1 if edad_titular is not None else 0) + len(edades_familia)
        cupo_total = 1 + max_benef + cupos_sin_limite
        if total_personas > cupo_total:
            problemas.append(f"son {total_personas} personas y el plan cubre máximo {cupo_total}")

        nota_mascota = ""
        precio_efectivo = precio
        if tiene_mascota:
            if incluye_mascota:
                nota_mascota = " (mascota incluida)"
            else:
                precio_efectivo = precio + precio_mascota_extra
                nota_mascota = f" (+${precio_mascota_extra:,.0f} mascota adicional = ${precio_efectivo:,.0f}/mes)"

        if problemas:
            evaluaciones.append(f"- {nombre} (${precio:,.0f}/mes): NO VIABLE — {'; '.join(problemas)}")
        else:
            evaluaciones.append(f"- {nombre} (${precio:,.0f}/mes){nota_mascota}: VIABLE")
            if plan_minimo_viable is None or precio_efectivo < (plan_minimo_viable.get("_precio_efectivo") or float("inf")):
                plan_minimo_viable = {**p, "_precio_efectivo": precio_efectivo}

    cabecera = (
        "\n\n## EVALUACION DETERMINISTICA DE PLANES (calculada por codigo, no inventes)\n"
        f"Composicion analizada: titular {edad_titular if edad_titular is not None else '?'} anos, "
        f"familiares {edades_familia or 'ninguno conocido'}, mascota: {'si' if tiene_mascota else 'no'}.\n"
        + "\n".join(evaluaciones)
    )

    if not plan_minimo_viable:
        return cabecera + (
            "\n\nNINGUN plan cubre esta composicion exactamente. Explica con honestidad al cliente"
            " que necesitamos ajustar el grupo o que un familiar quedaria sin cobertura."
        )

    nombre_min = plan_minimo_viable.get("name") or plan_minimo_viable.get("plan_key")
    precio_efectivo_min = plan_minimo_viable.get("_precio_efectivo") or plan_minimo_viable.get("price") or 0
    return cabecera + (
        f"\n\nPLAN MINIMO VIABLE (mas barato que cubre todo): {nombre_min} a ${precio_efectivo_min:,.0f}/mes."
        "\n\nREGLAS PARA TI (Natalia):\n"
        "1. NO marques como invalido un plan que aqui aparece como VIABLE.\n"
        "2. NO inventes razones de exclusion (limites de edad falsos, sub-limites, etc).\n"
        "3. Puedes recomendar un plan superior al minimo viable solo si justificas el valor"
        " agregado de forma honesta (mascota incluida, Golden Offers, cupos sin limite) —"
        " nunca inventando que el plan inferior 'no cubre'."
    )


_benefits_cache: str | None = None
_benefits_cache_ts: float = 0


async def cargar_golden_offers() -> str:
    global _benefits_cache, _benefits_cache_ts
    import time
    now = time.time()
    if _benefits_cache and (now - _benefits_cache_ts) < 600:
        return _benefits_cache

    sb_url = os.getenv("SUPABASE_URL", "")
    sb_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "") or os.getenv("SUPABASE_KEY", "") or os.getenv("SUPABASE_ANON_KEY", "")
    if not sb_url or not sb_key:
        return ""

    try:
        async with httpx.AsyncClient(timeout=10) as http:
            r = await http.get(
                f"{sb_url}/rest/v1/benefits?is_active=eq.true&order=display_order&select=merchant_name,discount_text,category",
                headers={"Authorization": f"Bearer {sb_key}", "apikey": sb_key},
            )
            if r.status_code == 200:
                benefits = r.json()
                if benefits:
                    lines = ["## TARJETA GOLDEN OFFERS (incluida en los 3 planes: Esencial, Plus y Total)"]
                    lines.append("Tarjeta de descuentos exclusivos en establecimientos aliados:\n")
                    by_cat: dict[str, list] = {}
                    for b in benefits:
                        cat = (b.get("category") or "otros").capitalize()
                        if cat not in by_cat:
                            by_cat[cat] = []
                        by_cat[cat].append(f"  - {b['merchant_name']}: {b['discount_text']}")
                    for cat, items in by_cat.items():
                        lines.append(f"**{cat}:**")
                        lines.extend(items)
                    _benefits_cache = "\n".join(lines)
                    _benefits_cache_ts = now
                    logger.info(f"Golden Offers cargadas: {len(benefits)} beneficios")
                    return _benefits_cache
    except Exception as e:
        logger.error(f"Error cargando Golden Offers: {e}")

    return _benefits_cache or ""


def obtener_mensaje_error() -> str:
    config = cargar_config_prompts()
    return config.get("error_message", "Se me fue la senal, me escribes de nuevo?")


def obtener_mensaje_fallback() -> str:
    config = cargar_config_prompts()
    return config.get("fallback_message", "No te entendi bien, me repites?")


_fallback_count: dict[str, int] = {}


def _fallback_recoger_datos(mensaje: str, telefono: str = "") -> str:
    """
    Cuando Claude no está disponible, en vez de decir 'se me fue la señal',
    pide datos del cliente para no perder el lead. Si falla 3+ veces seguidas,
    hace handoff a un asesor humano.
    """
    count = _fallback_count.get(telefono, 0) + 1
    _fallback_count[telefono] = count

    if count >= 3:
        _fallback_count[telefono] = 0
        return (
            "Disculpa la demora, estoy teniendo inconvenientes tecnicos en este momento. "
            "Voy a pedirle a mi jefe que te contacte personalmente para atenderte. "
            "Mientras tanto, si quieres adelantar, dejame tu nombre completo y tu ciudad. "
            "Tambien puedes escribirnos a sac@mutuo.la o llamar al +57 324 8789475."
        )

    msg_lower = mensaje.lower() if mensaje else ""
    if any(w in msg_lower for w in ["pagar", "pago", "cuota", "deuda", "link", "efecty", "nequi"]):
        return (
            "Estoy verificando tu cuenta. Mientras tanto, "
            "me confirmas tu numero de cedula para buscarte en el sistema?"
        )
    elif any(w in msg_lower for w in ["afiliado", "afiliacion", "plan", "estado"]):
        return (
            "Claro, dejame verificar tu informacion. "
            "Me compartes tu numero de cedula y nombre completo?"
        )
    else:
        return (
            "Hola! Soy Natalia de Mutuo, Club de Bienestar Familiar. "
            "Me cuentas tu nombre y de que ciudad me escribes? "
            "Asi te recomiendo el plan ideal para tu familia."
        )


import re as _re_extract


_CEDULA_RE = _re_extract.compile(r'\b\d{1,3}(?:[\.\s]\d{3}){1,3}\b|\b\d{7,10}\b')
_EMAIL_RE = _re_extract.compile(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}')
_FECHA_RE = _re_extract.compile(
    r'\b(?:0?[1-9]|[12]\d|3[01])[\/\-\.](?:0?[1-9]|1[0-2])[\/\-\.](?:19|20)\d{2}\b'
)
# Edades: captura números de 2 dígitos (18-99) mencionados por el cliente
_EDAD_RE = _re_extract.compile(r'\b([1-9]\d)\b')
_CIUDADES = [
    "cucuta", "cúcuta", "bogota", "bogotá", "medellin", "medellín", "cali",
    "barranquilla", "cartagena", "bucaramanga", "pereira", "manizales",
    "santa marta", "ibague", "ibagué", "villavicencio", "pasto", "neiva",
    "monteria", "montería", "armenia", "sincelejo", "valledupar", "popayan",
    "popayán", "tunja", "riohacha", "yopal", "florencia", "mocoa", "quibdo",
    "quibdó", "leticia", "arauca", "mitu", "mitú", "puerto carreno",
    "puerto carreño", "san andres", "san andrés", "soledad", "soacha",
    "bello", "envigado", "itagui", "itagüí", "floridablanca",
]


async def _extraer_hechos_confirmados(historial: list[dict]) -> str:
    """
    Escanea el historial completo y extrae datos que el CLIENTE ya confirmó:
    nombre, cédula, email, fecha de nacimiento, ciudad, plan elegido,
    beneficiarios mencionados. Se inyecta en el system prompt para que el
    modelo NO pueda olvidarlos aunque la conversación sea larga.

    Solo mira mensajes `user` para evitar contaminarse con alucinaciones
    previas del asistente.
    """
    if not historial:
        return ""

    user_texts = [m.get("content", "") for m in historial if m.get("role") == "user"]
    if not user_texts:
        return ""

    blob = "\n".join(user_texts)
    blob_lower = blob.lower()

    hechos: list[str] = []

    # Nombre completo (2-4 palabras capitalizadas, al menos un apellido)
    nombre_match = _re_extract.search(
        r'\b([A-ZÁÉÍÓÚÑ][a-záéíóúñ]{1,}(?:\s+[A-ZÁÉÍÓÚÑ][a-záéíóúñ]{1,}){1,3})\b',
        blob
    )
    if nombre_match:
        candidato = nombre_match.group(1).strip()
        # Descartar si es una ciudad conocida o una sola palabra corta
        if len(candidato.split()) >= 2 and candidato.lower() not in _CIUDADES:
            hechos.append(
                f"Nombre completo del cliente (tal como lo escribio): {candidato}. "
                f"Usa EXACTAMENTE este nombre. NO lo cambies ni lo acortes."
            )

    # Cédula / documento
    cedulas = _CEDULA_RE.findall(blob)
    # Filtrar teléfonos (10 dígitos empezando en 3) y precios ($25.000, $29.900)
    cedulas_validas = []
    for c in cedulas:
        solo_digitos = c.replace('.', '').replace(' ', '')
        if len(solo_digitos) < 6 or len(solo_digitos) > 11:
            continue
        if len(solo_digitos) == 10 and solo_digitos.startswith('3'):
            continue  # teléfono celular
        if solo_digitos in ('25000', '29900', '35000', '45000'):
            continue  # precio de plan
        cedulas_validas.append(c)
    if cedulas_validas:
        hechos.append(f"Cedulas/documentos mencionados por el cliente: {', '.join(set(cedulas_validas))}")

    # Email
    emails = _EMAIL_RE.findall(blob)
    if emails:
        hechos.append(f"Email del cliente: {emails[0]}")

    # Fechas (probables nacimientos)
    fechas = _FECHA_RE.findall(blob)
    if fechas:
        hechos.append(f"Fechas mencionadas (probables nacimientos): {', '.join(set(fechas))}")

    # Ciudad
    ciudades_encontradas = []
    for c in _CIUDADES:
        if c in blob_lower and c.title() not in ciudades_encontradas:
            ciudades_encontradas.append(c.title())
    if ciudades_encontradas:
        hechos.append(
            f"Ciudad(es) que mencionó el cliente: {', '.join(ciudades_encontradas)}. "
            f"USA ESA CIUDAD. NO la cambies por otra."
        )

    # Plan mencionado por el cliente
    for match in _re_extract.finditer(
        r'(plan\s+(?:familia|familiar)?\s*(?:basico|básico|plus|total|premium))',
        blob_lower
    ):
        hechos.append(f"Plan mencionado por el cliente: {match.group(1)}")
        break

    for match in _re_extract.finditer(r'(\d+)\s*(?:personas|beneficiarios|familiares|cupos|hijos)', blob_lower):
        hechos.append(f"Cantidad mencionada por el cliente: {match.group(0)}")
        break

    # Plan ya recomendado — usar la ÚLTIMA mención en el historial (más reciente)
    # Escanear mensajes del bot + mensajes del usuario que confirmen un plan
    plan_recomendado = None
    for msg in reversed(historial):  # más reciente primero
        texto_msg = msg.get("content", "").lower()
        m_plan = _re_extract.search(
            r'plan\s+(?:familia\s+)?(esencial|plus|total)',
            texto_msg
        )
        if m_plan:
            plan_recomendado = m_plan.group(1)
            break
        # También detectar si el cliente confirmó un precio específico
        if _re_extract.search(r'38\.?000|38000', texto_msg):
            plan_recomendado = "total"
            break
        if _re_extract.search(r'29\.?900|29900', texto_msg):
            plan_recomendado = "plus"
            break
        if _re_extract.search(r'25\.?000|25000', texto_msg) and not _re_extract.search(r'38|29', texto_msg):
            plan_recomendado = "esencial"
            break
    if plan_recomendado:
        precios = {"esencial": 25000, "plus": 29900, "total": 38000}
        precio = precios.get(plan_recomendado, 0)
        diario = round(precio / 30)
        hechos.append(
            f"PLAN YA RECOMENDADO AL CLIENTE: Plan Familia {plan_recomendado.title()} "
            f"(${precio:,}/mes = ${diario:,}/dia). "
            f"NO cambies este plan a menos que el cliente lo pida explicitamente. "
            f"NO preguntes cuantas personas son — ya lo sabes. "
            f"Si el cliente pregunta por el costo diario, usa SIEMPRE ${diario:,}/dia."
        )

    # ── Edades de familiares ──
    # Escaneamos TODOS los mensajes (user + assistant) en pares para capturar
    # edades que el cliente confirmó en respuesta a preguntas del bot.
    edades_familiares: list[str] = []
    edad_titular_int: int | None = None
    edades_familia_int: list[int] = []
    all_msgs = historial  # incluye ambos roles para contexto de la pregunta
    for i, m in enumerate(all_msgs):
        if m.get("role") != "user":
            continue
        texto = m.get("content", "")
        # Detectar edades numéricas en el mensaje del cliente
        edades_en_msg = _EDAD_RE.findall(texto)
        if not edades_en_msg:
            continue
        # Buscar contexto: ¿la pregunta anterior del bot mencionaba a quién?
        contexto_pregunta = ""
        if i > 0:
            prev = all_msgs[i - 1].get("content", "").lower()
            if any(w in prev for w in ["papá", "papa", "mamá", "mama", "padres", "madre", "padre", "años tienen"]):
                contexto_pregunta = "de sus padres/papás"
            elif any(w in prev for w in ["años tienes", "tu edad", "cuántos años tiene", "tú cuántos"]):
                contexto_pregunta = "del titular"
            elif any(w in prev for w in ["hijo", "hija", "niño", "niña"]):
                contexto_pregunta = "de sus hijos"
            elif any(w in prev for w in ["esposa", "esposo", "cónyuge", "pareja"]):
                contexto_pregunta = "de su cónyuge/pareja"
        # También detectar si el propio mensaje del cliente da pistas ("yo", "mis papás", etc.)
        texto_lower = texto.lower()
        yo_edad = None
        familia_edades = []
        # Patrón "X y Y, yo Z" o "yo Z, mis papás X y Y"
        m_yo = _re_extract.search(r'\byo\s+(\d{2})\b', texto_lower)
        if m_yo:
            yo_edad = m_yo.group(1)
        m_papas = _re_extract.search(
            r'(?:pap[aá]s?|padres?|madre?|mam[aá]s?)[^\d]*(\d{2})[^\d]*(?:y[^\d]*(\d{2}))?',
            texto_lower
        )
        if m_papas:
            familia_edades = [g for g in m_papas.groups() if g]
        if yo_edad:
            edades_familiares.append(f"Titular: {yo_edad} años")
            try:
                edad_titular_int = int(yo_edad)
            except (TypeError, ValueError):
                pass
        if familia_edades:
            edades_familiares.append(f"Padres/familiares: {' y '.join(familia_edades)} años")
            for e in familia_edades:
                try:
                    edades_familia_int.append(int(e))
                except (TypeError, ValueError):
                    pass
        elif edades_en_msg and not yo_edad and not familia_edades:
            # Fallback: reportar edades con el contexto inferido de la pregunta anterior
            desc = contexto_pregunta or "familiares mencionados"
            edades_familiares.append(f"Edades {desc}: {', '.join(edades_en_msg)} años")
            for e in edades_en_msg:
                try:
                    edades_familia_int.append(int(e))
                except (TypeError, ValueError):
                    pass

    if edades_familiares:
        resumen_edades = "; ".join(edades_familiares)
        hechos.append(
            f"EDADES YA CONFIRMADAS POR EL CLIENTE: {resumen_edades}. "
            "NUNCA olvides estos valores. Son DETERMINANTES para elegir el plan correcto. "
            "ANTES de recomendar cualquier plan, cruza estas edades con los limites de edad "
            "que aparecen en PLANES ACTIVOS (datos reales del sistema). "
            "NO uses ningun numero de edad que no venga de PLANES ACTIVOS. "
            "REGLA DE CONTEO (aplicala literal, sin atajos):\n"
            "  1. El TITULAR se evalua contra titular_edad_maxima. Si cabe ahi, entra como "
            "titular y NO consume cupo sin limite. NUNCA lo cuentes como 'mayor que necesita cupo'.\n"
            "  2. Solo los BENEFICIARIOS (mama, papa, suegros, etc.) que superen "
            "edad_maxima_beneficiarios consumen cupo sin limite.\n"
            "  3. Si el numero de beneficiarios mayores <= cupos sin limite del plan, ese plan ALCANZA.\n"
            "Ejemplo: titular 60 + mama 95 → titular cabe en titular (18-69), mama (1 persona > 69) "
            "usa 1 cupo. Plan Plus (1 cupo) ALCANZA. NO subas a Total inventando que el titular "
            "tambien necesita cupo."
        )

    # ── Evaluación determinística de planes ──
    # Si tenemos al menos una edad, calculamos por código qué planes son viables
    # en vez de dejar la decisión al LLM (que ya alucinó límites de edad falsos).
    tiene_mascota = bool(_re_extract.search(
        r'\b(mascota|perro|gato|gata|perrito|gatito|cachorro|canino|felino)\b',
        blob_lower
    ))
    veredicto = None
    if edad_titular_int is not None or edades_familia_int:
        try:
            planes_data = await planes_raw()
            veredicto = _evaluar_planes_para_familia(
                edad_titular=edad_titular_int,
                edades_familia=edades_familia_int,
                tiene_mascota=tiene_mascota,
                planes=planes_data,
            )
        except Exception as e:
            logger.error(f"Error en evaluacion deterministica: {e}")

    if not hechos and not veredicto:
        return ""

    bloque_hechos = ""
    if hechos:
        bloque_hechos = (
            "\n\n## HECHOS YA CONFIRMADOS POR EL CLIENTE (fuente: sus propios mensajes)\n"
            "Estos datos YA los dio el cliente. NO los preguntes de nuevo. "
            "NO los sustituyas por otros. NO inventes alternativas.\n- "
            + "\n- ".join(hechos)
        )

    return bloque_hechos + (veredicto or "")


_client_cache: dict[str, tuple[str, float]] = {}


def invalidar_cache_cliente(telefono: str) -> None:
    """Elimina la entrada cacheada de un teléfono para forzar re-consulta."""
    _client_cache.pop(telefono, None)


async def _buscar_cliente_por_telefono(telefono: str) -> str | None:
    """Busca si el teléfono tiene afiliación y retorna contexto para el prompt."""
    if not telefono:
        return None

    import time
    now = time.time()
    cached = _client_cache.get(telefono)
    if cached and (now - cached[1]) < 30:
        return cached[0] or None

    sb_url = os.getenv("SUPABASE_URL", "")
    sb_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "") or os.getenv("SUPABASE_KEY", "") or os.getenv("SUPABASE_ANON_KEY", "")
    if not sb_url or not sb_key:
        return None

    try:
        from agent.mutuo_actions import _phone_variants
        phone_variants = _phone_variants(telefono)
        or_filter = ",".join(f"phone.eq.{p}" for p in phone_variants)

        async with httpx.AsyncClient(timeout=5) as http:
            r = await http.get(
                f"{sb_url}/rest/v1/b2c_affiliations?or=({or_filter})&select=id,first_name,last_name,selected_plan,payment_status,is_active,email,document_number&order=created_at.desc&limit=1",
                headers={"Authorization": f"Bearer {sb_key}", "apikey": sb_key},
            )
            if r.status_code == 200:
                data = r.json()
                if data:
                    aff = data[0]
                    ha_pagado = aff.get("payment_status") == "paid"
                    ctx = (
                        f"## CLIENTE EXISTENTE — NO ES PROSPECTO\n"
                        f"Esta persona YA esta afiliada a Mutuo. NO le vendas un plan nuevo.\n"
                        f"Nombre: {aff.get('first_name', '')} {aff.get('last_name', '')}\n"
                        f"Plan: {aff.get('selected_plan', 'N/A')}\n"
                        f"Estado de pago: {aff.get('payment_status', 'N/A')}\n"
                        f"Cuenta activa: {aff.get('is_active', True)}\n"
                        f"Email: {aff.get('email', '')}\n"
                        f"Cedula: {aff.get('document_number', '')}\n\n"
                        "REGLA CRITICA — NO RESETEAR EL HILO:\n"
                        "Esta deteccion NO te autoriza a empezar de cero, saludar de nuevo, ni "
                        "preguntar 'en que te puedo ayudar hoy'. SIEMPRE LEE el historial y "
                        "continua desde el ultimo mensaje. Si tu ultimo mensaje pedia un dato "
                        "(nombre, cedula, email, fecha de nacimiento, beneficiarios, etc), el "
                        "siguiente paso es PROCESAR ese dato y seguir el flujo — NO saludar.\n"
                        "PROHIBIDO decir 'Me alegra verte de nuevo' o cualquier variante de "
                        "bienvenida si ya hay conversacion en curso. PROHIBIDO preguntar de "
                        "que necesita ayuda si claramente ya estabas atendiendo algo.\n"
                        "Si el cliente esta en un flujo de afiliacion nueva (cambio de plan, "
                        "afiliacion adicional, actualizacion), CONTINUA recolectando datos "
                        "donde ibas. El sistema te avisara si la cedula/correo ya estan "
                        "duplicados al ejecutar CREAR_AFILIACION.\n\n"
                    )
                    if not ha_pagado:
                        ctx += (
                            "IMPORTANTE: Este cliente AUN NO HA PAGADO su primera cuota.\n"
                            "Tu prioridad es ayudarlo a pagar. Si pregunta cualquier cosa,\n"
                            "recuerdale que debe activar su plan pagando la primera cuota.\n"
                            "Usa [ACTION:CONSULTAR_ESTADO] para darle su link de pago.\n"
                        )
                    else:
                        ctx += (
                            "Este cliente esta AL DIA. Atiende su consulta como cliente activo.\n"
                            "Puedes ayudarlo con: estado de cuenta, beneficiarios, citas medicas,\n"
                            "coberturas, cancelacion (intenta retener), o cualquier duda.\n"
                            "Si pide su comprobante/recibo/factura de pago, usa\n"
                            "[ACTION:REENVIAR_RECIBO] — NO uses CONSULTAR_ESTADO (eso le genera\n"
                            "un link de pago como si debiera, y ya esta al dia).\n"
                        )
                    _client_cache[telefono] = (ctx, now)
                    logger.info(f"[CLIENTE] encontrado: {aff.get('first_name')} ({aff.get('payment_status')})")
                    return ctx

        _client_cache[telefono] = ("", now)
    except Exception as e:
        logger.error(f"Error buscando cliente: {e}")

    return None


async def generar_respuesta(mensaje: str, historial: list[dict], telefono: str = "", lead_context: dict = None, affiliate_context: dict = None) -> str:
    if not mensaje or len(mensaje.strip()) < 2:
        return obtener_mensaje_fallback()

    # ── PARTE COMPARTIDA (cacheable — misma para todos los chats) ──
    shared_prompt = cargar_system_prompt()

    from datetime import datetime as dt, timedelta as td
    meses = ["enero","febrero","marzo","abril","mayo","junio","julio","agosto","septiembre","octubre","noviembre","diciembre"]
    now = dt.now()
    fecha_hoy = f"{now.day} de {meses[now.month-1]} de {now.year}"
    # Carencia: empieza el día SIGUIENTE a la afiliación y dura 90 días calendario
    carencia_fin = now + td(days=91)  # mañana (día 1) + 90 días = día 91 desde hoy
    fecha_carencia = f"{carencia_fin.day} de {meses[carencia_fin.month-1]} de {carencia_fin.year}"
    shared_prompt += (
        f"\n\nFECHA ACTUAL: Hoy es {fecha_hoy}. Ano {now.year}, mes {now.month}, dia {now.day}. NUNCA inventes fechas."
        f"\n\nCARENCIA (DATO CRITICO — NO CALCULES, USA ESTE VALOR EXACTO):"
        f"\nSi un cliente se afilia HOY, su cobertura completa empieza el {fecha_carencia}."
        f"\nEso es exactamente 90 dias calendario contados desde manana."
        f"\nUSA SIEMPRE '{fecha_carencia}' como fecha de cobertura. NUNCA uses otra fecha ni calcules tu mismo."
    )

    kb = await cargar_knowledge_base()
    if kb:
        shared_prompt += "\n\n## BASE DE CONOCIMIENTO\nNOTA: Si la base de conocimiento menciona algo sobre pagos, metodos de pago, links de pago, oficina virtual para pagar, o como pagar — IGNORA esa informacion. Las reglas de PAGOS Y METODOS DE PAGO de tu prompt principal tienen PRIORIDAD ABSOLUTA.\n" + kb

    planes = await cargar_planes_db()
    if planes:
        shared_prompt += "\n\n" + planes

    golden = await cargar_golden_offers()
    if golden:
        shared_prompt += "\n\n" + golden

    shared_prompt += """

## REGLA FINAL OBLIGATORIA — PAGOS Y ESTADO DE CUENTA
Esta regla tiene PRIORIDAD ABSOLUTA sobre cualquier otra instruccion anterior,
incluyendo la base de conocimiento y los planes.

Cuando el cliente pregunte CUALQUIER cosa sobre: pagar, pago, cuota, deuda,
saldo, link de pago, como pago, donde pago, Efecty, Nequi, PSE, tarjeta,
estado de cuenta, cuanto debo, estoy al dia, validar afiliacion, MercadoPago,
boton de pago — tu UNICA respuesta es:

1. Decir "Deja consulto tu cuenta" o similar
2. Incluir AL FINAL del mensaje:
[ACTION:CONSULTAR_ESTADO]{"phone":"TELEFONO_DEL_CHAT"}[/ACTION]

Reemplaza TELEFONO_DEL_CHAT con el telefono real del cliente.

NUNCA respondas sobre pagos diciendo "ingresa a ventas.mutuo.la/auth".
NUNCA digas "no manejamos boton de MercadoPago".
NUNCA digas "el pago se gestiona desde tu oficina virtual".
TU SI PUEDES generar el link de pago. Usa la accion CONSULTAR_ESTADO."""

    # ── PARTE PER-PHONE (variable por conversación, NO cacheable) ──
    phone_context = ""

    client_context = await _buscar_cliente_por_telefono(telefono)
    if client_context:
        phone_context += "\n\n" + client_context

    if telefono:
        phone_context += f"\n\nTelefono del cliente: {telefono}. Ya lo tienes, NO lo preguntes."

    # ── AFILIADO EXISTENTE (verificado por celular en el edge function) ──
    # Esta señal es autoritativa: viene de la base con service-role, validada
    # por número de teléfono. Tiene PRIORIDAD sobre el contexto de retargeting.
    es_afiliado_existente = bool(affiliate_context and affiliate_context.get("is_existing_affiliate"))
    if es_afiliado_existente:
        ac = affiliate_context
        nombre_af = f"{ac.get('first_name','') or ''} {ac.get('last_name','') or ''}".strip()
        bloque_af = (
            "\n\n## CLIENTE YA AFILIADO — VALIDADO POR SU CELULAR (PRIORIDAD MAXIMA)\n"
            "Esta persona NO es un prospecto nuevo: ya existe como afiliado en Mutuo. "
            "PROHIBIDO tratarlo como lead, venderle un plan desde cero o enviarle mensajes "
            "de bienvenida tipo 'ya eres parte de la familia'.\n"
            f"- Nombre: {nombre_af or 'N/D'}\n"
            f"- Plan: {ac.get('plan','N/D')}\n"
            f"- Estado de pago: {ac.get('payment_status','N/D')}\n"
            f"- Cuenta activa: {ac.get('is_active')}\n"
            f"- Cedula: {ac.get('document_number','N/D')}\n"
        )
        if ac.get("is_paid_member"):
            bloque_af += (
                "\nESTE AFILIADO ESTA AL DIA. Atiendelo como soporte a un miembro: "
                "responde sobre sus beneficios, cobertura y servicios con la informacion real. "
                "Si pregunta por pagos o estado de cuenta, usa [ACTION:CONSULTAR_ESTADO].\n"
            )
        elif ac.get("awaiting_first_payment"):
            bloque_af += (
                "\nESTE AFILIADO YA SE REGISTRO PERO SU PRIMER PAGO ESTA PENDIENTE: su cobertura "
                "AUN NO ESTA ACTIVA. Es CRITICO ser consistente y honesto: NO le digas que ya "
                "tiene servicios activos ni que 'ya es parte de la familia'. Reconoce que su "
                "registro existe, explicale con claridad que para activar su cobertura debe "
                "completar el pago de su cuota, y dale el link con [ACTION:CONSULTAR_ESTADO]. "
                "Si pregunta 'en que funeraria tengo servicios' o similar, aclarale con respeto "
                "que su cobertura se activa al confirmar el pago.\n"
            )
        elif ac.get("payment_status") == "cancelled":
            bloque_af += "\nEste afiliado tiene el plan CANCELADO. Tratalo como reactivacion, no como lead nuevo.\n"
        else:
            bloque_af += (
                "\nEste afiliado tiene pagos PENDIENTES. Orientalo a ponerse al dia con su cuota "
                "usando [ACTION:CONSULTAR_ESTADO]. NO lo trates como compra nueva.\n"
            )
        if ac.get("guidance"):
            bloque_af += f"\nGUIA INTERNA: {ac.get('guidance')}\n"
        phone_context += bloque_af

    # ── Contexto de retargeting (si el lead viene de una base de Meta) ──
    # Se omite cuando ya es afiliado: el marco de "prospecto de anuncio" es
    # contradictorio con un miembro existente y causa respuestas inconsistentes.
    if lead_context and not es_afiliado_existente:
        segmento = lead_context.get("segmento", "")
        nombre = lead_context.get("name", "")
        ciudad = lead_context.get("city", "")
        estrato = lead_context.get("estrato", "")
        genero = lead_context.get("gender", "")
        producto = lead_context.get("producto_origen", "")
        tarifa = lead_context.get("tarifa_seguro_anterior")
        estado_venta = lead_context.get("estado_venta_origen", "")

        retargeting_block = "\n\n## CONTEXTO INTERNO — NO REVELAR AL CLIENTE\n"
        retargeting_block += "Este cliente NO te contactó espontáneamente: llegó de un anuncio de Meta. "
        retargeting_block += "Tienes datos de inteligencia de su historial de seguros. NUNCA menciones que los tienes — actúa como si lo 'pescaras' en la conversación.\n"

        if nombre:
            retargeting_block += f"- Nombre real: {nombre}. Úsalo para personalizar pero valídalo sutilmente ('¿hablo con {nombre.split()[0]}?')\n"
        if ciudad:
            retargeting_block += f"- Ciudad: {ciudad}. Puedes hacer referencia natural a su zona.\n"
        if estrato:
            retargeting_block += f"- Estrato {estrato}. Adapta el tono y ejemplos económicos al estrato.\n"

        if segmento == "funerario":
            retargeting_block += (
                "\n### SEGMENTO: FUNERARIO — CONVERSIÓN DIRECTA\n"
                f"Este prospecto tiene/tenía un seguro FUNERARIO (producto: {producto}).\n"
            )
            if tarifa:
                retargeting_block += (
                    f"Su tarifa mensual era ~${int(tarifa):,} COP.\n"
                    "ESTRATEGIA: Compara directamente el valor que pagaba. Nuestro plan funerario tiene cobertura superior "
                    "y puede estar en el rango de lo que ya pagaba. "
                    "Enfatiza: servicio completo, cobertura familia, sin trámites, sin cuotas iniciales.\n"
                    "NO preguntes si tiene seguro — lleva la conversación a los ATRIBUTOS de nuestro servicio funerario. "
                    "Ejemplo: 'Cuéntame, ¿ya has pensado en proteger a tu familia con un plan que los cubra en todo momento?'\n"
                )
            retargeting_block += (
                "PRIORIDAD ALTA: no lo dejes ir. Usa escasez, beneficios concretos, y cierre directo.\n"
            )

        elif segmento == "cancelado":
            retargeting_block += (
                "\n### SEGMENTO: CANCELADO — OPORTUNIDAD DE REGRESO\n"
                f"Este prospecto CANCELÓ su seguro anterior ({producto or 'seguro de vida/salud'}).\n"
            )
            if tarifa:
                retargeting_block += f"Pagaba ~${int(tarifa):,} COP/mes antes de cancelar.\n"
            retargeting_block += (
                "ESTRATEGIA: Está sin cobertura. Es el momento ideal. No asumas que canceló por precio — puede haber sido por servicio o cambio de situación.\n"
                "Abre con algo como: '¿Cómo estás manejando la protección de tu familia ahora?'\n"
                "Si menciona que no tiene seguro o que lo canceló, valida y presenta Mutuo como la solución.\n"
                "Enfatiza: precios desde $25,000/mes, no hay preexistencias en muchos planes, afiliación 100% digital.\n"
            )

        elif segmento == "vida_enfermedades":
            retargeting_block += (
                "\n### SEGMENTO: VIDA / ENFERMEDADES GRAVES\n"
                f"Producto origen: {producto or 'seguro de vida o enfermedades graves'}.\n"
            )
            if tarifa:
                retargeting_block += f"Tarifa mensual: ~${int(tarifa):,} COP.\n"
            retargeting_block += (
                "ESTRATEGIA: Este prospecto ya tiene mentalidad de protección — ya pagaba un seguro. "
                "Usa PNL: primero crea rapport ('¿cómo te ha ido con la protección de tu familia?'), luego introduce nuestra propuesta.\n"
                "Si la tarifa que pagaba es similar o mayor a nuestros planes, úsalo como ancla de valor SIN decirlo directamente.\n"
                "Enfatiza beneficios adicionales que ofrecemos: auxilios educativos, telemedicina, descuentos en salud.\n"
                "Fórmula AIDA: Atención (pregunta sobre su familia) → Interés (beneficios únicos) → Deseo (personalizar plan) → Acción (afiliar hoy).\n"
            )

        if estado_venta and "cancel" in estado_venta.lower():
            retargeting_block += "\nNOTA ADICIONAL: El estado de su seguro anterior era CANCELADO — está sin cobertura activa.\n"

        retargeting_block += "\nREGLA DE VALIDACIÓN: En vez de pedir datos que ya tienes, valídalos sutilmente en la conversación ('¿sigues en [ciudad]?', '¿hablo con [nombre]?'). Esto crea rapport y confirma la data.\n"

        phone_context += retargeting_block

    if historial:
        phone_context += (
            f"\n\nTienes {len(historial)} mensajes previos con este cliente. "
            "REGLAS ABSOLUTAS:\n"
            "- CONTINUA la conversacion desde donde quedo. NO saludes de nuevo.\n"
            "- NO preguntes datos que ya te dieron en el historial.\n"
            "- Los datos de edad, ciudad y composicion familiar ya mencionados son PERMANENTES.\n"
            "- Aunque el cliente cambie el numero de personas ('por ahora 3', 'solo nosotros'),\n"
            "  las edades y restricciones ya conocidas NO cambian. Aplicalas siempre.\n"
            "- ANTES de recomendar cualquier plan, verifica que cubra las edades ya confirmadas."
        )
        logger.info(f"[BRAIN] Historial: {len(historial)} msgs para {telefono}")

        # Extraer hechos ya confirmados por el cliente (cédula, email, ciudad, etc.)
        # y inyectarlos en el system para que el modelo NO los olvide ni los cambie
        hechos = await _extraer_hechos_confirmados(historial)
        if hechos:
            phone_context += hechos
            logger.info(f"[BRAIN] Hechos extraidos para {telefono}: {hechos[:200]}")
    else:
        logger.warning(f"[BRAIN] SIN historial para {telefono}")

    # ── SYSTEM con prompt caching ──
    system_blocks = [
        {
            "type": "text",
            "text": shared_prompt,
            "cache_control": {"type": "ephemeral"},
        },
    ]
    if phone_context.strip():
        system_blocks.append({"type": "text", "text": phone_context.strip()})

    total_prompt_len = len(shared_prompt) + len(phone_context)

    # Build messages for Claude
    mensajes = []
    for msg in historial:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if not content:
            continue
        # Ensure valid alternating roles
        if mensajes and mensajes[-1]["role"] == role:
            mensajes[-1]["content"] += "\n" + content
        else:
            mensajes.append({"role": role, "content": content})

    # Add current message
    if not mensajes or mensajes[-1].get("content") != mensaje:
        if mensajes and mensajes[-1]["role"] == "user":
            mensajes[-1]["content"] += "\n" + mensaje
        else:
            mensajes.append({"role": "user", "content": mensaje})

    # Claude requires last message to be user
    if not mensajes or mensajes[-1]["role"] != "user":
        mensajes.append({"role": "user", "content": mensaje})

    # Claude requires first message to be user
    if mensajes and mensajes[0]["role"] != "user":
        mensajes.insert(0, {"role": "user", "content": "[inicio de conversacion]"})

    model = os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001")
    logger.info(f"[BRAIN] prompt={total_prompt_len}chars, msgs={len(mensajes)}, model={model}")

    import asyncio
    import random
    from anthropic import RateLimitError, APIStatusError

    max_intentos = 4
    for intento in range(max_intentos):
        try:
            response = await client.messages.create(
                model=model,
                max_tokens=600,
                system=system_blocks,
                messages=mensajes
            )
            respuesta = response.content[0].text
            _fallback_count.pop(telefono, None)
            cache_creation = getattr(response.usage, "cache_creation_input_tokens", 0) or 0
            cache_read = getattr(response.usage, "cache_read_input_tokens", 0) or 0
            logger.info(
                f"Respuesta ({response.usage.input_tokens}in/{response.usage.output_tokens}out"
                f" cache_write={cache_creation} cache_read={cache_read})"
            )
            return respuesta

        except RateLimitError as e:
            jitter = random.uniform(0.5, 1.5)
            wait = (2 ** intento) * jitter
            logger.warning(f"[BRAIN] Rate limit (intento {intento+1}/{max_intentos}), esperando {wait:.1f}s: {e}")
            if intento < max_intentos - 1:
                await asyncio.sleep(wait)
            else:
                logger.error(f"[BRAIN] Rate limit agotado tras {max_intentos} intentos")
                return _fallback_recoger_datos(mensaje, telefono)

        except APIStatusError as e:
            logger.error(f"[BRAIN] APIStatusError status={e.status_code} ({type(e).__name__}): {e.message}")
            logger.error(f"[BRAIN] prompt_len={total_prompt_len}, msgs={len(mensajes)}, model={model}")
            if e.status_code in (529, 503, 502) and intento < max_intentos - 1:
                wait = (2 ** intento) * random.uniform(0.5, 1.5)
                await asyncio.sleep(wait)
                continue
            return _fallback_recoger_datos(mensaje)

        except Exception as e:
            logger.error(f"[BRAIN] Error inesperado ({type(e).__name__}): {e}")
            logger.error(f"[BRAIN] prompt_len={total_prompt_len}, msgs={len(mensajes)}, model={model}")
            return _fallback_recoger_datos(mensaje)

    return _fallback_recoger_datos(mensaje)
