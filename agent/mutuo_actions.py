# agent/mutuo_actions.py — Acciones del bot en Supabase
# Desarrollado por Catalítico LLC para Mutuo Fintech S.A.S.
#
# Herramientas que el bot puede ejecutar durante la conversación:
# - Crear afiliación en b2c_affiliations
# - Generar link de pago MercadoPago
# - Enviar contrato por email + WhatsApp
# - Crear ticket de cancelación con radicado
# - Consultar estado de cuenta

import os
import re
import logging
import httpx
import math
from datetime import datetime, date

logger = logging.getLogger("mutuo-bot")

_MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio",
          "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"]


def _nombre_periodo(month, year) -> str:
    """Devuelve 'mayo 2026' a partir de month/year. Cadena vacia si no es valido."""
    try:
        m = int(month)
        if 1 <= m <= 12:
            return f"{_MESES[m - 1]} {int(year)}"
    except (TypeError, ValueError):
        pass
    return ""


def _parse_birth_date(value) -> str | None:
    """Acepta DD/MM/YYYY, DD-MM-YYYY, DD.MM.YYYY o YYYY-MM-DD y devuelve ISO YYYY-MM-DD."""
    if not value:
        return None
    s = str(value).strip()
    if not s:
        return None
    # Formato ISO ya válido
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", s)
    if m:
        try:
            datetime.strptime(s, "%Y-%m-%d")
            return s
        except ValueError:
            return None
    # DD/MM/YYYY o DD-MM-YYYY o DD.MM.YYYY
    m = re.match(r"^(\d{1,2})[\/\-\.](\d{1,2})[\/\-\.](\d{4})$", s)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            return date(y, mo, d).isoformat()
        except ValueError:
            return None
    return None


def _calc_age(birth_iso: str | None) -> int | None:
    if not birth_iso:
        return None
    try:
        bd = datetime.strptime(birth_iso, "%Y-%m-%d").date()
    except ValueError:
        return None
    today = date.today()
    years = today.year - bd.year - ((today.month, today.day) < (bd.month, bd.day))
    return years if years >= 0 else None

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "") or os.getenv("SUPABASE_KEY", "") or os.getenv("SUPABASE_ANON_KEY", "")
MP_ACCESS_TOKEN = os.getenv("MERCADOPAGO_ACCESS_TOKEN", "")


# ── Normalización y validación ────────────────────────────────────────────────

def normalizar_telefono(phone: str) -> str:
    """Normaliza a +57XXXXXXXXXX. Función única para todo el sistema."""
    clean = re.sub(r"[^\d]", "", str(phone).split("@")[0])
    if len(clean) == 10 and clean.startswith("3"):
        return f"+57{clean}"
    if len(clean) == 12 and clean.startswith("57"):
        return f"+{clean}"
    if len(clean) == 13 and clean.startswith("057"):
        return f"+{clean[1:]}"
    return f"+57{clean[-10:]}" if len(clean) >= 10 else phone


def es_celular_co(phone: str) -> bool:
    """True si es un celular colombiano real. Evita tratar un PSID de Messenger
    (número largo de Facebook) como teléfono y corromper el registro."""
    clean = re.sub(r"[^\d]", "", str(phone).split("@")[0])
    if len(clean) == 10 and clean.startswith("3"):
        return True
    if len(clean) == 12 and clean.startswith("573"):
        return True
    return False


def _phone_variants(phone: str) -> list[str]:
    canonical = normalizar_telefono(phone)
    local = re.sub(r"[^\d]", "", canonical)[-10:]
    return list(set([canonical, local, f"57{local}", re.sub(r"[^\d]", "", canonical)]))


