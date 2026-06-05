# messenger_main.py — Servidor FastAPI para el bot de Facebook Messenger (Pauta)
# Mutuo Fintech S.A.S. — Origen AI Agent

"""
Bot de Messenger para campañas de pauta (Click-to-Messenger).
Reutiliza el motor conversacional de WhatsApp (OrigenIA, brain, tools, CRM)
adaptado al canal de Facebook Messenger con PSID como identificador.

Variables de entorno requeridas:
  MESSENGER_PAGE_ACCESS_TOKEN  — token de acceso a la página de Facebook
  MESSENGER_VERIFY_TOKEN       — token de verificación del webhook (ej: mutuo-messenger-verify)
  ANTHROPIC_API_KEY            — clave API de Anthropic
  SUPABASE_URL                 — URL de Supabase
  SUPABASE_SERVICE_ROLE_KEY    — clave de servicio Supabase

Variables opcionales:
  MESSENGER_PORT               — puerto (default: 8001)
  PLAN_IMAGE_BASE_URL          — base URL imágenes de planes
  ENVIRONMENT                  — development | production
"""

import os
import re
import json
import logging
import asyncio
from collections import OrderedDict
from contextlib import asynccontextmanager

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse

from agent.memory import inicializar_db, guardar_mensaje, obtener_historial
from agent.providers.messenger import ProveedorMessenger
from agent.mutuo_actions import (
    crear_afiliacion,
    generar_link_pago,
    consultar_estado_cuenta,
    crear_ticket_cancelacion,
    consultar_radicado,
    consultar_cuenta_por_cedula,
    actualizar_beneficiarios,
    reenviar_recibo,
)
from agent.crm_sync import sync_inbound, sync_outbound, save_message, get_or_create_conversation
from origen_ia.agent.core import OrigenIA, cost_tracker
from origen_ia.crm.vendu_client import VenduCRMClient

load_dotenv()

ENVIRONMENT = os.getenv("ENVIRONMENT", "development")
log_level = logging.DEBUG if ENVIRONMENT == "development" else logging.INFO
logging.basicConfig(level=log_level)
logger = logging.getLogger("mutuo-messenger")

PORT = int(os.getenv("MESSENGER_PORT", 8001))
_PLAN_IMAGE_BASE = os.getenv("PLAN_IMAGE_BASE_URL", "https://ventas.mutuo.la/planes").rstrip("/")
PLAN_IMAGE_URLS = {
    "esencial": f"{_PLAN_IMAGE_BASE}/plan-esencial.jpg",
    "plus":     f"{_PLAN_IMAGE_BASE}/plan-plus.jpg",
    "total":    f"{_PLAN_IMAGE_BASE}/plan-total.jpg",
}

proveedor = ProveedorMessenger()
crm = VenduCRMClient()

# Sesiones activas OrigenIA por PSID
sesiones: dict[str, OrigenIA] = {}

# Deduplicación de mensajes
_processed_message_ids: OrderedDict[str, bool] = OrderedDict()
_MAX_PROCESSED_CACHE = 5000
_dedup_lock = asyncio.Lock()

# Lock por PSID
_psid_locks: dict[str, asyncio.Lock] = {}

# Semáforo Anthropic
_api_semaphore = asyncio.Semaphore(5)

# ─── Referral de campaña (PSID → ref de anuncio) ─────────────────────────────
_ad_referrals: dict[str, dict] = {}


async def _is_duplicate(mid: str) -> bool:
    if not mid:
        return False
    async with _dedup_lock:
        if mid in _processed_message_ids:
            return True
        _processed_message_ids[mid] = True
        if len(_processed_message_ids) > _MAX_PROCESSED_CACHE:
            _processed_message_ids.popitem(last=False)
        return False


def _get_lock(psid: str) -> asyncio.Lock:
    if psid not in _psid_locks:
        _psid_locks[psid] = asyncio.Lock()
    return _psid_locks[psid]


