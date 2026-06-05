# agent/providers/whapi.py — Adaptador para Whapi.cloud
# Mutuo Fintech — Bot WhatsApp

import os
import logging
import httpx
from fastapi import Request
from agent.providers.base import ProveedorWhatsApp, MensajeEntrante

logger = logging.getLogger("mutuo-bot")


# Marcadores sintéticos para media sin texto — el bot los mapea a respuestas fallback
_MEDIA_MARKERS = {
    "audio": "[AUDIO_RECIBIDO]",
    "voice": "[AUDIO_RECIBIDO]",
    "ptt": "[AUDIO_RECIBIDO]",
    "image": "[IMAGEN_RECIBIDA]",
    "video": "[VIDEO_RECIBIDO]",
    "document": "[DOCUMENTO_RECIBIDO]",
    "sticker": "[STICKER_RECIBIDO]",
    "location": "[UBICACION_RECIBIDA]",
    "contact": "[CONTACTO_RECIBIDO]",
    "contacts": "[CONTACTO_RECIBIDO]",
}


def _extraer_texto(msg: dict) -> str:
    """Extrae texto desde cualquier campo que Whapi pueda usar (body, caption, preview)."""
    return (
        (msg.get("text") or {}).get("body")
        or msg.get("body")
        or msg.get("caption")
        or (msg.get("image") or {}).get("caption")
        or (msg.get("video") or {}).get("caption")
        or (msg.get("document") or {}).get("caption")
        or (msg.get("link_preview") or {}).get("body")
        or (msg.get("link_preview") or {}).get("title")
        or msg.get("_derived_text")  # inyectado por el edge function upstream
        or ""
    )


class ProveedorWhapi(ProveedorWhatsApp):
    """Proveedor de WhatsApp usando Whapi.cloud (REST API simple)."""

    def __init__(self):
        self.token = os.getenv("WHAPI_TOKEN") or os.getenv("WHAPI_API_KEY")
        self.url_envio = "https://gate.whapi.cloud/messages/text"
        self.url_imagen = "https://gate.whapi.cloud/messages/image"

    async def parsear_webhook(self, request: Request) -> list[MensajeEntrante]:
        """Parsea el payload de Whapi.cloud. Acepta texto, captions y media (con marcador)."""
        body = await request.json()
        lead_context = body.get("_lead_context")  # injected by whapi-inbound-webhook edge function
        affiliate_context = body.get("_affiliate_context")  # existing-affiliate status, keyed by phone
        mensajes = []
        for msg in body.get("messages", []):
            msg_type = msg.get("type", "text")
            texto = _extraer_texto(msg)

            # Si no hay texto pero es un tipo de media conocido, usar marcador sintético
            if not texto and msg_type in _MEDIA_MARKERS:
                texto = _MEDIA_MARKERS[msg_type]
            # Si el tipo ES audio/voz y además había texto (raro), priorizar marcador
            elif msg_type in ("audio", "voice", "ptt"):
                texto = _MEDIA_MARKERS[msg_type]

            mensajes.append(MensajeEntrante(
                telefono=msg.get("chat_id", ""),
                texto=texto,
                mensaje_id=msg.get("id", ""),
                es_propio=msg.get("from_me", False),
                lead_context=lead_context,
                affiliate_context=affiliate_context,
            ))
        return mensajes

    async def enviar_mensaje(self, telefono: str, mensaje: str) -> bool:
        """Envia mensaje via Whapi.cloud."""
        if not self.token:
            logger.warning("WHAPI_TOKEN no configurado — mensaje no enviado")
            return False
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient() as client:
            r = await client.post(
                self.url_envio,
                json={"to": telefono, "body": mensaje},
                headers=headers,
            )
            if r.status_code != 200:
                logger.error(f"Error Whapi: {r.status_code} — {r.text}")
            return r.status_code == 200

    async def enviar_imagen(self, telefono: str, imagen_url: str, caption: str = "") -> bool:
        """Envia imagen via Whapi.cloud."""
        if not self.token:
            logger.warning("WHAPI_TOKEN no configurado — imagen no enviada")
            return False
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        payload = {
            "to": telefono,
            "media": imagen_url,
        }
        if caption:
            payload["caption"] = caption
        async with httpx.AsyncClient() as client:
            r = await client.post(
                self.url_imagen,
                json=payload,
                headers=headers,
            )
            if r.status_code != 200:
                logger.error(f"Error Whapi imagen: {r.status_code} — {r.text}")
            return r.status_code == 200
