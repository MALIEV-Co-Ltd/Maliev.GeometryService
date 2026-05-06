from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    RABBITMQ_URI: str = "amqp://guest:guest@localhost:5672/"
    UPLOAD_SERVICE_URL: str = "http://localhost:6900"

    JWT_SECURITY_KEY: str = ""
    JWT_PRIVATE_KEY: str = (
        ""  # RSA private key PEM (Base64-encoded) — preferred for signing
    )
    JWT_ISSUER: str = "https://api.maliev.com"
    JWT_AUDIENCE: str = "https://api.maliev.com"

    # Set empty to disable OpenTelemetry (useful for local development)
    OTEL_EXPORTER_OTLP_ENDPOINT: str = ""
    SERVICE_NAME: str = "maliev-geometryservice"

    MAX_FILE_SIZE_MB: int = 200
    GEOMETRY_MAIN_WORKERS: int | None = None
    GEOMETRY_DFM_WORKERS: int = 2
    GEOMETRY_PROCESS_DFM_TIMEOUT_SECONDS: int = 180
    GEOMETRY_PREVIEW_RENDER_WORKERS: int = 8
    GEOMETRY_DFM_BODY_WORKERS: int = 4
    GEOMETRY_FILE_INGEST_CONCURRENCY: int = 2
    GEOMETRY_ARTIFACT_CONCURRENCY: int = 2
    GEOMETRY_RABBITMQ_PREFETCH: int | None = None

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
