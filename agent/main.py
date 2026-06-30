# agent/main.py — Servidor FastAPI + Webhook de WhatsApp
# Mutuo Fintech S.A.S. — Origen AI Agent

"""
Servidor principal del agente de IA.
Funciona con cualquier proveedor (Whapi, Meta, Twilio) gracias a la capa de providers.
Usa el motor conversacional para prospección y ventas de planes de protección familiar.
"""

import os
import re
import json
import logging
import asyncio
from datetime import datetime
from collections import OrderedDict
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.responses import PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
import httpx

from agent.memory import inicializar_db, guardar_mensaje, obtener_historial, obtener_leads
from agent.providers import obtener_proveedor
from agent.mutuo_actions import crear_afiliacion, generar_link_pago, consultar_estado_cuenta, crear_ticket_cancelacion, consultar_radicado, _parse_birth_date, _calc_age, consultar_cuenta_por_cedula, actualizar_beneficiarios, reenviar_recibo
from agent.crm_sync import sync_inbound, sync_outbound, get_or_create_conversation, update_conversation, save_message
from datetime import timezone
from agent.outbound import OutboundCampaign
# voice_handler se usa en voice_main.py (servicio separado en Railway)
from origen_ia.agent.core import OrigenIA, cost_tracker
from origen_ia.crm.vendu_client import VenduCRMClient
from origen_ia.analytics.kpi_tracker import KPITracker
from origen_ia.config.campaign_prompts import MUTUO_OUTBOUND

load_dotenv()

# Configuracion de logging
ENVIRONMENT = os.getenv("ENVIRONMENT", "development")
log_level = logging.DEBUG if ENVIRONMENT == "development" else logging.INFO
logging.basicConfig(level=log_level)
logger = logging.getLogger("origen-ai")

# Proveedor de WhatsApp
proveedor = obtener_proveedor()
PORT = int(os.getenv("PORT", 8000))

# URLs de imagenes de planes (JPG en public/planes/ del sitio publico)
_PLAN_IMAGE_BASE = os.getenv("PLAN_IMAGE_BASE_URL", "https://ventas.mutuo.la/planes").rstrip("/")
PLAN_IMAGE_URLS = {
    "esencial": f"{_PLAN_IMAGE_BASE}/plan-esencial.jpg",
    "plus": f"{_PLAN_IMAGE_BASE}/plan-plus.jpg",
    "total": f"{_PLAN_IMAGE_BASE}/plan-total.jpg",
}

# QR de Nequi para pagos (imagen en public/pagos/ del sitio publico).
# La llave Bre-B se incluye como caption de respaldo por si el cliente no puede escanear.
NEQUI_QR_URL = os.getenv("NEQUI_QR_URL", "https://ventas.mutuo.la/pagos/nequi-qr.png")
NEQUI_LLAVE_BREB = os.getenv("NEQUI_LLAVE_BREB", "009 106 4547")
NEQUI_QR_CAPTION = (
    "Escanea este QR con tu app Nequi y queda activa tu afiliacion.\n\n"
    f"O usa la llave Bre-B: {NEQUI_LLAVE_BREB}\n"
    "A nombre de Mutuo Fintech S.A.S.\n\n"
    "Cuando pagues, mandanos el comprobante por aqui para confirmar tu pago."
)

# Sesiones activas de Origen AI (por telefono)
sesiones: dict[str, OrigenIA] = {}

# Teléfonos contactados por campaña outbound (para inyectar contexto)
outbound_phones: set[str] = set()

# Teléfonos con conversación inbound activa — outbound NO debe enviarles más
active_inbound_phones: set[str] = set()

# Deduplicación de mensajes — cache LRU de mensaje_id ya procesados
# Evita procesar el mismo mensaje cuando el proveedor reintenta el webhook
_processed_message_ids: OrderedDict[str, bool] = OrderedDict()
_MAX_PROCESSED_CACHE = 5000

# Lock por teléfono para evitar procesamiento concurrente del mismo lead
_phone_locks: dict[str, asyncio.Lock] = {}
_MAX_PHONE_LOCKS = 2000


def _prune_locks(locks: dict[str, asyncio.Lock], max_size: int) -> None:
    """Elimina locks que no están en uso cuando el dict supera max_size.
    Evita el crecimiento ilimitado de memoria en uptime largo. Solo borra
    locks NO adquiridos, así nunca interrumpe un procesamiento en curso."""
    if len(locks) <= max_size:
        return
    for key in [k for k, lk in locks.items() if not lk.locked()]:
        del locks[key]
        if len(locks) <= max_size:
            break

# Semáforo global: máximo N llamadas concurrentes a Anthropic
_api_semaphore = asyncio.Semaphore(5)


_dedup_lock = asyncio.Lock()


async def _is_duplicate_message(mensaje_id: str) -> bool:
    """Verifica si ya procesamos este mensaje. Atómico — evita race condition entre corutinas."""
    if not mensaje_id:
        return False
    async with _dedup_lock:
        if mensaje_id in _processed_message_ids:
            return True
        _processed_message_ids[mensaje_id] = True
        if len(_processed_message_ids) > _MAX_PROCESSED_CACHE:
            _processed_message_ids.popitem(last=False)
        return False


def _get_phone_lock(telefono: str) -> asyncio.Lock:
    """Obtiene un lock único por teléfono para evitar procesamiento concurrente."""
    lock = _phone_locks.get(telefono)
    if lock is None:
        _prune_locks(_phone_locks, _MAX_PHONE_LOCKS)
        lock = _phone_locks.setdefault(telefono, asyncio.Lock())
    return lock

# CRM y KPIs
crm = VenduCRMClient()
kpi_tracker = KPITracker()

# Campaña outbound
campaign = OutboundCampaign(proveedor)

# Estado cacheado de Whapi (actualizado cada 5 min por el monitor)
whapi_status: dict = {"status": "unknown", "healthy": False, "message": "No verificado aún"}

# Watchdog de inbound — detecta el "apagón silencioso": Whapi sigue conectado
# (el monitor de /health da OK) pero dejó de entregar webhooks y no llega ni un
# solo mensaje de clientes. Sin esto, el bot puede quedar mudo horas sin avisar.
_last_inbound_at: datetime = datetime.now(timezone.utc)
_inbound_alerted: bool = False
INBOUND_WATCHDOG_MINUTES = int(os.getenv("INBOUND_WATCHDOG_MINUTES", "30"))


async def check_is_outbound_lead(telefono: str) -> dict | None:
    """Consulta Supabase para saber si este teléfono es de una campaña outbound."""
    sb_url = os.getenv("SUPABASE_URL", "")
    sb_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "") or os.getenv("SUPABASE_ANON_KEY", "")
    if not sb_url or not sb_key:
        return None

    # Normalizar: buscar con y sin 57
    phone_clean = telefono.replace("+", "")
    phone_short = phone_clean[2:] if phone_clean.startswith("57") and len(phone_clean) == 12 else phone_clean

    url = (
        f"{sb_url}/rest/v1/lead_database_entries"
        f"?or=(phone.eq.{phone_clean},phone.eq.{phone_short})"
        f"&assigned_to=eq.bot"
        f"&select=id,name,city,address,estrato,status"
        f"&limit=1"
    )
    headers = {
        "Authorization": f"Bearer {sb_key}",
        "apikey": sb_key,
    }
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(url, headers=headers)
            if r.status_code == 200:
                data = r.json()
                if data:
                    return data[0]
    except Exception as e:
        logger.warning(f"Error checking outbound lead: {e}")
    return None


async def load_history_from_supabase(telefono: str) -> tuple[list[dict], dict]:
    """Carga historial previo y datos del prospecto desde Supabase.
    Retorna (historial, perfil_data)."""
    sb_url = os.getenv("SUPABASE_URL", "")
    sb_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "") or os.getenv("SUPABASE_KEY", "") or os.getenv("SUPABASE_ANON_KEY", "")
    if not sb_url or not sb_key:
        return [], {}

    phone_clean = telefono.replace("+", "").replace(" ", "")
    phone_short = phone_clean[2:] if phone_clean.startswith("57") and len(phone_clean) == 12 else phone_clean

    historial: list[dict] = []
    perfil_data: dict = {}

    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r_conv = await c.get(
                f"{sb_url}/rest/v1/whatsapp_conversations"
                f"?or=(phone.eq.{phone_clean},phone.eq.{phone_short},phone_number.eq.{phone_clean},phone_number.eq.{phone_short})"
                f"&select=id,prospect_name,city,department,current_operator,interest,disc_profile,status,notes"
                f"&limit=1",
                headers={"Authorization": f"Bearer {sb_key}", "apikey": sb_key},
            )
            if r_conv.status_code == 200:
                convs = r_conv.json()
                if convs:
                    conv = convs[0]
                    perfil_data = {
                        "nombre": conv.get("prospect_name", ""),
                        "ciudad": conv.get("city", ""),
                        "departamento": conv.get("department", ""),
                        "operador_actual": conv.get("current_operator", ""),
                        "interest": conv.get("interest", ""),
                        "disc_profile": conv.get("disc_profile", ""),
                        "status": conv.get("status", ""),
                        "notes": conv.get("notes", ""),
                    }
                    r_msgs = await c.get(
                        f"{sb_url}/rest/v1/whatsapp_messages"
                        f"?conversation_id=eq.{conv['id']}"
                        f"&select=role,content"
                        f"&order=created_at.desc&limit=50",
                        headers={"Authorization": f"Bearer {sb_key}", "apikey": sb_key},
                    )
                    if r_msgs.status_code == 200:
                        msgs = r_msgs.json()
                        msgs.reverse()
                        for m in msgs:
                            if m.get("role") and m.get("content"):
                                historial.append({"role": m["role"], "content": m["content"]})
    except Exception as e:
        logger.warning(f"[HISTORY] Error cargando historial de {telefono}: {e}")

    return historial, perfil_data


