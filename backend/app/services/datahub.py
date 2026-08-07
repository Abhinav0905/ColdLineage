import httpx
from app.core.config import settings

class DataHubService:
    """Thin OpenAPI writeback adapter. In demo mode, the same payload is retained in our audit log.
    For a live DataHub instance set DATAHUB_GMS_URL and DATAHUB_TOKEN.
    """
    def __init__(self):
        self.base = settings.datahub_gms_url.rstrip('/')
        self.headers = {"Content-Type":"application/json"}
        if settings.datahub_token: self.headers["Authorization"] = f"Bearer {settings.datahub_token}"
    async def writeback(self, urn: str, archive: dict):
        payload = {
            "entityUrn": urn,
            "aspectName": "datasetProperties",
            "aspect": {"customProperties": {
                "coldlineage.status": "ARCHIVED",
                "coldlineage.objectUri": archive.get("object_uri", ""),
                "coldlineage.archivedThrough": archive.get("cutoff_date", ""),
                "coldlineage.sha256": archive.get("sha256", ""),
                "coldlineage.restoreSla": "on-demand demo / policy-defined production"
            }}
        }
        if settings.demo_mode:
            return {"mode":"demo", "payload":payload, "written":False}
        url = self.base + "/openapi/v3/entity/dataset"
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(url, headers=self.headers, json=payload)
            return {"mode":"live", "status_code":r.status_code, "body":r.text[:1000], "written":r.is_success}
