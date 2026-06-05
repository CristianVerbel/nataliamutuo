# agent/providers/base.py — Clase base para proveedores de WhatsApp
# Mutuo Fintech — Bot WhatsApp

"""
Define la interfaz comun que todos los proveedores de WhatsApp deben implementar.
Esto permite cambiar de proveedor sin modificar el resto del codigo.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from fastapi import Request


@dataclass
class MensajeEntrante:
    """Mensaje normalizado — mismo formato sin importar el proveedor."""
    telefono: str             # Numero del remitente
    texto: str                # Contenido del mensaje
    mensaje_id: str           # ID unico del mensaje
    es_propio: bool           # True si lo envio el agente (se ignora)
    lead_context: dict = None # Contexto de retargeting (segmento, tarifa, producto, etc.)
    affiliate_context: dict = None # Estado de afiliado existente (validado por celular)


class ProveedorWhatsApp(ABC):
    """Interfaz que cada proveedor de WhatsApp debe implementar."""

    @abstractmethod
    async def parsear_webhook(self, request: Request) -> list[MensajeEntrante]:
        """Extrae y normaliza mensajes del payload del webhook."""
        ...

    @abstractmethod
    async def enviar_mensaje(self, telefono: str, mensaje: str) -> bool:
        """Envia un mensaje de texto. Retorna True si fue exitoso."""
        ...

    async def enviar_imagen(self, telefono: str, imagen_url: str, caption: str = "") -> bool:
        """Envia una imagen con caption opcional. Retorna True si fue exitoso."""
        return False

    async def validar_webhook(self, request: Request) -> dict | int | None:
        """Verificacion GET del webhook (solo Meta la requiere). Retorna respuesta o None."""
        return None