# ─── Historial desde Supabase ─────────────────────────────────────────────────

async def _load_history(psid: str) -> tuple[list[dict], dict]:
    """Carga historial y perfil desde messenger_conversations en Supabase."""
    sb_url = os.getenv("SUPABASE_URL", "")
    sb_key = (
        os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
        or os.getenv("SUPABASE_KEY", "")
        or os.getenv("SUPABASE_ANON_KEY", "")
    )
    if not sb_url or not sb_key:
        return [], {}

    headers = {"Authorization": f"Bearer {sb_key}", "apikey": sb_key}
    historial: list[dict] = []
    perfil: dict = {}

    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(
                f"{sb_url}/rest/v1/messenger_conversations"
                f"?psid=eq.{psid}"
                f"&select=id,prospect_name,city,interest,disc_profile,status,notes"
                f"&limit=1",
                headers=headers,
            )
            if r.status_code == 200 and r.json():
                conv = r.json()[0]
                perfil = {
                    "nombre": conv.get("prospect_name", ""),
                    "ciudad": conv.get("city", ""),
                    "interest": conv.get("interest", ""),
                    "disc_profile": conv.get("disc_profile", ""),
                    "status": conv.get("status", ""),
                }
                r2 = await c.get(
                    f"{sb_url}/rest/v1/messenger_messages"
                    f"?conversation_id=eq.{conv['id']}"
                    f"&select=role,content"
                    f"&order=created_at.desc&limit=50",
                    headers=headers,
                )
                if r2.status_code == 200:
                    msgs = r2.json()
                    msgs.reverse()
                    for m in msgs:
                        if m.get("role") and m.get("content"):
                            historial.append({"role": m["role"], "content": m["content"]})
    except Exception as e:
        logger.warning(f"[HISTORY] Error cargando historial de {psid}: {e}")

    return historial, perfil


async def _get_session(psid: str, lead_context: dict | None = None) -> OrigenIA:
    if psid not in sesiones:
        agente = OrigenIA(canal="messenger")

        historial, perfil = await _load_history(psid)
        if historial:
            agente.historial = historial
            agente.turnos = len([m for m in historial if m["role"] == "user"])

        if perfil.get("nombre"):
            agente.profile.nombre = perfil["nombre"].split()[0].title()
            agente.profile.nombre_completo = perfil["nombre"]
        if perfil.get("ciudad"):
            agente.profile.ciudad = perfil["ciudad"]
        if perfil.get("interest"):
            agente.profile.paquete_recomendado = perfil["interest"]

        agente.is_returning = len(historial) > 0

        # Contexto del anuncio de pauta (ref, ad_id)
        if lead_context:
            _ad_referrals[psid] = lead_context
            agente.campaign_context = (
                f"[CONTEXTO_ANUNCIO] El usuario llegó desde un anuncio de Facebook. "
                f"Referral ref={lead_context.get('ref', '')} "
                f"ad_id={lead_context.get('ad_id', '')} "
                f"source={lead_context.get('source', '')}. "
                f"Trátalo como un prospecto de pauta — está interesado en protección familiar."
            )
            agente.is_outbound = False  # inbound desde ad

        # Intentar obtener el nombre real desde Graph API
        try:
            perfil_fb = await proveedor.obtener_perfil_usuario(psid)
            if perfil_fb.get("first_name") and not agente.profile.nombre:
                agente.profile.nombre = perfil_fb["first_name"].title()
                agente.profile.nombre_completo = perfil_fb.get("name", "").title()
                logger.info(f"[FB-PROFILE] {psid} → {agente.profile.nombre}")
        except Exception:
            pass

        sesiones[psid] = agente
    return sesiones[psid]


# ─── Sanitización y acciones (reutilizadas de main.py) ───────────────────────

_BRACKET_LEAK_RE = re.compile(r'\[[^\[\]\n]{1,80}\]')


