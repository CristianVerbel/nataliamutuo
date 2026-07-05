# Integración bot ⇄ sistema principal (mutuoventas)

Objetivo: que el bot y el sistema principal sean **independientes en fallas**.
El bot solo comparte con el sistema un **contrato** (API + webhooks); cada uno
tiene su propio repo, deploy y base de datos.

## Inventario de acoplamiento actual (medido en el código)

### El bot lee/escribe DIRECTO en la BD del sistema (PostgREST)

**Estado propio del bot** → debe migrar a la base PROPIA del bot (Fase 2):

| Tabla | Uso |
|---|---|
| `whatsapp_conversations` | sesiones/estado de conversación |
| `whatsapp_messages` | historial de mensajes |
| `messenger_conversations` / `messenger_messages` | ídem Messenger |
| `bot_interaction_settings` | preferencias del bot |

**Datos de negocio** → quedan en el sistema; el bot los consumirá por gateway (Fase 3):

| Tabla | Uso desde el bot |
|---|---|
| `b2c_affiliations` (19 refs) | estado del afiliado por celular |
| `payment_transactions` | deuda/cuotas |
| `plans`, `benefits` | catálogo |
| `lead_database_entries` | leads para outbound |
| `cancellation_requests`, `customer_tasks`, `advisors` | operaciones |
| `affiliation_audit_log`, `payment_portfolio_history`, `voice_call_sessions`, `report_recipients`, `ai_knowledge_base` | varios |

### Edge functions del sistema que el bot llama

`whatsapp-sync` (CRM), `send-payment-receipt`, `send-client-welcome-all`,
`send-alert`, `send-cancellation-admin-alert`, `voice-knowledge-base`,
`mercadopago-webhook`.

### El sistema llama AL bot

- `whapi-inbound-webhook` (edge) reenvía mensajes entrantes al bot.
- Admin UI (`AdminWhatsApp*.tsx`) consulta la API del bot (`BOT_INTERNAL_API_KEY`).

## Plan por fases

### Fase 1 — Repo independiente ✅
Código del bot extraído con historial (`git subtree split`) a `mutuo-whatsapp-bot`.
Railway pasa a desplegar desde este repo. Cero cambio de comportamiento.

### Fase 2 — Base de datos propia del bot
- Crear Postgres propio (recomendado: **Railway Postgres**, junto al bot; si
  Supabase del sistema se cae, el bot conserva memoria y sesiones).
- Migrar las 5 tablas de estado del bot + copiar datos.
- `DATABASE_URL` propia en el bot; retirar esas tablas del acceso al Supabase
  del sistema.

### Fase 3 — Gateway API (contrato único)
En el sistema, un edge function `bot-gateway` autenticado con `x-bot-secret`:

```
POST /bot-gateway { action, payload }
  affiliate.lookup        { phone }            → estado del afiliado
  affiliate.debt          { affiliation_id }   → cuotas/deuda + link de pago
  plans.list              {}                   → catálogo
  leads.next / leads.update                    → outbound
  cancellation.create / task.create            → operaciones
  audit.log                                    → auditoría
```

Y del sistema hacia el bot, webhooks firmados (`x-bot-secret`):

```
POST {BOT_URL}/events
  payment.received   → el bot agradece/confirma en el chat
  affiliation.created→ bienvenida conversacional
```

Reglas de resiliencia:
- Timeout corto (5s) + reintento exponencial en el bot al llamar al gateway.
- Si el gateway no responde: el bot **degrada** (responde sin datos de cuenta,
  encola la acción) en vez de fallar la conversación.
- Si el bot no responde: el sistema encola el evento y reintenta (pg_cron).

## Secretos del contrato

- `WHATSAPP_BOT_SECRET`: firma bot ⇄ sistema (ya existe).
- `BOT_INTERNAL_API_KEY`: auth de la API del bot para el Admin UI (ya existe).
- Rotarlos al completar la separación.
