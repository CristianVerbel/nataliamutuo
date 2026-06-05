# crm/lead_record.py — Modelo de datos del lead para Vendu CRM

from dataclasses import dataclass, field, asdict
from datetime import datetime


@dataclass
class LeadRecord:
    """Registro de lead para Vendu CRM."""
    timestamp: str = ""
    agente: str = "Natalia"
    canal: str = "whatsapp"
    estado_final: str = ""
    perfil_cliente: dict = field(default_factory=dict)
    paquete_ofrecido: str = ""
    paquete_cerrado: str = ""
    objeciones_encontradas: list = field(default_factory=list)
    intentos_cierre: int = 0
    temperatura_final: int = 0
    duracion_conversacion_turnos: int = 0
    motivo_perdida: str = ""
    datos_instalacion: dict = field(default_factory=dict)
    requiere_seguimiento_humano: bool = False
    notas_para_closer: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        if not d["timestamp"]:
            d["timestamp"] = datetime.utcnow().isoformat()
        return d
