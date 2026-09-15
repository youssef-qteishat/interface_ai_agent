import asyncio
import json
import secrets
from dataclasses import asdict

from fastapi import FastAPI, HTTPException, Request, Form
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from apps.cred_union_sim import db, fault

app = FastAPI(title="Credit Union Ops Simulator")
app.mount("/static", StaticFiles(directory="apps/cred_union_sim/static"), name="static")

templates = Jinja2Templates(directory="apps/cred_union_sim/templates")


@app.on_event("startup")
async def on_startup():
    db.init_db()
    db.seed_db()
    fault.set_fault_profile("default")


@app.get("/")
async def index(request: Request):
    active_fault = fault.get_active_fault_profile()
    return templates.TemplateResponse(
        request,
        "base.html",
        {"tenant_theme": active_fault.tenant_theme}
    )


@app.get("/dev/fault-profile")
async def get_fault_profile():
    """Development-only endpoint. Not linked from the UI.
    Not accessible to the discovery agent (blocked by policy engine)."""
    return asdict(fault.get_active_fault_profile())


@app.post("/dev/fault-profile/{profile_name}")
async def set_fault_profile(profile_name: str):
    """Development-only endpoint. Not linked from the UI.
    Not accessible to the discovery agent (blocked by policy engine)."""
    try:
        fault.set_fault_profile(profile_name)
    except ValueError:
        raise HTTPException(status_code=404, detail=f"Unknown fault profile: {profile_name}")
    return {"status": "ok", "active_profile": profile_name}

@app.get("/dev/audit-log")
async def get_audit_log():
    """Development-only endpoint. Not linked from the UI.
    Not accessible to the discovery agent (blocked by policy engine)."""
    entries = db.list_audit_log()
    for entry in entries:
        if entry["details"] is not None:
            entry["details"] = json.loads(entry["details"])
    return entries

@app.get("/servicing/members/search")
async def member_search_page(request: Request, member_id: str | None = None):
    id_suffix = secrets.token_hex(4)  # Generated DOM ID suffix
    active_fault = fault.get_active_fault_profile()
    members = None
    if member_id:
        member = db.get_member(member_id)
        members = [member] if member else None
    return templates.TemplateResponse(
        request,
        "member_search.html",
        {
            "id_suffix": id_suffix,
            "error": None,
            "member_id": member_id,
            "members": members,
            "tenant_theme": active_fault.tenant_theme
        }
    )

@app.post("/servicing/members/search")
async def member_search_submit(request: Request, member_id: str = Form(...)):
    # Check fault profile for overlay delay
    active_fault = fault.get_active_fault_profile()
    if active_fault.overlay_delay_ms > 0:
        await asyncio.sleep(active_fault.overlay_delay_ms / 1000)

    # Query database
    member = db.get_member(member_id)
    db.log_audit(member_id=member_id, action="search", details={"member_id": member_id})
    if member is None:
        # Return "not found" partial
        return templates.TemplateResponse(
            request,
            "_search_results.html",
            {"members": None}
        )

    members = [member]
    return templates.TemplateResponse(
        request,
        "_search_results.html",
        {"members": members}
    )

@app.get("/servicing/members/{member_id}")
async def member_detail_page(request: Request, member_id: str):
    member = db.get_member(member_id)
    if member is None:
        raise HTTPException(status_code=404, detail=f"Member not found: {member_id}")

    accounts = db.get_accounts_by_member(member_id)
    active_fault = fault.get_active_fault_profile()
    db.log_audit(member_id=member_id, action="view_detail", details={"member_id": member_id})
    return templates.TemplateResponse(
        request,
        "member_detail.html",
        {
            "member": member,
            "accounts": accounts,
            "tenant_theme": active_fault.tenant_theme
        }
    )

@app.get("/servicing/accounts/open")
async def open_account_page(request: Request, member_id: str):
    member = db.get_member(member_id)
    if member is None:
        raise HTTPException(status_code=404, detail=f"Member not found: {member_id}")

    accounts = db.get_accounts_by_member(member_id)
    suffix = secrets.token_hex(4)
    active_fault = fault.get_active_fault_profile()
    return templates.TemplateResponse(
        request,
        "open_subaccount.html",
        {
            "member": member,
            "accounts": accounts,
            "suffix": suffix,
            "tenant_theme": active_fault.tenant_theme
        }
    )

@app.post("/servicing/accounts/open/submit")
async def submit_subaccount_form(
    request: Request,
    member_id: str = Form(...),
    account_type: str = Form(...),
    opening_amount: str = Form(...),
    funding_account_id: str = Form(...),
    disclosure_accepted: str = Form(default="")
):
    # Validate disclosure
    if not disclosure_accepted:
        return templates.TemplateResponse(
            request,
            "_form_error.html",
            {
                "error": "You must accept the account disclosure to continue."
            }
        )

    # Validate amount
    try:
        amount = float(opening_amount)
        if amount <= 0:
            raise ValueError
    except ValueError:
        return templates.TemplateResponse(
            request,
            "_form_error.html",
            {
                "error": "Opening amount must be a positive number (e.g., 25.00)."
            }
        )

    # Check fault profile for overlay
    active_fault = fault.get_active_fault_profile()
    if active_fault.overlay_delay_ms > 0:
        await asyncio.sleep(active_fault.overlay_delay_ms / 1000)

    # Create sub-account application record
    sub_account_app = db.create_subaccount_app(
        member_id=member_id,
        account_type=account_type,
        opening_amount=amount,
        funding_account_id=funding_account_id
    )
    db.log_audit(
        member_id=member_id,
        action="open_subaccount",
        details={
            "account_type": account_type,
            "opening_amount": opening_amount,
            "funding_account_id": funding_account_id,
            "app_id": sub_account_app["app_id"]
        }
    )

    # Return review partial
    member = db.get_member(member_id)
    funding_acct = db.get_account(funding_account_id)
    return templates.TemplateResponse(
        request,
        "_review_panel.html",
        {
            "app": sub_account_app,
            "member": member,
            "funding_acct": funding_acct,
            "account_type": account_type,
            "opening_amount": amount,
            "fault": active_fault
        }
    )

@app.post("/servicing/accounts/open/finalize")
async def finalize_subaccount(request: Request, app_id: str = Form(...)):
    sub_account_app = db.get_subaccount_app(app_id)
    if sub_account_app is None:
        raise HTTPException(status_code=404, detail=f"Application not found: {app_id}")

    member = db.get_member(sub_account_app["member_id"])
    funding_acct = db.get_account(sub_account_app["funding_account_id"])
    db.log_audit(
        member_id=sub_account_app["member_id"],
        action="review",
        details={"app_id": app_id}
    )

    return templates.TemplateResponse(
        request,
        "_account_finalized.html",
        {
            "app": sub_account_app,
            "member": member,
            "funding_acct": funding_acct
        }
    )