def _sanitizar(texto: str) -> str:
    if not texto or '[' not in texto:
        return texto

    def _sub(m: re.Match) -> str:
        c = m.group(0)
        return c if c.startswith('[http') else ''

    limpio = _BRACKET_LEAK_RE.sub(_sub, texto)
    limpio = re.sub(r'[ \t]+', ' ', limpio)
    limpio = re.sub(r'\n{3,}', '\n\n', limpio)
    lineas = []
    for linea in limpio.split('\n'):
        d = linea.strip(' -•·*\t')
        if not d:
            continue
        if re.match(r'^\d+[\.\)\-]?\s*$', d):
            continue
        if re.match(r'^[A-Za-zÁÉÍÓÚáéíóúÑñ ]+:\s*$', d):
            continue
        lineas.append(linea.rstrip())
    return '\n'.join(lineas).strip()


async def _link_messenger_affiliation(psid: str, phone: str, affiliation_id: str) -> None:
    """Guarda phone + affiliation_id en la conversación de Messenger (por psid).

    Garantiza que el historial quede SIEMPRE enlazado al perfil del afiliado en
    el admin apenas se crea la afiliación. El trigger de la BD también enlaza por
    teléfono como respaldo, pero esto lo hace inmediato y sin depender del orden.
    """
    sb_url = os.getenv("SUPABASE_URL", "")
    sb_key = (
        os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
        or os.getenv("SUPABASE_KEY", "")
        or os.getenv("SUPABASE_ANON_KEY", "")
    )
    if not (sb_url and sb_key and affiliation_id):
        return
    payload: dict = {"affiliation_id": affiliation_id}
    if phone:
        payload["phone"] = phone
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            await c.patch(
                f"{sb_url}/rest/v1/messenger_conversations?psid=eq.{psid}",
                headers={
                    "Authorization": f"Bearer {sb_key}",
                    "apikey": sb_key,
                    "Content-Type": "application/json",
                    "Prefer": "return=minimal",
                },
                json=payload,
            )
        logger.info(f"[MSGR-LINK] {psid} → afiliación {affiliation_id}")
    except Exception as e:
        logger.warning(f"[MSGR-LINK] No se pudo enlazar afiliación a {psid}: {e}")


