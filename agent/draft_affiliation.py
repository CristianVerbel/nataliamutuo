# agent/draft_affiliation.py — Borrador progresivo de afiliación (anti-olvido del bot)
# Desarrollado por Catalitico LLC para Mutuo Fintech S.A.S.
#
# Cuando un prospecto empieza a dar datos por WhatsApp persistimos un "borrador"
# de afiliación en b2c_affiliations (status=in_progress, is_active=false) y lo
# vamos nutriendo mensaje a mensaje. Así el SISTEMA —y no la memoria volátil del
# LLM— es la fuente de verdad: el bot deja de olvidar o inventar datos que el
# cliente ya entregó. Apenas el cliente se identifica con nombre y apellido (su
# celular ya lo tenemos) nace el borrador como lead; según da más datos se nutre
# hasta que la acción CREAR_AFILIACION lo convierte en afiliado real.

import os
import json
import logging
import asyncio
import httpx
from datetime import datetime
from anthropic import AsyncAnthropic
from dotenv import load_dotenv

from agent.mutuo_actions import (
    _parse_birth_date,
    _calc_age,
    normalizar_telefono,
    es_celular_co,
    _phone_variants,
)

load_dotenv()
logger = logging.getLogger("mutuo-bot")

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = (
    os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    or os.getenv("SUPABASE_KEY", "")
    or os.getenv("SUPABASE_ANON_KEY", "")
)

HEADERS = {
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "apikey": SUPABASE_KEY,
    "Content-Type": "application/json",
}

_anthropic_key = os.getenv("ANTHROPIC_API_KEY")
_ai = AsyncAnthropic(api_key=_anthropic_key) if _anthropic_key else None

# Evita extracciones concurrentes para el mismo teléfono (ráfagas de mensajes).
_locks: dict[str, asyncio.Lock] = {}

PLAN_NAMES = {
    "esencial": "Familia Esencial",
    "plus": "Familia Plus",
    "total": "Familia Total",
}

_BORRADOR_PROMPT = """Analiza esta conversación de WhatsApp entre un bot de ventas de Mutuo (Club de Bienestar Familiar) y un prospecto.

Tu tarea es extraer TODOS los datos de afiliación que el cliente haya entregado hasta ahora, para guardarlos en el sistema. Devuelve SOLO JSON, sin texto adicional.

Reglas:
- Extrae únicamente lo que el CLIENTE dijo. Si un dato no aparece, pon null (o [] para listas). NUNCA inventes.
- Acumula: incluye todo lo dicho en cualquier punto de la conversación, no solo lo último.

Campos:
- estado: "ninguna" si el cliente NO se ha identificado todavía (solo saludos o preguntas generales, sin dar su nombre); en cualquier otro caso pon "borrador".
- first_name, last_name: nombre y apellido del TITULAR.
- document_type: "CC" o "CE" (null si no lo dio).
- document_number: solo dígitos (null si no lo dio).
- email (null si no lo dio).
- birth_date: fecha de nacimiento del TITULAR en formato DD/MM/AAAA (null si no la dio).
- address, municipality, department (null si no los dio).
- plan: "esencial", "plus" o "total" (null si no eligió).
- beneficiarios: array de {{primerNombre, apellido, parentesco, fechaNac}} ([] si ninguno).
- mascotas: array de {{nombre, tipo, raza, edad}} ([] si ninguna).
- notas: texto breve con qué datos clave faltan todavía.

CONVERSACIÓN:
{conversation}"""


def _format_conv(historial: list[dict]) -> str:
    lines = []
    for m in historial or []:
        role = m.get("role")
        content = (m.get("content") or "").strip()
        if not content:
            continue
        sender = "CLIENTE" if role == "user" else "BOT"
        lines.append(f"[{sender}] {content}")
    return "\n".join(lines)


async def _buscar_afiliacion(telefono: str) -> dict | None:
    """Devuelve la fila de b2c_affiliations más reciente para el teléfono (cualquier estado)."""
    or_filter = ",".join(f"phone.eq.{p}" for p in _phone_variants(telefono))
    try:
        async with httpx.AsyncClient(timeout=8) as http:
            r = await http.get(
                f"{SUPABASE_URL}/rest/v1/b2c_affiliations?or=({or_filter})"
                f"&select=id,status,first_name,last_name,document_type,document_number,"
                f"email,birth_date,address,municipality,department,selected_plan,beneficiarios"
                f"&order=created_at.desc&limit=1",
                headers=HEADERS,
            )
            if r.status_code == 200:
                rows = r.json()
                if rows:
                    return rows[0]
    except Exception as e:
        logger.warning(f"[BORRADOR] error buscando afiliación {telefono}: {e}")
    return None


