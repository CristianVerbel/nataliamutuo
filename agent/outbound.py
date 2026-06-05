# agent/outbound.py — Motor de campañas outbound
# Mutuo Fintech — Origen IA
#
# Gestiona el envío masivo de mensajes salientes con control anti-spam.
# Toma leads de Supabase (lead_database_entries asignados al bot) y los
# contacta uno por uno con delays prudentes.

import os
import asyncio
import logging
import random
from datetime import datetime, timezone, timedelta
from enum import Enum

import httpx
from dotenv import load_dotenv

from agent.providers.base import ProveedorWhatsApp
from origen_ia.config.campaign_prompts import build_outbound_opener, MUTUO_OUTBOUND

load_dotenv()
logger = logging.getLogger("origen-ai")

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "")
BOT_SECRET = os.getenv("WHATSAPP_BOT_SECRET", "")

# Zona horaria Colombia (UTC-5)
COL_TZ = timezone(timedelta(hours=-5))

# Configuración anti-spam
MIN_DELAY_SECONDS = 30       # Mínimo entre mensajes
MAX_DELAY_SECONDS = 60       # Máximo entre mensajes
MAX_PER_HOUR = 60            # Máximo mensajes por hora
MAX_PER_DAY = 500            # Máximo mensajes por día
HOUR_START = 7               # Hora de inicio (7am COL)
HOUR_END = 19                # Hora de fin (7pm COL)
BATCH_SIZE = 50              # Leads por batch de Supabase


