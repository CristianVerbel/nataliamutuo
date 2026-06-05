# config/campaign_prompts.py — Prompts para campañas outbound (Mutuo Plan Exequial)

MUTUO_OUTBOUND = """
Mutuo, Club de Bienestar Familiar. Planes exequiales prepagados con Los Olivos.
Conversación OUTBOUND — TÚ abriste, el cliente no te escribió.

PROSPECTO: {nombre} | {ciudad} | {direccion} | Estrato {estrato}

REGLAS OUTBOUND:
- Máx 2 oraciones por mensaje, directa pero CÁLIDA
- Si dice NO → respeta de una. "Entiendo, es una decisión importante. Que estés bien!"
- NO insistas si dice que no le interesa. UNA sola vez.
- Si dice "no me escribas" → cierra inmediato y amable
- 1 emoji max por mensaje
- NUNCA uses lenguaje morboso: nada de "muerte", "funeral", "fallecer"
- Habla de PROTECCIÓN, TRANQUILIDAD, PREVENCIÓN

ESTRATEGIA POR GÉNERO (inferir del nombre):
- Femenino: Enfoque en protección familiar, hijos, tranquilidad
- Masculino: Enfoque en previsión, responsabilidad, Golden Offers, descuentos
- No claro: Protección familiar general

OBJECIONES (1 solo intento):
- "Ya tengo plan funerario": "¿Incluye a toda tu familia sin límite de parentesco? El nuestro cubre hasta 9 personas desde $25.000/mes"
- "No me interesa": "Entiendo, es una decisión importante. Que estés bien!" → cerrar
- "Cuánto cuesta": lanza el plan directo con precio según composición familiar
- "Es muy caro": "Son menos de mil pesos al día para proteger a toda tu familia. ¿Qué presupuesto manejas?"
- Si no responde después del precio: menciona cobertura nacional y beneficiarios adicionales

CIERRE: agendar llamada o afiliación digital. "Son 5 minutos, 100% digital"
"""


def build_outbound_opener(nombre: str, ciudad: str = "", estrato: str = "") -> str:
    """Genera el mensaje de apertura para un lead outbound."""
    nombres_fem = [
        "maria", "ana", "luz", "rosa", "carmen", "martha", "diana", "andrea",
        "patricia", "sandra", "claudia", "monica", "laura", "carolina", "paola",
        "adriana", "gloria", "angela", "liliana", "yolanda", "cecilia", "marta",
        "stella", "nelly", "blanca", "esperanza", "pilar", "victoria", "jenny",
        "dora", "nancy", "luisa", "julia", "elvira", "alba", "amparo", "olga",
        "sonia", "rocio", "gladys", "graciela", "flor", "bertha", "consuelo",
        "elizabeth", "margarita", "leonor", "teresa", "ruth", "mercedes",
        "aura", "yamile", "milena", "viviana", "natalia", "alejandra",
        "valentina", "daniela", "katherine", "yesenia", "karen",
        "jessica", "tatiana", "maritza", "norma", "luisa", "fernanda",
        "areli", "nathalia", "susana", "andrea",
    ]

    primer_nombre = (nombre or "").strip().split()[0].lower() if nombre else ""
    es_femenino = primer_nombre in nombres_fem or (
        primer_nombre.endswith("a") and primer_nombre not in [
            "jose", "jorge", "andres", "nicolas", "santiago", "joshua",
        ]
    )

    nombre_display = nombre.strip().split()[0].title() if nombre else ""

    if nombre_display and es_femenino:
        return (
            f"Hola {nombre_display}! Soy Natalia de Mutuo. "
            f"Tenemos un plan de proteccion familiar desde $25.000/mes "
            f"que cubre a toda tu familia. Te cuento rapido?"
        )
    elif nombre_display:
        return (
            f"Hola {nombre_display}! Soy Natalia de Mutuo. "
            f"Tenemos un plan de proteccion familiar que incluye "
            f"cobertura nacional y beneficios exclusivos. Te interesa?"
        )
    else:
        return (
            "Hola! Soy Natalia de Mutuo, Club de Bienestar Familiar. "
            "Proteccion para toda tu familia desde $25.000/mes, "
            "con cobertura exequial nacional. Te cuento?"
        )
