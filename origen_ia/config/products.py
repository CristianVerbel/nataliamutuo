# config/products.py — Catálogo completo de planes Mutuo Plan Exequial
# Mutuo Fintech S.A.S. — Origen IA
#
# 3 planes principales + addon. Proveedor: Los Olivos (Centralco Ltda).
# Las imágenes se sirven desde Supabase Storage (bucket: plan-images).

import os

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
STORAGE_BASE = f"{SUPABASE_URL}/storage/v1/object/public/plan-images" if SUPABASE_URL else ""

# ═══════════════════════════════════════════════════
# CATÁLOGO DE PLANES — Ordenados por precio ascendente
# ═══════════════════════════════════════════════════

PAQUETES = {
    # ── PLAN FAMILIA ESENCIAL ──────────────────────
    "familia_esencial": {
        "nombre": "Plan Familia Esencial",
        "tipo": "basico",
        "precio": "$25.000",
        "precio_num": 25000,
        "orden": 1,
        "cobertura": "Titular + 5 beneficiarios (sin importar parentesco, menores de 69 años)",
        "incluye": [
            "1 evento/homenaje cubierto al año",
            "Sala de homenaje hasta 24 horas (Sede Murillo o Sede 38)",
            "Inhumación en lote por 4 años o cremación con urna cineraria en Parque Los Olivos",
            "Traslado del cuerpo entre ciudades hasta 300 km",
            "3 referencias de cofre a elegir",
            "Ofrenda floral incluida",
            "Carroza de lujo + transporte para 25 personas",
            "Trámites legales completos (certificado de defunción)",
            "Conjunto recordatorio + video homenaje + honras fúnebres",
            "1 mascota incluida con beneficios (recogida, incineración colectiva, kit de homenaje y apoyo psicológico)",
            "Tarjeta Golden Offers con descuentos en comercios aliados",
        ],
        "beneficios_extra": [
            "1 mascota incluida (perro o gato, 3 meses a 5 años)",
            "Tarjeta Golden Offers",
        ],
        "golden_offers": True,
        "exhumacion": False,
        "columbario": False,
        "imagen": f"{STORAGE_BASE}/exequial_familia_feed_1080x1350.png",
        "argumento": (
            "Protección exequial completa para toda tu familia por solo "
            "$25.000/mes. Sala de homenaje, cremación o inhumación, traslados, "
            "1 mascota incluida y Tarjeta Golden Offers. Menos de un café al día."
        ),
        "triggers": [
            "barato", "económico", "basico", "esencial", "sencillo",
            "lo más barato", "precio bajo", "algo básico",
        ],
    },

    # ── PLAN FAMILIA PLUS ──────────────────────────
    "familia_plus": {
        "nombre": "Plan Familia Plus",
        "tipo": "intermedio",
        "precio": "$29.900",
        "precio_num": 29900,
        "orden": 2,
        "cobertura": "Titular + 7 beneficiarios (sin importar parentesco) + 1 sin límite de edad",
        "incluye": [
            "2 eventos/homenajes cubiertos al año",
            "Sala de homenaje hasta 24 horas (Sede Murillo, Sede 38 o Sede Parque Cementerio)",
            "Inhumación en lote por 4 años o cremación con urna cineraria en Parque Los Olivos",
            "Traslado del cuerpo entre ciudades hasta 300 km",
            "6 referencias de cofre a elegir",
            "Ofrenda floral incluida",
            "Carroza de lujo + transporte para 25 personas",
            "Trámites legales completos (certificado de defunción)",
            "Conjunto recordatorio + video homenaje + honras fúnebres",
            "1 mascota incluida con beneficios (recogida, incineración colectiva, kit de homenaje y apoyo psicológico)",
            "Tarjeta Golden Offers con descuentos en comercios aliados",
        ],
        "beneficios_extra": [
            "1 beneficiario sin límite de edad (cónyuge, padre, madre o suegros)",
            "1 mascota incluida (perro o gato, 3 meses a 5 años)",
            "Tarjeta Golden Offers",
        ],
        "golden_offers": True,
        "exhumacion": False,
        "columbario": False,
        "imagen": f"{STORAGE_BASE}/exequial_tranquilidad_feed_1080x1350.png",
        "argumento": (
            "El plan más elegido. Cubre hasta 9 personas incluyendo 1 familiar "
            "sin límite de edad (cónyuge, padre, madre o suegros), 1 mascota incluida "
            "y Tarjeta Golden Offers. $29.900/mes, menos de mil pesos al día."
        ),
        "triggers": [
            "plus", "intermedio", "padres", "suegros", "sin limite de edad",
            "familiar mayor", "persona mayor", "adulto mayor", "abuela", "abuelo",
            "mama", "papa", "edad",
        ],
    },

    # ── PLAN FAMILIA TOTAL ─────────────────────────
    "familia_total": {
        "nombre": "Plan Familia Total",
        "tipo": "premium",
        "precio": "$38.000",
        "precio_num": 38000,
        "orden": 3,
        "cobertura": "Titular + 6 beneficiarios (con parentesco) + 2 sin límite de edad",
        "incluye": [
            "Eventos/homenajes ilimitados al año",
            "Sala de homenaje hasta 24 horas (todas las sedes)",
            "Inhumación en lote por 4 años o cremación con urna cineraria en Parque Los Olivos",
            "Traslado del cuerpo entre ciudades hasta 300 km",
            "4 referencias de cofre a elegir",
            "Ofrenda floral incluida",
            "Carroza de lujo + transporte para 25 personas",
            "Trámites legales completos (certificado de defunción)",
            "Conjunto recordatorio + video homenaje + honras fúnebres",
            "Exhumación incluida",
            "Columbario incluido (el plan debe permanecer activo)",
            "Tarjeta Golden Offers con descuentos exclusivos",
        ],
        "beneficios_extra": [
            "2 beneficiarios sin límite de edad (padres y/o suegros)",
            "6 beneficiarios con parentesco (cónyuge, hijos, nietos, hermanos, tíos, sobrinos, primos, yernos, padres y suegros)",
            "Exhumación y columbario incluidos",
            "Mascota con pago adicional + asistencias en vida (veterinario, paseo, guardería, legal) con pago adicional",
            "Tarjeta Golden Offers: descuentos en McDonald's, Juan Valdez, PriceSmart y 20+ aliados",
        ],
        "golden_offers": True,
        "exhumacion": True,
        "columbario": True,
        "imagen": f"{STORAGE_BASE}/exequial_completo_feed_1080x1350.png",
        "argumento": (
            "La protección más completa. Cubre toda tu familia incluyendo 2 familiares "
            "sin límite de edad, exhumación y columbario incluidos, y la tarjeta Golden "
            "Offers con descuentos en McDonald's, Juan Valdez, PriceSmart y más. $38.000/mes."
        ),
        "triggers": [
            "total", "completo", "premium", "mejor", "todo incluido",
            "mascota", "perro", "gato", "golden", "descuento",
            "lo mejor", "el mas completo", "todos los beneficios",
        ],
    },
}

