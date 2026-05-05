from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "GrafanaDB Restore API"
    log_level: str = "INFO"
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    restore_image: str = "tancorp/grafana-db-restore:latest"
    max_backup_listing: int = 200
    allowed_backup_extensions: list[str] = Field(default_factory=lambda: [".sql", ".dump", ".backup", ".tar"])

    # MinIO
    minio_bucket: str = "elkintranet-nonprod-monitoring"
    minio_prefix: str = ""
    minio_endpoint_url: str = "http://192.168.1.123:9000"
    minio_region: str = "us-east-1"
    minio_credentials_secret_name: str = "minio-credentials"
    minio_mc_image: str = "tancorp/grafana-db-restore:latest"
    minio_access_key: str = "L7eyFIgYlKRUivfaaXIU"
    minio_secret_key: str = "tmqx8db1ABc3VhCAhj1RBlQBAbDcZGqsSFaksJEV"


@lru_cache
def get_settings() -> Settings:
    return Settings()