async def _ejecutar_acciones(respuesta: str, psid: str) -> tuple[str, str | None, str | None]:
    """Parsea y ejecuta acciones [ACTION:TIPO]{json}[/ACTION] del bot."""
    action_match = re.search(r'\[ACTION:(\w+)\](.*?)\[/ACTION\]', respuesta, re.DOTALL)
    if not action_match:
        return _sanitizar(respuesta), None, None

    action_type = action_match.group(1)
    action_data_str = action_match.group(2).strip()
    texto_limpio = _sanitizar(respuesta[:action_match.start()].strip())

    try:
        action_data = json.loads(action_data_str)
    except json.JSONDecodeError:
        logger.error(f"[ACTION] JSON inválido: {action_data_str[:200]}")
        return texto_limpio, None, None

    respuesta_extra: str | None = None
    imagen_url: str | None = None

    if action_type == "CREAR_AFILIACION":
        # NO usar el PSID como teléfono: corrompe el registro (queda un +57 falso)
        # y rompe el envío de contrato por WhatsApp y el dedup por teléfono.
        # Guardamos el PSID aparte; el teléfono solo si el cliente dio uno real.
        action_data["messenger_psid"] = psid
        action_data["phone"] = str(action_data.get("phone", "")).strip()
        result = await crear_afiliacion(action_data)
        if result.get("duplicate"):
            nombre = result.get("first_name") or "amigo"
            recovery = result.get("recovery_link") or "https://ventas.mutuo.la/auth"
            respuesta_extra = (
                f"Hola {nombre}, ya tienes una afiliación activa con nosotros.\n\n"
                f"Para acceder a tu cuenta:\n{recovery}"
            )
        elif result.get("success"):
            # Enlazar la conversación de Messenger al afiliado recién creado para
            # que su historial quede disponible en el perfil del admin.
            await _link_messenger_affiliation(
                psid, action_data.get("phone", ""), result.get("affiliation_id", "")
            )
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
                await asyncio.sleep(2)

            if link_result.get("success"):
                respuesta_extra = (
                    f"¡Tu afiliación quedó registrada!\n\n"
                    f"Te enviamos el contrato y los accesos a tu cuenta al correo que nos diste.\n\n"
                    f"Para activar tu cobertura realiza el primer pago aquí:\n"
                    f"{link_result['payment_link']}\n\n"
                    f"Puedes pagar con tarjeta, PSE, Efecty o Nequi.\n\n"
                    f"Términos y condiciones: https://www.mutuo.la/terms\n"
                    f"Habeas data: https://www.mutuo.la/habeas-data"
                )
            else:
                import urllib.parse
                cedula_enc = urllib.parse.quote(action_data.get("document_number", ""))
                email_enc = urllib.parse.quote(action_data.get("email", ""))
                respuesta_extra = (
                    f"¡Tu afiliación quedó registrada!\n\n"
                    f"Para el primer pago ingresa aquí:\n"
                    f"https://ventas.mutuo.la/recaudo?cedula={cedula_enc}&email={email_enc}&auto=true"
                )
        else:
            respuesta_extra = "Hubo un problema registrando tu afiliación. Escríbenos a sac@mutuo.la"

    elif action_type == "CONSULTAR_ESTADO":
        action_data["phone"] = action_data.get("phone", psid)
        result = await consultar_estado_cuenta(action_data["phone"])
        if result.get("found"):
            if result["total_deuda"] > 0:
                link_result = await generar_link_pago(result["affiliation_id"], int(result["total_deuda"]), result["name"])
                link_text = f"\n\nPaga aquí: {link_result['payment_link']}" if link_result.get("success") else ""
                respuesta_extra = (
                    f"Hola {result['name']}!\n\n"
                    f"Plan: {result['plan']}\n"
                    f"Deuda: ${int(result['total_deuda']):,} COP\n"
                    f"Cuotas pendientes: {result['cuotas_pendientes']}"
                    f"{link_text}"
                )
            else:
                respuesta_extra = f"Hola {result['name']}! Tu cuenta está al día."

    elif action_type == "REENVIAR_RECIBO":
        action_data["phone"] = action_data.get("phone", psid)
        result = await reenviar_recibo(action_data["phone"])
        if result.get("found") is False:
            respuesta_extra = (
                "No encontré una afiliación asociada a esta cuenta. "
                "Si pagaste con otro número o cédula, pásame tu número de cédula y la reviso."
            )
        elif result.get("sent"):
            respuesta_extra = (
                f"Listo {result.get('name', '')}! Te acabo de reenviar tu recibo de caja "
                f"con el detalle de tu pago."
            )
        elif result.get("reason") == "sin_pago":
            respuesta_extra = (
                "Todavía no veo el pago reflejado en el sistema. Los pagos pueden tardar "
                "unos minutos en confirmarse. Apenas se registre, te llega el recibo automáticamente."
            )
        elif result.get("reason") == "sin_telefono":
            respuesta_extra = (
                "Encontré tu cuenta pero no tengo un número de WhatsApp registrado para enviarte el recibo. "
                "Escríbenos a sac@mutuo.la y te lo hacemos llegar."
            )
        else:
            respuesta_extra = (
                "No pude reenviar el recibo en este momento. Inténtalo de nuevo en un rato "
                "o escríbenos a sac@mutuo.la."
            )

    elif action_type == "CREAR_TICKET_CANCELACION":
        action_data["phone"] = action_data.get("phone", psid)
        result = await crear_ticket_cancelacion(
            action_data["phone"],
            action_data.get("reason", "Solicitud del cliente"),
            int(action_data.get("retention_attempts", 0) or 0),
        )
        if result.get("success"):
            respuesta_extra = (
                f"Listo, registré tu solicitud de cancelación.\n\nRadicado: {result['radicado']}\n\n"
                f"Desde ya no se generarán nuevos cobros. Nuestro equipo la tramita en máximo "
                f"24 horas hábiles. Guarda tu radicado para cualquier seguimiento."
            )
        else:
            respuesta_extra = "No pude registrar la cancelación. Escribe a sac@mutuo.la"

    elif action_type == "CONSULTAR_RADICADO":
        identificador = str(action_data.get("radicado") or action_data.get("phone") or psid).strip()
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
            respuesta_extra = f"Estado de tu radicado {result['radicado']}:\n\n{estado_txt}{notas}"
        else:
            respuesta_extra = (
                "No encontré ningún radicado con esos datos. Verifica el número "
                "(formato CAN-AAAAMMDD-XXXXXX) o escríbenos a sac@mutuo.la"
            )

    elif action_type == "CONSULTAR_POR_CEDULA":
        cedula = str(action_data.get("cedula", "")).strip()
        result = await consultar_cuenta_por_cedula(cedula)
        if result.get("found"):
            r = result
            deuda_txt = f"Deuda: ${int(r['total_deuda']):,} COP\nCuotas: {r['cuotas_pendientes']}" if r["total_deuda"] > 0 else "Al día, sin pagos pendientes."
            respuesta_extra = f"Hola {r['name']}!\n\nPlan: {r['plan']}\nTarifa: ${int(r['tarifa']):,}/mes\n{deuda_txt}"
            if r["total_deuda"] > 0:
                link_result = await generar_link_pago(r["affiliation_id"], int(r["total_deuda"]), r["name"])
                if link_result.get("success"):
                    respuesta_extra += f"\n\nPaga aquí: {link_result['payment_link']}"

    elif action_type == "ACTUALIZAR_BENEFICIARIOS":
        result = await actualizar_beneficiarios(
            str(action_data.get("affiliation_id", "")),
            action_data.get("beneficiarios", []),
            action_data.get("motivo", ""),
        )
        if result.get("success"):
            respuesta_extra = (
                f"¡Listo {result.get('name', '')}! Beneficiarios actualizados.\n\n"
                f"Antes: {result['beneficiarios_antes']} / Ahora: {result['beneficiarios_despues']}"
            )
        else:
            respuesta_extra = "No pude actualizar los beneficiarios. Escribe a sac@mutuo.la"

    elif action_type == "ENVIAR_IMAGEN_PLAN":
        plan_slug = str(action_data.get("plan", "")).lower().strip()
        imagen_url = PLAN_IMAGE_URLS.get(plan_slug)
        if not imagen_url:
            logger.warning(f"[ACTION ENVIAR_IMAGEN_PLAN] plan desconocido: {plan_slug!r}")

    return texto_limpio, respuesta_extra, imagen_url


