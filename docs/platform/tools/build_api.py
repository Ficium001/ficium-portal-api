#!/usr/bin/env python3
"""Generate API_REFERENCE.md by scanning ficium-portal-api and ficium/api."""
import re
import glob
from pathlib import Path

ROOT = Path("/home/claude")
API = ROOT / "ficium-portal-api"

GROUPS = [
    ("Marketplace", ["marketplace.py", "request_chat.py"]),
    ("Bidding automation", ["autobid.py"]),
    ("Pipelines", ["pipeline.py", "pipeline_templates.py"]),
    ("Approvals & dual control", ["approvals.py", "approval_engine.py"]),
    ("Documents & e-signature", ["doc_templates/router.py", "documents.py", "esign.py"]),
    ("Institution administration", [
        "institutions.py", "members.py", "groups.py", "benefits.py",
        "catalog.py", "auth_provision.py", "notifications.py"]),
    ("Integration", ["api_keys.py", "webhooks.py", "entitlements.py"]),
    ("Platform admin", ["admin.py"]),
    ("Server-to-server (no JWT)", ["public.py"]),
    ("Versioned public API", ["v1/marketplace.py"]),
]

DESC = {
    # marketplace
    "list_requests": "Anonymised (Phase 1) requests visible to the caller's institution",
    "list_my_bids": "Bids submitted by the caller's institution, with request context",
    "submit_bid": "Place a bid. Rejected outside the open bid window",
    "get_bid": "Single bid detail",
    "get_bid_reveal": "Phase 2 borrower identity. Winning institution only, post-acceptance",
    "reject_request": "Decline to bid, with a reason",
    "close_expired": "Sweep requests whose bid window has closed",
    "sync_requests": "Pull the next batch from the App DB. `?full_resync=true` resets the cursor to epoch",
    "sync_health": "Cursor position, last-run counts, `more_pending`",
    "list_message_templates": "Structured chat catalogue, incl. `params_schema`",
    "list_messages": "Thread for one `(request_id, institution_id)` pair. `sender_id` masked",
    "send_message": "Send a structured message; free text only post-acceptance for the winner",
    # autobid
    "create_rule": "Create an auto-bid rule (starts in draft)",
    "new_version": "New immutable version of an existing rule",
    "submit_rule": "Submit a version for approval",
    "approve_rule": "Approve a version — required before it can act",
    "reject_rule": "Reject a submitted version",
    "pause_rule": "Suspend an active rule",
    "resume_rule": "Reactivate a paused rule",
    "retire_rule": "Permanently retire a rule",
    "list_rules": "All rules for the institution",
    "get_rule": "Single rule with version history",
    "list_executions": "Auto-bid execution log",
    # pipeline
    "list_pipelines": "Active loan pipelines",
    "get_pipeline": "Pipeline with stages and the Phase 2 identity reveal",
    "advance_stage": "Move a stage forward (maker)",
    "approve_stage": "Approve a stage awaiting checker sign-off",
    "list_templates": "Templates",
    "create_template": "Create a template",
    "get_template": "Template with stage definitions",
    "update_template": "Update template metadata",
    "add_stage": "Append a stage definition",
    "update_stage": "Update a stage definition",
    "delete_stage": "Remove a stage definition",
    # approvals
    "list_pending_actions": "Maker-checker queue. `?scope=bids|internal` partitions it",
    "submit_for_approval": "Submit an action for four-eyes approval",
    "approve_action": "Approve (checker must differ from maker)",
    "reject_action": "Reject with a note",
    "execute_user_update": "Execute an approved user change",
    "provision_user_from_action": "Provision a member from an approved action",
    "list_committees": "Approval committees",
    "create_committee": "Create a committee with quorum and tie-break rules",
    "add_committee_member": "Add a member (voting or observer)",
    "end_committee_membership": "End a membership (sets `valid_to`)",
    "list_delegations": "Delegations of authority",
    "create_delegation": "Delegate approval authority for a bounded period",
    "revoke_delegation": "Revoke a delegation early",
    "activate_template": "Publish a draft approval template",
    "list_doa_rules": "Delegation-of-authority routing rules",
    "create_doa_rule": "Create a routing rule (lowest priority number wins)",
    "simulate_routing": "Dry-run routing without creating an instance",
    "route_entity": "Route an entity into an approval chain",
    "my_inbox": "Stages awaiting the caller's action, incl. delegated",
    "cast_action": "Approve / reject / abstain on a stage",
    "withdraw_instance": "Withdraw an in-flight approval",
    "instance_timeline": "Full timeline for an instance",
    "approval_analytics": "Cycle times, bottlenecks, SLA breaches",
    # docs
    "retire_template": "Retire a document template",
    "list_versions": "Versions of a document template",
    "upload_version": "Upload a new .docx version (draft)",
    "decide_version": "Approve or reject a version — author cannot approve their own",
    "list_merge_fields": "Available `{{ field }}` merge tags",
    "generate_document": "Generate .docx/.pdf against a deal snapshot",
    "list_generations": "Documents generated for a deal",
    "download_generation": "Download a generated document",
    "list_doc_types": "Compliance document types",
    "get_compliance": "Compliance rollup incl. `can_bid` and missing documents",
    "list_documents": "Institution compliance library",
    "register_document": "Register an uploaded document",
    "review_document": "Approve or reject a document",
    "get_upload_url": "Signed upload URL for the institution-docs bucket",
    "create_envelope": "Create a signature envelope (accepts `doc_generation_id`, `approval_instance_id`)",
    "list_envelopes": "Envelopes with status",
    "envelope_events": "Hash-chained audit trail for an envelope",
    "sealed_url": "Signed URL for the sealed PDF",
    "ceremony_state": "Public: signing ceremony state for a token",
    "request_otp": "Public: send the signer OTP",
    "verify_otp": "Public: verify the OTP",
    "sign": "Public: record the signature",
    "decline": "Public: decline to sign",
    # institution admin
    "get_my_institution": "Caller's institution profile",
    "get_my_role": "Caller's member record and role",
    "get_my_group": "Caller's group and module permissions",
    "get_my_group_debug": "Group resolution diagnostics",
    "list_members": "Institution members",
    "list_pending_member_actions": "Member changes awaiting approval",
    "get_member": "Single member",
    "update_member": "Update a member (may route to maker-checker)",
    "deactivate_member": "Deactivate a member",
    "reactivate_member": "Reactivate a member",
    "reset_member_password": "Force a password reset",
    "get_member_audit": "Audit trail for one member",
    "list_groups": "Institution groups",
    "list_pending_group_actions": "Group changes awaiting approval",
    "get_my_modules": "Modules licensed AND permitted for the caller",
    "get_my_products": "Products in the caller's group scope",
    "list_licensed_products": "Products licensed to the institution",
    "list_benefit_categories": "Benefit categories",
    "list_benefits": "Institution benefits",
    "create_benefit": "Create a benefit",
    "update_benefit": "Update a benefit",
    "deactivate_benefit": "Deactivate a benefit",
    "list_products": "Product catalogue",
    "list_audit_events": "Institution audit trail",
    "upsert_sla_config": "Set bid-window and integration overrides",
    "list_webhooks": "Registered webhooks",
    "provision_member": "Provision a portal login for a member",
    "unread_count": "Unread notification count",
    "mark_read": "Mark one notification read",
    "mark_all_read": "Mark all read",
    # integration
    "list_keys": "API keys (hashes never returned)",
    "create_key": "Create a key — enters maker-checker as pending",
    "approve_key": "Approve and activate a key",
    "revoke_key": "Revoke a key",
    "delete_key": "Hard-delete a key record",
    "create_webhook": "Register a webhook (endpoint URL is SSRF-validated)",
    "update_webhook": "Update a webhook",
    "delete_webhook": "Delete a webhook",
    "test_webhook": "Send a test event",
    "list_deliveries": "Delivery log with attempts and responses",
    "reset_failures": "Reset the failure counter (circuit breaker)",
    "my_entitlements": "Modules licensed to the institution",
    "my_usage": "Metered usage against entitlements",
    # platform admin
    "admin_metrics": "Platform KPIs",
    "admin_users": "All portal users",
    "admin_roles": "Admin roles",
    "admin_sessions": "Active sessions",
    "admin_dual_control": "Platform dual-control queue",
    "admin_audit": "Platform audit trail",
    "admin_institutions": "All institutions with onboarding state",
    "admin_user_groups": "Group definitions",
    "submit_dual_control": "Submit a platform action for approval",
    "approve_dual_control": "Approve a platform action",
    "reject_dual_control": "Reject a platform action",
    "terminate_session": "Force-terminate a session",
    "admin_list_documents": "KYB documents pending review",
    "admin_review_document": "Approve or reject a KYB document",
    # public s2s
    "get_bids_for_request": "Bids for one request (called by the borrower app)",
    "get_bids_bulk": "Bids for many requests in one call",
    "accept_bid": "Accept a bid — triggers Phase 2 reveal and pipeline creation",
    "get_pipeline_for_borrower": "Borrower-visible pipeline stages only",
    "market_intelligence": "Aggregated market rate intelligence",
    # v1
    "list_bids": "Bids (API key auth)",
    "advance_pipeline_stage": "Advance a pipeline stage (API key auth)",
    "analytics_summary": "Win rate, volume, deal value (API key auth)",
}