async def _extraer_datos(historial: list[dict]) -> dict | None:
    if not _ai:
        return None
    conv = _format_conv(historial)
    if len(conv.strip()) < 12:
        return None
    try:
        msg = await _ai.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            messages=[
                {"role": "user", "content": _BORRADOR_PROMPT.format(conversation=conv[-6000:])}
            ],
        )
        text = msg.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        return json.loads(text.strip())
    except Exception as e:
        logger.warning(f"[BORRADOR] extracción Haiku falló: {e}")
        return None


def _campos_borrador(datos: dict) -> dict:
    """Construye el subconjunto de columnas con valores NO vacíos extraídos."""
    out: dict = {}

    def _set(key: str, value):
        if value not in (None, "", [], {}):
            out[key] = value

    _set("first_name", (datos.get("first_name") or "").strip())
    _set("last_name", (datos.get("last_name") or "").strip())
    _set("document_type", datos.get("document_type"))
    _set("document_number", (datos.get("document_number") or "").strip() or None)
    _set("email", (datos.get("email") or "").strip() or None)

    birth_iso = _parse_birth_date(datos.get("birth_date") or datos.get("fecha_nacimiento"))
    if birth_iso:
        out["birth_date"] = birth_iso
        edad = _calc_age(birth_iso)
        if edad is not None:
            out["age"] = edad

    _set("address", datos.get("address"))
    _set("municipality", datos.get("municipality"))
    _set("department", datos.get("department"))

    plan_key = (datos.get("plan") or "").lower().strip()
    if plan_key in PLAN_NAMES:
        out["selected_plan"] = PLAN_NAMES[plan_key]

    beneficiarios = datos.get("beneficiarios") or []
    if beneficiarios:
        out["beneficiarios"] = beneficiarios

    mascotas = datos.get("mascotas") or []
    if mascotas:
        out["has_pet"] = True
        first_pet = mascotas[0]
        out["pet_name"] = first_pet.get("nombre") or ""
        out["pet_type"] = first_pet.get("tipo") or ""
        out["pet_breed"] = first_pet.get("raza") or ""
        out["mercadopago_payment_data"] = {"mascotas": mascotas}

    return out


async def _audit(http: httpx.AsyncClient, aff_id: str, descripcion: str, telefono: str, notas: str):
    try:
        await http.post(
            f"{SUPABASE_URL}/rest/v1/affiliation_audit_log",
            headers=HEADERS,
            json={
                "affiliation_id": aff_id,
                "event_type": "draft_progress",
                "event_category": "lead",
                "description": descripcion,
                "changed_by_email": "whatsapp_bot",
                "changed_by_type": "system",
                "metadata": {"phone": telefono, "notas": notas},
            },
            timeout=5,
        )
    except Exception:
        pass


