# agent/closer.py — Modulo de cierres

CIERRES = {
    "directo": (
        "Listo, entonces procedemos. Me confirmas la direccion exacta "
        "para la instalacion y tu nombre completo?"
    ),
    "alternativo": (
        "Arrancamos con el completo que incluye entretenimiento digital, "
        "o prefieres solo internet por ahora?"
    ),
    "urgencia": (
        "La disponibilidad de instalacion en tu zona para esta semana se "
        "esta llenando. Si lo agendamos hoy, te garantizo la franja del "
        "horario que prefieras."
    ),
    "resumen_valor": (
        "Entonces para resumir: tienes internet de alta velocidad, TV, "
        "linea fija y entretenimiento digital, todo por {precio} al mes, "
        "instalacion a domicilio, sin costo adicional. Lo agendamos?"
    ),
    "asuncion": (
        "Para que direccion seria la instalacion?"
    ),
}

DATOS_CIERRE_PREGUNTAS = {
    "nombre_completo": "Me confirmas tu nombre completo tal como aparece en tu cedula?",
    "cedula": "Y tu numero de cedula?",
    "direccion": "La direccion exacta donde hacemos la instalacion? Incluye apto o torre si aplica.",
    "telefono": "Un numero de contacto donde te pueda llamar el tecnico?",
    "email": "Tu correo electronico para enviarte la confirmacion?",
    "franja_instalacion": "Que franja prefieres: manana (8am a 1pm) o tarde (1pm a 6pm)?",
    "fecha_instalacion": "Y para que fecha te gustaria agendar la instalacion?",
}

CONFIRMACION_FINAL = (
    "Perfecto {nombre}. Te confirmo que quedaste agendado(a) para {fecha} "
    "en la franja de {franja}. El tecnico te llamara antes de llegar. "
    "Tienes alguna duda antes de cerrar?"
)