def validar_email(email: str) -> bool:
    return bool(re.match(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$", email or ""))


def validar_cedula(doc: str) -> bool:
    digits = re.sub(r"[^\d]", "", doc or "")
    return 6 <= len(digits) <= 11 and not (len(digits) == 10 and digits.startswith("3"))


HEADERS = {
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "apikey": SUPABASE_KEY,
    "Content-Type": "application/json",
    "Prefer": "return=representation",
}


async def crear_afiliacion(datos: dict) -> dict:
    """
    Crea una afiliación completa en b2c_affiliations.
    datos debe incluir: first_name, last_name, document_number, email, phone,
    selected_plan, address, municipality, department, beneficiarios[]
    """
    try:
        # Cargar precios reales de la DB
        plan_key = datos.get("plan", "esencial").lower()
        plan_info = {"name": f"Familia {plan_key.title()}", "price": 25000, "pet_count_included": 0, "adicional_mascota_price": 15000, "adicional_persona_price": 15000, "max_beneficiarios": 6}

        try:
            async with httpx.AsyncClient(timeout=5) as c:
                r = await c.get(
                    f"{SUPABASE_URL}/rest/v1/plans?plan_key=eq.{plan_key}&select=name,price,pet_count_included,adicional_mascota_price,adicional_persona_price,max_beneficiarios&limit=1",
                    headers={**HEADERS, "Prefer": ""},
                )
                if r.status_code == 200:
                    data = r.json()
                    if data:
                        plan_info = {
                            "name": data[0]["name"],
                            "price": data[0]["price"],
                            "pet_count_included": data[0].get("pet_count_included", 0),
                            "adicional_mascota_price": data[0].get("adicional_mascota_price", 15000),
                            "adicional_persona_price": data[0].get("adicional_persona_price", 15000),
                            "max_beneficiarios": data[0].get("max_beneficiarios", 6),
                        }
                        logger.info(f"[PLAN] {plan_key} → {plan_info['name']} ${plan_info['price']}")
                    else:
                        logger.warning(f"[PLAN] plan_key={plan_key} no encontrado en DB, usando fallback")
        except Exception as e:
            logger.error(f"[PLAN] Error cargando plan: {e}")

        # Si el "teléfono" recibido no es un celular real (ej. PSID de Messenger),
        # no lo guardamos como teléfono: corrompe el registro y rompe el envío de
        # contrato por WhatsApp y la detección de duplicados por teléfono.
        raw_phone = str(datos.get("phone", "")).strip()
        if raw_phone and not es_celular_co(raw_phone):
            logger.info(f"[AFILIACION] Teléfono '{raw_phone}' no es celular CO (¿PSID?); se omite.")
            datos = {**datos, "messenger_psid": datos.get("messenger_psid") or raw_phone, "phone": ""}

        # Validar campos obligatorios. El teléfono es obligatorio salvo en
        # canales sin teléfono (Messenger), donde el contacto es por el chat.
        required = ["first_name", "last_name", "document_number", "email"]
        if not datos.get("messenger_psid"):
            required.append("phone")
        missing = [f for f in required if not datos.get(f)]
        if missing:
            logger.error(f"[AFILIACION] Campos faltantes: {missing}")
            return {"success": False, "error": f"Faltan campos obligatorios: {', '.join(missing)}"}

        if not validar_email(datos.get("email", "")):
            logger.error(f"[AFILIACION] Email inválido: {datos.get('email')}")
            return {"success": False, "error": f"Email inválido: {datos.get('email')}. Pide al cliente que lo corrija."}

        doc_type = datos.get("document_type", "CC")
        if doc_type == "CC" and not validar_cedula(datos.get("document_number", "")):
            logger.error(f"[AFILIACION] Cédula inválida: {datos.get('document_number')}")
            return {"success": False, "error": f"Número de cédula inválido: {datos.get('document_number')}"}

        # Bloquear duplicados de titular: una persona no puede afiliarse dos
        # veces. Si ya tiene afiliación activa, redirigir a recuperar cuenta.
        try:
            async with httpx.AsyncClient(timeout=8) as dup_client:
                dup_r = await dup_client.post(
                    f"{SUPABASE_URL}/rest/v1/rpc/find_active_titular",
                    headers={**HEADERS, "Prefer": ""},
                    json={
                        "p_document_number": datos.get("document_number") or None,
                        "p_email": datos.get("email") or None,
                        "p_phone": datos.get("phone") or None,
                    },
                )
                if dup_r.status_code == 200:
                    rows = dup_r.json() or []
                    existing = rows[0] if isinstance(rows, list) and rows else None
                    if existing:
                        logger.info(
                            f"[AFILIACION] Duplicado detectado por {existing.get('matched_field')} → "
                            f"{existing.get('first_name')} (id={existing.get('id')})"
                        )
                        from urllib.parse import quote
                        cedula_q = quote(existing.get("document_number") or "")
                        email_q = quote(existing.get("email") or "")
                        recovery_link = (
                            f"https://ventas.mutuo.la/auth?cedula={cedula_q}&email={email_q}"
                        )
                        return {
                            "success": False,
                            "duplicate": True,
                            "matched_field": existing.get("matched_field"),
                            "first_name": existing.get("first_name"),
                            "recovery_link": recovery_link,
                            "error": "Ya existe una afiliación activa para este titular.",
                        }
                else:
                    logger.warning(
                        f"[AFILIACION] find_active_titular respondió {dup_r.status_code}: {dup_r.text[:200]}"
                    )
        except Exception as e:
            # Fail-open: si la verificación falla por red, no bloqueamos a un cliente legítimo
            logger.warning(f"[AFILIACION] Error verificando duplicado: {e}")

        # Procesar mascotas (soporta array o formato legacy de 1 mascota)
        mascotas = datos.get("mascotas", [])
        if not mascotas and datos.get("pet_name"):
            mascotas = [{"nombre": datos["pet_name"], "tipo": datos.get("pet_type", ""), "raza": datos.get("pet_breed", ""), "edad": datos.get("pet_age", 0)}]

        # Primera mascota va en los campos legacy del DB
        first_pet = mascotas[0] if mascotas else None
        additional_pets = mascotas[1:] if len(mascotas) > 1 else []

        # Calcular precio ajustado con adicionales
        base_price = plan_info["price"]
        pets_included = plan_info["pet_count_included"]
        extra_pets_count = max(0, len(mascotas) - pets_included)
        pet_surcharge = extra_pets_count * plan_info["adicional_mascota_price"]

        beneficiarios_raw = datos.get("beneficiarios", [])
        beneficiarios = []
        for b in beneficiarios_raw:
            b_iso = _parse_birth_date(b.get("fechaNac") or b.get("fecha_nacimiento"))
            if b_iso:
                b = {**b, "fechaNac": b_iso, "fecha_nacimiento": b_iso}
                edad = _calc_age(b_iso)
                if edad is not None and not b.get("edad"):
                    b["edad"] = edad
            beneficiarios.append(b)

        max_ben = plan_info["max_beneficiarios"]
        extra_people_count = max(0, len(beneficiarios) - max_ben)
        people_surcharge = extra_people_count * plan_info["adicional_persona_price"]

        adjusted_price = base_price + pet_surcharge + people_surcharge

        if adjusted_price != base_price:
            logger.info(f"[PRECIO AJUSTADO] base={base_price} + mascotas_extra={pet_surcharge} + personas_extra={people_surcharge} = {adjusted_price}")

        # Normalizar fecha de nacimiento del titular (acepta DD/MM/YYYY)
        birth_iso = _parse_birth_date(datos.get("birth_date") or datos.get("fecha_nacimiento"))
        age_value = _calc_age(birth_iso) if birth_iso else None
        if not birth_iso:
            logger.warning(f"[AFILIACION] Sin fecha de nacimiento del titular para {datos.get('document_number','?')} — se requiere para cobertura legal")

        payload = {
            "first_name": datos.get("first_name", ""),
            "last_name": datos.get("last_name", ""),
            "document_type": datos.get("document_type", "CC"),
            "document_number": datos.get("document_number", ""),
            "email": datos.get("email", ""),
            "phone": normalizar_telefono(raw_phone) if es_celular_co(raw_phone) else "",
            "country_code": "+57",
            "birth_date": birth_iso,
            "age": age_value,
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
            "mercadopago_payment_data": {
                "mascotas": mascotas,
                "additional_pets": [{"nombre": p["nombre"], "tipo": p["tipo"], "raza": p["raza"], "edad": p.get("edad", 0)} for p in additional_pets],
                "extra_pets_count": extra_pets_count,
                "extra_people_count": extra_people_count,
                "pet_surcharge": pet_surcharge,
                "people_surcharge": people_surcharge,
                "base_plan_price": base_price,
                "adjusted_price": adjusted_price,
            },
            "consentimientos": {
                "dataTreatment": True,
                "creditBureaus": True,
                "contractAccepted": True,
                "acceptanceTimestamp": datetime.now().isoformat(),
            },
            "payment_status": "pending",
            "status": "completed",
            "is_active": True,
            "is_assisted_sale": False,
            "current_step": 5,
            "completed_steps": [1, 2, 3, 4],
            "session_id": f"wa-{datetime.now().strftime('%Y%m%d%H%M%S')}-{datos.get('document_number', '')}",
        }

        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"{SUPABASE_URL}/rest/v1/b2c_affiliations",
                headers=HEADERS,
                json=payload,
            )

            if r.status_code in (200, 201):
                result = r.json()
                # Fix crítico: evitar IndexError si Supabase retorna lista vacía
                if isinstance(result, list):
                    if not result:
                        logger.error("[AFILIACION] Supabase retornó lista vacía tras 200/201")
                        return {"success": False, "error": "Supabase no retornó el registro creado"}
                    aff_id = result[0].get("id")
                elif isinstance(result, dict):
                    aff_id = result.get("id")
                else:
                    aff_id = None
                if not aff_id:
                    logger.error(f"[AFILIACION] ID no encontrado en respuesta: {str(result)[:200]}")
                    return {"success": False, "error": "No se obtuvo ID de afiliación"}
                logger.info(f"[AFILIACIÓN CREADA] {datos.get('first_name')} {datos.get('last_name')} - ID: {aff_id}")

                # Vincular conversación de WhatsApp con la afiliación (soporte legal del chat).
                # GARANTÍA: aseguramos que la conversación EXISTA en Supabase y que el
                # historial esté persistido (vuelca el respaldo en RAM si la sync en vivo
                # falló), para que el chat SIEMPRE aparezca en el perfil del afiliado.
                phone_norm = re.sub(r"[^\d]", "", normalizar_telefono(datos.get("phone", "")))
                if phone_norm:
                    try:
                        from agent.memory import asegurar_historial_en_supabase
                        await asegurar_historial_en_supabase(datos.get("phone", ""))
                    except Exception as e:
                        logger.warning(f"[CRM] asegurar_historial falló: {e}")
                    try:
                        # Enlazamos por phone y phone_number, con y sin '+', y fijamos
                        # affiliation_id + sale_id (vínculo canónico del panel).
                        await client.patch(
                            f"{SUPABASE_URL}/rest/v1/whatsapp_conversations"
                            f"?or=(phone.eq.{phone_norm},phone.eq.+{phone_norm},"
                            f"phone_number.eq.{phone_norm},phone_number.eq.+{phone_norm})",
                            headers={**HEADERS, "Prefer": "return=minimal"},
                            json={"affiliation_id": aff_id, "sale_id": aff_id, "status": "convertido"},
                        )
                        logger.info(f"[CRM] Conversación WhatsApp vinculada a afiliación {aff_id} (phone={phone_norm})")
                    except Exception as e:
                        logger.warning(f"[CRM] Error vinculando conversación: {e}")

                # Check for incomplete beneficiary data and create alert
                beneficiarios = datos.get("beneficiarios", [])
                incomplete = [b for b in beneficiarios if not b.get("fechaNac") and not b.get("fecha_nacimiento")]
                if incomplete:
                    names = ", ".join(b.get("primerNombre", b.get("nombre", "?")) for b in incomplete)
                    try:
                        await client.post(
                            f"{SUPABASE_URL}/rest/v1/affiliation_audit_log",
                            headers=HEADERS,
                            json={
                                "affiliation_id": aff_id,
                                "event_type": "incomplete_beneficiary_data",
                                "event_category": "alert",
                                "description": f"Beneficiarios con datos incompletos (sin fecha de nacimiento): {names}. El cliente debe completar desde su cuenta.",
                                "changed_by_email": "whatsapp_bot",
                                "changed_by_type": "system",
                                "metadata": {"incomplete_beneficiaries": [b.get("primerNombre", "") for b in incomplete]},
                            },
                        )
                    except Exception as e:
                        logger.warning(f"Error registrando alerta de beneficiarios incompletos: {e}")

                # Envío de bienvenida + contrato con reintentos
                import asyncio as _aio
                contrato_enviado = False
                for _intento in range(3):
                    try:
                        rc = await client.post(
                            f"{SUPABASE_URL}/functions/v1/send-client-welcome-all",
                            headers={"Authorization": f"Bearer {SUPABASE_KEY}", "Content-Type": "application/json"},
                            json={"affiliationId": aff_id},
                            timeout=20,
                        )
                        if rc.status_code < 400:
                            contrato_enviado = True
                            logger.info(f"[CONTRATO ENVIADO] {aff_id} (intento {_intento+1})")
                            break
                        logger.warning(f"[CONTRATO] intento {_intento+1} status={rc.status_code}")
                    except Exception as e:
                        logger.warning(f"[CONTRATO] intento {_intento+1} error: {e}")
                    if _intento < 2:
                        await _aio.sleep(2 ** _intento)
                if not contrato_enviado:
                    logger.error(f"[CONTRATO] No se pudo enviar tras 3 intentos para {aff_id}")
                    # Dejar rastro para que el equipo lo reenvie desde el panel en vez
                    # de que el cliente se quede sin contrato silenciosamente.
                    try:
                        await client.post(
                            f"{SUPABASE_URL}/rest/v1/affiliation_audit_log",
                            headers=HEADERS,
                            json={
                                "affiliation_id": aff_id,
                                "event_type": "contract_send_failed",
                                "event_category": "alert",
                                "description": "No se pudo enviar el contrato/bienvenida al cliente tras 3 intentos. Reenviar manualmente desde el panel.",
                                "changed_by_email": "whatsapp_bot",
                                "changed_by_type": "system",
                            },
                        )
                    except Exception as e:
                        logger.warning(f"[CONTRATO] No se pudo registrar alerta de envio fallido: {e}")

                return {"success": True, "affiliation_id": aff_id, "plan": plan_info["name"], "price": adjusted_price, "contrato_enviado": contrato_enviado}
            else:
                logger.error(f"Error creando afiliación: {r.status_code} - {r.text[:300]}")
                return {"success": False, "error": r.text[:200]}

    except Exception as e:
        logger.error(f"Excepción creando afiliación: {e}")
        return {"success": False, "error": str(e)}


async def generar_link_pago(affiliation_id: str, amount: int = 25000, name: str = "", email: str = "", doc_number: str = "") -> dict:
    """Genera un link de pago MercadoPago para una afiliación.

    El link debe quedar asociado al correo REAL del cliente. Cuando quien llama no
    pasa el correo (caso típico de las consultas de estado/cédula, que solo enviaban
    id+monto+nombre), lo buscamos en la afiliación. Si aun así no hay un correo
    válido, se OMITE `payer.email` para que el cliente escriba el suyo en el checkout
    de MercadoPago. Nunca prellenamos un correo de Mutuo (ej. cliente@mutuo.la): ese
    era justo el bug que dejaba el link "con el correo de Mutuo y no el del cliente",
    impidiéndole pagar.
    """
    if not MP_ACCESS_TOKEN:
        return {"success": False, "error": "MercadoPago no configurado"}

    # Enriquecer correo/documento/nombre desde la afiliación cuando falten, para que
    # el link use siempre los datos reales del cliente y no un placeholder.
    if not (email and doc_number and name):
        try:
            async with httpx.AsyncClient(timeout=10) as _c:
                det = await _c.get(
                    f"{SUPABASE_URL}/rest/v1/b2c_affiliations?id=eq.{affiliation_id}"
                    f"&select=email,document_number,first_name,last_name&limit=1",
                    headers=HEADERS,
                )
            if det.status_code == 200 and det.json():
                row = det.json()[0]
                email = email or (row.get("email") or "")
                doc_number = doc_number or (row.get("document_number") or "")
                if not name:
                    name = f"{row.get('first_name') or ''} {row.get('last_name') or ''}".strip()
        except Exception as e:
            logger.warning(f"[LINK PAGO] No se pudo enriquecer datos del cliente {affiliation_id}: {e}")

    email = (email or "").strip()
    if email and not validar_email(email):
        logger.warning(f"[LINK PAGO] Correo inválido para {affiliation_id} ({email!r}); se omite payer.email")
        email = ""

    # Construir el payer solo con los datos que realmente tenemos. Si no hay correo
    # válido, no enviamos `email` y MercadoPago le pedirá el suyo al cliente.
    payer: dict = {}
    if name:
        payer["name"] = name
    if email:
        payer["email"] = email
    if doc_number:
        payer["identification"] = {"type": "CC", "number": str(doc_number)}

    try:
        preference = {
            "items": [{
                "title": "Mutuo - Club de Bienestar Familiar",
                "description": "Afiliación mensual Mutuo",
                "quantity": 1,
                "unit_price": amount,
                "currency_id": "COP",
            }],
            "payer": payer,
            "back_urls": {
                "success": "https://ventas.mutuo.la/recaudo?status=success",
                "failure": "https://ventas.mutuo.la/recaudo?status=failure",
                "pending": "https://ventas.mutuo.la/recaudo?status=pending",
            },
            "auto_return": "approved",
            "external_reference": affiliation_id,
            "notification_url": f"{SUPABASE_URL}/functions/v1/mercadopago-webhook",
            "statement_descriptor": "MUTUO",
        }

        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                "https://api.mercadopago.com/checkout/preferences",
                headers={"Authorization": f"Bearer {MP_ACCESS_TOKEN}", "Content-Type": "application/json"},
                json=preference,
            )

            if r.status_code in (200, 201):
                data = r.json()
                link = data.get("init_point", "")
                logger.info(f"[LINK PAGO] {affiliation_id} → {link}")
                return {"success": True, "payment_link": link}
            else:
                logger.error(f"Error MP: {r.status_code} - {r.text[:200]}")
                return {"success": False, "error": "Error generando link de pago"}

    except Exception as e:
        logger.error(f"Excepción MP: {e}")
        return {"success": False, "error": str(e)}


