from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    RABBITMQ_URI: str = "amqp://guest:guest@localhost:5672/"
    STORAGE_ENDPOINT: str = "localhost:9000"
    STORAGE_ACCESS_KEY: str = "minioadmin"
    STORAGE_SECRET_KEY: str = "minioadmin"
    STORAGE_BUCKET: str = "uploads"

    OTEL_EXPORTER_OTLP_ENDPOINT: str = "http://localhost:4317"
    SERVICE_NAME: str = "geometry-service"

    MAX_FILE_SIZE_MB: int = 200

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
