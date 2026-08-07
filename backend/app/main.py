import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import settings
from app.core.db import Base, engine
from app.routers.api import VERSION, router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)

app = FastAPI(
    title="ColdLineage",
    version=VERSION,
    description=(
        "Range-scoped, evidence-backed data tiering with DataHub as the context system.\n\n"
        "DataHub can tell you a table is cold. It cannot tell you that half a table is cold, "
        "and it cannot move a byte. This service decides whether a specific date range inside "
        "a still-hot table is safe to archive -- by parsing the real SQL of every downstream "
        "consumer to learn how far back each one actually reads -- then executes the move with "
        "read-back verification before any delete, and writes the archive provenance back into "
        "DataHub so the next reader inherits it."
    ),
)

# The UI is served from a different origin in the demo compose stack. Credentials are not
# used, so the wildcard cannot be paired with cookie auth here.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type"],
)

Base.metadata.create_all(bind=engine)
app.include_router(router)


@app.get("/")
def root():
    return {
        "service": settings.app_name,
        "version": VERSION,
        "docs": "/docs",
        "datahub_mode": settings.datahub_mode,
        "datahub_gms_url": settings.datahub_gms_url,
    }
