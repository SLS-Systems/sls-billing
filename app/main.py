from fastapi import FastAPI

from app.routers import billing

app = FastAPI(title="CI Billing", version="1.0.0", docs_url=None, redoc_url=None)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "billing"}


app.include_router(billing.router, prefix="/api/v1")

print(f"[CI Billing] Registered {len(app.routes)} routes")
