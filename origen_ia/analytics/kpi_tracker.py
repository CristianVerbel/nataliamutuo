# analytics/kpi_tracker.py — Tracking de metricas de ventas

import json
import logging
from datetime import datetime
from collections import Counter

logger = logging.getLogger("origen-ia")


class KPITracker:
    """Trackea metricas de rendimiento del agente."""

    def __init__(self):
        self.leads_marcados = 0
        self.leads_contactados = 0
        self.leads_calificados = 0
        self.ofertas_hechas = 0
        self.ventas_cerradas = 0
        self.ventas_con_ott = 0
        self.ventas_triple = 0
        self.temperaturas_cierre = []
        self.objeciones_counter = Counter()
        self.turnos_hasta_cierre = []
        self.sesiones = []

    def registrar_sesion(self, registro_crm: dict):
        """Registra una sesion de venta completada."""
        self.sesiones.append(registro_crm)
        self.leads_marcados += 1
        self.leads_contactados += 1

        estado = registro_crm.get("estado_final", "")
        if estado not in ("PROSPECTO",):
            self.leads_calificados += 1

        paquete_ofrecido = registro_crm.get("paquete_ofrecido", "")
        if paquete_ofrecido:
            self.ofertas_hechas += 1

        if estado == "CERRADO_GANADO":
            self.ventas_cerradas += 1
            paquete = registro_crm.get("paquete_cerrado", "")
            if "ott" in paquete.lower():
                self.ventas_con_ott += 1
            if "triple" in paquete.lower():
                self.ventas_triple += 1
            self.temperaturas_cierre.append(registro_crm.get("temperatura_final", 0))
            self.turnos_hasta_cierre.append(registro_crm.get("duracion_conversacion_turnos", 0))

        for obj in registro_crm.get("objeciones_encontradas", []):
            self.objeciones_counter[obj] += 1

    def get_kpis(self) -> dict:
        """Calcula y retorna todas las metricas."""
        return {
            "tasa_contacto": self._safe_div(self.leads_contactados, self.leads_marcados),
            "tasa_calificacion": self._safe_div(self.leads_calificados, self.leads_contactados),
            "tasa_oferta": self._safe_div(self.ofertas_hechas, self.leads_calificados),
            "tasa_cierre": self._safe_div(self.ventas_cerradas, self.ofertas_hechas),
            "tasa_ott": self._safe_div(self.ventas_con_ott, self.ventas_cerradas),
            "tasa_triple": self._safe_div(self.ventas_triple, self.ventas_cerradas),
            "temperatura_promedio_cierre": (
                sum(self.temperaturas_cierre) / len(self.temperaturas_cierre)
                if self.temperaturas_cierre else 0
            ),
            "objecion_mas_frecuente": (
                self.objeciones_counter.most_common(3) if self.objeciones_counter else []
            ),
            "promedio_turnos_hasta_cierre": (
                sum(self.turnos_hasta_cierre) / len(self.turnos_hasta_cierre)
                if self.turnos_hasta_cierre else 0
            ),
            "total_sesiones": len(self.sesiones),
            "total_ventas": self.ventas_cerradas,
        }

    def _safe_div(self, a: int, b: int) -> float:
        return round(a / b, 4) if b > 0 else 0.0

    def log_kpis(self):
        """Imprime KPIs en el log."""
        kpis = self.get_kpis()
        logger.info(f"[KPIs] {json.dumps(kpis, ensure_ascii=False)}")
