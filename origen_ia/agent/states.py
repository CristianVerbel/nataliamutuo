# agent/states.py — Maquina de estados del lead

from origen_ia.config.rules import TRANSICIONES


class LeadStateMachine:
    """Gestiona el estado del lead durante la conversacion."""

    def __init__(self):
        self.estado = "PROSPECTO"
        self.historial_estados = ["PROSPECTO"]

    def puede_transicionar(self, nuevo_estado: str) -> bool:
        return nuevo_estado in TRANSICIONES.get(self.estado, [])

    def transicionar(self, nuevo_estado: str) -> bool:
        if self.puede_transicionar(nuevo_estado):
            self.estado = nuevo_estado
            self.historial_estados.append(nuevo_estado)
            return True
        return False

    def es_final(self) -> bool:
        return self.estado in ("CERRADO_GANADO", "CERRADO_PERDIDO")

    def fase_conversacional(self) -> str:
        """Mapea estado a fase de la conversacion."""
        mapa = {
            "PROSPECTO": "APERTURA",
            "CONTACTADO": "CALIFICACION",
            "CALIFICADO": "PRESENTACION_OFERTA",
            "OFERTA_ENVIADA": "PRESENTACION_OFERTA",
            "OBJECION_ACTIVA": "MANEJO_OBJECIONES",
            "NEGOCIANDO": "CIERRE",
            "CERRADO_GANADO": "CIERRE_CONFIRMADO",
            "CERRADO_PERDIDO": "FINALIZADO",
        }
        return mapa.get(self.estado, "APERTURA")

    def to_dict(self) -> dict:
        return {
            "estado": self.estado,
            "fase": self.fase_conversacional(),
            "historial": self.historial_estados,
        }
