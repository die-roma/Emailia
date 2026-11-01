"""Emailia - autonomous email triage agent.

Reads the personal inboxes (Gmail-main, Gmail-odr, Outlook), filters out
noise (social notifications, promotions, newsletters), forwards relevant
mail to the Life hub (lifeproj.drm@gmail.com), labels routed threads at
the source, and sends a run report to the hub.
"""

from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import BaseModel

import mistralai.workflows as workflows
from mistralai.workflows import Depends
from mistralai.workflows.plugins.mistralai.connectors import (
    ToolCallClient,
    connector,
    uses_connectors,
)

HUB_ADDRESS = "lifeproj.drm@gmail.com"
ROUTED_LABEL = "Agent/Routed"
MAX_THREADS = 25

# ---------------------------------------------------------------------------
# Connector slots: one named credential per inbox
# ---------------------------------------------------------------------------
gmail_main = connector("gmail", credentials_name="gmail-main")
gmail_odr = connector("gmail", credentials_name="gmail-odr")
gmail_hub = connector("gmail", credentials_name="gmail_cred15736822")
outlook_conn = connector("outlook")

# ---------------------------------------------------------------------------
# Classification rules
# ---------------------------------------------------------------------------
NOISE_DOMAINS = {
    "facebookmail.com", "instagram.com", "linkedin.com", "lnkd.in",
    "twitter.com", "x.com", "tiktok.com", "pinterest.com", "medium.com",
    "substack.com", "mailchimp.com", "sendinblue.com", "hubspotmail.com",
    "klaviyo.com", "sendgrid.net", "criteo.com", "adnxs.com",
}

NOISE_SENDER_TOKENS = (
    "no-reply", "noreply", "no_reply", "donotreply", "do-not-reply",
    "newsletter", "promotion", "promotions", "deals@", "news@",
)

NOISE_LABELS = {"CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL", "CATEGORY_FORUMS"}

NOISE_SUBJECT_PATTERNS = (
    "unsubscribe", "newsletter", "digest", "promotion", "promo code",
    "black friday", "cyber monday", "flash sale", "limited time",
    "% off", "%off", "giveaway",
)


def classify(sender: str, subject: str, label_ids: Optional[list]) -> tuple:
    """Return (verdict, reason). Verdict is 'relevant' or 'noise'."""
    s = (sender or "").lower()
    subj = (subject or "").lower()
    labels = {str(x).upper() for x in (label_ids or [])}

    if labels & NOISE_LABELS:
        return "noise", "gmail-category-label"

    domain = s.split("@")[-1] if "@" in s else ""
    if domain in NOISE_DOMAINS:
        return "noise", f"noise-domain:{domain}"

    for token in NOISE_SENDER_TOKENS:
        if token in s:
            return "noise", f"noise-sender:{token}"

    for pattern in NOISE_SUBJECT_PATTERNS:
        if pattern in subj:
            return "noise", f"noise-subject:{pattern}"

    return "relevant", "default-relevant"


# ---------------------------------------------------------------------------
# Defensive helpers for unknown connector tool schemas
# ---------------------------------------------------------------------------
def _dig(obj: Any, *paths, default=None):
    for path in paths:
        cur = obj
        found = True
        for key in path:
            if isinstance(cur, dict) and key in cur:
                cur = cur[key]
            else:
                found = False
                break
        if found and cur is not None:
            return cur
    return default


async def _call(client: ToolCallClient, tool: str, arg_variants: list):
    """Call a connector tool, trying several argument-key spellings."""
    last_error = None
    for args in arg_variants:
        try:
            res = await client.call_tool(tool_name=tool, arguments=args)
            return True, res
        except Exception as exc:  # noqa: BLE001
            last_error = exc
    return False, last_error


# ---------------------------------------------------------------------------
# Gmail triage
# ---------------------------------------------------------------------------
async def _ensure_routed_label(source: ToolCallClient):
    ok, res = await _call(source, "list_labels", [{}])
    labels = _dig(res, ("labels",), ("result", "labels"), default=[]) or []
    for label in labels:
        name = _dig(label, ("name",), ("displayName",), default="")
        if name == ROUTED_LABEL:
            return _dig(label, ("id",), ("labelId",), default=None)
    ok, res = await _call(source, "create_label", [{"name": ROUTED_LABEL}])
    return _dig(res, ("id",), ("label", "id"), ("result", "id"), default=None)


