# tests/test_local.py — Simulador de chat en terminal
# ORIGEN IA — Agente de Ventas Hogar

"""
Prueba ORIGEN IA sin necesitar WhatsApp.
Simula una conversacion de venta en la terminal.
"""

import asyncio
import sys
import os
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from origen_ia.agent.core import OrigenIA


async def main():
    agente = OrigenIA(canal="chat")

    print()
    print("=" * 60)
    print("   Origen AI — Test Local (Natalia)")
    print("=" * 60)
    print()
    print("  Escribe como si fueras un prospecto.")
    print("  Comandos:")
    print("    'estado'   — ver estado interno del agente")
    print("    'perfil'   — ver perfil del cliente")
    print("    'kpis'     — ver registro CRM")
    print("    'salir'    — terminar")
    print()
    print("-" * 60)
    print()

    while True:
        try:
            mensaje = input("Prospecto: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n\nTest finalizado.")
            break

        if not mensaje:
            continue

        if mensaje.lower() == "salir":
            # Mostrar registro CRM final
            print("\n--- REGISTRO CRM ---")
            print(json.dumps(agente.generar_registro_crm(), indent=2, ensure_ascii=False))
            print("\nTest finalizado.")
            break

        if mensaje.lower() == "estado":
            print(f"\n[Estado] {agente.state_machine.to_dict()}")
            print(f"[Temperatura] {agente.profile.temperatura}/10")
            print(f"[Paquete] {agente.recommender.paquete_actual}")
            print(f"[Turnos] {agente.turnos}")
            print(f"[Objeciones] {agente.profile.objeciones_detectadas}\n")
            continue

        if mensaje.lower() == "perfil":
            print(f"\n{agente.profile.to_json()}\n")
            continue

        if mensaje.lower() == "kpis":
            print(f"\n{json.dumps(agente.generar_registro_crm(), indent=2, ensure_ascii=False)}\n")
            continue

        print("\nNatalia: ", end="", flush=True)
        respuesta = await agente.responder(mensaje)
        print(respuesta)
        print(f"  [{agente.state_machine.estado} | T:{agente.profile.temperatura}/10 | {agente.recommender.paquete_actual}]")
        print()

        if agente.state_machine.es_final():
            print("\n--- CONVERSACION FINALIZADA ---")
            print(json.dumps(agente.generar_registro_crm(), indent=2, ensure_ascii=False))
            break


if __name__ == "__main__":
    asyncio.run(main())
