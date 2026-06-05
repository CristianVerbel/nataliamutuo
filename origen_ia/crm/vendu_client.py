# crm/vendu_client.py — Cliente HTTP para Vendu CRM (Supabase)

import os
import logging
import httpx
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger("origen-ia")

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "")
BOT_SECRET = os.getenv("WHATSAPP_BOT_SECRET", "")


class VenduCRMClient:
    """Cliente para sincronizar datos con Vendu CRM via Supabase Edge Function."""

    def __init__(self):
        self.sync_url = f"{SUPABASE_URL}/functions/v1/whatsapp-sync" if SUPABASE_URL else ""
        self.headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {SUPABASE_ANON_KEY}",
            "x-bot-secret": BOT_SECRET,
        }
        self.enabled = bool(SUPABASE_URL and SUPABASE_ANON_KEY)

    async def sync_message(self, phone: str, role: str, content: str, perfil: dict = None):
        """Sincroniza un mensaje con Supabase."""
        if not self.enabled:
            return

        payload = {
            "action": "sync_message",
            "data": {
                "phone": phone,
                "role": role,
                "content": content,
            },
        }
        # Agregar datos del perfil si hay
        if perfil:
            for key in ["prospect_name", "city", "department", "current_operator", "interest", "disc_profile"]:
                val = perfil.get(key)
                if val:
                    payload["data"][key] = val

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(self.sync_url, json=payload, headers=self.headers)
        except Exception as e:
            logger.warning(f"CRM sync failed (non-blocking): {e}")

    async def update_status(self, phone: str, status: str, perfil: dict = None, notes: str = ""):
        """Actualiza el estado de la conversacion en Supabase."""
        if not self.enabled:
            return

        payload = {
            "action": "update_status",
            "data": {
                "phone": phone,
                "status": status,
            },
        }
        if perfil:
            for key in ["prospect_name", "city", "department", "interest", "disc_profile"]:
                val = perfil.get(key)
                if val:
                    payload["data"][key] = val
        if notes:
            payload["data"]["notes"] = notes

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(self.sync_url, json=payload, headers=self.headers)
        except Exception as e:
            logger.warning(f"CRM status update failed: {e}")

    async def create_sale(self, conversation_id: str, sponsor_id: str, product_id: str,
                          client_name: str, client_phone: str, client_doc: str = "",
                          advisor_id: str = None):
        """Crea una venta en Vendu desde una conversacion."""
        if not self.enabled:
            return None

        payload = {
            "action": "create_sale",
            "data": {
                "conversation_id": conversation_id,
                "sponsor_id": sponsor_id,
                "product_id": product_id,
                "client_name": client_name,
                "client_doc": client_doc,
                "client_phone": client_phone,
                "advisor_id": advisor_id,
            },
        }

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.post(self.sync_url, json=payload, headers=self.headers)
                if r.status_code == 200:
                    return r.json()
        except Exception as e:
            logger.error(f"CRM create sale failed: {e}")
        return None

    async def update_lead_entry(self, phone: str, data: dict):
        """Actualiza el lead en lead_database_entries con datos nuevos del cliente."""
        if not self.enabled:
            return

        sb_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "") or SUPABASE_ANON_KEY
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {sb_key}",
            "apikey": sb_key,
            "Prefer": "return=minimal",
        }

        # Normalizar teléfono
        phone_clean = phone.replace("+", "").replace(" ", "")
        phone_short = phone_clean[2:] if phone_clean.startswith("57") and len(phone_clean) == 12 else phone_clean

        url = (
            f"{SUPABASE_URL}/rest/v1/lead_database_entries"
            f"?or=(phone.eq.{phone_clean},phone.eq.{phone_short})"
            f"&assigned_to=eq.bot"
        )

        update = {}
        if data.get("name"):
            update["name"] = data["name"]
        if data.get("city"):
            update["city"] = data["city"]
        if data.get("status"):
            update["status"] = data["status"]
        if data.get("notes"):
            update["notes"] = data["notes"]

        if not update:
            return

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.patch(url, json=update, headers=headers)
                logger.info(f"[CRM] Lead {phone} actualizado: {list(update.keys())}")
        except Exception as e:
            logger.warning(f"Lead entry update failed: {e}")