async def consultar_estado_cuenta(phone: str) -> dict:
    """Consulta el estado de cuenta de un afiliado por teléfono."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            phone_variants = _phone_variants(phone)
            or_filter = ",".join(f"phone.eq.{p}" for p in phone_variants)

            r = await client.get(
                f"{SUPABASE_URL}/rest/v1/b2c_affiliations?or=({or_filter})&select=id,first_name,last_name,selected_plan,selected_plan_price,tarifa_personalizada,canal,payment_status,is_active,payment_date,created_at&order=created_at.desc&limit=1",
                headers=HEADERS,
            )
            if r.status_code == 200:
                data = r.json()
                if data:
                    aff = data[0]
                    txr = await client.get(
                        f"{SUPABASE_URL}/rest/v1/payment_transactions?affiliation_id=eq.{aff['id']}&payment_status=in.(pending,overdue)&select=amount,month_applied,year_applied",
                        headers=HEADERS,
                    )
                    txs = txr.json() if txr.status_code == 200 else []

                    paid_r = await client.get(
                        f"{SUPABASE_URL}/rest/v1/payment_transactions?affiliation_id=eq.{aff['id']}&payment_status=eq.paid"
                        f"&select=month_applied,year_applied&order=year_applied.desc,month_applied.desc",
                        headers=HEADERS,
                    )
                    paid_txs = paid_r.json() if paid_r.status_code == 200 else []

                    total_deuda = sum(t.get("amount", 0) for t in txs)
                    cuotas_pendientes = len(txs)

                    # Periodos exactos pagos y pendientes, para que el bot informe meses
                    # reales y no afirme cosas como "tu pago corresponde a junio" cuando
                    # en realidad solo pago mayo.
                    ultimo_periodo_pagado = ""
                    if paid_txs:
                        p = paid_txs[0]
                        ultimo_periodo_pagado = _nombre_periodo(p.get("month_applied"), p.get("year_applied"))
                    meses_pendientes = [
                        _nombre_periodo(t.get("month_applied"), t.get("year_applied"))
                        for t in sorted(txs, key=lambda x: (x.get("year_applied") or 0, x.get("month_applied") or 0))
                    ]
                    meses_pendientes = [m for m in meses_pendientes if m]

                    # Tarifa mensual real: precio efectivo sincronizado > tarifa
                    # personalizada > precio del plan > default. Se calcula siempre
                    # para poder generar links de pago anticipado a clientes que
                    # estan al dia y quieren adelantar una cuota.
                    monthly_fee = aff.get("selected_plan_price") or aff.get("tarifa_personalizada") or 0
                    if not monthly_fee:
                        monthly_fee = 24900
                        try:
                            plan_raw = (aff.get("selected_plan") or "").lower()
                            plan_key = plan_raw.replace("familia ", "").replace("familia_", "").strip()
                            if plan_key:
                                pr = await client.get(
                                    f"{SUPABASE_URL}/rest/v1/plans?plan_key=eq.{plan_key}&select=price&limit=1",
                                    headers=HEADERS,
                                )
                                if pr.status_code == 200 and pr.json():
                                    monthly_fee = pr.json()[0].get("price", 24900)
                        except Exception:
                            pass

                    try:
                        created = datetime.fromisoformat(aff["created_at"].replace("Z", "+00:00"))
                        dias_desde_afiliacion = (datetime.now(created.tzinfo) - created).days
                    except Exception:
                        dias_desde_afiliacion = 999

                    if cuotas_pendientes == 0 and len(paid_txs) == 0 and aff.get("payment_status") != "paid":
                        if dias_desde_afiliacion >= 30:
                            months_since = max(1, dias_desde_afiliacion // 30)
                            total_deuda = monthly_fee * months_since
                            cuotas_pendientes = months_since

                    # Primer ciclo en curso: el primer pago está PENDIENTE, no en
                    # mora. No reportamos deuda vencida; el mensaje debe ser de
                    # activacion/bienvenida, no de cobro de cuota vencida.
                    primer_pago_pendiente = (
                        dias_desde_afiliacion < 30
                        and len(paid_txs) == 0
                        and aff.get("payment_status") != "paid"
                    )
                    if primer_pago_pendiente:
                        total_deuda = 0
                        cuotas_pendientes = 0
                        meses_pendientes = []

                    return {
                        "success": True,
                        "found": True,
                        "name": f"{aff['first_name']} {aff['last_name']}",
                        "plan": aff["selected_plan"],
                        "payment_status": aff["payment_status"],
                        "is_active": aff["is_active"],
                        "total_deuda": total_deuda,
                        "cuotas_pendientes": cuotas_pendientes,
                        "tarifa": monthly_fee,
                        "canal": aff.get("canal"),
                        "ultimo_periodo_pagado": ultimo_periodo_pagado,
                        "meses_pendientes": meses_pendientes,
                        "primer_pago_pendiente": primer_pago_pendiente,
                        "affiliation_id": aff["id"],
                    }

        return {"success": True, "found": False}

    except Exception as e:
        logger.error(f"Error consultando estado: {e}")
        return {"success": False, "error": str(e)}


async def reenviar_recibo(phone: str) -> dict:
    """
    Reenvía el recibo de caja (comprobante de pago) por WhatsApp al afiliado.

    Lo usa el bot cuando un cliente que YA pagó pide su comprobante/recibo. Busca
    la afiliación por teléfono e invoca la edge function `send-payment-receipt`
    con force=true para reenviarlo aunque ya se haya generado antes.

    Devuelve:
      - {success, sent, name}                     → recibo enviado
      - {success, sent: False, reason}            → no había pago registrado aún
      - {success: True, found: False}             → no se encontró la afiliación
      - {success: False, error}                   → error técnico
    """
    try:
        estado = await consultar_estado_cuenta(phone)
        if not estado.get("found"):
            return {"success": True, "found": False}

        affiliation_id = estado["affiliation_id"]

        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(
                f"{SUPABASE_URL}/functions/v1/send-payment-receipt",
                headers={"Authorization": f"Bearer {SUPABASE_KEY}", "Content-Type": "application/json"},
                json={"affiliation_id": affiliation_id, "source": "manual", "force": True},
            )

        if r.status_code >= 400:
            logger.error(f"[RECIBO] send-payment-receipt {r.status_code}: {r.text[:200]}")
            return {"success": False, "error": f"HTTP {r.status_code}"}

        data = r.json() if r.text else {}
        # Sin pago registrado todavía: el webhook de MP aún no reflejó el pago.
        if data.get("skipped") == "sin_monto":
            logger.info(f"[RECIBO] {affiliation_id} sin pago registrado aún — no se reenvía")
            return {"success": True, "sent": False, "reason": "sin_pago", "name": estado.get("name", "")}
        if data.get("skipped") == "sin_telefono":
            return {"success": True, "sent": False, "reason": "sin_telefono", "name": estado.get("name", "")}

        if data.get("success"):
            logger.info(f"[RECIBO REENVIADO] {affiliation_id} → {data.get('recibo')} ({data.get('provider')})")
            return {"success": True, "sent": True, "name": estado.get("name", ""), "recibo": data.get("recibo")}

        return {"success": False, "error": data.get("error", "Respuesta inesperada")}

    except Exception as e:
        logger.error(f"Error reenviando recibo: {e}")
        return {"success": False, "error": str(e)}


async def crear_ticket_cancelacion(phone: str, reason: str, retention_attempts: int = 0, cedula: str = "") -> dict:
    """
    Registra una SOLICITUD de cancelación (flujo híbrido). NO desactiva la cuenta:
    eso lo hace el admin al darle trámite al radicado.

    - Crea un radicado en cancellation_requests (estado 'pendiente')
    - Frena cobros futuros: cancela payment_transactions pendientes/vencidas
    - Marca la afiliación como pending_cancellation = true (sigue activa)
    - Registra en payment_portfolio_history y affiliation_audit_log (historial)
    - Dispara alerta por email a todos los admins

    Funciona aunque el cliente NO haya pagado: la afiliación existe en el sistema,
    se renueva sola y puede generar cobros/correos. El radicado es lo único que la
    detiene. Si no aparece por teléfono (p.ej. número mal guardado), cae a cédula.
    """
    try:
        estado = await consultar_estado_cuenta(phone)
        if not estado.get("found") and cedula:
            logger.info(f"[CANCELACIÓN] No encontrada por teléfono {phone}; intentando por cédula {cedula}")
            estado = await consultar_cuenta_por_cedula(cedula)
        if not estado.get("found"):
            return {"success": False, "error": "No se encontró la afiliación"}

        affiliation_id = estado["affiliation_id"]
        previous_status = estado.get("payment_status") or "unknown"
        radicado = f"CAN-{datetime.now().strftime('%Y%m%d')}-{datetime.now().strftime('%H%M%S')}"
        requested_at = datetime.now().isoformat()

        # Datos de contacto para el radicado
        client_email, client_document = "", ""
        async with httpx.AsyncClient(timeout=15) as client:
            # Evitar duplicados: si ya hay un radicado pendiente, devolver ese.
            try:
                dup = await client.get(
                    f"{SUPABASE_URL}/rest/v1/cancellation_requests"
                    f"?affiliation_id=eq.{affiliation_id}&status=eq.pendiente"
                    f"&select=radicado&order=requested_at.desc&limit=1",
                    headers=HEADERS,
                )
                if dup.status_code == 200 and dup.json():
                    existing = dup.json()[0]["radicado"]
                    logger.info(f"[CANCELACIÓN] Ya existe radicado pendiente {existing} para {affiliation_id}")
                    return {
                        "success": True,
                        "radicado": existing,
                        "name": estado["name"],
                        "plan": estado.get("plan", ""),
                        "already_exists": True,
                        "message": (
                            f"Ya tienes una solicitud de cancelación en trámite. Radicado: {existing}. "
                            "No se generan nuevos cobros mientras se tramita."
                        ),
                    }
            except Exception:
                pass

            try:
                det = await client.get(
                    f"{SUPABASE_URL}/rest/v1/b2c_affiliations?id=eq.{affiliation_id}"
                    f"&select=email,document_number,phone",
                    headers=HEADERS,
                )
                if det.status_code == 200 and det.json():
                    row = det.json()[0]
                    client_email = row.get("email") or ""
                    client_document = row.get("document_number") or ""
            except Exception:
                pass

            # 1. Crear el radicado (estado pendiente)
            req_resp = await client.post(
                f"{SUPABASE_URL}/rest/v1/cancellation_requests",
                headers=HEADERS,
                json={
                    "radicado": radicado,
                    "affiliation_id": affiliation_id,
                    "client_name": estado.get("name", ""),
                    "client_phone": phone,
                    "client_email": client_email,
                    "client_document": client_document,
                    "plan": estado.get("plan", ""),
                    "reason": reason,
                    "channel": "whatsapp",
                    "status": "pendiente",
                    "retention_attempts": retention_attempts,
                    "requested_at": requested_at,
                    "metadata": {"phone": phone, "previous_status": previous_status},
                },
            )
            if req_resp.status_code >= 400:
                logger.error(f"[CANCELACIÓN] Falló INSERT cancellation_requests: {req_resp.status_code} {req_resp.text}")
                return {"success": False, "error": "No se pudo registrar la solicitud"}

            # 2. Frenar cobros futuros: cancelar transacciones pendientes / vencidas
            await client.patch(
                f"{SUPABASE_URL}/rest/v1/payment_transactions"
                f"?affiliation_id=eq.{affiliation_id}"
                f"&payment_status=in.(pending,overdue)",
                headers=HEADERS,
                json={"payment_status": "cancelled", "notes": f"Cobro frenado por solicitud de cancelación. Radicado: {radicado}"},
            )

            # 3. Marcar la afiliación como cancelación en trámite (sigue activa)
            aff_resp = await client.patch(
                f"{SUPABASE_URL}/rest/v1/b2c_affiliations?id=eq.{affiliation_id}",
                headers=HEADERS,
                json={
                    "pending_cancellation": True,
                    "pending_cancellation_radicado": radicado,
                    "deactivation_reason": reason,
                },
            )
            if aff_resp.status_code >= 400:
                logger.warning(f"[CANCELACIÓN] No se pudo marcar pending_cancellation: {aff_resp.status_code} {aff_resp.text}")

            # 4. Historial de cartera
            await client.post(
                f"{SUPABASE_URL}/rest/v1/payment_portfolio_history",
                headers=HEADERS,
                json={
                    "affiliation_id": affiliation_id,
                    "action": "cancellation_requested",
                    "previous_value": previous_status,
                    "new_value": "pending_cancellation",
                    "changed_by_email": "whatsapp_bot",
                    "notes": f"Solicitud de cancelación vía WhatsApp. Radicado: {radicado}. Razón: {reason}. Cobros frenados, pendiente de trámite.",
                },
            )

            # 5. Audit log (queda en el historial del cliente)
            await client.post(
                f"{SUPABASE_URL}/rest/v1/affiliation_audit_log",
                headers=HEADERS,
                json={
                    "affiliation_id": affiliation_id,
                    "event_type": "cancellation_requested",
                    "event_category": "account",
                    "description": (
                        f"Solicitud de cancelación vía WhatsApp. Radicado: {radicado}. Razón: {reason}. "
                        "Se frenaron los cobros y la cuenta quedó en cancelación en trámite."
                    ),
                    "changed_by_email": "whatsapp_bot",
                    "changed_by_type": "system",
                    "old_value": {"pending_cancellation": False},
                    "new_value": {"pending_cancellation": True},
                    "metadata": {
                        "radicado": radicado,
                        "reason": reason,
                        "phone": phone,
                        "channel": "whatsapp",
                        "requested_at": requested_at,
                        "retention_attempts": retention_attempts,
                    },
                },
            )

        # 6. Disparar alerta a admins (no bloquea la respuesta al cliente si falla)
        await _alertar_admins_cancelacion(
            affiliation_id=affiliation_id,
            radicado=radicado,
            client_name=estado.get("name", ""),
            client_phone=phone,
            plan=estado.get("plan", ""),
            reason=reason,
            cancelled_at=requested_at,
            status="pendiente",
        )

        logger.info(f"[CANCELACIÓN SOLICITADA] {phone} → {affiliation_id} → Radicado: {radicado}")
        return {
            "success": True,
            "radicado": radicado,
            "name": estado["name"],
            "plan": estado.get("plan", ""),
            "message": (
                f"Tu solicitud de cancelación quedó registrada. Radicado: {radicado}. "
                "No se generarán nuevos cobros mientras se tramita."
            ),
        }

    except Exception as e:
        logger.error(f"Error procesando cancelación: {e}")
        return {"success": False, "error": str(e)}


async def consultar_radicado(identificador: str) -> dict:
    """
    Consulta el estado de un radicado de cancelación.
    Acepta el número de radicado (CAN-...) o el teléfono del cliente.
    Devuelve el más reciente si hay varios.
    """
    try:
        ident = (identificador or "").strip()
        async with httpx.AsyncClient(timeout=10) as client:
            url = None
            if ident.upper().startswith("CAN-"):
                url = (
                    f"{SUPABASE_URL}/rest/v1/cancellation_requests"
                    f"?radicado=eq.{ident.upper()}"
                    f"&select=radicado,status,plan,reason,requested_at,processed_at,resolution_notes"
                    f"&order=requested_at.desc&limit=1"
                )
            else:
                variants = _phone_variants(ident)
                or_filter = ",".join(f"client_phone.eq.{p}" for p in variants)
                url = (
                    f"{SUPABASE_URL}/rest/v1/cancellation_requests"
                    f"?or=({or_filter})"
                    f"&select=radicado,status,plan,reason,requested_at,processed_at,resolution_notes"
                    f"&order=requested_at.desc&limit=1"
                )

            r = await client.get(url, headers=HEADERS)
            if r.status_code == 200 and r.json():
                req = r.json()[0]
                return {"success": True, "found": True, **req}

        return {"success": True, "found": False}

    except Exception as e:
        logger.error(f"Error consultando radicado: {e}")
        return {"success": False, "error": str(e)}


async def _alertar_admins_cancelacion(
    affiliation_id: str,
    radicado: str,
    client_name: str,
    client_phone: str,
    plan: str,
    reason: str,
    cancelled_at: str,
    status: str = "pendiente",
) -> None:
    """Llama al edge function send-cancellation-admin-alert. No bloquea si falla."""
    try:
        alert_url = f"{SUPABASE_URL}/functions/v1/send-cancellation-admin-alert"
        bot_api_key = os.getenv("BOT_INTERNAL_API_KEY", "")
        headers = {
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
        }
        if bot_api_key:
            headers["x-api-key"] = bot_api_key

        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                alert_url,
                headers=headers,
                json={
                    "affiliation_id": affiliation_id,
                    "radicado": radicado,
                    "client_name": client_name,
                    "client_phone": client_phone,
                    "plan": plan,
                    "reason": reason,
                    "cancelled_at": cancelled_at,
                    "status": status,
                },
            )
            if resp.status_code >= 400:
                logger.warning(f"[ALERTA ADMIN] {resp.status_code}: {resp.text}")
            else:
                logger.info(f"[ALERTA ADMIN] enviada para radicado {radicado}")
    except Exception as e:
        logger.warning(f"[ALERTA ADMIN] No se pudo notificar a admins: {e}")


async def enviar_recordatorio_beneficios(phone: str) -> dict:
    """Envía un recordatorio de beneficios al afiliado."""
    estado = await consultar_estado_cuenta(phone)
    if not estado.get("found"):
        return {"success": False, "error": "No se encontró la afiliación"}

    plan = estado.get("plan", "").lower()

    beneficios = "Tus beneficios activos:\n\n"
    beneficios += "- Cobertura exequial familiar con cobertura nacional\n"
    beneficios += "- Tarjeta Golden Offers con descuentos en comercios aliados\n"
    beneficios += "- Hijos cubiertos desde la concepción\n"

    if "plus" in plan:
        beneficios += "- 2 eventos/homenajes cubiertos al año\n"
        beneficios += "- 1 beneficiario sin límite de edad\n"
        beneficios += "- 1 mascota incluida\n"
    elif "total" in plan:
        beneficios += "- Eventos/homenajes ilimitados al año\n"
        beneficios += "- 2 beneficiarios sin límite de edad\n"
        beneficios += "- Exhumación y columbario incluidos\n"
    else:
        beneficios += "- 1 evento/homenaje cubierto al año\n"
        beneficios += "- 1 mascota incluida\n"

    beneficios += "\nRecuerda que puedes agregar o modificar beneficiarios en cualquier momento."

    return {"success": True, "beneficios": beneficios, "name": estado["name"], "plan": estado["plan"]}


async def consultar_cuenta_por_cedula(cedula: str) -> dict:
    """Consulta el estado de cuenta de un afiliado por cédula (útil para clientes legacy)."""
    try:
        cedula_clean = cedula.replace(".", "").replace("-", "").replace(" ", "").strip()
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"{SUPABASE_URL}/rest/v1/b2c_affiliations"
                f"?document_number=eq.{cedula_clean}"
                f"&select=id,first_name,last_name,selected_plan,selected_plan_price,plan_legacy_nombre,tarifa_personalizada,payment_status,is_active,payment_date,created_at,canal,beneficiarios,email,phone",
                headers=HEADERS,
            )
            if r.status_code == 200 and r.json():
                aff = r.json()[0]
                txr = await client.get(
                    f"{SUPABASE_URL}/rest/v1/payment_transactions"
                    f"?affiliation_id=eq.{aff['id']}&payment_status=in.(pending,overdue)"
                    f"&select=amount,month_applied,year_applied,due_date",
                    headers=HEADERS,
                )
                txs = txr.json() if txr.status_code == 200 else []
                total_deuda = sum(t.get("amount", 0) for t in txs)
                cuotas = len(txs)
                plan_nombre = aff.get("plan_legacy_nombre") or aff.get("selected_plan", "")
                # Tarifa efectiva: precio sincronizado > tarifa personalizada >
                # precio del plan > default. Evita generar links de pago anticipado
                # con un monto desactualizado.
                tarifa = aff.get("selected_plan_price") or aff.get("tarifa_personalizada") or 0
                if not tarifa:
                    tarifa = 24900
                    try:
                        plan_raw = (aff.get("selected_plan") or "").lower()
                        plan_key = plan_raw.replace("familia ", "").replace("familia_", "").strip()
                        if plan_key:
                            pr = await client.get(
                                f"{SUPABASE_URL}/rest/v1/plans?plan_key=eq.{plan_key}&select=price&limit=1",
                                headers=HEADERS,
                            )
                            if pr.status_code == 200 and pr.json():
                                tarifa = pr.json()[0].get("price", 24900)
                    except Exception:
                        pass

                paid_r = await client.get(
                    f"{SUPABASE_URL}/rest/v1/payment_transactions"
                    f"?affiliation_id=eq.{aff['id']}&payment_status=eq.paid"
                    f"&select=month_applied,year_applied&order=year_applied.desc,month_applied.desc",
                    headers=HEADERS,
                )
                paid_txs = paid_r.json() if paid_r.status_code == 200 else []
                ultimo_periodo_pagado = ""
                if paid_txs:
                    p = paid_txs[0]
                    ultimo_periodo_pagado = _nombre_periodo(p.get("month_applied"), p.get("year_applied"))
                meses_pendientes = [
                    _nombre_periodo(t.get("month_applied"), t.get("year_applied"))
                    for t in sorted(txs, key=lambda x: (x.get("year_applied") or 0, x.get("month_applied") or 0))
                ]
                meses_pendientes = [m for m in meses_pendientes if m]

                # Primer ciclo en curso: el primer pago está PENDIENTE, no en mora.
                try:
                    created = datetime.fromisoformat(aff["created_at"].replace("Z", "+00:00"))
                    dias_desde_afiliacion = (datetime.now(created.tzinfo) - created).days
                except Exception:
                    dias_desde_afiliacion = 999
                primer_pago_pendiente = (
                    dias_desde_afiliacion < 30
                    and len(paid_txs) == 0
                    and aff.get("payment_status") != "paid"
                )
                if primer_pago_pendiente:
                    total_deuda = 0
                    cuotas = 0
                    meses_pendientes = []

                return {
                    "success": True, "found": True,
                    "affiliation_id": aff["id"],
                    "name": f"{aff['first_name']} {aff['last_name']}",
                    "plan": plan_nombre,
                    "tarifa": tarifa,
                    "canal": aff.get("canal"),
                    "payment_status": aff.get("payment_status"),
                    "is_active": aff.get("is_active"),
                    "total_deuda": total_deuda,
                    "cuotas_pendientes": cuotas,
                    "ultimo_periodo_pagado": ultimo_periodo_pagado,
                    "meses_pendientes": meses_pendientes,
                    "primer_pago_pendiente": primer_pago_pendiente,
                    "beneficiarios": aff.get("beneficiarios") or [],
                    "email": aff.get("email"),
                    "phone": aff.get("phone"),
                }
        return {"success": True, "found": False}
    except Exception as e:
        logger.error(f"Error consultando por cédula: {e}")
        return {"success": False, "error": str(e)}


async def actualizar_beneficiarios(affiliation_id: str, beneficiarios: list, motivo: str = "") -> dict:
    """
    Actualiza la lista de beneficiarios de una afiliación.
    beneficiarios: lista con estructura {primerNombre, segundoNombre, primerApellido,
                   segundoApellido, fechaNac, edad, parentesco, sinLimiteEdad}
    """
    try:
        # Validate and normalize each beneficiary
        normalized = []
        for b in beneficiarios:
            nombre = b.get("nombre") or b.get("primerNombre", "")
            # Support both legacy format {nombre} and UI format {primerNombre}
            if nombre and not b.get("primerNombre"):
                parts = nombre.strip().split()
                b["primerNombre"] = parts[0] if parts else ""
                b["segundoNombre"] = parts[1] if len(parts) > 3 else ""
                b["primerApellido"] = parts[-2] if len(parts) >= 3 else (parts[1] if len(parts) == 2 else "")
                b["segundoApellido"] = parts[-1] if len(parts) >= 4 else ""

            entry = {
                "id": b.get("id") or __import__("uuid").uuid4().hex[:12],
                "primerNombre": b.get("primerNombre", ""),
                "segundoNombre": b.get("segundoNombre", ""),
                "primerApellido": b.get("primerApellido", ""),
                "segundoApellido": b.get("segundoApellido", ""),
                "fechaNac": b.get("fechaNac") or b.get("fecha_nacimiento") or "",
                "edad": b.get("edad") or 0,
                "parentesco": b.get("parentesco", ""),
                "sinLimiteEdad": b.get("sinLimiteEdad", False),
            }
            normalized.append(entry)

        async with httpx.AsyncClient(timeout=10) as client:
            # Save snapshot of current beneficiaries to audit log first
            snapshot_r = await client.get(
                f"{SUPABASE_URL}/rest/v1/b2c_affiliations?id=eq.{affiliation_id}&select=beneficiarios,first_name,last_name",
                headers=HEADERS,
            )
            old_bens = []
            aff_name = ""
            if snapshot_r.status_code == 200 and snapshot_r.json():
                old_bens = snapshot_r.json()[0].get("beneficiarios") or []
                aff_name = f"{snapshot_r.json()[0].get('first_name','')} {snapshot_r.json()[0].get('last_name','')}".strip()

            # Update beneficiarios JSONB
            upd = await client.patch(
                f"{SUPABASE_URL}/rest/v1/b2c_affiliations?id=eq.{affiliation_id}",
                headers={**HEADERS, "Prefer": "return=minimal"},
                json={"beneficiarios": normalized},
            )
            if upd.status_code not in (200, 204):
                return {"success": False, "error": f"DB error {upd.status_code}"}

            # Audit log
            await client.post(
                f"{SUPABASE_URL}/rest/v1/affiliation_audit_log",
                headers=HEADERS,
                json={
                    "affiliation_id": affiliation_id,
                    "event_type": "beneficiaries_updated",
                    "event_category": "beneficiary",
                    "description": f"Beneficiarios actualizados vía bot WhatsApp. {motivo}".strip(),
                    "changed_by_email": "bot@mutuo.la",
                    "changed_by_type": "system",
                    "old_value": {"beneficiarios": old_bens},
                    "new_value": {"beneficiarios": normalized},
                    "metadata": {"motivo": motivo, "count_before": len(old_bens), "count_after": len(normalized)},
                },
            )

        return {
            "success": True,
            "name": aff_name,
            "beneficiarios_antes": len(old_bens),
            "beneficiarios_despues": len(normalized),
        }
    except Exception as e:
        logger.error(f"Error actualizando beneficiarios: {e}")
        return {"success": False, "error": str(e)}