async def get_or_create_session(telefono: str, is_outbound: bool = False, lead_data: dict = None) -> OrigenIA:
    """Obtiene o crea una sesion de Origen AI. Si es nueva, carga historial previo desde Supabase."""
    if telefono not in sesiones:
        agente = OrigenIA(canal="whatsapp")

        # Cargar historial previo desde Supabase (clave para que el bot recuerde conversaciones anteriores)
        historial, perfil_data = await load_history_from_supabase(telefono)
        if historial:
            agente.historial = historial
            agente.turnos = len([m for m in historial if m["role"] == "user"])
            logger.info(f"[HISTORY] {telefono} — {len(historial)} msgs previos cargados desde Supabase")

        # Pre-poblar perfil con datos de Supabase
        if perfil_data.get("nombre"):
            agente.profile.nombre = perfil_data["nombre"].split()[0].title()
            agente.profile.nombre_completo = perfil_data["nombre"]
        if perfil_data.get("ciudad"):
            agente.profile.ciudad = perfil_data["ciudad"]
        if perfil_data.get("departamento"):
            agente.profile.departamento = perfil_data.get("departamento", "")
        if perfil_data.get("interest"):
            agente.profile.paquete_recomendado = perfil_data["interest"]
        if perfil_data.get("disc_profile"):
            agente.profile.perfil_disc = perfil_data["disc_profile"]

        # Marcar usuario que regresa — el bot NO debe saludar de nuevo ni pedir datos que ya tiene
        agente.is_returning = len(historial) > 0
        if agente.is_returning:
            logger.info(f"[RETURNING] {telefono} — usuario con historial previo ({len(historial)} msgs)")

        if is_outbound or telefono in outbound_phones:
            lead_info = lead_data or {}
            campaign_ctx = MUTUO_OUTBOUND.format(
                nombre=lead_info.get("name", "") or perfil_data.get("nombre", ""),
                ciudad=lead_info.get("city", "") or perfil_data.get("ciudad", ""),
                direccion=lead_info.get("address", ""),
                estrato=lead_info.get("estrato", ""),
            )
            agente.campaign_context = campaign_ctx
            agente.is_outbound = True

            if lead_info.get("name") and not agente.profile.nombre:
                agente.profile.nombre = lead_info["name"].split()[0].title()
                agente.profile.nombre_completo = lead_info["name"].title()
            if lead_info.get("city") and not agente.profile.ciudad:
                agente.profile.ciudad = lead_info["city"]
            if lead_info.get("estrato"):
                agente.profile.estrato = lead_info["estrato"]
            agente.profile.telefono = telefono

            logger.info(f"[OUTBOUND→INBOUND] {telefono} — perfil: {agente.profile.nombre}, {agente.profile.ciudad}")
        else:
            agente.is_outbound = False

        sesiones[telefono] = agente
    return sesiones[telefono]


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Inicializa la base de datos y auto-inicia campaña outbound."""
    await inicializar_db()
    logger.info("Base de datos inicializada")
    logger.info(f"Origen AI corriendo en puerto {PORT}")
    logger.info(f"Proveedor de WhatsApp: {proveedor.__class__.__name__}")

    # Auto-iniciar campaña outbound (se ejecuta solo en horario 7am-7pm COL)
    campaign.start()
    logger.info("[CAMPAIGN] Auto-iniciada al arrancar el bot")

    # Monitor de Whapi: chequea conexión cada 5 min y sincroniza historial al reconectarse
    asyncio.create_task(whapi_health_monitor())
    logger.info("[WHAPI-HEALTH] Monitor iniciado")

    # Watchdog de inbound: alerta si dejamos de recibir mensajes en horario hábil
    # (cubre el caso de Whapi conectado pero sin entregar webhooks)
    asyncio.create_task(inbound_watchdog())
    logger.info("[INBOUND-WATCHDOG] Watchdog iniciado")

    # Reactivación de leads inactivos (cada hora, horario hábil)
    from agent.reactivation import reactivation_loop
    asyncio.create_task(reactivation_loop(proveedor, crm))
    logger.info("[REACTIVATION] Loop iniciado")

    # Reportes automáticos (cada hora por WA, diario por email a las 7pm)
    from agent.reports import report_scheduler
    asyncio.create_task(report_scheduler(proveedor))
    logger.info("[REPORT] Scheduler iniciado")

    # Cobro diario embebido: el pg_cron de Supabase es frágil (si pierde el
    # secreto del Vault deja de cobrar sin error). Lo corremos desde el bot,
    # que sí permanece vivo. Las edge functions deduplican: no hay doble cobro.
    from agent.collection import collection_loop
    asyncio.create_task(collection_loop())
    logger.info("[COBRO] Scheduler iniciado")

    yield


app = FastAPI(
    title="Origen AI — Mutuo Plan Exequial (Natalia)",
    version="2.0.0",
    lifespan=lifespan
)

# CORS — permite que el frontend de Mutuo llame a los endpoints del bot
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)



@app.get("/admin/crm-test")
async def crm_test(request: Request):
    """Diagnóstico: verifica que crm_sync puede escribir en Supabase."""
    secret = request.headers.get("x-import-secret", "")
    if secret != IMPORT_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")
    sb_url = os.getenv("SUPABASE_URL", "")
    sb_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "") or os.getenv("SUPABASE_KEY", "") or os.getenv("SUPABASE_ANON_KEY", "")
    result = {"sb_url_set": bool(sb_url), "sb_key_set": bool(sb_key), "sb_key_prefix": sb_key[:12] if sb_key else None}
    # Test direct insert into whatsapp_conversations
    import httpx as _httpx
    _headers = {"Authorization": f"Bearer {sb_key}", "apikey": sb_key,
                "Content-Type": "application/json", "Prefer": "return=representation"}
    try:
        async with _httpx.AsyncClient(timeout=10) as _http:
            # Try GET first
            rg = await _http.get(f"{sb_url}/rest/v1/whatsapp_conversations?phone=eq.571234567890&select=id&limit=1",
                                  headers={**_headers, "Prefer": ""})
            result["get_status"] = rg.status_code
            result["get_body"] = rg.text[:300]
            # Try POST
            rp = await _http.post(f"{sb_url}/rest/v1/whatsapp_conversations",
                                   headers=_headers,
                                   json={"phone": "571234567890", "status": "nuevo",
                                         "handoff_status": "bot", "prospect_name": "TEST_DIAG"})
            result["post_status"] = rp.status_code
            result["post_body"] = rp.text[:300]
    except Exception as e:
        result["error"] = str(e)
    return result


@app.post("/admin/run-collection")
async def run_collection(request: Request):
    """Dispara el cobro ahora mismo (cartera + run-daily + drenar cola).
    Sirve para recuperar el backlog sin esperar a la corrida de las 9am.
    Protegido con el mismo secreto que el resto de endpoints admin."""
    secret = request.headers.get("x-import-secret", "")
    if secret != IMPORT_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")
    from agent.collection import run_collection_now
    results = await run_collection_now()
    return {"triggered": True, "results": results}


@app.get("/")
async def health_check():
    return {
        "status": "ok",
        "service": "origen-ia",
        "agent": "Natalia",
        "product": "Mutuo Plan Exequial",
        "version": "2.0.0",
        "sesiones_activas": len(sesiones),
    }


@app.get("/test-ai")
async def test_ai():
    """Test directo a la API de IA para debug."""
    import os
    from anthropic import AsyncAnthropic
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        return {"error": "ANTHROPIC_API_KEY not set"}
    c = AsyncAnthropic(api_key=api_key)
    try:
        model = os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001")
        r = await c.messages.create(model=model, max_tokens=50, messages=[{"role": "user", "content": "Di hola"}])
        return {"ok": True, "text": r.content[0].text[:100]}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}


@app.get("/webhook")
async def webhook_verificacion(request: Request):
    resultado = await proveedor.validar_webhook(request)
    if resultado is not None:
        return PlainTextResponse(str(resultado))
    return {"status": "ok"}


_BRACKET_LEAK_RE = re.compile(r'\[[^\[\]\n]{1,80}\]')


def _sanitizar_placeholders(texto: str) -> str:
    """
    Quita corchetes tipo [falta X], [por confirmar], [Hijo 1], [nombre],
    [pendiente] que el modelo puede filtrar al resumen del cliente.
    Preserva intencionalmente [ACTION:...] porque ese se procesa antes;
    si llega aquí, es ruido y también se elimina.
    """
    if not texto or '[' not in texto:
        return texto

    # Remover cada corchete que parezca placeholder (no un link markdown)
    def _sub(match: re.Match) -> str:
        contenido = match.group(0)
        # Dejar pasar URLs entre corchetes (raros pero posibles)
        if contenido.startswith('[http'):
            return contenido
        return ''

    limpio = _BRACKET_LEAK_RE.sub(_sub, texto)
    # Colapsar espacios y saltos de línea que queden huérfanos
    limpio = re.sub(r'[ \t]+', ' ', limpio)
    limpio = re.sub(r'\n{3,}', '\n\n', limpio)
    # Quitar líneas que quedaron vacías tipo "• ", "1. -", "Nombre:"
    lineas = []
    for linea in limpio.split('\n'):
        despojada = linea.strip(' -•·*\t')
        if not despojada:
            continue
        # Línea que es solo numeración huérfana tipo "2.", "3)", "1 -"
        if re.match(r'^\d+[\.\)\-]?\s*$', despojada):
            continue
        # Línea tipo "Nombre:" o "Cédula:" sin valor
        if re.match(r'^[A-Za-zÁÉÍÓÚáéíóúÑñ ]+:\s*$', despojada):
            continue
        lineas.append(linea.rstrip())
    return '\n'.join(lineas).strip()


# ── Red de seguridad: promesa de consulta sin acción ──────────────────────────
# El bot (LLM) a veces responde "deja consulto tu cuenta y te muestro todo" o
# "deja reviso tu cuenta y te genero el link de pago" SIN emitir la acción
# [ACTION:CONSULTAR_ESTADO]. El cliente queda esperando los detalles (plan, deuda,
# link) que nunca llegan. Detectamos esa promesa por palabra clave y disparamos la
# consulta igual, aunque el LLM haya fallado.
_PROMESA_CONSULTA_RE = re.compile(
    r"(deja|d[eé]jame|voy\s+a|perm[ií]teme|ya\s+mismo)\s+(te\s+)?"
    r"(consult|revis|verific|chequ|mir)\w*"
    r"|(consult|revis|verific)\w*\s+(tu|su)\s+(cuenta|estado|plan|saldo|informaci[oó]n)"
    r"|te\s+(muestro|genero|paso|env[ií]o|doy)\s+(todo|el\s+link|tu\s+link|el\s+detalle|tu\s+plan|el\s+estado)"
    r"|te\s+lo\s+(reviso|consulto|muestro|genero)",
    re.IGNORECASE,
)
# No disparar CONSULTAR_ESTADO cuando la promesa es de recibo/comprobante (eso lo
# maneja REENVIAR_RECIBO) para no generarle un link de pago a quien ya pagó.
_PROMESA_EXCLUIR_RE = re.compile(
    r"recibo|comprobante|factura|soporte\s+de\s+pago|cancelaci[oó]n|radicado|nequi",
    re.IGNORECASE,
)


def _promete_consulta_sin_accion(respuesta: str) -> bool:
    """True si el bot prometió consultar/mostrar la cuenta o el link pero no emitió acción."""
    t = respuesta or ""
    if "[ACTION:" in t:
        return False
    if _PROMESA_EXCLUIR_RE.search(t):
        return False
    return bool(_PROMESA_CONSULTA_RE.search(t))


async def _ejecutar_acciones(respuesta: str, telefono: str) -> tuple[str, str | None, str | None]:
    """
    Parsea la respuesta del bot buscando [ACTION:TIPO]{json}[/ACTION].
    Ejecuta la acción y retorna (texto_limpio, mensaje_extra, imagen_url).
    """
    action_match = re.search(r'\[ACTION:(\w+)\](.*?)\[/ACTION\]', respuesta, re.DOTALL)
    if not action_match:
        # Red de seguridad: el bot prometió consultar/mostrar la cuenta o generar el
        # link pero NO emitió [ACTION:CONSULTAR_ESTADO]. El cliente se queda esperando
        # sus detalles que nunca llegan ("deja consulto tu cuenta y te muestro todo"
        # sin acción → nunca veía su plan ni su link). Disparamos la consulta para
        # entregarle plan + estado + link en el mismo turno.
        if _promete_consulta_sin_accion(respuesta):
            logger.warning(
                f"[CONSULTA RED-SEGURIDAD] {telefono}: el bot prometió consultar/mostrar "
                f"la cuenta sin emitir acción → disparando CONSULTAR_ESTADO"
            )
            respuesta = f'{respuesta}\n[ACTION:CONSULTAR_ESTADO]{{"phone":"{telefono}"}}[/ACTION]'
            action_match = re.search(r'\[ACTION:(\w+)\](.*?)\[/ACTION\]', respuesta, re.DOTALL)
        else:
            return _sanitizar_placeholders(respuesta), None, None

    action_type = action_match.group(1)
    action_data_str = action_match.group(2).strip()

    # Limpiar la respuesta (remover el bloque de acción + placeholders leak)
    texto_limpio = _sanitizar_placeholders(respuesta[:action_match.start()].strip())

    try:
        action_data = json.loads(action_data_str)
    except json.JSONDecodeError:
        logger.error(f"[ACTION] JSON inválido: {action_data_str[:200]}")
        return texto_limpio, None, None

    respuesta_extra = None
    imagen_url: str | None = None

    # ── Habeas Data: bloquear gestiones de cuenta si el afiliado las desactivó ──
    # (estado de cuenta, recibos, beneficiarios, cancelaciones, radicados, pagos).
    try:
        from agent.bot_preferences import can_interact, CATEGORY_SOPORTE, ACCOUNT_SUPPORT_ACTIONS
        if action_type in ACCOUNT_SUPPORT_ACTIONS and not await can_interact(telefono, CATEGORY_SOPORTE):
            logger.info(f"[HABEAS] {telefono} — acción de soporte '{action_type}' bloqueada por preferencias")
            return texto_limpio, (
                "Para gestiones de tu cuenta, por seguridad escríbenos a sac@mutuo.la "
                "o llámanos y con gusto te ayudamos por ese medio. 💜"
            ), None
    except Exception as _hp:
        logger.warning(f"[HABEAS] error verificando soporte (fail-open): {_hp}")

    if action_type == "CREAR_AFILIACION":
        action_data["phone"] = action_data.get("phone", telefono)
        result = await crear_afiliacion(action_data)
        if result.get("duplicate"):
            nombre = result.get("first_name") or "amigo"
            recovery = result.get("recovery_link") or "https://ventas.mutuo.la/auth"
            respuesta_extra = (
                f"Hola {nombre}, ya tienes una afiliacion activa con nosotros.\n\n"
                f"Una misma persona solo puede ser titular una vez. "
                f"Para entrar a tu cuenta y recuperar tu acceso (te enviamos un codigo a tu correo):\n"
                f"{recovery}\n\n"
                f"Si quieres agregar mas beneficiarios o cambiar tu plan, lo puedes hacer desde alli."
            )
            logger.info(f"[ACTION CREAR_AFILIACION DUPLICATE] {telefono} → {result.get('matched_field')}")
        elif result.get("success"):
            # No prometer el contrato si el envio fallo: el bot decia "Te envie tu
            # contrato" siempre, incluso cuando los 3 intentos fallaban. Usamos el
            # flag real que devuelve crear_afiliacion.
            contrato_ok = result.get("contrato_enviado", True)
            contrato_line = (
                "Te envie tu contrato y los accesos a tu cuenta al correo que nos diste."
                if contrato_ok else
                "Tu contrato y los accesos a tu cuenta te llegaran al correo en unos minutos. "
                "Si no lo ves, revisa la carpeta de spam o escribenos por aqui y te lo reenviamos."
            )
            # Generar link de pago con reintentos
            link_result = {"success": False}
            for intento in range(3):
                link_result = await generar_link_pago(
                    result["affiliation_id"],
                    result.get("price", 25000),
                    action_data.get("first_name", ""),
                    action_data.get("email", ""),
                    action_data.get("document_number", ""),
                )
                if link_result.get("success"):
                    break
                logger.warning(f"[PAGO] Intento {intento+1}/3 fallido para {telefono}")
                import asyncio
                await asyncio.sleep(2)

            if link_result.get("success"):
                respuesta_extra = (
                    f"Tu afiliacion quedo registrada exitosamente.\n\n"
                    f"{contrato_line}\n\n"
                    f"Para activar tu cobertura, realiza el primer pago aqui:\n"
                    f"{link_result['payment_link']}\n\n"
                    f"Puedes pagar con tarjeta, PSE, Efecty o Nequi."
                )
            else:
                # Fallback: link directo a recaudo con cedula y email para auto-consulta
                import urllib.parse
                cedula_enc = urllib.parse.quote(action_data.get('document_number', ''))
                email_enc = urllib.parse.quote(action_data.get('email', ''))
                fallback_link = f"https://ventas.mutuo.la/recaudo?cedula={cedula_enc}&email={email_enc}&auto=true"
                respuesta_extra = (
                    f"Tu afiliacion quedo registrada exitosamente.\n\n"
                    f"{contrato_line}\n\n"
                    f"Para pagar tu primera cuota ingresa aqui:\n"
                    f"{fallback_link}\n\n"
                    f"Puedes pagar con tarjeta, PSE, Efecty o Nequi."
                )
            logger.info(f"[ACTION CREAR_AFILIACION] {telefono} → {result.get('plan')} (contrato_enviado={contrato_ok})")
            # El lead ya se convirtió en afiliado real: cerrar el borrador en curso
            # para no dejar un duplicado in_progress en el panel.
            try:
                from agent.draft_affiliation import cerrar_borradores
                asyncio.create_task(cerrar_borradores(telefono))
            except Exception:
                pass
        else:
            # Diferenciar errores de DATOS (el cliente los puede corregir) de errores
            # de sistema. Antes cualquier fallo mandaba al cliente a soporte, incluso
            # cuando solo habia que re-pedirle la cedula o el correo.
            err = (result.get("error") or "").lower()
            if "cedula" in err or "cédula" in err or "documento" in err:
                respuesta_extra = (
                    "Ese numero de documento no me cuadra. Me confirmas tu numero de "
                    "cedula completo, solo numeros, para registrarte bien?"
                )
            elif "email" in err or "correo" in err:
                respuesta_extra = (
                    "Ese correo no me parece valido. Me lo confirmas bien escrito? "
                    "(por ejemplo: nombre@gmail.com)"
                )
            elif "faltan campos" in err or "obligatorio" in err:
                respuesta_extra = (
                    "Me falto un dato para completar tu afiliacion. Confirmame por favor "
                    "tu nombre completo, numero de cedula, correo y ciudad."
                )
            else:
                respuesta_extra = "Hubo un problema registrando tu afiliacion. Intentalo de nuevo o escribenos a sac@mutuo.la"
            logger.error(f"[ACTION CREAR_AFILIACION FAILED] {telefono} → {result.get('error')}")

    elif action_type == "CONSULTAR_ESTADO":
        action_data["phone"] = action_data.get("phone", telefono)
        result = await consultar_estado_cuenta(action_data["phone"])
        if result.get("found"):
            if result.get("payment_status") == "paid":
                from agent.brain import invalidar_cache_cliente
                invalidar_cache_cliente(telefono)
            if result.get("primer_pago_pendiente"):
                # Afiliado recién creado dentro de su primer ciclo: NO está en mora.
                # Mensaje de activación/bienvenida con el link del primer pago.
                monto = int(result.get("tarifa") or 24900)
                link_result = await generar_link_pago(result["affiliation_id"], monto, result["name"])
                link_text = f"\n\n{link_result['payment_link']}" if link_result.get("success") else ""
                respuesta_extra = (
                    f"Hola {result['name']}! 💜 Bienvenido a Mutuo, Club de Bienestar Familiar.\n\n"
                    f"Para activar tu plan {result['plan']} y empezar a disfrutar los beneficios, "
                    f"realiza tu primer pago de ${monto:,} COP:"
                    f"{link_text}\n\n"
                    f"Puedes pagar con tarjeta, PSE, Efecty o Nequi. Cualquier duda, aquí estoy 💜"
                )
            elif result["total_deuda"] > 0:
                link_result = await generar_link_pago(result["affiliation_id"], int(result["total_deuda"]), result["name"])
                link_text = f"\n\nPaga aquí: {link_result['payment_link']}" if link_result.get("success") else ""
                meses_pend = result.get("meses_pendientes") or []
                detalle_meses = f"Meses pendientes: {', '.join(meses_pend)}\n" if meses_pend else ""
                respuesta_extra = (
                    f"Hola {result['name']}!\n\n"
                    f"Plan: {result['plan']}\n"
                    f"Deuda: ${int(result['total_deuda']):,} COP\n"
                    f"{detalle_meses}"
                    f"Cuotas pendientes: {result['cuotas_pendientes']}"
                    f"{link_text}"
                )
            else:
                # Nombrar el ultimo mes pagado evita el error de decir "estas al dia"
                # de forma que el cliente entienda que el mes en curso ya esta cubierto
                # cuando solo pago el mes anterior.
                ultimo = result.get("ultimo_periodo_pagado")
                if ultimo:
                    respuesta_extra = (
                        f"Hola {result['name']}! No tienes cobros vencidos en este momento. "
                        f"Tu última cuota registrada es la de {ultimo}. "
                        f"Si quieres adelantar tu siguiente cuota, dime y te genero el link 💜"
                    )
                else:
                    respuesta_extra = f"Hola {result['name']}! No tienes cobros vencidos en este momento."
        else:
            respuesta_extra = None  # Bot handles the "not found" response

    elif action_type == "PAGO_ANTICIPADO":
        # Cliente que quiere pagar (aunque este al dia, p.ej. adelantar la cuota del
        # proximo mes). Si debe, le damos el link de la deuda; si esta al dia, le
        # generamos un link por el valor de una cuota mensual para que adelante.
        action_data["phone"] = action_data.get("phone", telefono)
        result = await consultar_estado_cuenta(action_data["phone"])
        if not result.get("found"):
            respuesta_extra = None  # Bot handles the "not found" response
        elif result.get("canal") == "empresarial":
            respuesta_extra = (
                f"Hola {result['name']}! Tu plan es por libranza a través de tu empresa, "
                f"así que el pago lo gestiona directamente tu empleador. No necesitas pagar "
                f"por aquí. Si tienes dudas, escríbenos a sac@mutuo.la 💜"
            )
        else:
            monto = int(result["total_deuda"]) if result["total_deuda"] > 0 else int(result.get("tarifa") or 24900)
            link_result = await generar_link_pago(result["affiliation_id"], monto, result["name"])
            if link_result.get("success"):
                if result.get("primer_pago_pendiente"):
                    respuesta_extra = (
                        f"Listo {result['name']} 💜 Para activar tu plan {result['plan']}, "
                        f"este es el link de tu primer pago (${monto:,} COP):\n\n"
                        f"{link_result['payment_link']}\n\n"
                        f"Apenas lo recibamos, tu plan queda activo. "
                        f"Puedes pagar con tarjeta, PSE, Efecty o Nequi."
                    )
                elif result["total_deuda"] > 0:
                    respuesta_extra = (
                        f"Listo {result['name']} 💜 Aquí tienes tu link de pago "
                        f"(${monto:,} COP):\n\n{link_result['payment_link']}\n\n"
                        f"Puedes pagar con tarjeta, PSE, Efecty o Nequi."
                    )
                else:
                    respuesta_extra = (
                        f"Listo {result['name']} 💜 Tu cuenta está al día, así que este "
                        f"link es para que adelantes tu próxima cuota (${monto:,} COP):\n\n"
                        f"{link_result['payment_link']}\n\n"
                        f"Puedes pagar con tarjeta, PSE, Efecty o Nequi."
                    )
            else:
                respuesta_extra = (
                    "Tuve un problema generando tu link de pago en este momento. "
                    "Inténtalo de nuevo en un rato o escríbenos a sac@mutuo.la y te ayudamos."
                )
                logger.error(f"[ACTION PAGO_ANTICIPADO FAILED] {telefono} → {link_result.get('error')}")

    elif action_type == "REENVIAR_RECIBO":
        action_data["phone"] = action_data.get("phone", telefono)
        result = await reenviar_recibo(action_data["phone"])
        if result.get("found") is False:
            respuesta_extra = (
                "No encontré una afiliación asociada a este número. "
                "Si pagaste con otro número o cédula, pásame tu número de cédula y la reviso."
            )
        elif result.get("sent"):
            respuesta_extra = (
                f"Listo {result.get('name', '')} 💜 Te acabo de reenviar tu recibo de caja "
                f"por aquí mismo. Revisa el mensaje con el detalle de tu pago."
            )
        elif result.get("reason") == "sin_pago":
            respuesta_extra = (
                "Todavía no veo el pago reflejado en el sistema. Los pagos pueden tardar "
                "unos minutos en confirmarse. Apenas se registre, te llega el recibo "
                "automáticamente. Si ya pasó un buen rato, cuéntame con qué medio pagaste y lo reviso."
            )
        elif result.get("reason") == "sin_telefono":
            respuesta_extra = (
                "Encontré tu cuenta pero no tengo un número de WhatsApp registrado para enviarte el recibo. "
                "Escríbenos a sac@mutuo.la y con gusto te lo hacemos llegar."
            )
        else:
            respuesta_extra = (
                "No pude reenviar el recibo en este momento. Inténtalo de nuevo en un rato "
                "o escríbenos a sac@mutuo.la y te ayudamos."
            )
            logger.error(f"[ACTION REENVIAR_RECIBO FAILED] {telefono} → {result.get('error')}")

    elif action_type == "CREAR_TICKET_CANCELACION":
        action_data["phone"] = action_data.get("phone", telefono)
        result = await crear_ticket_cancelacion(
            action_data["phone"],
            action_data.get("reason", "Solicitud del cliente"),
            int(action_data.get("retention_attempts", 0) or 0),
            str(action_data.get("cedula", "") or ""),
        )
        if result.get("success"):
            respuesta_extra = (
                f"Listo, registré tu solicitud de cancelación.\n\n"
                f"Radicado: *{result['radicado']}*\n\n"
                f"Desde ya no se generarán nuevos cobros. Nuestro equipo la tramita en máximo "
                f"24 horas hábiles y queda en firme. Puedes consultar el estado escribiéndome "
                f"tu radicado cuando quieras. Guárdalo para cualquier seguimiento."
            )
            logger.info(f"[ACTION CANCELACION_SOLICITADA] {telefono} → {result.get('radicado')}")
        else:
            respuesta_extra = "No pude registrar la cancelación. Por favor contacta a sac@mutuo.la"

    elif action_type == "CONSULTAR_RADICADO":
        identificador = str(action_data.get("radicado") or action_data.get("phone") or telefono).strip()
        result = await consultar_radicado(identificador)
        if result.get("found"):
            estado_map = {
                "pendiente": "En trámite (cobros frenados, pendiente de cierre por nuestro equipo)",
                "tramitada": "Cancelación tramitada y en firme. No se generan cobros.",
                "rechazada": "No procedió la cancelación. Tu plan sigue activo.",
                "retenida": "Quedó en pausa de retención. Tu plan sigue activo.",
            }
            estado_txt = estado_map.get(result.get("status", ""), result.get("status", ""))
            notas = f"\n\nNota: {result['resolution_notes']}" if result.get("resolution_notes") else ""
            respuesta_extra = (
                f"Estado de tu radicado *{result['radicado']}*:\n\n"
                f"{estado_txt}{notas}"
            )
            logger.info(f"[ACTION CONSULTAR_RADICADO] {identificador} → {result.get('status')}")
        else:
            respuesta_extra = (
                "No encontré ningún radicado de cancelación con esos datos. "
                "Verifica el número (formato CAN-AAAAMMDD-XXXXXX) o escríbenos a sac@mutuo.la"
            )

    elif action_type == "CONSULTAR_POR_CEDULA":
        cedula = str(action_data.get("cedula", "")).strip()
        result = await consultar_cuenta_por_cedula(cedula)
        if result.get("found"):
            r = result
            if r["total_deuda"] > 0:
                meses_pend = r.get("meses_pendientes") or []
                detalle_meses = f"\nMeses pendientes: {', '.join(meses_pend)}" if meses_pend else ""
                deuda_txt = f"Deuda: ${int(r['total_deuda']):,} COP{detalle_meses}\nCuotas pendientes: {r['cuotas_pendientes']}"
            else:
                ultimo = r.get("ultimo_periodo_pagado")
                deuda_txt = (
                    f"Sin cobros vencidos. Tu última cuota registrada es la de {ultimo}."
                    if ultimo else "Sin cobros vencidos en este momento."
                )
            canal_txt = " (libranza empresarial)" if r.get("canal") == "empresarial" else ""
            respuesta_extra = (
                f"Hola {r['name']}!\n\n"
                f"Plan: {r['plan']}{canal_txt}\n"
                f"Tarifa: ${int(r['tarifa']):,}/mes\n"
                f"{deuda_txt}"
            )
            if r["total_deuda"] > 0 and r.get("canal") != "empresarial":
                link_result = await generar_link_pago(r["affiliation_id"], int(r["total_deuda"]), r["name"])
                if link_result.get("success"):
                    respuesta_extra += f"\n\nPaga aqui: {link_result['payment_link']}"
            logger.info(f"[ACTION CONSULTAR_POR_CEDULA] {cedula} → encontrado")
        else:
            respuesta_extra = None  # bot handles not-found response
            logger.info(f"[ACTION CONSULTAR_POR_CEDULA] {cedula} → no encontrado")

    elif action_type == "ACTUALIZAR_BENEFICIARIOS":
        affiliation_id = str(action_data.get("affiliation_id", "")).strip()
        beneficiarios = action_data.get("beneficiarios", [])
        motivo = action_data.get("motivo", "")
        result = await actualizar_beneficiarios(affiliation_id, beneficiarios, motivo)
        if result.get("success"):
            respuesta_extra = (
                f"Listo {result.get('name', '')}! Los beneficiarios quedaron actualizados.\n\n"
                f"Antes: {result['beneficiarios_antes']} / Ahora: {result['beneficiarios_despues']}\n\n"
                f"Si necesitas hacer otro cambio, escribeme cuando quieras."
            )
            logger.info(f"[ACTION ACTUALIZAR_BENEFICIARIOS] {affiliation_id} → {result['beneficiarios_despues']} beneficiarios")
        else:
            respuesta_extra = "No pude actualizar los beneficiarios. Intenta de nuevo o escribe a sac@mutuo.la"
            logger.error(f"[ACTION ACTUALIZAR_BENEFICIARIOS FAILED] {affiliation_id} → {result.get('error')}")

    elif action_type == "ENVIAR_IMAGEN_PLAN":
        plan_slug = str(action_data.get("plan", "")).lower().strip()
        imagen_url = PLAN_IMAGE_URLS.get(plan_slug)
        if imagen_url:
            logger.info(f"[ACTION ENVIAR_IMAGEN_PLAN] {telefono} → {plan_slug}")
        else:
            logger.warning(f"[ACTION ENVIAR_IMAGEN_PLAN] plan desconocido: {plan_slug!r}")

    elif action_type == "ENVIAR_QR_NEQUI":
        # Cliente pidio pagar por Nequi: enviamos el QR + la llave Bre-B como respaldo.
        imagen_url = NEQUI_QR_URL
        respuesta_extra = NEQUI_QR_CAPTION
        logger.info(f"[ACTION ENVIAR_QR_NEQUI] {telefono} → QR enviado")

    return texto_limpio, respuesta_extra, imagen_url


def _extract_crm_profile(user_msg: str, bot_response: str, action_result: str | None) -> dict:
    """Extrae nombre, ciudad y status del CRM basado en el contenido de la conversación."""
    perfil: dict = {}

    if action_result and "afiliacion quedo registrada" in action_result.lower():
        perfil["status"] = "convertido"
    elif action_result and ("plan:" in action_result.lower() or "deuda:" in action_result.lower()):
        perfil["status"] = "en_progreso"
    elif any(w in user_msg.lower() for w in ["quiero", "precio", "cuanto", "plan", "afiliar"]):
        perfil["status"] = "caliente"
    elif len(user_msg.split()) > 2:
        perfil["status"] = "en_progreso"

    # Extraer nombre: buscar "me llamo X" o "soy X" en el mensaje del usuario
    import re as _re
    name_match = _re.search(r'(?:me llamo|soy|mi nombre es)\s+([A-ZÁÉÍÓÚÑa-záéíóúñ]+(?:\s+[A-ZÁÉÍÓÚÑa-záéíóúñ]+)?)', user_msg, _re.IGNORECASE)
    if name_match:
        perfil["prospect_name"] = name_match.group(1).strip().title()

    # Extraer ciudad de ciudades colombianas comunes
    ciudades = ["bogota", "medellin", "cali", "barranquilla", "cartagena", "bucaramanga",
                "santa marta", "pereira", "manizales", "cucuta", "ibague", "villavicencio",
                "pasto", "monteria", "valledupar", "neiva", "armenia", "sincelejo",
                "popayan", "riohacha", "tunja", "florencia", "quibdo", "soledad",
                "soacha", "bello", "itagui", "envigado", "dosquebradas", "tulua"]
    msg_lower = user_msg.lower().replace("á", "a").replace("é", "e").replace("í", "i").replace("ó", "o").replace("ú", "u")
    for ciudad in ciudades:
        if ciudad in msg_lower:
            perfil["city"] = ciudad.title()
            break

    return perfil


# ── Red de seguridad determinista de cancelación ──────────────────────────────
# El bot (LLM) a veces NO emite [ACTION:CREAR_TICKET_CANCELACION] aunque el
# cliente pida la baja con toda claridad — y entonces la cancelación se pierde
# (caso Luis). Esta red detecta solicitudes EXPLÍCITAS por palabra clave (igual
# que el detector de exclusión) y garantiza el radicado aunque el LLM falle.
# Crea un radicado PENDIENTE (reversible): frena cobros, marca pending_cancellation
# y alerta a admins. NO desactiva la cuenta ni reemplaza la conversación de
# retención: el bot sigue respondiendo y el admin puede rechazar/retener.
_CANCEL_INTENT_RE = re.compile(
    r"solicito\s+mi\s+(retiro|cancelaci[oó]n)"
    r"|cancelaci[oó]n\s+inmediata"
    r"|(quiero|deseo|necesito|exijo|pido|solicito)\s+(cancelar|anular|darme?\s+de\s+baja|retirarme)"
    r"|darme?\s+de\s+baja"
    r"|(quiero|deseo)\s+retirarme"
    r"|cancel(en|ar|a|o)\s+(mi|la)\s+(afiliaci[oó]n|plan|membres[ií]a|suscripci[oó]n|cobertura|cuenta|servicio)"
    r"|anul(en|ar|a)\s+(mi|la)\s+(afiliaci[oó]n|plan|membres[ií]a)",
    re.IGNORECASE,
)
# Guarda contra negaciones / preguntas hipotéticas (no son una solicitud firme).
_CANCEL_NEGATION_RE = re.compile(
    r"\bno\s+(quiero|deseo|voy\s+a|pienso|me\s+quiero)\s+(cancelar|darme?\s+de\s+baja|retirar)"
    r"|c[oó]mo\s+(puedo\s+)?(cancel|darme?\s+de\s+baja)",
    re.IGNORECASE,
)


def _es_cancelacion_explicita(texto: str) -> bool:
    t = (texto or "").lower()
    if _CANCEL_NEGATION_RE.search(t):
        return False
    return bool(_CANCEL_INTENT_RE.search(t))


async def _red_seguridad_cancelacion(clean_text: str, respuesta_llm: str, telefono: str) -> None:
    """Si el cliente pidió la baja de forma explícita y el LLM NO emitió la acción,
    registramos el radicado igual (idempotente). No bloquea el flujo."""
    try:
        if "CREAR_TICKET_CANCELACION" in (respuesta_llm or ""):
            return  # el LLM ya lo manejó en esta respuesta
        if not _es_cancelacion_explicita(clean_text):
            return
        result = await crear_ticket_cancelacion(
            telefono,
            reason=f'Solicitud explícita de baja por WhatsApp: "{clean_text[:160]}"',
            retention_attempts=0,
        )
        if result.get("success") and not result.get("already_exists"):
            logger.warning(
                f"[CANCELACION RED-SEGURIDAD] {telefono} pidió baja explícita y el LLM no emitió "
                f"la acción → radicado creado automáticamente: {result.get('radicado')}"
            )
        elif not result.get("success"):
            # Normalmente: es un lead sin afiliación → nada que cancelar (no es error).
            logger.info(f"[CANCELACION RED-SEGURIDAD] {telefono} sin afiliación que cancelar: {result.get('error')}")
    except Exception as e:
        logger.warning(f"[CANCELACION RED-SEGURIDAD] error no bloqueante: {e}")


async def _process_inbound_message(msg) -> None:
    """Procesa un mensaje inbound individual. Se ejecuta en background task."""
    # Lock por teléfono: evita procesamiento concurrente del mismo lead
    lock = _get_phone_lock(msg.telefono)
    async with lock:
        try:
            # Marcar teléfono como inbound activo para que outbound no envíe más
            active_inbound_phones.add(msg.telefono)

            # Fallbacks para media sin texto — responder pidiendo que escriba por texto
            _MEDIA_FALLBACKS = {
                "[AUDIO_RECIBIDO]": (
                    "[Audio]",
                    "Disculpa, tengo los audios deshabilitados. ¿Me escribes por texto?",
                ),
                "[IMAGEN_RECIBIDA]": (
                    "[Imagen]",
                    "¡Hola! Recibí tu imagen pero aún no puedo verlas por aquí. "
                    "¿Me cuentas por texto en qué te puedo ayudar?",
                ),
                "[VIDEO_RECIBIDO]": (
                    "[Video]",
                    "¡Hola! Recibí tu video pero no puedo reproducirlo por aquí. "
                    "¿Me escribes por texto qué necesitas?",
                ),
                "[DOCUMENTO_RECIBIDO]": (
                    "[Documento]",
                    "Recibí tu documento. Para avanzar más rápido, cuéntame por texto "
                    "qué necesitas (plan, pago, información) y te guío paso a paso.",
                ),
                "[STICKER_RECIBIDO]": (
                    "[Sticker]",
                    "¡Hola! ¿En qué te puedo ayudar hoy? Cuéntame si buscas información "
                    "sobre tu plan o necesitas ayuda con tu cuenta.",
                ),
                "[UBICACION_RECIBIDA]": (
                    "[Ubicación]",
                    "Gracias por compartir tu ubicación. ¿Me cuentas en qué te puedo ayudar?",
                ),
                "[CONTACTO_RECIBIDO]": (
                    "[Contacto]",
                    "Gracias por compartir el contacto. ¿En qué te puedo ayudar?",
                ),
            }
            if msg.texto in _MEDIA_FALLBACKS:
                etiqueta, respuesta_media = _MEDIA_FALLBACKS[msg.texto]
                await proveedor.enviar_mensaje(msg.telefono, respuesta_media)
                await guardar_mensaje(msg.telefono, "user", etiqueta)
                await guardar_mensaje(msg.telefono, "assistant", respuesta_media)
                logger.info(f"[MEDIA:{etiqueta}] {msg.telefono} — respondido con fallback")
                return

            # Lead de anuncio CTWA sin texto propio: tratarlo como saludo normal
            if msg.texto.startswith("[CTWA_REFERRAL]"):
                msg.texto = "Hola, quiero más información"

            # ── Check handoff_status — silenciar bot si asesor tomó el chat ──
            _sb_url = os.getenv("SUPABASE_URL", "")
            _sb_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "") or os.getenv("SUPABASE_KEY", "") or os.getenv("SUPABASE_ANON_KEY", "")
            if _sb_url and _sb_key:
                digits_only = msg.telefono.replace("+", "").replace(" ", "")
                phone_variants = {msg.telefono, digits_only, f"+{digits_only}"}
                if digits_only.startswith("57") and len(digits_only) >= 12:
                    local = digits_only[2:]
                    phone_variants.update({local, f"57{local}"})
                phone_variants.discard("")
                or_clause = ",".join(f"phone.eq.{p}" for p in phone_variants)
                try:
                    async with httpx.AsyncClient(timeout=5) as _hc:
                        _hr = await _hc.get(
                            f"{_sb_url}/rest/v1/whatsapp_conversations?or=({or_clause})&select=handoff_status,bot_paused&limit=1",
                            headers={"Authorization": f"Bearer {_sb_key}", "apikey": _sb_key},
                        )
                        if _hr.status_code == 200 and _hr.json():
                            _hdata = _hr.json()[0]
                            _hs = _hdata.get("handoff_status", "bot")
                            _bp = _hdata.get("bot_paused", False)
                            if _hs in ("human_active", "won", "lost") or _bp:
                                # Guardar el mensaje pero NO responder — el asesor tiene el chat
                                await guardar_mensaje(msg.telefono, "user", msg.texto)
                                try:
                                    conv_id = await get_or_create_conversation(msg.telefono)
                                    if conv_id:
                                        await save_message(conv_id, "user", msg.texto)
                                except Exception:
                                    pass
                                logger.info(f"[HANDOFF={_hs} paused={_bp}] {msg.telefono} — bot silenciado")
                                return
                except Exception as _he:
                    logger.warning(f"[HANDOFF] Error verificando: {_he}")

            # ── Habeas Data: preferencias de interacción por afiliado ──
            # Si el afiliado tiene el bot apagado o sin permiso para responder a
            # mensajes entrantes (ni servicio al cliente ni soporte de cuenta),
            # guardamos el mensaje pero NO respondemos. Fail-open ante errores.
            try:
                from agent.bot_preferences import (
                    can_interact, CATEGORY_INBOUND, CATEGORY_SOPORTE, CATEGORY_SERVICIO,
                )
                _allow_inbound = await can_interact(msg.telefono, CATEGORY_INBOUND)
                _allow_servicio = await can_interact(msg.telefono, CATEGORY_SERVICIO)
                _allow_soporte = await can_interact(msg.telefono, CATEGORY_SOPORTE)
                if not _allow_inbound or not (_allow_servicio or _allow_soporte):
                    await guardar_mensaje(msg.telefono, "user", msg.texto)
                    try:
                        _conv_id = await get_or_create_conversation(msg.telefono)
                        if _conv_id:
                            await save_message(_conv_id, "user", msg.texto)
                    except Exception:
                        pass
                    logger.info(
                        f"[HABEAS] {msg.telefono} — bot silenciado por preferencias "
                        f"(inbound={_allow_inbound}, servicio={_allow_servicio}, soporte={_allow_soporte})"
                    )
                    return
            except Exception as _hp:
                logger.warning(f"[HABEAS] error verificando preferencias (fail-open): {_hp}")

            # ── Detectar solicitud de exclusión ──
            _msg_lower_excl = msg.texto.lower()
            _exclusion_kw = ["no me contacten", "no me escriban", "no quiero que me contacten",
                             "no me llamen", "no me escribas más", "eliminar mi número",
                             "excluir", "no me molesten", "borre mi número"]
            if any(kw in _msg_lower_excl for kw in _exclusion_kw):
                from agent.reactivation import mark_excluded
                await mark_excluded(msg.telefono)
                _excl_resp = "Listo, tu número ha sido excluido de nuestras comunicaciones. Disculpa la molestia. ¡Que estés bien! 🙌"
                await proveedor.enviar_mensaje(msg.telefono, _excl_resp)
                await guardar_mensaje(msg.telefono, "user", msg.texto)
                await guardar_mensaje(msg.telefono, "assistant", _excl_resp)
                logger.info(f"[EXCLUSION] {msg.telefono} solicitó exclusión")
                return

            # Sanitizar: remover action tags del mensaje del usuario (anti-injection)
            clean_text = re.sub(r'\[ACTION:.*?\].*?\[/ACTION\]', '', msg.texto, flags=re.DOTALL).strip()
            if not clean_text:
                clean_text = msg.texto

            # Guardar mensaje del usuario primero (antes de generar respuesta)
            await guardar_mensaje(msg.telefono, "user", clean_text)

            # Cargar historial de conversación (incluye el mensaje que acabamos de guardar).
            # 60 mensajes: una afiliación larga (enumerar beneficiarios uno por uno, datos,
            # autorizaciones, dirección) supera fácil los 20 y, si se trunca, el modelo pierde
            # el parentesco de cada beneficiario y los pone a todos como "Hijo" en el resumen.
            historial = await obtener_historial(msg.telefono, limite=60)

            # Generar respuesta con brain.py (semáforo limita concurrencia a Anthropic)
            from agent.brain import generar_respuesta
            async with _api_semaphore:
                respuesta = await generar_respuesta(
                    clean_text, historial,
                    telefono=msg.telefono,
                    lead_context=getattr(msg, "lead_context", None),
                    affiliate_context=getattr(msg, "affiliate_context", None),
                )

            # ── Parse and execute actions from the AI response ──
            respuesta_limpia, respuesta_extra, imagen_url = await _ejecutar_acciones(respuesta, msg.telefono)

            # Red de seguridad: si pidió la baja explícitamente y el LLM no emitió
            # la acción, registramos el radicado igual (idempotente, no bloqueante).
            await _red_seguridad_cancelacion(clean_text, respuesta, msg.telefono)

            # Guardar respuesta LIMPIA (sin action tags) para no inflar historial futuro
            await guardar_mensaje(msg.telefono, "assistant", respuesta_limpia)

            # Sincronizar mensajes con CRM de Supabase (whatsapp_conversations + whatsapp_messages)
            try:
                crm_perfil = _extract_crm_profile(clean_text, respuesta_limpia, respuesta_extra)
                # Sync heredado (vendu)
                await crm.sync_message(msg.telefono, "user", msg.texto, crm_perfil)
                await crm.sync_message(msg.telefono, "assistant", respuesta_limpia, crm_perfil)
                # Sync nuevo CRM nativo
                conv_id = await sync_inbound(
                    phone=msg.telefono,
                    content=clean_text,
                    prospect_name=crm_perfil.get("prospect_name"),
                    city=crm_perfil.get("city"),
                    department=crm_perfil.get("department"),
                    interest=crm_perfil.get("interest"),
                    disc_profile=crm_perfil.get("disc_profile"),
                    current_operator=crm_perfil.get("current_operator"),
                )
                await sync_outbound(msg.telefono, respuesta_limpia, conv_id=conv_id)
                if respuesta_extra:
                    await sync_outbound(msg.telefono, respuesta_extra, conv_id=conv_id)
            except Exception as crm_err:
                logger.warning(f"CRM sync error (non-blocking): {crm_err}")

            # Enviar respuesta por WhatsApp (texto limpio sin action tags)
            await proveedor.enviar_mensaje(msg.telefono, respuesta_limpia)

            # Si la acción retornó una imagen (ej: imagen del plan recomendado), enviarla
            if imagen_url:
                try:
                    ok = await proveedor.enviar_imagen(msg.telefono, imagen_url)
                    if not ok:
                        logger.warning(f"[IMG] proveedor no envio imagen a {msg.telefono}: {imagen_url}")
                except Exception as img_err:
                    logger.warning(f"[IMG] error enviando imagen a {msg.telefono}: {img_err}")

            # Si hubo acción con resultado (link de pago, radicado, etc.), enviar mensaje adicional
            if respuesta_extra:
                await proveedor.enviar_mensaje(msg.telefono, respuesta_extra)

            logger.info(f"[OUT] {msg.telefono}: {respuesta_limpia}")

            # Persistir/nutrir el borrador de afiliación en la base (fuente de
            # verdad). En segundo plano para no sumar latencia a la respuesta: el
            # sistema —no la memoria del LLM— recuerda los datos del prospecto.
            try:
                from agent.draft_affiliation import actualizar_borrador
                asyncio.create_task(
                    actualizar_borrador(
                        msg.telefono,
                        historial + [{"role": "assistant", "content": respuesta_limpia}],
                    )
                )
            except Exception as _draft_err:
                logger.debug(f"[BORRADOR] no se pudo programar: {_draft_err}")
        except Exception as e:
            logger.error(f"Error procesando mensaje de {msg.telefono}: {e}", exc_info=True)
            try:
                from agent.brain import _fallback_recoger_datos
                fallback = _fallback_recoger_datos(msg.texto or "", msg.telefono)
                await proveedor.enviar_mensaje(msg.telefono, fallback)
                logger.info(f"[FALLBACK] {msg.telefono}: {fallback}")
            except Exception as fb_err:
                logger.error(f"Error enviando fallback a {msg.telefono}: {fb_err}")


@app.post("/webhook")
async def webhook_handler(request: Request, background_tasks: BackgroundTasks):
    """
    Recibe mensajes de WhatsApp.
    Responde 200 OK inmediatamente y procesa en background para evitar
    que el proveedor reintente por timeout (lo que causa duplicados).
    """
    try:
        mensajes = await proveedor.parsear_webhook(request)

        global _last_inbound_at, _inbound_alerted
        for msg in mensajes:
            if msg.es_propio or not msg.texto:
                continue

            # Señal de vida para el watchdog: recibimos un inbound real de un cliente.
            # Se marca ANTES de dedup para reflejar que Whapi sigue entregando webhooks.
            _last_inbound_at = datetime.now(timezone.utc)
            _inbound_alerted = False

            # Deduplicación: ignorar mensaje ya procesado (retry del proveedor)
            if await _is_duplicate_message(msg.mensaje_id):
                logger.info(f"[DEDUP] Ignorado mensaje duplicado {msg.mensaje_id} de {msg.telefono}")
                continue

            logger.info(f"[IN] {msg.telefono}: {msg.texto}")

            # Procesar en background — el webhook retorna 200 OK inmediatamente
            background_tasks.add_task(_process_inbound_message, msg)

        return {"status": "ok"}

    except Exception as e:
        # Traceback completo para poder diagnosticar (antes solo se veía el str del error).
        logger.error(f"Error en webhook: {e}", exc_info=True)
        # Siempre retornamos 200 OK aun en errores para evitar retries del proveedor
        return {"status": "error", "detail": str(e)}


def _mapear_estado_crm(estado_agente: str) -> str:
    """Mapea estados de Origen AI a estados de whatsapp_conversations."""
    mapa = {
        "PROSPECTO": "nuevo",
        "CONTACTADO": "en_progreso",
        "CALIFICADO": "en_progreso",
        "OFERTA_ENVIADA": "caliente",
        "OBJECION_ACTIVA": "caliente",
        "NEGOCIANDO": "caliente",
        "CERRADO_GANADO": "convertido",
        "CERRADO_PERDIDO": "descartado",
    }
    return mapa.get(estado_agente, "en_progreso")


@app.get("/leads")
async def listar_leads(estado: str = None):
    leads = await obtener_leads(estado)
    return {"total": len(leads), "leads": leads}


# ── Endpoint para registrar mensajes salientes manuales en la memoria del bot ──
@app.post("/memory/outbound")
async def registrar_mensaje_saliente(request: Request):
    """
    Llamado por el webhook de Whapi cuando llega un echo from_me=true.
    Guarda el mensaje en la memoria del bot (RAM + Supabase) como 'assistant'
    para que Natalia mantenga el contexto de lo que se envió manualmente.
    Body: { phone: str, text: str }
    """
    try:
        body = await request.json()
        phone = body.get("phone", "").strip()
        text = body.get("text", "").strip()
        if not phone or not text:
            return {"ok": False, "detail": "phone y text son obligatorios"}
        phone_clean = phone.replace("+", "").replace(" ", "").replace("-", "")
        if len(phone_clean) == 10:
            phone_clean = "57" + phone_clean
        await guardar_mensaje(phone_clean, "assistant", text)
        logger.info(f"[MEMORY:OUTBOUND] {phone_clean}: {text[:80]}")
        return {"ok": True}
    except Exception as e:
        logger.warning(f"[MEMORY:OUTBOUND] Error: {e}")
        return {"ok": False, "detail": str(e)}


@app.get("/kpis")
async def ver_kpis():
    """Dashboard de KPIs del agente."""
    return kpi_tracker.get_kpis()


@app.get("/sesiones")
async def ver_sesiones():
    """Ver sesiones activas."""
    return {
        "total": len(sesiones),
        "sesiones": {
            tel: {
                "estado": s.state_machine.estado,
                "fase": s.state_machine.fase_conversacional(),
                "temperatura": s.profile.temperatura,
                "turnos": s.turnos,
                "paquete": s.recommender.paquete_actual,
                "nombre": s.profile.nombre,
            }
            for tel, s in sesiones.items()
        },
    }


# ── Endpoints de Campañas Outbound ──────────────────
@app.post("/campaign/start")
async def iniciar_campana(request: Request):
    """
    Inicia la campaña outbound.
    Body opcional: {"database_id": "uuid-de-la-base"}
    Si no se pasa database_id, toma TODOS los leads asignados al bot.
    """
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass

    database_id = body.get("database_id")
    result = campaign.start(database_id=database_id)

    # Registrar phones contactados para contexto inbound
    # (se actualizará conforme se envíen)
    original_send = campaign._send_to_lead

    async def tracked_send(lead):
        phone = campaign._normalize_phone(lead.get("phone", ""))
        if phone:
            outbound_phones.add(phone)
        return await original_send(lead)

    campaign._send_to_lead = tracked_send

    logger.info(f"[CAMPAIGN] Iniciada — db={database_id}")
    return result


@app.post("/campaign/stop")
async def detener_campana():
    """Detiene la campaña outbound."""
    result = campaign.stop()
    logger.info("[CAMPAIGN] Detenida manualmente")
    return result


@app.get("/campaign/status")
async def estado_campana():
    """Retorna el estado actual de la campaña outbound."""
    return campaign.get_status()


@app.get("/costs")
async def ver_costos():
    """Dashboard de costos de API."""
    return cost_tracker.get_stats()


# ── Endpoint para envio manual desde el CRM ─────────────────────────
@app.post("/messages/send")
async def enviar_mensaje_manual(request: Request):
    """
    Envia un mensaje de WhatsApp manualmente desde el panel CRM.
    Body: { phone, message, prospect_name?, city?, department?, current_operator?, interest?, disc_profile? }
    """
    body = await request.json()
    phone = body.get("phone", "").strip()
    message = body.get("message", "").strip()

    if not phone or not message:
        raise HTTPException(status_code=400, detail="phone y message son obligatorios")

    # Normalizar teléfono
    phone_clean = phone.replace("+", "").replace(" ", "").replace("-", "")
    if len(phone_clean) == 10:
        phone_clean = "57" + phone_clean

    # Enviar via Whapi
    enviado = await proveedor.enviar_mensaje(phone_clean, message)
    if not enviado:
        raise HTTPException(status_code=502, detail="Error al enviar mensaje via WhatsApp")

    # Guardar en memoria local
    await guardar_mensaje(phone_clean, "admin", message)

    # Sincronizar con Supabase CRM
    crm_perfil = {}
    for key in ["prospect_name", "city", "department", "current_operator", "interest", "disc_profile"]:
        val = body.get(key)
        if val:
            crm_perfil[key] = val

    await crm.sync_message(phone_clean, "admin", message, crm_perfil)

    logger.info(f"[MANUAL] {phone_clean}: {message[:80]}")
    return {"ok": True, "phone": phone_clean}


# ── Import masivo desde Whapi ─────────────────────────────────────────────────

IMPORT_SECRET = os.getenv("IMPORT_SECRET", "mutuo-import-2024")

_import_status: dict = {"running": False, "created": 0, "skipped_exists": 0,
                        "skipped_incomplete": 0, "errors": 0, "log": [], "done": False}

_EXTRACTION_PROMPT = """Analiza esta conversación de WhatsApp entre un bot de ventas de Mutuo (Club de Bienestar Familiar) y un cliente.