class CampaignStatus(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    FINISHED = "finished"
    ERROR = "error"


class OutboundCampaign:
    """Gestiona el envío de mensajes outbound a leads asignados al bot."""

    def __init__(self, proveedor: ProveedorWhatsApp):
        self.proveedor = proveedor
        self.status = CampaignStatus.IDLE
        self.task: asyncio.Task | None = None

        # Contadores
        self.sent_today = 0
        self.sent_this_hour = 0
        self.total_sent = 0
        self.total_errors = 0
        self.last_reset_hour = -1
        self.last_reset_day = -1

        # Campaign config
        self.database_id: str | None = None
        self.campaign_prompt = MUTUO_OUTBOUND

        # Supabase headers
        key = SUPABASE_SERVICE_KEY or SUPABASE_ANON_KEY
        self.sb_headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
            "apikey": key,
        }

        # Edge function headers
        self.sync_headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {SUPABASE_ANON_KEY}",
            "x-bot-secret": BOT_SECRET,
        }

    def _now_col(self) -> datetime:
        return datetime.now(COL_TZ)

    def _is_within_hours(self) -> bool:
        """Verifica que estemos en horario permitido (8am-8pm Colombia)."""
        hour = self._now_col().hour
        return HOUR_START <= hour < HOUR_END

    def _reset_counters_if_needed(self):
        """Resetea contadores por hora y por día."""
        now = self._now_col()
        if now.hour != self.last_reset_hour:
            self.sent_this_hour = 0
            self.last_reset_hour = now.hour
        if now.day != self.last_reset_day:
            self.sent_today = 0
            self.last_reset_day = now.day

    def _random_delay(self) -> float:
        """Genera un delay aleatorio entre mensajes para parecer natural."""
        return random.uniform(MIN_DELAY_SECONDS, MAX_DELAY_SECONDS)

    async def _fetch_pending_leads(self) -> list[dict]:
        """Obtiene leads asignados al bot que aún no han sido contactados."""
        if not SUPABASE_URL:
            return []

        url = (
            f"{SUPABASE_URL}/rest/v1/lead_database_entries"
            f"?status=eq.asignado_bot"
            f"&select=id,phone,name,city,address,estrato,raw_data,contact_attempts"
            f"&order=created_at.asc"
            f"&limit={BATCH_SIZE}"
        )
        if self.database_id:
            url += f"&database_id=eq.{self.database_id}"

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.get(url, headers=self.sb_headers)
                if r.status_code == 200:
                    return r.json()
                logger.error(f"Supabase fetch error: {r.status_code} {r.text}")
        except Exception as e:
            logger.error(f"Error fetching leads: {e}")
        return []

    async def _update_lead_status(self, lead_id: str, status: str, attempts: int = None):
        """Actualiza el estado de un lead en Supabase."""
        if not SUPABASE_URL:
            return

        url = f"{SUPABASE_URL}/rest/v1/lead_database_entries?id=eq.{lead_id}"
        data = {"status": status, "last_contact_at": datetime.now(timezone.utc).isoformat()}
        if attempts is not None:
            data["contact_attempts"] = attempts

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.patch(url, json=data, headers=self.sb_headers)
        except Exception as e:
            logger.warning(f"Error updating lead {lead_id}: {e}")

    async def _sync_message_to_crm(self, phone: str, role: str, content: str, perfil: dict = None):
        """Sincroniza mensaje con el CRM de Supabase (edge function)."""
        if not SUPABASE_URL or not SUPABASE_ANON_KEY:
            return

        sync_url = f"{SUPABASE_URL}/functions/v1/whatsapp-sync"
        payload = {
            "action": "sync_message",
            "data": {
                "phone": phone,
                "role": role,
                "content": content,
            },
        }
        if perfil:
            for key in ["prospect_name", "city"]:
                val = perfil.get(key)
                if val:
                    payload["data"][key] = val

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(sync_url, json=payload, headers=self.sync_headers)
        except Exception as e:
            logger.warning(f"CRM sync (outbound) failed: {e}")

    def _normalize_phone(self, phone: str) -> str:
        """Normaliza el número de teléfono para WhatsApp."""
        if not phone:
            return ""
        clean = "".join(c for c in phone if c.isdigit())
        # Colombia: agregar 57 si no lo tiene
        if len(clean) == 10 and clean.startswith("3"):
            clean = "57" + clean
        elif len(clean) == 7:  # Fijo sin indicativo
            return ""  # No enviar a fijos
        return clean

    async def _send_to_lead(self, lead: dict) -> bool:
        """Envía el mensaje de apertura a un lead individual."""
        phone = self._normalize_phone(lead.get("phone", ""))
        if not phone:
            logger.warning(f"Lead {lead['id']} sin teléfono válido — descartado")
            await self._update_lead_status(lead["id"], "descartado")
            return False

        # Si el teléfono ya tiene una conversación inbound activa, NO enviar mensaje outbound
        # Esto previene que outbound mande "¿Me escuchaste?" mientras el usuario ya está respondiendo
        try:
            from agent.main import active_inbound_phones, sesiones
            if phone in active_inbound_phones or phone in sesiones:
                logger.info(f"[OUTBOUND SKIP] {phone} ya tiene conversación activa — no enviar apertura")
                await self._update_lead_status(lead["id"], "en_gestion")
                return False
        except ImportError:
            pass  # main.py aun no importado

        name = lead.get("name", "")
        city = lead.get("city", "")
        estrato = lead.get("estrato", "")

        # Generar mensaje de apertura personalizado
        mensaje = build_outbound_opener(name, city, estrato)

        try:
            # Enviar vía WhatsApp
            ok = await self.proveedor.enviar_mensaje(phone, mensaje)

            if ok:
                # Actualizar lead a "en_gestion"
                attempts = (lead.get("contact_attempts") or 0) + 1
                await self._update_lead_status(lead["id"], "en_gestion", attempts)

                # Sincronizar con CRM
                await self._sync_message_to_crm(
                    phone, "assistant", mensaje,
                    {"prospect_name": name, "city": city}
                )

                self.total_sent += 1
                self.sent_today += 1
                self.sent_this_hour += 1

                logger.info(f"[OUTBOUND] ✓ Enviado a {phone} ({name}) — #{self.total_sent}")
                return True
            else:
                self.total_errors += 1
                logger.warning(f"[OUTBOUND] ✗ Error enviando a {phone}")
                return False

        except Exception as e:
            self.total_errors += 1
            logger.error(f"[OUTBOUND] Error con lead {lead['id']}: {e}")
            return False

    async def _campaign_loop(self):
        """Loop principal de la campaña — corre en background."""
        logger.info("[OUTBOUND] Campaña iniciada")
        self.status = CampaignStatus.RUNNING

        try:
            while self.status == CampaignStatus.RUNNING:
                # Cada iteración va protegida: un error puntual (Supabase caído,
                # timeout, etc.) NO debe matar la campaña. Se loguea y se reintenta.
                try:
                    self._reset_counters_if_needed()

                    # Verificar horario
                    if not self._is_within_hours():
                        now_col = self._now_col()
                        logger.info(f"[OUTBOUND] Fuera de horario ({now_col.hour}h COL) — esperando...")
                        # Calcular segundos hasta las 8am del día siguiente
                        if now_col.hour >= HOUR_END:
                            next_start = now_col.replace(hour=HOUR_START, minute=0, second=0) + timedelta(days=1)
                        else:
                            next_start = now_col.replace(hour=HOUR_START, minute=0, second=0)
                        wait = (next_start - now_col).total_seconds()
                        await asyncio.sleep(min(wait, 300))  # Check cada 5 min max
                        continue

                    # Verificar límites
                    if self.sent_this_hour >= MAX_PER_HOUR:
                        logger.info(f"[OUTBOUND] Límite por hora alcanzado ({MAX_PER_HOUR}) — esperando...")
                        await asyncio.sleep(120)  # Esperar 2 min
                        continue

                    if self.sent_today >= MAX_PER_DAY:
                        logger.info(f"[OUTBOUND] Límite diario alcanzado ({MAX_PER_DAY}) — esperando mañana...")
                        await asyncio.sleep(300)
                        continue

                    # Obtener leads pendientes
                    leads = await self._fetch_pending_leads()

                    if not leads:
                        logger.info("[OUTBOUND] No hay más leads pendientes — campaña finalizada")
                        self.status = CampaignStatus.FINISHED
                        break

                    # Enviar uno por uno con delay
                    for lead in leads:
                        if self.status != CampaignStatus.RUNNING:
                            break

                        # Re-check limits
                        self._reset_counters_if_needed()
                        if self.sent_this_hour >= MAX_PER_HOUR or self.sent_today >= MAX_PER_DAY:
                            break
                        if not self._is_within_hours():
                            break

                        await self._send_to_lead(lead)

                        # Delay anti-spam
                        delay = self._random_delay()
                        logger.debug(f"[OUTBOUND] Esperando {delay:.0f}s antes del siguiente...")
                        await asyncio.sleep(delay)

                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.error(f"[OUTBOUND] Error en iteración (la campaña continúa): {e}")
                    await asyncio.sleep(30)
                    continue

        except asyncio.CancelledError:
            logger.info("[OUTBOUND] Campaña cancelada")
            self.status = CampaignStatus.PAUSED
        except Exception as e:
            logger.error(f"[OUTBOUND] Error fatal en campaña: {e}")
            self.status = CampaignStatus.ERROR

    def start(self, database_id: str = None):
        """Inicia la campaña en background."""
        if self.status == CampaignStatus.RUNNING:
            return {"status": "already_running"}

        self.database_id = database_id
        self.status = CampaignStatus.RUNNING
        self.task = asyncio.create_task(self._campaign_loop())

        return {
            "status": "started",
            "database_id": database_id,
            "config": {
                "min_delay": MIN_DELAY_SECONDS,
                "max_delay": MAX_DELAY_SECONDS,
                "max_per_hour": MAX_PER_HOUR,
                "max_per_day": MAX_PER_DAY,
                "hours": f"{HOUR_START}:00 - {HOUR_END}:00 COL",
            },
        }

    def stop(self):
        """Detiene la campaña."""
        if self.task and not self.task.done():
            self.task.cancel()
        self.status = CampaignStatus.PAUSED
        return {"status": "stopped"}

    def get_status(self) -> dict:
        """Retorna el estado actual de la campaña."""
        return {
            "status": self.status.value,
            "database_id": self.database_id,
            "stats": {
                "total_sent": self.total_sent,
                "sent_today": self.sent_today,
                "sent_this_hour": self.sent_this_hour,
                "total_errors": self.total_errors,
            },
            "limits": {
                "max_per_hour": MAX_PER_HOUR,
                "max_per_day": MAX_PER_DAY,
                "remaining_hour": max(0, MAX_PER_HOUR - self.sent_this_hour),
                "remaining_day": max(0, MAX_PER_DAY - self.sent_today),
            },
            "current_time_col": self._now_col().strftime("%Y-%m-%d %H:%M:%S"),
            "within_hours": self._is_within_hours(),
        }
