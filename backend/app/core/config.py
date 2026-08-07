from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    app_name: str = "ColdLineage"
    database_url: str = "postgresql+psycopg://coldlineage:coldlineage@postgres:5432/coldlineage"
    minio_endpoint: str = "http://minio:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    minio_bucket: str = "coldlineage-archive"
    datahub_gms_url: str = "http://host.docker.internal:8080"
    datahub_token: str | None = None
    demo_mode: bool = True
    hot_storage_cost_per_gb_month: float = 0.12
    cold_storage_cost_per_gb_month: float = 0.02
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

settings = Settings()
