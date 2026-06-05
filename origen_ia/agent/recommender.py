# agent/recommender.py — Motor de recomendación de plan exequial
# Mutuo Fintech — Origen IA

from origen_ia.config.products import PAQUETES, PLAN_ORDER


class PackageRecommender:
    """Recomienda plan exequial según perfil del cliente."""

    def __init__(self):
        # Default: Familia Plus (mejor relación precio/valor)
        self.paquete_actual = "familia_plus"
        self.historial_ofertas = ["familia_plus"]

    def get_paquete_actual(self) -> dict:
        """Retorna el plan actualmente recomendado."""
        return PAQUETES.get(self.paquete_actual, PAQUETES["familia_plus"])

    def set_paquete(self, plan_key: str):
        """Establece un plan específico."""
        if plan_key in PAQUETES:
            self.paquete_actual = plan_key
            if plan_key not in self.historial_ofertas:
                self.historial_ofertas.append(plan_key)

    def bajar_paquete(self, **kwargs) -> dict | None:
        """Baja al siguiente plan más barato."""
        try:
            idx = PLAN_ORDER.index(self.paquete_actual)
        except ValueError:
            idx = 1  # Default al medio

        if idx <= 0:
            return None  # Ya está en el mínimo

        self.paquete_actual = PLAN_ORDER[idx - 1]
        self.historial_ofertas.append(self.paquete_actual)
        return PAQUETES[self.paquete_actual]

    def subir_paquete(self) -> dict | None:
        """Sube al siguiente plan más caro (upsell)."""
        try:
            idx = PLAN_ORDER.index(self.paquete_actual)
        except ValueError:
            return None

        if idx >= len(PLAN_ORDER) - 1:
            return None

        self.paquete_actual = PLAN_ORDER[idx + 1]
        self.historial_ofertas.append(self.paquete_actual)
        return PAQUETES[self.paquete_actual]

    def recomendar_por_perfil(self, perfil: dict) -> str:
        """Recomienda el mejor plan según el perfil del cliente."""
        # Padres/suegros mayores → Plus o Total
        tiene_mayores = perfil.get("tiene_padres_mayores")
        if tiene_mayores:
            num_mayores = perfil.get("num_beneficiarios", 0)
            # Si tiene 2+ familiares mayores → Total (2 sin límite de edad)
            if num_mayores and num_mayores > 8:
                return "familia_total"
            return "familia_plus"

        # Mascotas → Plus (incluye 1 mascota con beneficios; en Total la mascota es con pago adicional)
        if perfil.get("tiene_mascotas"):
            return "familia_plus"

        # Busca lo más completo (exhumación, columbario, 2 cupos sin límite) → Total
        # Nota: Golden Offers ya viene en los 3 planes, no es diferenciador.
        motivacion = (perfil.get("motivacion", "") or "").lower()
        if any(kw in motivacion for kw in ["lo mejor", "completo", "premium", "exhumaci", "columbario"]):
            return "familia_total"

        # Presupuesto bajo → Esencial
        disposicion = (perfil.get("disposicion_pago", "") or "").lower()
        if any(kw in disposicion for kw in ["barato", "económico", "poco", "bajo"]):
            return "familia_esencial"

        # Default: Plus (mejor relación precio/valor)
        return "familia_plus"

    def puede_bajar(self) -> bool:
        """Si hay un plan inferior disponible."""
        try:
            idx = PLAN_ORDER.index(self.paquete_actual)
            return idx > 0
        except ValueError:
            return False

    def puede_subir(self) -> bool:
        """Si hay un plan superior disponible."""
        try:
            idx = PLAN_ORDER.index(self.paquete_actual)
            return idx < len(PLAN_ORDER) - 1
        except ValueError:
            return False

    def to_dict(self) -> dict:
        paq = PAQUETES.get(self.paquete_actual, {})
        return {
            "paquete_actual": self.paquete_actual,
            "nombre_paquete": paq.get("nombre", self.paquete_actual),
            "precio": paq.get("precio", ""),
            "historial_ofertas": self.historial_ofertas,
            "puede_bajar": self.puede_bajar(),
            "puede_subir": self.puede_subir(),
        }
