from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    RABBITMQ_URI: str = "amqp://guest:guest@localhost:5672/"
    STORAGE_ENDPOINT: str = "localhost:9000"
    STORAGE_ACCESS_KEY: str = "minioadmin"
    STORAGE_SECRET_KEY: str = "minioadmin"
    STORAGE_BUCKET: str = "uploads"

    UPLOAD_SERVICE_URL: str = "http://localhost:6900"

    JWT_SECURITY_KEY: str = ""
    JWT_PRIVATE_KEY: str = (
        ""  # RSA private key PEM (Base64-encoded) — preferred for signing
    )
    JWT_ISSUER: str = "https://api.maliev.com"
    JWT_AUDIENCE: str = "https://api.maliev.com"

    OTEL_EXPORTER_OTLP_ENDPOINT: str = "http://localhost:4317"
    SERVICE_NAME: str = "maliev-geometryservice"

    MAX_FILE_SIZE_MB: int = 200

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
