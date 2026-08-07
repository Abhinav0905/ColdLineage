from datetime import datetime, timezone
from math import log1p

class TemperatureService:
    @staticmethod
    def score(ds):
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        days = 9999 if not ds.last_query_at else max((now - ds.last_query_at).days, 0)
        recency = max(0.0, 1.0 - min(days, 730) / 730)
        query = min(log1p(ds.query_count_90d) / log1p(1000), 1.0)
        downstream = min(ds.downstream_active / 5, 1.0)
        critical = min(max(ds.business_criticality, 0), 1)
        # Higher = hotter. Legal holds do not make data hotter; they block archival separately.
        value = 100 * (0.42 * recency + 0.28 * query + 0.18 * downstream + 0.12 * critical)
        return round(value, 1)

    @staticmethod
    def classification(score):
        if score >= 75: return "HOT"
        if score >= 45: return "WARM"
        if score >= 20: return "COOL"
        if score >= 8: return "COLD"
        return "FROZEN"