def _extract_crm_profile(user_msg: str, bot_response: str, action_result: str | None) -> dict:
    perfil: dict = {}
    if action_result and "afiliaci" in action_result.lower() and "registrada" in action_result.lower():
        perfil["status"] = "convertido"
    elif any(w in user_msg.lower() for w in ["quiero", "precio", "cuanto", "plan", "afiliar"]):
        perfil["status"] = "caliente"
    elif len(user_msg.split()) > 2:
        perfil["status"] = "en_progreso"

    name_match = re.search(r'(?:me llamo|soy|mi nombre es)\s+([A-ZÁÉÍÓÚÑa-záéíóúñ]+(?:\s+[A-ZÁÉÍÓÚÑa-záéíóúñ]+)?)', user_msg, re.IGNORECASE)
    if name_match:
        perfil["prospect_name"] = name_match.group(1).strip().title()

    ciudades = ["bogota", "medellin", "cali", "barranquilla", "cartagena", "bucaramanga",
                "pereira", "manizales", "cucuta", "ibague", "villavicencio", "pasto",
                "monteria", "valledupar", "neiva", "armenia", "sincelejo", "popayan",
                "soacha", "bello", "itagui", "envigado", "dosquebradas", "tulua"]
    msg_norm = user_msg.lower().replace("á","a").replace("é","e").replace("í","i").replace("ó","o").replace("ú","u")
    for ciudad in ciudades:
        if ciudad in msg_norm:
            perfil["city"] = ciudad.title()
            break

    return perfil