def endpoints(path):
    s = (API / "app" / "api" / path).read_text()
    m = re.search(r"APIRouter\(([^)]*)\)", s, re.S)
    prefix = ""
    if m:
        pm = re.search(r'prefix\s*=\s*["\']([^"\']+)', m.group(1))
        prefix = pm.group(1) if pm else ""
    found = re.findall(
        r"@router\.(get|post|put|patch|delete)\(\s*[\"']([^\"']*)[\"'](.*?)\)\s*\n"
        r"(?:@[^\n]*\n)*\s*(?:async\s+)?def\s+(\w+)",
        s, re.S)
    out = []
    for meth, p, rest, fn in found:
        mod = re.findall(r'require_module\(["\']([^"\']+)', rest)
        out.append((meth.upper(), prefix + p, fn, mod[0] if mod else ""))
    return out


lines = [
    "# Ficium — API Reference",
    "",
    "_Generated 1 August 2026 by scanning the route decorators in `ficium-portal-api`_",
    "_and `ficium/api`. Regenerate with `build_api.py` rather than hand-editing._",
    "",
    "## Authentication",
    "",
    "| Surface | Scheme |",
    "|---|---|",
    "| Portal routes | `Authorization: Bearer <RS256 JWT>` from `ficium-auth`. "
    "`institution_id` and role are read from the claims. |",
    "| `/v1/*` | `fic_live_<64 hex>` API key, verified against an HMAC. Scoped per key. |",
    "| `/public/*` | Shared-secret server-to-server. Called by `ficium`'s Vercel "
    "functions, never from a browser. |",
    "| `/esign/public/{token}` | Unauthenticated ceremony token plus OTP. |",
    "",
    "Module-gated routes carry a `require_module(...)` dependency and return **403** "
    "when the institution lacks the entitlement — distinct from **401** for a bad token.",
    "",
    "## Conventions",
    "",
    "- All request and response bodies are JSON unless noted.",
    "- Money is decimal; never parse it as a float.",
    "- Timestamps are ISO 8601 with offset.",
    "- Mutating endpoints accept an idempotency key where a retry could double-write.",
    "",
]

