# agent/tools.py — Herramientas del agente
# Mutuo Fintech — Bot WhatsApp

"""
Herramientas especificas para el bot de ventas de Mutuo.
Calificacion de leads y productos por zona.
"""

import yaml
import logging

logger = logging.getLogger("mutuo-bot")


def cargar_info_negocio() -> dict:
    """Carga la informacion del negocio desde business.yaml."""
    try:
        with open("config/business.yaml", "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        logger.error("config/business.yaml no encontrado")
        return {}


# Zonas de cobertura Los Olivos — cobertura nacional
# Mercado principal: Barranquilla / Costa Caribe
COSTA_CARIBE = ["Atlantico", "Magdalena", "La Guajira", "Cesar", "Bolivar", "Sucre", "Cordoba"]
CENTRAL = ["Bogota D.C.", "Cundinamarca", "Santander", "Boyaca"]
EJE_CAFETERO = ["Caldas", "Risaralda", "Quindio", "Antioquia"]
SUROCCIDENTE = ["Valle del Cauca", "Cauca", "Narino"]


def productos_disponibles_por_zona(departamento: str) -> list[str]:
    """Retorna los planes disponibles segun el departamento.
    Todos los planes exequiales tienen cobertura nacional via Los Olivos."""
    planes = ["Plan Familia Esencial", "Plan Familia Plus", "Plan Familia Total"]

    dep_normalizado = departamento.strip()

    # Todos los departamentos tienen acceso a los mismos planes
    # Los Olivos tiene cobertura nacional
    if dep_normalizado in COSTA_CARIBE:
        planes.append("Golden Offers disponible con aliados locales")

    return planes


def obtener_horario() -> dict:
    """Retorna el horario de atencion del negocio."""
    info = cargar_info_negocio()
    return {
        "horario": info.get("negocio", {}).get("horario", "Lunes a Sabado 7am a 8pm"),
    }