Clasifica la conversación y extrae todos los datos disponibles en JSON.

Campos a extraer (pon null si no está disponible):
- estado: "completa" | "borrador" | "ninguna"
  * "completa" = dio nombre, cédula, email Y eligió plan
  * "borrador" = mostró interés real y dio al menos nombre O teléfono, pero faltan datos clave
  * "ninguna" = solo curiosidad, preguntas generales, sin intención de compra, o conversación muy corta
- first_name, last_name
- document_type: "CC" o "CE" (null si no lo dio)
- document_number: (null si no lo dio)
- email: (null si no lo dio)
- phone: (tomar del chat)
- birth_date: fecha de nacimiento del TITULAR en formato DD/MM/AAAA (null si no la dio)
- address, municipality, department
- plan: "esencial", "plus" o "total" (null si no eligió)
- beneficiarios: array de {{primerNombre, apellido, parentesco, fechaNac}} ([] si ninguno)
- mascotas: array de {{nombre, tipo, raza, edad_numero}} ([] si ninguna)
- notas: texto breve con qué datos faltan o por qué quedó incompleto

Responde SOLO con JSON, sin texto adicional.

CONVERSACIÓN:
{conversation}"""


def _norm_phone(phone: str) -> str:
    raw = phone.split("@")[0].replace("+", "").replace(" ", "").replace("-", "")
    local = raw[-10:] if len(raw) >= 10 else raw
    return f"57{local}"


async def _affiliation_exists_check(http: httpx.AsyncClient, phone: str) -> bool:
    raw = phone.split("@")[0].replace("+", "").replace(" ", "").replace("-", "")
    local = raw[-10:] if len(raw) >= 10 else raw
    variants = list(set([f"+57{local}", local, f"57{local}", raw]))
    or_filter = ",".join(f"phone.eq.{p}" for p in variants)
    sb_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "") or os.getenv("SUPABASE_KEY", "") or os.getenv("SUPABASE_ANON_KEY", "")
    sb_url = os.getenv("SUPABASE_URL", "")
    r = await http.get(
        f"{sb_url}/rest/v1/b2c_affiliations?or=({or_filter})&select=id&limit=1",
        headers={"Authorization": f"Bearer {sb_key}", "apikey": sb_key, "Prefer": ""},
        timeout=10,
    )
    return r.status_code == 200 and bool(r.json())


async def _run_import():
    from datetime import datetime
    global _import_status
    _import_status = {"running": True, "created": 0, "skipped_exists": 0,
                      "skipped_incomplete": 0, "errors": 0, "log": [], "done": False}

    whapi_token = os.getenv("WHAPI_TOKEN", "") or os.getenv("WHAPI_API_KEY", "")
    sb_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "") or os.getenv("SUPABASE_KEY", "") or os.getenv("SUPABASE_ANON_KEY", "")
    sb_url = os.getenv("SUPABASE_URL", "")
    anthropic_key = os.getenv("ANTHROPIC_API_KEY", "")

    headers_whapi = {"Authorization": f"Bearer {whapi_token}", "Content-Type": "application/json"}
    headers_sb = {"Authorization": f"Bearer {sb_key}", "apikey": sb_key,
                  "Content-Type": "application/json", "Prefer": "return=representation"}

    from anthropic import AsyncAnthropic
    ai = AsyncAnthropic(api_key=anthropic_key)

    def _log(msg):
        logger.info(f"[IMPORT] {msg}")
        _import_status["log"].append(msg)
        if len(_import_status["log"]) > 500:
            _import_status["log"] = _import_status["log"][-500:]

    try:
        async with httpx.AsyncClient(timeout=30) as http:
            # 1. Obtener todos los chats
            chats = []
            offset = 0
            while True:
                r = await http.get("https://gate.whapi.cloud/chats",
                                   headers=headers_whapi,
                                   params={"count": 100, "offset": offset})
                if r.status_code != 200:
                    _log(f"Error Whapi /chats {r.status_code}")
                    break
                batch = [c for c in r.json().get("chats", [])
                         if not c.get("is_group") and not str(c.get("id", "")).endswith("@g.us")]
                chats.extend(batch)
                if len(r.json().get("chats", [])) < 100:
                    break
                offset += 100

            _log(f"Total chats individuales: {len(chats)}")

            for i, chat in enumerate(chats):
                chat_id = chat.get("id", "")
                name = chat.get("name", chat_id)

                # Mensajes
                r = await http.get(f"https://gate.whapi.cloud/messages/list/{chat_id}",
                                   headers=headers_whapi, params={"count": 150})
                if r.status_code != 200:
                    _import_status["skipped_incomplete"] += 1
                    continue
                messages = r.json().get("messages", [])
                if len(messages) < 2:
                    _import_status["skipped_incomplete"] += 1
                    continue

                # ── SIEMPRE importar historial al CRM ──────────────────────────
                phone_norm = _norm_phone(chat_id)
                conv_id = await get_or_create_conversation(phone_norm, chat.get("name"))
                if conv_id:
                    for m in reversed(messages):
                        role = "assistant" if m.get("from_me") else "user"
                        text = ((m.get("text") or {}).get("body") or m.get("body")
                                or m.get("caption") or f"[{m.get('type','media')}]")
                        ts_unix = m.get("timestamp")
                        ts = datetime.fromtimestamp(ts_unix, tz=timezone.utc) if ts_unix else None
                        await save_message(conv_id, role, text, ts)
                    latest = messages[0] if messages else None
                    if latest and latest.get("timestamp"):
                        await update_conversation(conv_id,
                            last_message_at=datetime.fromtimestamp(latest["timestamp"], tz=timezone.utc).isoformat(),
                            prospect_name=chat.get("name") or phone_norm,
                        )
                    _log(f"[{i+1}/{len(chats)}] {name} — 💬 {len(messages)} mensajes → CRM")

                # ── Verificar si ya tiene afiliación (omitir análisis IA) ──────
                if await _affiliation_exists_check(http, chat_id):
                    _import_status["skipped_exists"] += 1
                    continue

                # Construir texto de conversación para análisis IA
                lines = []
                for m in reversed(messages):
                    sender = "CLIENTE" if not m.get("from_me") else "BOT"
                    text = ((m.get("text") or {}).get("body") or m.get("body")
                            or m.get("caption") or f"[{m.get('type','media')}]")
                    lines.append(f"[{sender}] {text}")
                conversation = "\n".join(lines)

                # Analizar con IA
                try:
                    msg = await ai.messages.create(
                        model="claude-haiku-4-5-20251001",
                        max_tokens=1024,
                        messages=[{"role": "user",
                                   "content": _EXTRACTION_PROMPT.format(conversation=conversation[-6000:])}]
                    )
                    text = msg.content[0].text.strip()
                    if text.startswith("```"):
                        text = text.split("```")[1]
                        if text.startswith("json"):
                            text = text[4:]
                    datos = json.loads(text)
                except Exception as e:
                    _log(f"[{i+1}] {name} — error IA: {e}")
                    _import_status["skipped_incomplete"] += 1
                    continue

                estado = datos.get("estado", "ninguna")
                if estado == "ninguna":
                    _import_status["skipped_incomplete"] += 1
                    continue

                datos["phone"] = _norm_phone(chat_id)
                es_borrador = estado == "borrador"

                _log(f"[{i+1}/{len(chats)}] {name} — {'BORRADOR' if es_borrador else 'COMPLETA'}: {datos.get('first_name')} {datos.get('last_name')} / {datos.get('plan')} / doc={datos.get('document_number')}")

                if es_borrador:
                    # Insertar directo como borrador (in_progress) sin pasar por crear_afiliacion
                    # que requiere campos obligatorios
                    phone_norm = datos["phone"]
                    plan_key = (datos.get("plan") or "esencial").lower()
                    plan_name = {"esencial": "Familia Esencial", "plus": "Familia Plus", "total": "Familia Total"}.get(plan_key, "Familia Esencial")
                    birth_iso = _parse_birth_date(datos.get("birth_date") or datos.get("fecha_nacimiento"))
                    age_value = _calc_age(birth_iso) if birth_iso else None
                    draft_payload = {
                        "first_name": datos.get("first_name") or "",
                        "last_name": datos.get("last_name") or "",
                        "document_type": datos.get("document_type") or "CC",
                        "document_number": datos.get("document_number") or "",
                        "email": datos.get("email") or "",
                        "phone": phone_norm,
                        "country_code": "+57",
                        "birth_date": birth_iso,
                        "age": age_value,
                        "address": datos.get("address") or "",
                        "municipality": datos.get("municipality") or "",
                        "department": datos.get("department") or "",
                        "selected_plan": plan_name,
                        "beneficiarios": datos.get("beneficiarios") or [],
                        "has_pet": bool(datos.get("mascotas")),
                        "status": "in_progress",
                        "is_active": False,
                        "payment_status": "pending",
                        "current_step": 1,
                        "completed_steps": [],
                        "session_id": f"wa-draft-{datetime.now().strftime('%Y%m%d%H%M%S')}-{phone_norm}",
                        "consentimientos": {},
                        "pending_tasks": [{"tipo": "completar_datos", "notas": datos.get("notas", "Datos incompletos — importado desde Whapi")}],
                    }
                    r = await http.post(f"{sb_url}/rest/v1/b2c_affiliations", headers=headers_sb, json=draft_payload, timeout=15)
                    if r.status_code in (200, 201):
                        result_data = r.json()
                        aff_id = result_data[0]["id"] if isinstance(result_data, list) else result_data.get("id")
                        _log(f"  📝 BORRADOR ID={aff_id} — faltan: {datos.get('notas','')}")
                        _import_status["created"] += 1
                        try:
                            await http.post(f"{sb_url}/rest/v1/affiliation_audit_log", headers=headers_sb, json={
                                "affiliation_id": aff_id, "event_type": "draft_imported",
                                "event_category": "import",
                                "description": f"Borrador importado desde Whapi. Datos faltantes: {datos.get('notas','')}",
                                "changed_by_email": "whapi_bulk_import", "changed_by_type": "system",
                                "metadata": {"phone": phone_norm, "chat_id": chat_id, "notas": datos.get("notas")},
                            }, timeout=5)
                        except Exception:
                            pass
                    else:
                        _log(f"  ❌ ERROR borrador: {r.text[:200]}")
                        _import_status["errors"] += 1
                else:
                    result = await crear_afiliacion(datos)
                    if result.get("success"):
                        _log(f"  ✅ CREADA ID={result['affiliation_id']}")
                        _import_status["created"] += 1
                        try:
                            await http.post(f"{sb_url}/rest/v1/affiliation_audit_log", headers=headers_sb, json={
                                "affiliation_id": result["affiliation_id"], "event_type": "affiliation_imported",
                                "event_category": "import",
                                "description": "Afiliación importada retroactivamente desde historial de Whapi",
                                "changed_by_email": "whapi_bulk_import", "changed_by_type": "system",
                                "metadata": {"phone": datos["phone"], "chat_id": chat_id},
                            }, timeout=5)
                        except Exception:
                            pass
                    else:
                        _log(f"  ❌ ERROR: {result.get('error', '')[:200]}")
                        _import_status["errors"] += 1

                await asyncio.sleep(0.3)

    except Exception as e:
        _log(f"Error general: {e}")
        logger.error(f"[IMPORT] Error: {e}", exc_info=True)
    finally:
        _import_status["running"] = False
        _import_status["done"] = True
        _log(f"FIN — creadas={_import_status['created']} ya_existían={_import_status['skipped_exists']} sin_afiliación={_import_status['skipped_incomplete']} errores={_import_status['errors']}")


@app.post("/admin/import-chats")
async def trigger_import(request: Request, background_tasks: BackgroundTasks):
    secret = request.headers.get("x-import-secret", "")
    if secret != IMPORT_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")
    if _import_status.get("running"):
        return {"ok": False, "detail": "Import ya en curso"}
    background_tasks.add_task(_run_import)
    return {"ok": True, "detail": "Import iniciado en background. Consulta /admin/import-status"}


@app.get("/admin/import-status")
async def import_status(request: Request):
    secret = request.headers.get("x-import-secret", "")
    if secret != IMPORT_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")
    return _import_status


# ── Whapi — Health monitor y sincronización de historial ─────────────

async def whapi_health_monitor():
    """Chequea Whapi cada 5 min y sincroniza historial al reconectarse."""
    from agent.whapi_sync import check_whapi_health, sync_all_history
    was_disconnected = False

    while True:
        try:
            result = await check_whapi_health()
            whapi_status.update(result)
            whapi_status["last_check"] = datetime.now(timezone.utc).isoformat()

            if not result.get("healthy"):
                if not was_disconnected:
                    logger.warning(f"[WHAPI-HEALTH] ⚠️ DESCONECTADO: {result.get('message')}")
                    was_disconnected = True
                    from agent.alerting import send_alert
                    await send_alert(
                        proveedor,
                        "🚫 Bot Mutuo: WhatsApp desconectado",
                        f"Whapi reporta: {result.get('message')}\n\n"
                        f"Mientras esté desconectado, el bot no recibe ni envía mensajes. "
                        f"Reconecta el canal en Whapi (re-escanear QR si aplica)."
                    )
            else:
                if was_disconnected:
                    logger.info("[WHAPI-HEALTH] ✅ RECONECTADO — sincronizando historial...")
                    was_disconnected = False
                    asyncio.create_task(sync_all_history())

        except Exception as e:
            logger.error(f"[WHAPI-HEALTH] Error monitor: {e}")

        await asyncio.sleep(300)


async def inbound_watchdog():
    """
    Vigila que sigamos recibiendo mensajes entrantes. Si en horario hábil
    (7am-7pm COL) pasan más de INBOUND_WATCHDOG_MINUTES sin un solo inbound,
    alerta al admin por WhatsApp.

    Cubre el punto ciego del monitor de /health: Whapi puede reportarse como
    'conectado' (auth) y aun así dejar de entregar webhooks al edge function/bot.
    En ese escenario no llega ningún mensaje pero el envío saliente sigue OK,
    por lo que la alerta SÍ llega al admin.
    """
    global _inbound_alerted
    from datetime import timedelta
    col_tz = timezone(timedelta(hours=-5))

    # Espera inicial para no alertar durante arranque/despliegue
    await asyncio.sleep(300)

    while True:
        try:
            now_utc = datetime.now(timezone.utc)
            now_col = now_utc.astimezone(col_tz)
            mins_since = (now_utc - _last_inbound_at).total_seconds() / 60.0
            in_business_hours = 7 <= now_col.hour < 19

            if mins_since < INBOUND_WATCHDOG_MINUTES:
                # Hay actividad reciente: re-armar la alerta para el próximo episodio
                if _inbound_alerted:
                    _inbound_alerted = False
                    logger.info("[INBOUND-WATCHDOG] ✅ Mensajes entrantes reanudados.")
            elif in_business_hours and not _inbound_alerted:
                _inbound_alerted = True
                logger.error(
                    f"[INBOUND-WATCHDOG] ⚠️ {mins_since:.0f} min sin mensajes entrantes "
                    f"(umbral {INBOUND_WATCHDOG_MINUTES} min). Whapi puede estar conectado "
                    f"pero sin entregar webhooks."
                )
                from agent.alerting import send_alert
                await send_alert(
                    proveedor,
                    "⚠️ Bot Mutuo: sin mensajes entrantes",
                    f"El bot lleva {mins_since:.0f} min sin recibir mensajes en horario hábil.\n\n"
                    f"Whapi puede figurar conectado pero sin entregar webhooks, o estar "
                    f"desconectado. Revisa el webhook del canal en Whapi (Settings → Webhooks, "
                    f"evento messages) y los logs del edge function whapi-inbound-webhook."
                )
        except Exception as e:
            logger.error(f"[INBOUND-WATCHDOG] Error: {e}")

        await asyncio.sleep(60)


@app.get("/inbound/status")
async def inbound_status():
    """Estado del watchdog de inbound — para monitoreo externo (UptimeRobot, etc.)."""
    now_utc = datetime.now(timezone.utc)
    mins_since = (now_utc - _last_inbound_at).total_seconds() / 60.0
    return {
        "last_inbound_at": _last_inbound_at.isoformat(),
        "minutes_since_last_inbound": round(mins_since, 1),
        "threshold_minutes": INBOUND_WATCHDOG_MINUTES,
        "alerted": _inbound_alerted,
    }


@app.post("/alert/test")
async def alert_test(request: Request):
    """Dispara una alerta de prueba por TODOS los canales (WhatsApp + Email + SMS).
    Requiere header x-import-secret. Útil para confirmar que las alertas llegan
    antes de que ocurra una caída real."""
    if request.headers.get("x-import-secret", "") != IMPORT_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")
    from agent.alerting import send_alert
    await send_alert(
        proveedor,
        "✅ Prueba de alertas Mutuo",
        "Esto es una prueba manual. Si recibes este mensaje, el canal funciona.",
    )
    return {"status": "sent", "channels": ["whatsapp", "email", "sms"]}


@app.get("/whapi/health")
async def whapi_health():
    """Verifica el estado de la conexión de Whapi (en tiempo real)."""
    from agent.whapi_sync import check_whapi_health
    result = await check_whapi_health()
    whapi_status.update(result)
    whapi_status["last_check"] = datetime.now(timezone.utc).isoformat()
    return result


@app.get("/whapi/status")
async def whapi_status_cached():
    """Retorna el último estado cacheado de Whapi (actualizado cada 5 min)."""
    return whapi_status


@app.get("/whapi/sync-history")
async def whapi_sync_history_get():
    """Sincroniza TODOS los chats de Whapi con Supabase (espera resultado)."""
    from agent.whapi_sync import sync_all_history
    stats = await sync_all_history()
    return stats


@app.post("/whapi/sync-history")
async def whapi_sync_history_post(background_tasks: BackgroundTasks):
    """Inicia sincronización en background (no bloquea)."""
    from agent.whapi_sync import sync_all_history

    async def _run():
        stats = await sync_all_history()
        logger.info(f"[WHAPI-SYNC] Completado: {stats}")

    background_tasks.add_task(_run)
    return {"status": "started", "message": "Sincronización iniciada. Tarda varios minutos según la cantidad de chats."}


# ═══════════════════════════════════════════════════════════════════════════════
# FACEBOOK MESSENGER — Bot para campañas de pauta (Click-to-Messenger)
# Comparte el mismo servidor que WhatsApp. Rutas: /webhook/messenger
# Variables requeridas: MESSENGER_PAGE_ACCESS_TOKEN, MESSENGER_VERIFY_TOKEN
# ═══════════════════════════════════════════════════════════════════════════════

# Instancia del proveedor Messenger (solo activa si el token está configurado)
_messenger_proveedor = None
_messenger_sesiones: dict[str, OrigenIA] = {}
_messenger_ad_refs: dict[str, dict] = {}
_messenger_locks: dict[str, asyncio.Lock] = {}
_messenger_processed: OrderedDict[str, bool] = OrderedDict()
_messenger_dedup_lock = asyncio.Lock()

_MESSENGER_MEDIA_FALLBACKS = {
    "[AUDIO_RECIBIDO]":    "Disculpa, tengo los audios deshabilitados. ¿Me escribes por texto?",
    "[IMAGEN_RECIBIDA]":   "¡Hola! Recibí tu imagen pero aún no puedo verlas. ¿Me cuentas por texto en qué te puedo ayudar?",
    "[VIDEO_RECIBIDO]":    "¡Hola! Recibí tu video pero no puedo reproducirlo. ¿Me escribes por texto qué necesitas?",
    "[DOCUMENTO_RECIBIDO]":"Recibí tu documento. Cuéntame por texto qué necesitas y te guío.",
    "[ARCHIVO_RECIBIDO]":  "Recibí un archivo. ¿Me cuentas por texto en qué te puedo ayudar?",
}


def _get_messenger_proveedor():
    global _messenger_proveedor
    if _messenger_proveedor is None:
        from agent.providers.messenger import ProveedorMessenger
        _messenger_proveedor = ProveedorMessenger()
    return _messenger_proveedor


async def _messenger_is_duplicate(mid: str) -> bool:
    if not mid:
        return False
    async with _messenger_dedup_lock:
        if mid in _messenger_processed:
            return True
        _messenger_processed[mid] = True
        if len(_messenger_processed) > _MAX_PROCESSED_CACHE:
            _messenger_processed.popitem(last=False)
        return False


def _get_messenger_lock(psid: str) -> asyncio.Lock:
    lock = _messenger_locks.get(psid)
    if lock is None:
        _prune_locks(_messenger_locks, _MAX_PHONE_LOCKS)
        lock = _messenger_locks.setdefault(psid, asyncio.Lock())
    return lock


async def _messenger_load_history(psid: str) -> tuple[list[dict], dict]:
    """Carga historial de messenger_conversations desde Supabase."""
    sb_url = os.getenv("SUPABASE_URL", "")
    sb_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "") or os.getenv("SUPABASE_KEY", "") or os.getenv("SUPABASE_ANON_KEY", "")
    if not sb_url or not sb_key:
        return [], {}
    headers = {"Authorization": f"Bearer {sb_key}", "apikey": sb_key}
    historial: list[dict] = []
    perfil: dict = {}
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(
                f"{sb_url}/rest/v1/messenger_conversations?psid=eq.{psid}"
                f"&select=id,prospect_name,city,interest,disc_profile,status&limit=1",
                headers=headers,
            )
            if r.status_code == 200 and r.json():
                conv = r.json()[0]
                perfil = {
                    "nombre": conv.get("prospect_name", ""),
                    "ciudad": conv.get("city", ""),
                    "interest": conv.get("interest", ""),
                }
                r2 = await c.get(
                    f"{sb_url}/rest/v1/messenger_messages?conversation_id=eq.{conv['id']}"
                    f"&select=role,content&order=created_at.desc&limit=50",
                    headers=headers,
                )
                if r2.status_code == 200:
                    msgs = r2.json()
                    msgs.reverse()
                    for m in msgs:
                        if m.get("role") and m.get("content"):
                            historial.append({"role": m["role"], "content": m["content"]})
    except Exception as e:
        logger.warning(f"[MESSENGER-HISTORY] {psid}: {e}")
    return historial, perfil


async def _messenger_get_session(psid: str, lead_context: dict | None = None) -> OrigenIA:
    if psid not in _messenger_sesiones:
        agente = OrigenIA(canal="messenger")
        historial, perfil = await _messenger_load_history(psid)
        if historial:
            agente.historial = historial
            agente.turnos = len([m for m in historial if m["role"] == "user"])
        if perfil.get("nombre"):
            agente.profile.nombre = perfil["nombre"].split()[0].title()
            agente.profile.nombre_completo = perfil["nombre"]
        if perfil.get("ciudad"):
            agente.profile.ciudad = perfil["ciudad"]
        agente.is_returning = len(historial) > 0

        if lead_context:
            _messenger_ad_refs[psid] = lead_context
            agente.campaign_context = (
                f"[CONTEXTO_ANUNCIO] El usuario llegó desde un anuncio de Facebook. "
                f"ref={lead_context.get('ref', '')} ad_id={lead_context.get('ad_id', '')}. "
                f"Es un prospecto de pauta interesado en protección familiar."
            )

        # Obtener nombre desde Graph API de Facebook
        try:
            mp = _get_messenger_proveedor()
            perfil_fb = await mp.obtener_perfil_usuario(psid)
            if perfil_fb.get("first_name") and not agente.profile.nombre:
                agente.profile.nombre = perfil_fb["first_name"].title()
                agente.profile.nombre_completo = perfil_fb.get("name", "").title()
        except Exception:
            pass

        _messenger_sesiones[psid] = agente
    return _messenger_sesiones[psid]


async def _messenger_sync_inbound(psid: str, content: str, prospect_name: str | None = None, city: str | None = None, ad_ref: str | None = None) -> str | None:
    sb_url = os.getenv("SUPABASE_URL", "")
    sb_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "") or os.getenv("SUPABASE_ANON_KEY", "")
    if not sb_url or not sb_key:
        return None
    headers = {"Authorization": f"Bearer {sb_key}", "apikey": sb_key, "Content-Type": "application/json", "Prefer": "return=representation"}
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{sb_url}/rest/v1/messenger_conversations?psid=eq.{psid}&select=id&limit=1", headers=headers)
            if r.status_code == 200 and r.json():
                conv_id = r.json()[0]["id"]
                upd: dict = {}
                if prospect_name:
                    upd["prospect_name"] = prospect_name
                if city:
                    upd["city"] = city
                if upd:
                    await c.patch(f"{sb_url}/rest/v1/messenger_conversations?id=eq.{conv_id}", headers=headers, json=upd)
            else:
                data: dict = {"psid": psid, "status": "en_progreso", "handoff_status": "bot", "channel": "messenger"}
                if prospect_name:
                    data["prospect_name"] = prospect_name
                if city:
                    data["city"] = city
                if ad_ref:
                    data["ad_ref"] = ad_ref
                rp = await c.post(f"{sb_url}/rest/v1/messenger_conversations", headers=headers, json=data)
                conv_id = rp.json()[0]["id"] if rp.status_code in (200, 201) and rp.json() else None
            if conv_id:
                await c.post(f"{sb_url}/rest/v1/messenger_messages", headers=headers, json={"conversation_id": conv_id, "role": "user", "content": content})
            return conv_id
    except Exception as e:
        logger.warning(f"[MESSENGER-CRM] sync_inbound: {e}")
    return None


async def _messenger_sync_outbound(psid: str, content: str, conv_id: str | None = None) -> None:
    sb_url = os.getenv("SUPABASE_URL", "")
    sb_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "") or os.getenv("SUPABASE_ANON_KEY", "")
    if not sb_url or not sb_key or not content:
        return
    headers = {"Authorization": f"Bearer {sb_key}", "apikey": sb_key, "Content-Type": "application/json", "Prefer": "return=representation"}
    try:
        if not conv_id:
            async with httpx.AsyncClient(timeout=5) as c:
                r = await c.get(f"{sb_url}/rest/v1/messenger_conversations?psid=eq.{psid}&select=id&limit=1", headers=headers)
                if r.status_code == 200 and r.json():
                    conv_id = r.json()[0]["id"]
        if conv_id:
            async with httpx.AsyncClient(timeout=5) as c:
                await c.post(f"{sb_url}/rest/v1/messenger_messages", headers=headers, json={"conversation_id": conv_id, "role": "assistant", "content": content})
    except Exception as e:
        logger.warning(f"[MESSENGER-CRM] sync_outbound: {e}")


async def _process_messenger_message(msg) -> None:
    """Procesa un mensaje de Messenger en background. Lógica paralela a WhatsApp sin interferir."""
    mp = _get_messenger_proveedor()
    lock = _get_messenger_lock(msg.telefono)
    async with lock:
        psid = msg.telefono
        try:
            # Media fallback
            if msg.texto in _MESSENGER_MEDIA_FALLBACKS:
                resp = _MESSENGER_MEDIA_FALLBACKS[msg.texto]
                await mp.enviar_mensaje(psid, resp)
                await guardar_mensaje(psid, "user", msg.texto)
                await guardar_mensaje(psid, "assistant", resp)
                return

            await mp.enviar_typing(psid)

            # Anti-injection
            clean_text = re.sub(r'\[ACTION:.*?\].*?\[/ACTION\]', '', msg.texto, flags=re.DOTALL).strip() or msg.texto

            # Verificar handoff (asesor tomó el chat)
            _sb_url = os.getenv("SUPABASE_URL", "")
            _sb_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "") or os.getenv("SUPABASE_KEY", "") or os.getenv("SUPABASE_ANON_KEY", "")
            if _sb_url and _sb_key:
                try:
                    async with httpx.AsyncClient(timeout=5) as hc:
                        hr = await hc.get(
                            f"{_sb_url}/rest/v1/messenger_conversations?psid=eq.{psid}&select=handoff_status,bot_paused&limit=1",
                            headers={"Authorization": f"Bearer {_sb_key}", "apikey": _sb_key},
                        )
                        if hr.status_code == 200 and hr.json():
                            hdata = hr.json()[0]
                            if hdata.get("handoff_status") in ("human_active", "won", "lost") or hdata.get("bot_paused"):
                                await guardar_mensaje(psid, "user", clean_text)
                                logger.info(f"[MESSENGER-HANDOFF] {psid} — bot silenciado")
                                return
                except Exception as he:
                    logger.warning(f"[MESSENGER-HANDOFF] {he}")

            await guardar_mensaje(psid, "user", clean_text)
            # 60 mensajes para no truncar el roster de beneficiarios en chats largos
            # (ver nota en el handler de WhatsApp).
            historial = await obtener_historial(psid, limite=60)

            await _messenger_get_session(psid, lead_context=getattr(msg, "lead_context", None))

            from agent.brain import generar_respuesta
            async with _api_semaphore:
                respuesta = await generar_respuesta(
                    clean_text, historial,
                    telefono=psid,
                    lead_context=getattr(msg, "lead_context", None) or _messenger_ad_refs.get(psid),
                )

            respuesta_limpia, respuesta_extra, imagen_url = await _ejecutar_acciones(respuesta, psid)
            await guardar_mensaje(psid, "assistant", respuesta_limpia)

            try:
                crm_perfil = _extract_crm_profile(clean_text, respuesta_limpia, respuesta_extra)
                conv_id = await _messenger_sync_inbound(
                    psid=psid, content=clean_text,
                    prospect_name=crm_perfil.get("prospect_name"),
                    city=crm_perfil.get("city"),
                    ad_ref=(_messenger_ad_refs.get(psid) or {}).get("ref"),
                )
                await _messenger_sync_outbound(psid, respuesta_limpia, conv_id=conv_id)
                if respuesta_extra:
                    await _messenger_sync_outbound(psid, respuesta_extra, conv_id=conv_id)
            except Exception as crm_err:
                logger.warning(f"[MESSENGER-CRM] {crm_err}")

            await mp.enviar_mensaje(psid, respuesta_limpia)

            if imagen_url:
                try:
                    await mp.enviar_imagen(psid, imagen_url)
                except Exception as img_err:
                    logger.warning(f"[MESSENGER-IMG] {img_err}")

            if respuesta_extra:
                await mp.enviar_mensaje(psid, respuesta_extra)

            logger.info(f"[MESSENGER-OUT→{psid}] {respuesta_limpia[:80]}")

        except Exception as e:
            logger.error(f"[MESSENGER-ERROR] {psid}: {e}", exc_info=True)
            try:
                from agent.brain import _fallback_recoger_datos
                await mp.enviar_mensaje(psid, _fallback_recoger_datos(msg.texto or "", psid))
            except Exception:
                pass


@app.get("/webhook/messenger")
async def messenger_webhook_verify(request: Request):
    """Verificación GET del webhook de Facebook Messenger."""
    mp = _get_messenger_proveedor()
    resultado = await mp.validar_webhook(request)
    if resultado is not None:
        return PlainTextResponse(str(resultado))
    return {"status": "ok"}


async def _reply_to_comment(comment_id: str, nombre: str) -> bool:
    """Responde a un comentario de Facebook con un mensaje breve invitando al DM."""
    token = os.getenv("MESSENGER_PAGE_ACCESS_TOKEN")
    if not token:
        return False
    texto = (
        f"¡Hola {nombre}! Te escribimos por privado con más información 📩"
        if nombre else
        "¡Hola! Te escribimos por privado con más información 📩"
    )
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(
                f"https://graph.facebook.com/v21.0/{comment_id}/comments",
                params={"access_token": token},
                json={"message": texto},
            )
            if r.status_code == 200:
                logger.info(f"[COMMENT-REPLY] {comment_id} → OK")
                return True
            logger.warning(f"[COMMENT-REPLY] {comment_id} → {r.status_code} {r.text[:200]}")
    except Exception as e:
        logger.warning(f"[COMMENT-REPLY] {e}")
    return False


async def _send_comment_dm(user_id: str, nombre: str, comment_text: str) -> None:
    """Envía un DM de Natalia a alguien que comentó en la página."""
    token = os.getenv("MESSENGER_PAGE_ACCESS_TOKEN")
    if not token:
        return

    # Mensaje de apertura contextual — inicia conversación de ventas
    saludo = f"¡Hola {nombre}!" if nombre else "¡Hola!"
    mensaje = (
        f"{saludo} Vi tu comentario y quería contarte sobre nuestro plan de protección "
        f"familiar de Mutuo — teleconsultas médicas, bienestar y cobertura exequial para "
        f"toda tu familia desde $24.900/mes. ¿Te cuento más?"
    )
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(
                "https://graph.facebook.com/v21.0/me/messages",
                params={"access_token": token},
                json={
                    "recipient": {"id": user_id},
                    "message": {"text": mensaje},
                    "messaging_type": "RESPONSE",
                },
            )
            if r.status_code == 200:
                logger.info(f"[COMMENT-DM] {user_id} → OK")
                # Pre-crear sesión con contexto de comentario y registrar el DM como salida de Natalia
                await _messenger_get_session(user_id, lead_context={"source": "page_comment", "comment": comment_text})
                await _messenger_sync_outbound(user_id, mensaje)
            else:
                logger.warning(f"[COMMENT-DM] {user_id} → {r.status_code} {r.text[:200]}")
    except Exception as e:
        logger.warning(f"[COMMENT-DM] {e}")


async def _handle_feed_comment(change: dict) -> None:
    """Procesa comentarios nuevos en la página — responde al comentario y envía DM."""
    value = change.get("value", {})
    if value.get("item") != "comment" or value.get("verb") != "add":
        return

    comment_id = value.get("comment_id", "")
    from_user = value.get("from", {})
    user_id = from_user.get("id", "")
    user_name = (from_user.get("name") or "").split()[0].title()
    comment_text = value.get("message", "")
    parent_id = value.get("parent_id", "")

    if not user_id or not comment_id:
        return

    # No responder a comentarios de la propia página
    page_id = os.getenv("MESSENGER_PAGE_ID", "117613301227150")
    if user_id == page_id:
        return

    # No responder a respuestas de comentarios (solo comentarios raíz)
    if parent_id and parent_id != value.get("post_id", ""):
        return

    # Deduplicar por comment_id
    if await _messenger_is_duplicate(f"comment_{comment_id}"):
        logger.debug(f"[COMMENT-DEDUP] {comment_id}")
        return

    logger.info(f"[COMMENT] {user_name} ({user_id}): {comment_text[:60]}")

    # 1. Responder al comentario públicamente
    await _reply_to_comment(comment_id, user_name)

    # 2. Enviar DM al inbox del usuario
    await _send_comment_dm(user_id, user_name, comment_text)


@app.post("/webhook/messenger")
async def messenger_webhook_handler(request: Request, background_tasks: BackgroundTasks):
    """
    Recibe mensajes y eventos de Facebook (Messenger DMs + comentarios de página).
    Comparte servidor con WhatsApp — rutas completamente separadas.
    """
    if not os.getenv("MESSENGER_PAGE_ACCESS_TOKEN"):
        return {"status": "messenger_not_configured"}
    try:
        body = await request.json()

        if body.get("object") != "page":
            return {"status": "ok"}

        mp = _get_messenger_proveedor()

        for entry in body.get("entry", []):
            # ── Mensajes de Messenger (DMs) ──
            for event in entry.get("messaging", []):
                sender_id = event.get("sender", {}).get("id", "")
                page_id = event.get("recipient", {}).get("id", "")
                if sender_id == page_id:
                    continue
                message = event.get("message", {})
                postback = event.get("postback", {})
                referral = event.get("referral", {}) or message.get("referral", {})
                lead_context = None
                if referral:
                    lead_context = {
                        "ref": referral.get("ref", ""),
                        "source": referral.get("source", ""),
                        "ad_id": referral.get("ad_id", ""),
                    }
                texto = ""
                if message.get("text"):
                    texto = message["text"]
                elif postback.get("payload"):
                    texto = postback.get("title", postback["payload"])
                elif message.get("attachments"):
                    att_type = message["attachments"][0].get("type", "archivo")
                    tipo_map = {"audio": "[AUDIO_RECIBIDO]", "image": "[IMAGEN_RECIBIDA]",
                                "video": "[VIDEO_RECIBIDO]", "file": "[DOCUMENTO_RECIBIDO]"}
                    texto = tipo_map.get(att_type, "[ARCHIVO_RECIBIDO]")
                if not texto:
                    continue
                mid = message.get("mid", postback.get("mid", sender_id))
                if await _messenger_is_duplicate(mid):
                    continue
                from agent.providers.base import MensajeEntrante
                msg = MensajeEntrante(
                    telefono=sender_id, texto=texto, mensaje_id=mid,
                    es_propio=False, lead_context=lead_context,
                )
                logger.info(f"[MESSENGER-IN←{sender_id}] {texto[:80]}")
                background_tasks.add_task(_process_messenger_message, msg)

            # ── Eventos de feed (comentarios en la página) ──
            for change in entry.get("changes", []):
                if change.get("field") == "feed":
                    background_tasks.add_task(_handle_feed_comment, change)

        return {"status": "ok"}
    except Exception as e:
        logger.error(f"[MESSENGER-WEBHOOK] {e}")
        return {"status": "error", "detail": str(e)}


@app.get("/messenger/sesiones")
async def messenger_sesiones():
    """Lista sesiones activas de Messenger."""
    return {"total": len(_messenger_sesiones), "psids": list(_messenger_sesiones.keys())}