# ─── Procesamiento de mensajes ────────────────────────────────────────────────

_MEDIA_FALLBACKS = {
    "[AUDIO_RECIBIDO]":    "Disculpa, tengo los audios deshabilitados. ¿Me escribes por texto?",
    "[IMAGEN_RECIBIDA]":   "¡Hola! Recibí tu imagen pero aún no puedo verlas. ¿Me cuentas por texto en qué te puedo ayudar?",
    "[VIDEO_RECIBIDO]":    "¡Hola! Recibí tu video pero no puedo reproducirlo. ¿Me escribes por texto qué necesitas?",
    "[DOCUMENTO_RECIBIDO]":"Recibí tu documento. Cuéntame por texto qué necesitas y te guío.",
    "[ARCHIVO_RECIBIDO]":  "Recibí un archivo. ¿Me cuentas por texto en qué te puedo ayudar?",
}


async def _process_message(msg) -> None:
    lock = _get_lock(msg.telefono)
    async with lock:
        psid = msg.telefono
        try:
            # Media fallback
            if msg.texto in _MEDIA_FALLBACKS:
                resp = _MEDIA_FALLBACKS[msg.texto]
                await proveedor.enviar_mensaje(psid, resp)
                await guardar_mensaje(psid, "user", msg.texto)
                await guardar_mensaje(psid, "assistant", resp)
                return

            # Indicador "escribiendo"
            await proveedor.enviar_typing(psid)

            # Anti-injection
            clean_text = re.sub(r'\[ACTION:.*?\].*?\[/ACTION\]', '', msg.texto, flags=re.DOTALL).strip() or msg.texto

            # Verificar si asesor tomó el chat
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
                            hs = hdata.get("handoff_status", "bot")
                            bp = hdata.get("bot_paused", False)
                            if hs in ("human_active", "won", "lost") or bp:
                                await guardar_mensaje(psid, "user", clean_text)
                                try:
                                    conv_id = await _get_or_create_messenger_conv(psid)
                                    if conv_id:
                                        await save_message(conv_id, "user", clean_text)
                                except Exception:
                                    pass
                                logger.info(f"[HANDOFF={hs}] {psid} — bot silenciado")
                                return
                except Exception as he:
                    logger.warning(f"[HANDOFF] {he}")

            await guardar_mensaje(psid, "user", clean_text)

            historial = await obtener_historial(psid, limite=20)

            agente = await _get_session(psid, lead_context=getattr(msg, "lead_context", None))

            from agent.brain import generar_respuesta
            async with _api_semaphore:
                respuesta = await generar_respuesta(
                    clean_text,
                    historial,
                    telefono=psid,
                    lead_context=getattr(msg, "lead_context", None) or _ad_referrals.get(psid),
                )

            respuesta_limpia, respuesta_extra, imagen_url = await _ejecutar_acciones(respuesta, psid)

            await guardar_mensaje(psid, "assistant", respuesta_limpia)

            # Sincronizar con CRM Supabase en tabla messenger_conversations/messages
            try:
                crm_perfil = _extract_crm_profile(clean_text, respuesta_limpia, respuesta_extra)
                await crm.sync_message(psid, "user", clean_text, crm_perfil)
                await crm.sync_message(psid, "assistant", respuesta_limpia, crm_perfil)

                conv_id = await _sync_messenger_inbound(
                    psid=psid,
                    content=clean_text,
                    prospect_name=crm_perfil.get("prospect_name"),
                    city=crm_perfil.get("city"),
                    ad_ref=_ad_referrals.get(psid, {}).get("ref"),
                )
                await _sync_messenger_outbound(psid, respuesta_limpia, conv_id=conv_id)
                if respuesta_extra:
                    await _sync_messenger_outbound(psid, respuesta_extra, conv_id=conv_id)
            except Exception as crm_err:
                logger.warning(f"[CRM] {crm_err}")

            # Enviar respuesta
            await proveedor.enviar_mensaje(psid, respuesta_limpia)

            if imagen_url:
                try:
                    await proveedor.enviar_imagen(psid, imagen_url)
                except Exception as img_err:
                    logger.warning(f"[IMG] {img_err}")

            if respuesta_extra:
                await proveedor.enviar_mensaje(psid, respuesta_extra)

            logger.info(f"[OUT→{psid}] {respuesta_limpia[:80]}")

        except Exception as e:
            logger.error(f"[ERROR] {psid}: {e}", exc_info=True)
            try:
                from agent.brain import _fallback_recoger_datos
                fallback = _fallback_recoger_datos(msg.texto or "", psid)
                await proveedor.enviar_mensaje(psid, fallback)
            except Exception as fb_err:
                logger.error(f"[FALLBACK] {fb_err}")


