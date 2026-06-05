# agent/alerting.py — Alertas multicanal FUERA DE BANDA
# Mutuo Fintech — Origen IA
#
# Notifica por WhatsApp + Email + SMS, cada canal INDEPENDIENTE: si uno falla,
# los demás siguen. Email y SMS NO dependen de Whapi, así que llegan aunque
# WhatsApp esté caído — que es justo cuando más necesitamos avisar.
#
# El email (Resend) y el SMS (Twilio) se envían a través del edge function
# `send-alert` de Supabase, que ya tiene esas API keys como secrets. Así el
# bot NO necesita conocer RESEND_API_KEY ni las credenciales de Twilio.

import os
import logging
import httpx

logger = logging.getLogger("mutuo-bot")

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "") or os.getenv("SUPABASE_ANON_KEY", "")


async def _post_alert_edge(payload: dict) -> bool:
    """Llama al edge function send-alert (tiene las keys de Resend/Twilio como
    secrets de Supabase). Best-effort."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return False
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(
                f"{SUPABASE_URL}/functions/v1/send-alert",
                json=payload,
                headers={
                    "Authorization": f"Bearer {SUPABASE_KEY}",
                    "apikey": SUPABASE_KEY,
                    "Content-Type": "application/json",
                },
            )
        if r.status_code == 200:
            logger.info(f"[ALERT] send-alert OK: {r.text[:150]}")
            return True
        logger.error(f"[ALERT] send-alert error {r.status_code}: {r.text[:200]}")
    except Exception as e:
        logger.error(f"[ALERT] send-alert falló: {e}")
    return False


async def send_email(to_list: list[str], subject: str, html: str) -> bool:
    """Envía email (vía edge function → Resend). Fuera de banda: no pasa por Whapi."""
    to_list = [e for e in (to_list or []) if e]
    if not to_list:
        return False
    return await _post_alert_edge({"subject": subject, "html": html, "emails": to_list})


async def _alert_whatsapp(proveedor, text: str) -> None:
    admin = os.getenv("ADMIN_WHATSAPP", "")
    if not (proveedor and admin):
        return
    try:
        await proveedor.enviar_mensaje(admin, text)
        logger.info(f"[ALERT] WhatsApp → {admin}")
    except Exception as e:
        # Esperable cuando lo caído es Whapi mismo; email/SMS cubren ese caso.
        logger.error(f"[ALERT] WhatsApp falló (¿Whapi caído?): {e}")


async def send_alert(proveedor, subject: str, text: str) -> None:
    """Dispara una alerta por TODOS los canales, cada uno independiente.
    Email y SMS (vía edge function send-alert) no dependen de Whapi."""
    full = f"{subject}\n\n{text}" if subject else text
    await _alert_whatsapp(proveedor, full)
    # Email + SMS a los destinatarios configurados como secrets en Supabase
    # (ALERT_EMAIL / ALERT_SMS_PHONE).
    await _post_alert_edge({"subject": subject or "Alerta Mutuo", "text": text})
