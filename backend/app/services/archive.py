import hashlib, io, json
from datetime import datetime
import boto3
import pandas as pd
from sqlalchemy import text
from app.core.config import settings

class ArchiveService:
    def __init__(self):
        self.s3 = boto3.client("s3", endpoint_url=settings.minio_endpoint,
            aws_access_key_id=settings.minio_access_key, aws_secret_access_key=settings.minio_secret_key)
    def ensure_bucket(self):
        try: self.s3.head_bucket(Bucket=settings.minio_bucket)
        except Exception: self.s3.create_bucket(Bucket=settings.minio_bucket)
    def preview(self, db, ds, cutoff_date):
        q = text(f'SELECT count(*) as n FROM "{ds.name}" WHERE "{ds.date_column}" < :cutoff')
        n = db.execute(q, {"cutoff": cutoff_date}).scalar() or 0
        total = max(ds.rows, 1)
        est_gb = ds.size_gb * n / total
        hot = est_gb * settings.hot_storage_cost_per_gb_month
        cold = est_gb * settings.cold_storage_cost_per_gb_month
        return {"rows": int(n), "estimated_gb": round(est_gb,3), "monthly_savings_usd": round(max(hot-cold,0),2)}
    def execute(self, db, ds, cutoff_date):
        self.ensure_bucket()
        df = pd.read_sql(text(f'SELECT * FROM "{ds.name}" WHERE "{ds.date_column}" < :cutoff ORDER BY 1'), db.connection(), params={"cutoff": cutoff_date})
        if df.empty: raise ValueError("No eligible rows found")
        buf = io.BytesIO(); df.to_parquet(buf, index=False); payload = buf.getvalue()
        checksum = hashlib.sha256(payload).hexdigest()
        key = f'{ds.name}/{cutoff_date}/{datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")}.parquet'
        self.s3.put_object(Bucket=settings.minio_bucket, Key=key, Body=payload, Metadata={"sha256": checksum})
        uri = f's3://{settings.minio_bucket}/{key}'
        manifest = {"dataset": ds.name, "cutoff_date": cutoff_date, "rows": len(df), "bytes": len(payload), "sha256": checksum, "columns": list(df.columns), "object_uri": uri}
        self.s3.put_object(Bucket=settings.minio_bucket, Key=key+'.manifest.json', Body=json.dumps(manifest).encode())
        # Delete only after object write + checksum creation. Demo transaction remains rollbackable until commit.
        db.execute(text(f'DELETE FROM "{ds.name}" WHERE "{ds.date_column}" < :cutoff'), {"cutoff": cutoff_date})
        db.commit()
        return manifest
    def restore(self, db, ds, object_uri, temporary=True):
        key = object_uri.split(f's3://{settings.minio_bucket}/',1)[1]
        obj = self.s3.get_object(Bucket=settings.minio_bucket, Key=key)
        payload = obj['Body'].read(); checksum = hashlib.sha256(payload).hexdigest()
        expected = obj.get('Metadata',{}).get('sha256')
        if expected and expected != checksum: raise ValueError('Checksum mismatch')
        df = pd.read_parquet(io.BytesIO(payload))
        table = f"restored_{ds.name}" if temporary else ds.name
        df.to_sql(table, db.connection(), if_exists="replace" if temporary else "append", index=False)
        db.commit()
        return {"table": table, "rows": len(df), "checksum": checksum}
