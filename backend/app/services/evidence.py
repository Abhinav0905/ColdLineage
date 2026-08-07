from datetime import datetime, timezone
from dateutil.relativedelta import relativedelta
from .temperature import TemperatureService

class EvidenceService:
    @staticmethod
    def build(ds):
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        cutoff = now - relativedelta(years=ds.retention_years)
        days_since_query = None if not ds.last_query_at else (now-ds.last_query_at).days
        score = TemperatureService.score(ds)
        blockers = []
        evidence = []
        if ds.legal_hold:
            blockers.append("Active legal hold")
        else:
            evidence.append({"type":"policy","label":"No active legal hold","status":"pass"})
        evidence.append({"type":"retention","label":f"Retention minimum: {ds.retention_years} years","status":"pass"})
        if days_since_query is None:
            evidence.append({"type":"usage","label":"No observed query history","status":"pass"})
        elif days_since_query > 365:
            evidence.append({"type":"usage","label":f"Last observed query: {days_since_query} days ago","status":"pass"})
        else:
            evidence.append({"type":"usage","label":f"Recent access: {days_since_query} days ago","status":"warn"})
        if ds.downstream_active:
            evidence.append({"type":"lineage","label":f"{ds.downstream_active} active downstream dependencies require simulation","status":"warn"})
        else:
            evidence.append({"type":"lineage","label":"No active downstream blockers","status":"pass"})
        if ds.phi or ds.pii:
            evidence.append({"type":"classification","label":"Sensitive-data controls must be preserved","status":"warn"})
        confidence = max(0.5, min(0.98, 0.97 - score/250 - 0.08*len(blockers)))
        return {
            "temperature": score,
            "classification": TemperatureService.classification(score),
            "retention_cutoff": cutoff.date().isoformat(),
            "eligible": not blockers and score < 35,
            "confidence": round(confidence, 2),
            "evidence": evidence,
            "blockers": blockers,
        }
