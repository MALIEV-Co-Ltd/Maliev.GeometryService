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

    # Phase-2 concurrency cap. 2 vCPU / 4 GB targets set this to 1 so a
    # second concurrent DFM request waits instead of doubling the working
    # set. Higher values are allowed when more CPU/RAM is available.
    GEOMETRY_DFM_SEMAPHORE: int = 2

    # Algorithm feature flags. Cache keys include a digest of these so
    # toggling a flag flushes affected results without manual eviction.
    USE_BREP_THICKNESS: bool = True
    USE_SDF_SMALL_FEATURES: bool = False

    # GCS-backed caches keyed by SHA-256.  Routed through UploadService
    # (which owns the GCS connection) — see src/infrastructure/upload_cache.py.
    # Cache objects live under the "cache/" path prefix, which UploadService
    # routes to a dedicated bucket with TTL lifecycle rules (30 d for
    # tessellation, 7 d for DFM results).  Both flags default off until the
    # UploadService bucket routing change is deployed.
    GEOMETRY_TESSELLATION_CACHE_ENABLED: bool = False
    GEOMETRY_DFM_RESULT_CACHE_ENABLED: bool = False
    # HTTP timeouts for the two cache hops (signed-URL lookup, then signed-URL
    # download).  Reads are short — the whole point is to skip a slow compute.
    GEOMETRY_CACHE_LOOKUP_TIMEOUT_SECONDS: float = 5.0
    GEOMETRY_CACHE_DOWNLOAD_TIMEOUT_SECONDS: float = 30.0

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