# ─── Helpers CRM Supabase para Messenger ─────────────────────────────────────

async def _get_or_create_messenger_conv(psid: str) -> str | None:
    sb_url = os.getenv("SUPABASE_URL", "")
    sb_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "") or os.getenv("SUPABASE_ANON_KEY", "")
    if not sb_url or not sb_key:
        return None
    headers = {
        "Authorization": f"Bearer {sb_key}",
        "apikey": sb_key,
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(
                f"{sb_url}/rest/v1/messenger_conversations?psid=eq.{psid}&select=id&limit=1",
                headers=headers,
            )
            if r.status_code == 200 and r.json():
                return r.json()[0]["id"]
            rp = await c.post(
                f"{sb_url}/rest/v1/messenger_conversations",
                headers=headers,
                json={"psid": psid, "status": "nuevo", "handoff_status": "bot"},
            )
            if rp.status_code in (200, 201) and rp.json():
                return rp.json()[0]["id"]
    except Exception as e:
        logger.warning(f"[MESSENGER-CRM] get_or_create_conv: {e}")
    return None


async def _sync_messenger_inbound(
    psid: str,
    content: str,
    prospect_name: str | None = None,
    city: str | None = None,
    ad_ref: str | None = None,
) -> str | None:
    sb_url = os.getenv("SUPABASE_URL", "")
    sb_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "") or os.getenv("SUPABASE_ANON_KEY", "")
    if not sb_url or not sb_key:
        return None
    headers = {
        "Authorization": f"Bearer {sb_key}",
        "apikey": sb_key,
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(
                f"{sb_url}/rest/v1/messenger_conversations?psid=eq.{psid}&select=id&limit=1",
                headers=headers,
            )
            if r.status_code == 200 and r.json():
                conv_id = r.json()[0]["id"]
                update_data: dict = {}
                if prospect_name:
                    update_data["prospect_name"] = prospect_name
                if city:
                    update_data["city"] = city
                if update_data:
                    await c.patch(
                        f"{sb_url}/rest/v1/messenger_conversations?id=eq.{conv_id}",
                        headers=headers,
                        json=update_data,
                    )
            else:
                create_data: dict = {
                    "psid": psid,
                    "status": "en_progreso",
                    "handoff_status": "bot",
                    "channel": "messenger",
                }
                if prospect_name:
                    create_data["prospect_name"] = prospect_name
                if city:
                    create_data["city"] = city
                if ad_ref:
                    create_data["ad_ref"] = ad_ref
                rp = await c.post(
                    f"{sb_url}/rest/v1/messenger_conversations",
                    headers=headers,
                    json=create_data,
                )
                conv_id = rp.json()[0]["id"] if rp.status_code in (200, 201) and rp.json() else None

            if conv_id:
                await c.post(
                    f"{sb_url}/rest/v1/messenger_messages",
                    headers=headers,
                    json={"conversation_id": conv_id, "role": "user", "content": content},
                )
            return conv_id
    except Exception as e:
        logger.warning(f"[MESSENGER-CRM] sync_inbound: {e}")
    return None


async def _sync_messenger_outbound(psid: str, content: str, conv_id: str | None = None) -> None:
    sb_url = os.getenv("SUPABASE_URL", "")
    sb_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "") or os.getenv("SUPABASE_ANON_KEY", "")
    if not sb_url or not sb_key or not content:
        return
    headers = {
        "Authorization": f"Bearer {sb_key}",
        "apikey": sb_key,
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }
    try:
        if not conv_id:
            async with httpx.AsyncClient(timeout=5) as c:
                r = await c.get(
                    f"{sb_url}/rest/v1/messenger_conversations?psid=eq.{psid}&select=id&limit=1",
                    headers=headers,
                )
                if r.status_code == 200 and r.json():
                    conv_id = r.json()[0]["id"]
        if conv_id:
            async with httpx.AsyncClient(timeout=5) as c:
                await c.post(
                    f"{sb_url}/rest/v1/messenger_messages",
                    headers=headers,
                    json={"conversation_id": conv_id, "role": "assistant", "content": content},
                )
    except Exception as e:
        logger.warning(f"[MESSENGER-CRM] sync_outbound: {e}")