async def _persistir(telefono: str, datos: dict, existing: dict | None) -> None:
    campos = _campos_borrador(datos)
    if not campos:
        return
    notas = (datos.get("notas") or "")[:300]

    async with httpx.AsyncClient(timeout=15) as http:
        if existing and existing.get("status") == "in_progress":
            # Nutrir el borrador existente: solo escribimos campos con valor nuevo,
            # nunca pisamos un dato ya guardado con un null. (updated_at lo pone
            # automáticamente el trigger de la tabla.)
            r = await http.patch(
                f"{SUPABASE_URL}/rest/v1/b2c_affiliations?id=eq.{existing['id']}",
                headers={**HEADERS, "Prefer": "return=minimal"},
                json=campos,
            )
            if r.status_code in (200, 204):
                logger.info(f"[BORRADOR] nutrido id={existing['id']} ({telefono}) campos={list(campos)}")
                await _audit(http, existing["id"], f"Borrador nutrido: {', '.join(campos)}", telefono, notas)
            else:
                logger.warning(f"[BORRADOR] PATCH falló {r.status_code}: {r.text[:200]}")
            return

        # No hay borrador previo: nace el lead. Requiere nombre + apellido.
        if not (campos.get("first_name") and campos.get("last_name")):
            return
        phone_norm = normalizar_telefono(telefono)
        payload = {
            **campos,
            "phone": phone_norm,
            "country_code": "+57",
            "document_type": campos.get("document_type") or "CC",
            "selected_plan": campos.get("selected_plan") or "",
            "status": "in_progress",
            "is_active": False,
            "payment_status": "pending",
            "current_step": 1,
            "completed_steps": [],
            "consentimientos": {},
            "session_id": f"wa-draft-{datetime.now().strftime('%Y%m%d%H%M%S')}-{phone_norm}",
            "pending_tasks": [{"tipo": "completar_datos", "notas": notas or "Lead en curso desde WhatsApp"}],
        }
        r = await http.post(
            f"{SUPABASE_URL}/rest/v1/b2c_affiliations",
            headers={**HEADERS, "Prefer": "return=representation"},
            json=payload,
        )
        if r.status_code in (200, 201):
            data = r.json()
            aff_id = data[0]["id"] if isinstance(data, list) else data.get("id")
            logger.info(f"[BORRADOR] creado id={aff_id} ({telefono}) {campos.get('first_name')} {campos.get('last_name')}")
            await _audit(http, aff_id, f"Lead creado desde WhatsApp: {', '.join(campos)}", telefono, notas)
        else:
            logger.warning(f"[BORRADOR] INSERT falló {r.status_code}: {r.text[:200]}")


async def actualizar_borrador(telefono: str, historial: list[dict]) -> None:
    """Crea o nutre el borrador de afiliación para este teléfono. Pensada para
    ejecutarse en segundo plano (no bloquea la respuesta al cliente)."""
    if not SUPABASE_URL or not SUPABASE_KEY or not _ai:
        return
    # Solo celulares colombianos reales. Un PSID de Messenger no es teléfono y
    # corrompería el registro / la deduplicación por teléfono.
    if not es_celular_co(telefono):
        return

    lock = _locks.setdefault(telefono, asyncio.Lock())
    if lock.locked():
        # Ya hay una extracción en curso para este número; el siguiente mensaje
        # recogerá los datos acumulados. Evita llamadas Haiku redundantes.
        return

    async with lock:
        try:
            existing = await _buscar_afiliacion(telefono)
            # Si ya es afiliado real (completado), no tocamos nada y ahorramos el Haiku.
            if existing and existing.get("status") == "completed":
                return

            datos = await _extraer_datos(historial)
            if not datos or datos.get("estado") == "ninguna":
                return

            first = (datos.get("first_name") or "").strip()
            last = (datos.get("last_name") or "").strip()
            # Para INICIAR el borrador exigimos nombre y apellido (el celular ya lo
            # tenemos). Si ya existe borrador, lo nutrimos aunque el mensaje actual
            # no repita el nombre.
            if not existing and not (first and last):
                return

            await _persistir(telefono, datos, existing)

            # Forzar que el próximo turno relea el dato fresco desde la base.
            try:
                from agent.brain import invalidar_cache_cliente
                invalidar_cache_cliente(telefono)
            except Exception:
                pass
        except Exception as e:
            logger.warning(f"[BORRADOR] error actualizando borrador {telefono}: {e}")


async def cerrar_borradores(telefono: str, motivo: str = "afiliacion_completada") -> None:
    """Cierra los borradores in_progress de un teléfono una vez existe la
    afiliación real, para no dejar leads huérfanos en el panel."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return
    if not es_celular_co(telefono):
        return
    or_filter = ",".join(f"phone.eq.{p}" for p in _phone_variants(telefono))
    try:
        async with httpx.AsyncClient(timeout=10) as http:
            await http.patch(
                f"{SUPABASE_URL}/rest/v1/b2c_affiliations"
                f"?and=(or({or_filter}),status.eq.in_progress)",
                headers={**HEADERS, "Prefer": "return=minimal"},
                json={"status": "cancelled"},
            )
        try:
            from agent.brain import invalidar_cache_cliente
            invalidar_cache_cliente(telefono)
        except Exception:
            pass
    except Exception as e:
        logger.debug(f"[BORRADOR] cerrar_borradores: {e}")