total = 0
for title, files in GROUPS:
    lines += [f"## {title}", ""]
    for f in files:
        eps = endpoints(f)
        if not eps:
            continue
        lines += [f"### `app/api/{f}`", ""]
        lines += ["| Method | Path | Module | Description |", "|---|---|---|---|"]
        for meth, path, fn, mod in eps:
            total += 1
            lines.append(
                f"| `{meth}` | `{path}` | {'`' + mod + '`' if mod else '—'} | "
                f"{DESC.get(fn, fn.replace('_', ' ').capitalize())} |")
        lines.append("")

# Vercel functions on the borrower app
vercel = sorted(Path(p).stem for p in glob.glob(str(ROOT / "ficium/api/*.ts")))
VDESC = {
    "accept-bid": "Accept a bid — proxies `/public/requests/{id}/accept-bid`",
    "chat": "FICO advisor streaming chat (SSE)",
    "intelligence": "Market intelligence for the Markets module",
    "internal": "Internal cron and maintenance handlers",
    "keepalive": "Warms the Railway API to reduce cold starts",
    "kyc": "KYC — Rekognition face match, liveness, MRZ, Claude Vision extraction",
    "market": "Market data refresh",
    "rate-applicant": "Calls the rating engine for risk tier and score",
    "request-actions": "Request lifecycle actions",
    "request-bids-bulk": "Bids for many requests in one call",
    "request-bids": "Bids for one request",
    "request-builder": "AI-assisted request construction",
}
lines += [
    "## Borrower app serverless functions (`ficium/api/*.ts`, Vercel)",
    "",
    f"**{len(vercel)} root-level functions — exactly at the plan ceiling.** "
    "Adding a thirteenth breaks the deploy. Shared code lives in `api/_lib/` and "
    "`api/_kyc/`, which are not counted as functions.",
    "",
    "| Function | Purpose |", "|---|---|",
]
for v in vercel:
    lines.append(f"| `/api/{v}` | {VDESC.get(v, v.replace('-', ' ').capitalize())} |")

lines += [
    "",
    "## Webhooks (outbound)",
    "",
    "Institutions register endpoints under Settings. Deliveries are signed "
    "HMAC-SHA256 over the raw body, retried with exponential backoff up to "
    "`retry_max`, and logged in full to `institution.webhook_delivery`. A rising "
    "`failure_count` trips a circuit breaker; `POST /webhooks/{id}/reset-failures` "
    "clears it. Endpoint URLs are SSRF-validated at registration "
    "(`app/core/ssrf.py`).",
    "",
    f"---",
    f"",
    f"_{total} portal API endpoints, {len(vercel)} borrower serverless functions._",
    "",
]

out = Path(__file__).parent / "API_REFERENCE.md"
out.write_text("\n".join(lines))
print(f"wrote {out} — {total} endpoints, {len(vercel)} vercel functions")
