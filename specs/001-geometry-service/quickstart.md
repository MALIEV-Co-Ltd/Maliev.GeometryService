# Quickstart: Geometry Analysis Service

## Prerequisites

- Python 3.11+
- Docker & Docker Compose
- Poetry (for dependency management)

## Setup

1. **Install Dependencies**:
   ```bash
   poetry install
   ```

2. **Environment Variables**:
   Copy `.env.example` to `.env`:
   ```bash
   cp .env.example .env
   ```
   *Required Variables:*
   - `RABBITMQ_URI`: amqp://guest:guest@localhost:5672/

## Running Locally

1. **Start Infrastructure (RabbitMQ)**:
   ```bash
   docker-compose up -d
   ```

2. **Run Service**:
   ```bash
   poetry run python -m src.main
   ```

## Testing

1. **Run Unit Tests**:
   ```bash
   poetry run pytest
   ```

2. **Run Integration Tests**:
   ```bash
   poetry run pytest -m integration
   ```