async def _triage_gmail(source: ToolCallClient, source_name: str, lookback_hours: int, dry_run: bool) -> dict:
    stats = {
        "mailbox": source_name,
        "kind": "gmail",
        "scanned": 0,
        "routed": [],
        "noise": 0,
        "errors": [],
    }

    query = f'in:inbox newer_than:{lookback_hours}h -label:"{ROUTED_LABEL}"'
    ok, res = await _call(source, "search_threads", [
        {"query": query, "pageSize": MAX_THREADS},
        {"query": query},
    ])
    if not ok:
        stats["errors"].append(f"search failed: {res}")
        return stats

    threads = _dig(res, ("threads",), ("result", "threads"), ("data", "threads"), default=[]) or []
    label_id = await _ensure_routed_label(source)

    for thread in threads:
        messages = _dig(thread, ("messages",), default=[]) or []
        if not messages:
            continue
        msg = messages[-1]
        sender = _dig(msg, ("sender",), ("from",), default="")
        subject = _dig(msg, ("subject",), default="")
        snippet = _dig(msg, ("snippet",), ("bodyPreview",), default="")
        date = _dig(msg, ("date",), default="")
        thread_id = _dig(msg, ("id",), ("threadId",), default=None) or _dig(thread, ("id",), default=None)
        label_ids = _dig(msg, ("labelIds",), default=[])

        verdict, reason = classify(sender, subject, label_ids)
        stats["scanned"] += 1
        if verdict == "noise":
            stats["noise"] += 1
            continue

        # skip mail the hub itself sent (avoid loops)
        if HUB_ADDRESS in (sender or "").lower():
            continue

        # try to fetch a fuller body for context
        body = snippet
        okb, resb = await _call(source, "get_thread", [
            {"threadId": thread_id},
            {"id": thread_id},
        ])
        if okb:
            msgs = _dig(resb, ("messages",), ("result", "messages"), default=[]) or []
            if msgs:
                last = msgs[-1]
                body = _dig(last, ("plaintextBody",), ("body",), default=None) or snippet

        body_text = (body or snippet or "")[:4000]
        if dry_run:
            stats["routed"].append({"from": sender, "subject": subject, "date": date, "dry_run": True})
            continue

        html = (
            "<div style='font-family:sans-serif;max-width:640px'>"
            f"<p><b>Original sender:</b> {sender}</p>"
            f"<p><b>Date:</b> {date}</p>"
            f"<p><b>Subject:</b> {subject}</p>"
            "<hr/><div>"
            + (body_text.replace("\n", "<br/>"))
            + "</div></div>"
        )
        oks, _ = await _call(source, "send_email", [
            {"to": [HUB_ADDRESS], "subject": f"[Emailia → Life] {subject}", "htmlBody": html},
            {"toRecipients": [HUB_ADDRESS], "subject": f"[Emailia → Life] {subject}", "htmlBody": html},
            {"to": HUB_ADDRESS, "subject": f"[Emailia → Life] {subject}", "htmlBody": html},
        ])
        if not oks:
            stats["errors"].append(f"forward failed for '{subject}'")
            continue

        if label_id and thread_id:
            await _call(source, "label_thread", [
                {"threadId": thread_id, "labelId": label_id},
                {"id": thread_id, "labelId": label_id},
                {"id": thread_id, "label_id": label_id},
            ])

        stats["routed"].append({"from": sender, "subject": subject, "date": date})

    return stats


# ---------------------------------------------------------------------------
# Outlook triage
# ---------------------------------------------------------------------------
async def _triage_outlook(outlook: ToolCallClient, dry_run: bool) -> dict:
    stats = {
        "mailbox": "Outlook",
        "kind": "outlook",
        "scanned": 0,
        "routed": [],
        "noise": 0,
        "errors": [],
    }

    ok, res = await _call(outlook, "search_outlook", [
        {"max_results": MAX_THREADS},
        {"pageSize": MAX_THREADS},
        {},
    ])
    if not ok:
        stats["errors"].append(f"search failed: {res}")
        return stats

    items = _dig(res, ("results",), ("value",), ("messages",), ("data",), default=[]) or []
    if isinstance(items, dict):
        items = _dig(items, ("results",), ("value",), default=[]) or []

    for item in items:
        sender = _dig(item, ("sender",), ("from",), ("metadata", "sender"), default="") or ""
        if isinstance(sender, dict):
            sender = _dig(sender, ("email",), ("address",), ("name",), default=str(sender))
        subject = _dig(item, ("subject",), ("metadata", "subject"), default="") or ""
        message_id = _dig(item, ("metadata", "message_id"), ("id",), ("message_id",), default=None)

        verdict, reason = classify(sender, subject, None)
        stats["scanned"] += 1
        if verdict == "noise":
            stats["noise"] += 1
            continue
        if HUB_ADDRESS in (sender or "").lower():
            continue

        body = _dig(item, ("bodyPreview",), ("preview",), default="") or ""
        if message_id:
            okb, resb = await _call(outlook, "open_outlook_email", [
                {"message_id": message_id},
                {"id": message_id},
            ])
            if okb:
                body = _dig(resb, ("body",), ("bodyPreview",), default=body) or body

        if dry_run:
            stats["routed"].append({"from": sender, "subject": subject, "dry_run": True})
            continue

        html = (
            "<div style='font-family:sans-serif;max-width:640px'>"
            f"<p><b>Routed from Outlook</b></p>"
            f"<p><b>Original sender:</b> {sender}</p>"
            f"<p><b>Subject:</b> {subject}</p>"
            "<hr/><div>" + str(body)[:4000] + "</div></div>"
        )
        stats["routed"].append({"from": sender, "subject": subject, "_pending_forward": html})

    return stats


