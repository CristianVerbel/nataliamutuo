# agent/providers/__init__.py — Factory de proveedores
# Mutuo Fintech — Bot WhatsApp

"""
Selecciona el proveedor de WhatsApp segun la variable WHATSAPP_PROVIDER en .env.
"""

import os
from agent.providers.base import ProveedorWhatsApp


def obtener_proveedor() -> ProveedorWhatsApp:
    """Retorna el proveedor de WhatsApp configurado en .env."""
    proveedor = os.getenv("WHATSAPP_PROVIDER", "whapi").lower()

    if proveedor == "whapi":
        from agent.providers.whapi import ProveedorWhapi
        return ProveedorWhapi()
    elif proveedor == "meta":
        from agent.providers.meta import ProveedorMeta
        return ProveedorMeta()
    elif proveedor == "twilio":
        from agent.providers.twilio import ProveedorTwilio
        return ProveedorTwilio()
    elif proveedor == "messenger":
        from agent.providers.messenger import ProveedorMessenger
        return ProveedorMessenger()
    else:
        raise ValueError(f"Proveedor no soportado: {proveedor}. Usa: whapi, meta, twilio o messenger")
