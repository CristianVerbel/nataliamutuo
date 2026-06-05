#!/usr/bin/env python3
"""
import_whapi_affiliations.py
Descarga todos los chats de Whapi, detecta afiliaciones completadas
que no estén en Supabase, y las crea automáticamente.

Uso:
  cd whatsapp-bot
  WHAPI_TOKEN=xxx SUPABASE_URL=xxx SUPABASE_KEY=xxx ANTHROPIC_API_KEY=xxx \
    python scripts/import_whapi_affiliations.py
"""

import asyncio
import json
import logging
import os
import sys
from datetime import datetime

import httpx
from anthropic import AsyncAnthropic
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("import-affiliations")

WHAPI_TOKEN   = os.getenv("WHAPI_TOKEN", "")
WHAPI_BASE    = "https://gate.whapi.cloud"
SB_URL        = os.getenv("SUPABASE_URL", "")
SB_KEY        = (os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_KEY") or os.getenv("SUPABASE_ANON_KEY", ""))
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")

HEADERS_WHAPI = {"Authorization": f"Bearer {WHAPI_TOKEN}", "Content-Type": "application/json"}
HEADERS_SB    = {"Authorization": f"Bearer {SB_KEY}", "apikey": SB_KEY, "Content-Type": "application/json", "Prefer": "return=representation"}

client_ai = AsyncAnthropic(api_key=ANTHROPIC_KEY)


# ── Whapi helpers ──────────────────────────────────────────────────────────────

async def whapi_get_chats(http: httpx.AsyncClient, count: int = 200) -> list[dict]:
    """Lista todos los chats individuales (no grupos)."""
    chats = []
    offset = 0
    while True:
        r = await http.get(
            f"{WHAPI_BASE}/chats",
            headers=HEADERS_WHAPI,
            params={"count": 100, "offset": offset},
            timeout=20,
        )
        if r.status_code != 200:
            logger.error(f"Whapi /chats error {r.status_code}: {r.text[:200]}")
            break
        data = r.json()
        batch = data.get("chats", [])
        # Solo chats individuales (no grupos)
        batch = [c for c in batch if not c.get("is_group") and not c.get("id", "").endswith("@g.us")]
        chats.extend(batch)
        if len(batch) < 100 or len(chats) >= count:
            break
        offset += 100
    logger.info(f"Encontrados {len(chats)} chats individuales")
    return chats


async def whapi_get_messages(http: httpx.AsyncClient, chat_id: str, count: int = 100) -> list[dict]:
    """Descarga los últimos N mensajes de un chat."""
    r = await http.get(
        f"{WHAPI_BASE}/messages/list/{chat_id}",
        headers=HEADERS_WHAPI,
        params={"count": count},
        timeout=20,
    )
    if r.status_code != 200:
        logger.warning(f"Whapi mensajes {chat_id} error {r.status_code}")
        return []
    return r.json().get("messages", [])


def build_conversation_text(messages: list[dict]) -> str:
    """Convierte lista de mensajes Whapi en texto legible para el LLM."""
    lines = []
    for m in reversed(messages):  # Whapi devuelve más reciente primero
        sender = "CLIENTE" if not m.get("from_me") else "BOT"
        text = (
            (m.get("text") or {}).get("body")
            or m.get("body")
            or m.get("caption")
            or f"[{m.get('type', 'media')}]"
        )
        ts = m.get("timestamp", "")
        lines.append(f"[{sender}] {text}")
    return "\n".join(lines)


# ── Supabase helpers ───────────────────────────────────────────────────────────

async def affiliation_exists(http: httpx.AsyncClient, phone: str) -> bool:
    """Verifica si ya existe una afiliación para ese teléfono."""
    raw = phone.split("@")[0].replace("+", "").replace(" ", "").replace("-", "")
    local = raw[-10:] if len(raw) >= 10 else raw
    variants = list(set([f"+57{local}", local, f"57{local}", raw]))
    or_filter = ",".join(f"phone.eq.{p}" for p in variants)
    r = await http.get(
        f"{SB_URL}/rest/v1/b2c_affiliations?or=({or_filter})&select=id&limit=1",
        headers={**HEADERS_SB, "Prefer": ""},
        timeout=10,
    )
    if r.status_code == 200 and r.json():
        return True
    return False


async def create_affiliation(http: httpx.AsyncClient, datos: dict) -> dict:
    """Crea la afiliación en Supabase."""
    # Cargar plan real
    plan_key = datos.get("plan", "esencial").lower().replace("familia ", "").replace("familia_", "").strip()
    plan_info = {"name": f"Familia {plan_key.title()}", "price": 25000, "pet_count_included": 0,
                 "adicional_mascota_price": 15000, "adicional_persona_price": 15000, "max_beneficiarios": 6}
    try:
        r = await http.get(
            f"{SB_URL}/rest/v1/plans?plan_key=eq.{plan_key}&select=name,price,pet_count_included,adicional_mascota_price,adicional_persona_price,max_beneficiarios&limit=1",
            headers={**HEADERS_SB, "Prefer": ""},
            timeout=5,
        )
        if r.status_code == 200 and r.json():
            d = r.json()[0]
            plan_info = {k: d.get(k, plan_info[k]) for k in plan_info}
    except Exception as e:
        logger.warning(f"No se pudo cargar plan {plan_key}: {e}")

    beneficiarios = datos.get("beneficiarios", [])
    mascotas = datos.get("mascotas", [])
    extra_pets = max(0, len(mascotas) - plan_info["pet_count_included"])
    extra_people = max(0, len(beneficiarios) - plan_info["max_beneficiarios"])
    adjusted_price = plan_info["price"] + extra_pets * plan_info["adicional_mascota_price"] + extra_people * plan_info["adicional_persona_price"]

    first_pet = mascotas[0] if mascotas else None
    payload = {
        "first_name": datos.get("first_name", ""),
        "last_name": datos.get("last_name", ""),
        "document_type": datos.get("document_type", "CC"),
        "document_number": datos.get("document_number", ""),
        "email": datos.get("email", ""),
        "phone": datos.get("phone", ""),
        "country_code": "+57",
        "address": datos.get("address", ""),
        "municipality": datos.get("municipality", ""),
        "department": datos.get("department", ""),
        "selected_plan": plan_info["name"],
        "beneficiarios": beneficiarios,
        "has_pet": bool(first_pet),
        "pet_name": first_pet["nombre"] if first_pet else "",
        "pet_type": first_pet["tipo"] if first_pet else "",
        "pet_breed": first_pet["raza"] if first_pet else "",
        "pet_age": first_pet.get("edad", 0) if first_pet else 0,
        "additional_data": {
            "mascotas": mascotas,
            "extra_pets_count": extra_pets,
            "extra_people_count": extra_people,
            "base_plan_price": plan_info["price"],
            "adjusted_price": adjusted_price,
            "import_source": "whapi_bulk_import",
            "imported_at": datetime.now().isoformat(),
        },
        "consentimientos": {
            "dataTreatment": True, "creditBureaus": True,
            "contractAccepted": True,
            "acceptanceTimestamp": datetime.now().isoformat(),
        },
        "payment_status": "pending",
        "status": "completed",
        "is_active": True,
        "is_assisted_sale": False,
        "current_step": 5,
        "completed_steps": [1, 2, 3, 4],
        "session_id": f"wa-import-{datetime.now().strftime('%Y%m%d%H%M%S')}-{datos.get('document_number', '')}",
    }

    r = await http.post(f"{SB_URL}/rest/v1/b2c_affiliations", headers=HEADERS_SB, json=payload, timeout=15)
    if r.status_code in (200, 201):
        result = r.json()
        aff_id = result[0]["id"] if isinstance(result, list) else result.get("id")
        # Log en audit
        try:
            await http.post(
                f"{SB_URL}/rest/v1/affiliation_audit_log",
                headers=HEADERS_SB,
                json={
                    "affiliation_id": aff_id,
                    "event_type": "affiliation_imported",
                    "event_category": "import",
                    "description": "Afiliación importada retroactivamente desde historial de Whapi",
                    "changed_by_email": "whapi_bulk_import",
                    "changed_by_type": "system",
                    "metadata": {"import_source": "whapi_bulk_import", "phone": datos.get("phone")},
                },
                timeout=5,
            )
        except Exception:
            pass
        return {"success": True, "affiliation_id": aff_id}
    else:
        return {"success": False, "error": r.text[:300]}


# ── AI analysis ───────────────────────────────────────────────────────────────

EXTRACTION_PROMPT = """Analiza esta conversación de WhatsApp entre un bot de ventas de Mutuo (Club de Bienestar Familiar) y un cliente.

Determina si el cliente COMPLETÓ una afiliación (dio todos sus datos y aceptó el plan).

Si sí completó la afiliación, extrae los datos en JSON. Si no completó, responde con {"completada": false}.

Campos a extraer (cuando estén disponibles):
- completada: true/false
- first_name: primer nombre del titular
- last_name: apellido(s) del titular
- document_type: "CC" (por defecto) o "CE"
- document_number: número de cédula
- email: correo electrónico
- phone: teléfono (el del chat, sin @c.us)
- address: dirección
- municipality: ciudad/municipio
- department: departamento
- plan: "esencial", "plus" o "total"
- beneficiarios: array de objetos con {primerNombre, apellido, parentesco, fechaNac (DD/MM/AAAA si la dieron)}
- mascotas: array de {nombre, tipo, raza, edad} (solo si las mencionaron)

Reglas:
- Solo marca completada=true si el cliente dio explícitamente nombre, cédula, email Y eligió un plan
- Si faltan datos clave (cédula o email), marca completada=false
- El plan "esencial" cuesta $25.000, "plus" $29.900, "total" $38.000
- Responde SOLO con el JSON, sin texto adicional

CONVERSACIÓN:
{conversation}
"""

async def analyze_conversation(conversation_text: str, phone: str) -> dict | None:
    """Usa Claude para extraer datos de afiliación de la conversación."""
    if len(conversation_text) < 200:
        return None  # Conversación muy corta, no vale la pena

    try:
        msg = await client_ai.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            messages=[{
                "role": "user",
                "content": EXTRACTION_PROMPT.format(conversation=conversation_text[-6000:])
            }]
        )
        text = msg.content[0].text.strip()
        # Limpiar markdown si lo hay
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        data = json.loads(text)
        if data.get("completada"):
            data["phone"] = phone.split("@")[0]
            return data
        return None
    except Exception as e:
        logger.warning(f"Error analizando {phone}: {e}")
        return None


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    # Validar config
    missing = [k for k, v in {"WHAPI_TOKEN": WHAPI_TOKEN, "SUPABASE_URL": SB_URL,
                               "SUPABASE_KEY": SB_KEY, "ANTHROPIC_API_KEY": ANTHROPIC_KEY}.items() if not v]
    if missing:
        logger.error(f"Faltan variables de entorno: {', '.join(missing)}")
        sys.exit(1)

    created = 0
    skipped_exists = 0
    skipped_incomplete = 0
    errors = 0

    async with httpx.AsyncClient() as http:
        chats = await whapi_get_chats(http, count=500)

        for i, chat in enumerate(chats):
            chat_id = chat.get("id", "")
            name = chat.get("name", chat_id)
            logger.info(f"[{i+1}/{len(chats)}] Procesando {name} ({chat_id})")

            # Descargar mensajes
            messages = await whapi_get_messages(http, chat_id, count=150)
            if not messages:
                skipped_incomplete += 1
                continue

            # Convertir a texto
            conversation = build_conversation_text(messages)

            # Verificar si ya existe afiliación
            if await affiliation_exists(http, chat_id):
                logger.info(f"  ↳ Ya tiene afiliación — omitiendo")
                skipped_exists += 1
                continue

            # Analizar con IA
            datos = await analyze_conversation(conversation, chat_id)
            if not datos:
                logger.info(f"  ↳ Sin afiliación completada en el chat")
                skipped_incomplete += 1
                continue

            logger.info(f"  ↳ Afiliación detectada: {datos.get('first_name')} {datos.get('last_name')} — {datos.get('plan')} — doc {datos.get('document_number')}")

            # Crear afiliación
            result = await create_affiliation(http, datos)
            if result["success"]:
                logger.info(f"  ✅ CREADA — ID: {result['affiliation_id']}")
                created += 1
            else:
                logger.error(f"  ❌ ERROR creando: {result['error']}")
                errors += 1

            # Pequeña pausa para no saturar APIs
            await asyncio.sleep(0.5)

    logger.info("=" * 50)
    logger.info(f"RESUMEN:")
    logger.info(f"  Afiliaciones creadas:    {created}")
    logger.info(f"  Ya existían en sistema:  {skipped_exists}")
    logger.info(f"  Sin afiliación completa: {skipped_incomplete}")
    logger.info(f"  Errores:                 {errors}")
    logger.info("=" * 50)


if __name__ == "__main__":
    asyncio.run(main())
