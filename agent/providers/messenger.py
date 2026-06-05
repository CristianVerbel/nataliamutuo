# agent/providers/messenger.py — Adaptador para Facebook Messenger (Send API)
# Mutuo Fintech — Bot Messenger para Pauta

import os
import logging
import httpx
from fastapi import Request
from agent.providers.base import ProveedorWhatsApp, MensajeEntrante

logger = logging.getLogger("mutuo-messenger")


class ProveedorMessenger(ProveedorWhatsApp):
    """Proveedor para Facebook Messenger usando la Send API de Meta."""

    def __init__(self):
        self.page_access_token = os.getenv("MESSENGER_PAGE_ACCESS_TOKEN")
        self.verify_token = os.getenv("MESSENGER_VERIFY_TOKEN", "mutuo-messenger-verify")
        self.api_version = "v21.0"

    async def validar_webhook(self, request: Request) -> dict | int | None:
        """Verificación GET requerida por Meta para Messenger."""
        params = request.query_params
        mode = params.get("hub.mode")
        token = params.get("hub.verify_token")
        challenge = params.get("hub.challenge")
        if mode == "subscribe" and token == self.verify_token:
            return challenge  # Meta envía número, pero lo retornamos como string
        return None

    async def parsear_webhook(self, request: Request) -> list[MensajeEntrante]:
        """Parsea el payload de Messenger. El identificador es el PSID del usuario."""
        body = await request.json()
        mensajes: list[MensajeEntrante] = []

        if body.get("object") != "page":
            return mensajes

        for entry in body.get("entry", []):
            for event in entry.get("messaging", []):
                sender_id = event.get("sender", {}).get("id", "")
                if not sender_id:
                    continue

                # Ignorar mensajes enviados por la página misma (eco)
                page_id = event.get("recipient", {}).get("id", "")
                if sender_id == page_id:
                    continue

                message = event.get("message", {})
                postback = event.get("postback", {})

                # Contexto de referral (desde anuncio Click-to-Messenger)
                referral = event.get("referral", {}) or message.get("referral", {})
                lead_context: dict | None = None
                if referral:
                    lead_context = {
                        "ref": referral.get("ref", ""),
                        "source": referral.get("source", ""),
                        "type": referral.get("type", ""),
                        "ad_id": referral.get("ad_id", ""),
                    }

                texto = ""
                if message.get("text"):
                    texto = message["text"]
                elif postback.get("payload"):
                    texto = postback.get("title", postback["payload"])
                elif message.get("attachments"):
                    # Audio, imagen, etc.
                    att_type = message["attachments"][0].get("type", "archivo")
                    tipo_map = {
                        "audio": "[AUDIO_RECIBIDO]",
                        "image": "[IMAGEN_RECIBIDA]",
                        "video": "[VIDEO_RECIBIDO]",
                        "file": "[DOCUMENTO_RECIBIDO]",
                    }
                    texto = tipo_map.get(att_type, "[ARCHIVO_RECIBIDO]")

                if not texto:
                    continue

                mensajes.append(MensajeEntrante(
                    telefono=sender_id,  # PSID actúa como identificador único
                    texto=texto,
                    mensaje_id=message.get("mid", postback.get("mid", sender_id)),
                    es_propio=False,
                    lead_context=lead_context,
                ))

        return mensajes

    async def enviar_mensaje(self, telefono: str, mensaje: str) -> bool:
        """Envía mensaje de texto vía Messenger Send API. `telefono` es el PSID."""
        if not self.page_access_token:
            logger.warning("MESSENGER_PAGE_ACCESS_TOKEN no configurado")
            return False

        url = f"https://graph.facebook.com/{self.api_version}/me/messages"
        params = {"access_token": self.page_access_token}

        # Messenger tiene límite de 2000 chars por mensaje — dividir si hace falta
        chunks = _split_message(mensaje)
        success = True
        async with httpx.AsyncClient(timeout=15) as client:
            for chunk in chunks:
                payload = {
                    "recipient": {"id": telefono},
                    "message": {"text": chunk},
                    "messaging_type": "RESPONSE",
                }
                r = await client.post(url, params=params, json=payload)
                if r.status_code != 200:
                    logger.error(f"[MESSENGER] Error Send API: {r.status_code} — {r.text[:300]}")
                    success = False
        return success

    async def enviar_imagen(self, telefono: str, imagen_url: str, caption: str = "") -> bool:
        """Envía imagen vía Messenger. Caption se envía como mensaje de texto previo."""
        if not self.page_access_token:
            return False

        url = f"https://graph.facebook.com/{self.api_version}/me/messages"
        params = {"access_token": self.page_access_token}

        async with httpx.AsyncClient(timeout=15) as client:
            payload = {
                "recipient": {"id": telefono},
                "message": {
                    "attachment": {
                        "type": "image",
                        "payload": {"url": imagen_url, "is_reusable": True},
                    }
                },
                "messaging_type": "RESPONSE",
            }
            r = await client.post(url, params=params, json=payload)
            if r.status_code != 200:
                logger.error(f"[MESSENGER] Error imagen: {r.status_code} — {r.text[:300]}")
                return False

            if caption:
                await self.enviar_mensaje(telefono, caption)

        return True

    async def enviar_typing(self, telefono: str) -> None:
        """Activa el indicador 'escribiendo...' en Messenger."""
        if not self.page_access_token:
            return
        url = f"https://graph.facebook.com/{self.api_version}/me/messages"
        params = {"access_token": self.page_access_token}
        payload = {
            "recipient": {"id": telefono},
            "sender_action": "typing_on",
        }
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                await client.post(url, params=params, json=payload)
        except Exception:
            pass

    async def obtener_perfil_usuario(self, psid: str) -> dict:
        """Obtiene nombre y foto del usuario desde la Graph API."""
        if not self.page_access_token:
            return {}
        url = f"https://graph.facebook.com/{self.api_version}/{psid}"
        params = {
            "fields": "name,first_name,last_name,profile_pic",
            "access_token": self.page_access_token,
        }
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(url, params=params)
                if r.status_code == 200:
                    return r.json()
        except Exception as e:
            logger.warning(f"[MESSENGER] Error obteniendo perfil de {psid}: {e}")
        return {}


def _split_message(texto: str, max_len: int = 1900) -> list[str]:
    """Divide un mensaje largo en partes de max_len caracteres sin cortar palabras."""
    if len(texto) <= max_len:
        return [texto]
    partes = []
    while texto:
        if len(texto) <= max_len:
            partes.append(texto)
            break
        corte = texto.rfind(" ", 0, max_len)
        if corte == -1:
            corte = max_len
        partes.append(texto[:corte].strip())
        texto = texto[corte:].strip()
    return partes