# Orden de preferencia para escalamiento (de más barato a más caro)
PLAN_ORDER = [
    "familia_esencial", "familia_plus", "familia_total",
]

# Todos los planes incluyen
BENEFICIOS_GENERALES = [
    "Cobertura nacional a través de la red Los Olivos",
    "Tarjeta Golden Offers con descuentos en comercios aliados",
    "Carroza de lujo y transporte para 25 personas",
    "Período de carencia: 90 días desde la activación",
    "Sin exámenes médicos ni preexistencias",
    "Hijos cubiertos desde la concepción",
    "Afiliación 100% digital en menos de 5 minutos",
    "Renovación automática cada 12 meses",
    "Cobertura en toda Colombia",
]
# Nota: los eventos/homenajes cubiertos por año varían según el plan
# (Esencial 1, Plus 2, Total ilimitado).

# Adicionales
ADDON_INFO = {
    "precio": "$9.900/mes",
    "precio_num": 9900,
    "descripcion": "Beneficiario adicional (menor de 69 años, cualquier parentesco)",
    "carencia": "120 días para cobertura funeraria",
}

ARGUMENTOS_POR_PERFIL = {
    "familia_grande": "cubre toda la familia, hijos desde la concepción, beneficiarios adicionales a $9.900",
    "padres_mayores": "1 o 2 beneficiarios sin límite de edad para padres y suegros",
    "joven_previsor": "protege a tu familia desde $25.000/mes, menos de un café al día",
    "amante_mascotas": "Planes Esencial y Plus incluyen 1 mascota con beneficios; en Total la mascota va con pago adicional",
    "busca_descuentos": "Tarjeta Golden Offers incluida en los 3 planes: descuentos en McDonald's, Juan Valdez, PriceSmart y 20+ aliados",
    "presupuesto_bajo": "Plan Esencial desde $25.000/mes, protección completa para 6 personas + mascota + Golden Offers",
    "adulto_mayor_hogar": "beneficiarios sin límite de edad, trámites completos, sala de homenaje incluida",
}