# ---------------------------------------------------------------------------
# Activities (one per credential-slot combination)
# ---------------------------------------------------------------------------
@workflows.activity(name="triage-gmail-main")
async def triage_gmail_main(lookback_hours: int, dry_run: bool,
                            source: ToolCallClient = Depends(gmail_main)) -> dict:
    return await _triage_gmail(source, "Gmail-main", lookback_hours, dry_run)


@workflows.activity(name="triage-gmail-odr")
async def triage_gmail_odr(lookback_hours: int, dry_run: bool,
                           source: ToolCallClient = Depends(gmail_odr)) -> dict:
    return await _triage_gmail(source, "Gmail-odr", lookback_hours, dry_run)


@workflows.activity(name="triage-outlook")
async def triage_outlook(dry_run: bool,
                         outlook: ToolCallClient = Depends(outlook_conn),
                         hub: ToolCallClient = Depends(gmail_hub)) -> dict:
    stats = await _triage_outlook(outlook, dry_run)
    forwarded = []
    for item in stats["routed"]:
        html = item.pop("_pending_forward", None)
        if not html:
            forwarded.append(item)
            continue
        ok, _ = await _call(hub, "send_email", [
            {"to": [HUB_ADDRESS], "subject": f"[Emailia → Life | Outlook] {item['subject']}", "htmlBody": html},
            {"to": HUB_ADDRESS, "subject": f"[Emailia → Life | Outlook] {item['subject']}", "htmlBody": html},
        ])
        if not ok:
            stats["errors"].append(f"forward failed for '{item['subject']}'")
        else:
            forwarded.append(item)
    stats["routed"] = forwarded
    return stats


@workflows.activity(name="send-report")
async def send_report(results: list, hub: ToolCallClient = Depends(gmail_hub)) -> dict:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    total_routed = sum(len(r.get("routed", [])) for r in results)
    total_noise = sum(r.get("noise", 0) for r in results)
    total_errors = sum(len(r.get("errors", [])) for r in results)

    rows = ""
    for r in results:
        for item in r.get("routed", []):
            rows += (
                f"<tr><td>{r['mailbox']}</td><td>{item.get('from','')}</td>"
                f"<td>{item.get('subject','')}</td></tr>"
            )

    html = (
        "<div style='font-family:sans-serif;max-width:720px'>"
        f"<h2>🤖 Emailia run report</h2><p>{now}</p>"
        f"<p>Routed: <b>{total_routed}</b> · Noise skipped: <b>{total_noise}</b> · Errors: <b>{total_errors}</b></p>"
        "<table border='1' cellpadding='6' cellspacing='0' style='border-collapse:collapse'>"
        "<tr><th>Mailbox</th><th>From</th><th>Subject</th></tr>" + rows + "</table>"
    )
    if total_errors:
        html += "<h3>Errors</h3><ul>" + "".join(
            f"<li>{e}</li>" for r in results for e in r.get("errors", [])
        ) + "</ul>"
    html += "</div>"

    ok, res = await _call(hub, "send_email", [
        {"to": [HUB_ADDRESS], "subject": f"🤖 Emailia report — {now}", "htmlBody": html},
        {"to": HUB_ADDRESS, "subject": f"🤖 Emailia report — {now}", "htmlBody": html},
    ])
    return {"sent": bool(ok), "routed": total_routed, "noise": total_noise, "errors": total_errors}


# ---------------------------------------------------------------------------
# The workflow
# ---------------------------------------------------------------------------
class EmailiaInput(BaseModel):
    lookback_hours: int = 12
    dry_run: bool = False


@workflows.workflow.define(
    name="emailia",
    workflow_display_name="Emailia",
    workflow_description=(
        "Autonomous email triage agent: reads Gmail-main, Gmail-odr and Outlook, "
        "filters noise (social, promotions, newsletters), forwards relevant mail "
        "to the Life hub and sends a run report."
    ),
    on_behalf_of=True,
)
@uses_connectors(gmail_main, gmail_odr, gmail_hub, outlook_conn)
class EmailiaWorkflow:
    @workflows.workflow.entrypoint
    async def run(self, input: EmailiaInput) -> dict:
        results = []
        results.append(await triage_gmail_main(input.lookback_hours, input.dry_run))
        results.append(await triage_gmail_odr(input.lookback_hours, input.dry_run))
        results.append(await triage_outlook(input.dry_run))
        report = await send_report(results)
        return {"report": report, "mailboxes": results}