# ─── FastAPI app ──────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    await inicializar_db()
    logger.info(f"[MESSENGER BOT] Corriendo en puerto {PORT}")
    yield


app = FastAPI(
    title="Origen AI — Messenger Bot (Pauta)",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Voice routes (Twilio Media Streams) ──────────────────────────────────────
from fastapi import WebSocket
from voice_handler import twiml_handler, voice_stream_handler

app.add_api_route("/voice/twiml", twiml_handler, methods=["POST", "GET"])
app.add_api_websocket_route("/voice/stream", voice_stream_handler)


@app.get("/")
async def health():
    return {
        "status": "ok",
        "service": "messenger-bot",
        "agent": "Natalia",
        "canal": "Facebook Messenger",
        "sesiones_activas": len(sesiones),
    }


@app.get("/webhook/messenger")
async def webhook_verify(request: Request):
    """Verificación del webhook de Meta."""
    resultado = await proveedor.validar_webhook(request)
    if resultado is not None:
        return PlainTextResponse(str(resultado))
    return {"status": "ok"}


@app.post("/webhook/messenger")
async def webhook_handler(request: Request, background_tasks: BackgroundTasks):
    """
    Recibe mensajes de Facebook Messenger.
    Responde 200 OK inmediatamente — procesamiento en background.
    """
    try:
        mensajes = await proveedor.parsear_webhook(request)
        for msg in mensajes:
            if msg.es_propio or not msg.texto:
                continue
            if await _is_duplicate(msg.mensaje_id):
                logger.debug(f"[DEDUP] {msg.mensaje_id}")
                continue
            logger.info(f"[IN←{msg.telefono}] {msg.texto[:80]}")
            background_tasks.add_task(_process_message, msg)
        return {"status": "ok"}
    except Exception as e:
        logger.error(f"[WEBHOOK] {e}")
        return {"status": "error", "detail": str(e)}


@app.get("/sesiones")
async def listar_sesiones():
    return {
        "total": len(sesiones),
        "psids": list(sesiones.keys()),
    }


@app.post("/sesiones/{psid}/reset")
async def reset_sesion(psid: str):
    """Reinicia la sesión de un PSID (útil para pruebas)."""
    if psid in sesiones:
        del sesiones[psid]
        return {"status": "ok", "psid": psid}
    return {"status": "not_found"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("messenger_main:app", host="0.0.0.0", port=PORT, reload=ENVIRONMENT == "development")
