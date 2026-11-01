# Emailia 🤖

Emailia is an autonomous email triage agent built as a [Mistral Workflow](https://docs.mistral.ai/studio/workflows).

## What she does

On a schedule, Emailia:

1. Reads the personal inboxes: **Gmail-main**, **Gmail-odr**, **Outlook**
2. Classifies every new message:
   - **Noise** (skipped, stays at source): social notifications, promotions, marketing, newsletters
   - **Relevant** (default): mail from real humans, school, receipts, invoices, invites, appointments
3. Forwards relevant messages to the **Life hub** (`lifeproj.drm@gmail.com`)
4. Labels routed threads at the source (`Agent/Routed`) so they are never processed twice
5. Sends a run report to the hub

## Credentials

No secrets live in this repository. The workflow uses Mistral Connector slots,
pinned to named credentials resolved at runtime by the Mistral platform:

| Slot | Credential | Mailbox |
|---|---|---|
| `gmail_main` | `gmail-main` | personal Gmail #1 |
| `gmail_odr` | `gmail-odr` | personal Gmail #2 |
| `gmail_hub` | `gmail_cred15736822` | Life hub (destination) |
| `outlook_conn` | default | Outlook inbox |

## Run

Input:

```json
{"lookback_hours": 12, "dry_run": false}
```

Set `dry_run: true` to classify without forwarding (report lists what would be routed).
