"""Stripe billing — checkout, portal, webhooks, subscription management."""
import uuid
from datetime import datetime

import stripe
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from sls_shared.auth import get_current_user
from sls_shared.database import get_db
from sls_shared.models.profile import Profile
from app.config import settings

router = APIRouter(prefix="/billing", tags=["billing"])

stripe.api_key = settings.stripe_secret_key

TIER_PRICES = {
    "starter": {"base": 4900, "per_resident": 800, "label": "Starter"},
    "professional": {"base": 9900, "per_resident": 600, "label": "Professional"},
    "enterprise": {"base": 19900, "per_resident": 400, "label": "Enterprise"},
}


# --- Checkout ---

class CheckoutRequest(BaseModel):
    tier: str = "starter"
    billing_period: str = "monthly"  # monthly or annual

@router.post("/create-checkout")
async def create_checkout(body: CheckoutRequest, current_user: Profile = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    org = str(current_user.organisation_id)

    # Get or create Stripe customer
    r = await db.execute(text("SELECT stripe_customer_id, billing_email FROM organisations WHERE id = :org"), {"org": org})
    row = r.fetchone()
    customer_id = row[0] if row else None

    if not customer_id:
        customer = stripe.Customer.create(email=current_user.email, metadata={"org_id": org})
        customer_id = customer.id
        await db.execute(text("UPDATE organisations SET stripe_customer_id = :cid, billing_email = :email WHERE id = :org"),
            {"cid": customer_id, "email": current_user.email, "org": org})
        await db.commit()

    tier = TIER_PRICES.get(body.tier)
    if not tier:
        raise HTTPException(status_code=400, detail="Invalid tier")

    # Create checkout session with base price
    multiplier = 10 if body.billing_period == "annual" else 1  # 10 months for annual (2 free)
    base_amount = tier["base"] * multiplier

    session = stripe.checkout.Session.create(
        customer=customer_id,
        mode="subscription",
        line_items=[{
            "price_data": {
                "currency": "gbp",
                "product_data": {"name": f"CI Care {tier['label']} — Base Fee"},
                "unit_amount": tier["base"],
                "recurring": {"interval": "year" if body.billing_period == "annual" else "month"},
            },
            "quantity": 1,
        }],
        subscription_data={
            "trial_period_days": 14,
            "metadata": {"org_id": org, "tier": body.tier},
        },
        success_url=f"{settings.frontend_url}/dashboard/settings/billing?success=true",
        cancel_url=f"{settings.frontend_url}/dashboard/settings/billing?canceled=true",
        metadata={"org_id": org, "tier": body.tier},
    )

    return {"checkout_url": session.url}


# --- Customer Portal ---

@router.post("/create-portal")
async def create_portal(current_user: Profile = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    org = str(current_user.organisation_id)
    r = await db.execute(text("SELECT stripe_customer_id FROM organisations WHERE id = :org"), {"org": org})
    row = r.fetchone()
    if not row or not row[0]:
        raise HTTPException(status_code=400, detail="No billing account found")

    session = stripe.billing_portal.Session.create(
        customer=row[0],
        return_url=f"{settings.frontend_url}/dashboard/settings/billing",
    )
    return {"portal_url": session.url}


# --- Subscription Info ---

@router.get("/subscription")
async def get_subscription(current_user: Profile = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    org = str(current_user.organisation_id)
    r = await db.execute(text("""
        SELECT subscription_tier, subscription_status, trial_ends_at::text,
               stripe_subscription_id, billing_email, resident_count_last_synced
        FROM organisations WHERE id = :org
    """), {"org": org})
    row = r.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Organisation not found")

    # Count current residents
    rc = await db.execute(text("SELECT COUNT(*) FROM residents WHERE organisation_id = :org AND status = 'active'"), {"org": org})
    resident_count = rc.scalar_one()

    tier_info = TIER_PRICES.get(row[0] or "starter", TIER_PRICES["starter"])
    estimated_monthly = (tier_info["base"] + resident_count * tier_info["per_resident"]) / 100

    return {
        "tier": row[0] or "starter",
        "status": row[1] or "trialing",
        "trial_ends_at": row[2],
        "has_subscription": bool(row[3]),
        "billing_email": row[4],
        "resident_count": resident_count,
        "estimated_monthly": round(estimated_monthly, 2),
        "base_fee": tier_info["base"] / 100,
        "per_resident_fee": tier_info["per_resident"] / 100,
    }


# --- Invoices ---

@router.get("/invoices")
async def list_invoices(current_user: Profile = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    org = str(current_user.organisation_id)
    r = await db.execute(text("""
        SELECT id, stripe_invoice_id, amount_due, amount_paid, currency, status,
               period_start::text, period_end::text, invoice_pdf_url, created_at::text
        FROM invoices WHERE organisation_id = :org ORDER BY created_at DESC LIMIT 24
    """), {"org": org})
    return [{"id": str(x[0]), "stripe_id": x[1], "amount_due": x[2], "amount_paid": x[3],
             "currency": x[4], "status": x[5], "period_start": x[6], "period_end": x[7],
             "pdf_url": x[8], "created_at": x[9]} for x in r.fetchall()]


# --- Webhook ---

@router.post("/webhook")
async def stripe_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    """Handle Stripe webhook events."""
    body = await request.body()
    sig = request.headers.get("stripe-signature", "")

    try:
        event = stripe.Webhook.construct_event(body, sig, settings.stripe_webhook_secret)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid webhook signature")

    event_type = event["type"]
    data = event["data"]["object"]

    # Deduplicate
    r = await db.execute(text("SELECT COUNT(*) FROM billing_events WHERE stripe_event_id = :eid"), {"eid": event["id"]})
    if r.scalar_one() > 0:
        return {"status": "already_processed"}

    # Log event
    org_id = data.get("metadata", {}).get("org_id") or None
    if not org_id and "customer" in data:
        cr = await db.execute(text("SELECT id FROM organisations WHERE stripe_customer_id = :cid"), {"cid": data["customer"]})
        org_row = cr.fetchone()
        if org_row: org_id = str(org_row[0])

    await db.execute(text("INSERT INTO billing_events (organisation_id, stripe_event_id, event_type, data) VALUES (:org::uuid, :eid, :etype, :data::jsonb)"),
        {"org": org_id, "eid": event["id"], "etype": event_type, "data": "{}"})

    if event_type == "customer.subscription.created" and org_id:
        tier = data.get("metadata", {}).get("tier", "starter")
        await db.execute(text("UPDATE organisations SET subscription_status = 'active', subscription_tier = :tier, stripe_subscription_id = :sid WHERE id = :org"),
            {"tier": tier, "sid": data["id"], "org": org_id})

    elif event_type == "customer.subscription.updated" and org_id:
        status_map = {"active": "active", "past_due": "past_due", "canceled": "canceled", "trialing": "trialing", "incomplete": "incomplete", "paused": "paused"}
        new_status = status_map.get(data.get("status"), "active")
        await db.execute(text("UPDATE organisations SET subscription_status = :status WHERE id = :org"),
            {"status": new_status, "org": org_id})

    elif event_type == "customer.subscription.deleted" and org_id:
        await db.execute(text("UPDATE organisations SET subscription_status = 'canceled' WHERE id = :org"), {"org": org_id})

    elif event_type == "invoice.paid" and org_id:
        await db.execute(text("""
            INSERT INTO invoices (organisation_id, stripe_invoice_id, amount_due, amount_paid, currency, status, period_start, period_end, invoice_pdf_url)
            VALUES (:org::uuid, :iid, :due, :paid, :cur, 'paid', to_timestamp(:ps), to_timestamp(:pe), :pdf)
            ON CONFLICT (stripe_invoice_id) DO UPDATE SET status = 'paid', amount_paid = :paid
        """), {"org": org_id, "iid": data["id"], "due": data.get("amount_due", 0), "paid": data.get("amount_paid", 0),
               "cur": data.get("currency", "gbp"), "ps": data.get("period_start", 0), "pe": data.get("period_end", 0),
               "pdf": data.get("invoice_pdf")})

    await db.commit()
    return {"status": "processed"}


# --- Sync Residents (called by cron) ---

@router.post("/sync-residents")
async def sync_residents(request: Request, db: AsyncSession = Depends(get_db)):
    """Sync resident counts to Stripe for all orgs with subscriptions."""
    orgs = await db.execute(text("SELECT id, stripe_subscription_id FROM organisations WHERE stripe_subscription_id IS NOT NULL AND subscription_status = 'active'"))
    for org_id, sub_id in orgs.fetchall():
        rc = await db.execute(text("SELECT COUNT(*) FROM residents WHERE organisation_id = :org AND status = 'active'"), {"org": str(org_id)})
        count = rc.scalar_one()
        await db.execute(text("UPDATE organisations SET resident_count_last_synced = :count, resident_count_synced_at = now() WHERE id = :org"),
            {"count": count, "org": str(org_id)})
    await db.commit()
    return {"status": "synced"}
