class SimulationService:
    @staticmethod
    def simulate(ds, cutoff_date: str):
        impacts = []
        deps = ds.metadata_json.get("dependencies", []) if ds.metadata_json else []
        for dep in deps:
            need_years = dep.get("history_years", 0)
            state = "safe" if need_years <= ds.retention_years else "warning"
            impacts.append({
                "name": dep.get("name", "unknown"),
                "type": dep.get("type", "dataset"),
                "history_years": need_years,
                "state": state,
                "reason": dep.get("reason", "Dependency checked against requested archival horizon")
            })
        blocked = any(x["state"] == "blocked" for x in impacts)
        warning = any(x["state"] == "warning" for x in impacts)
        recommendation = "DO_NOT_ARCHIVE" if blocked else ("ARCHIVE_WITH_REHYDRATION" if warning else "SAFE_TO_ARCHIVE")
        return {"cutoff_date": cutoff_date, "recommendation": recommendation, "impacts": impacts}
