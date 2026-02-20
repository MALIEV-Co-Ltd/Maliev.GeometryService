# Stage 1: Build
FROM python:3.11-slim AS builder

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    POETRY_VERSION=2.2.1 \
    POETRY_HOME="/opt/poetry" \
    POETRY_VIRTUALENVS_IN_PROJECT=true \
    POETRY_NO_INTERACTION=1 \
    PYSETUP_PATH="/opt/pysetup" \
    VENV_PATH="/opt/pysetup/.venv"

ENV PATH="$POETRY_HOME/bin:$VENV_PATH/bin:$PATH"

RUN apt-get update \
    && apt-get install --no-install-recommends -y \
    curl \
    build-essential

# Install Poetry
RUN curl -sSL https://install.python-poetry.org | python3 -

WORKDIR $PYSETUP_PATH
COPY pyproject.toml poetry.lock* ./

RUN poetry install --only main --no-root

# Stage 2: Production
FROM python:3.11-slim AS production

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYSETUP_PATH="/opt/pysetup" \
    VENV_PATH="/opt/pysetup/.venv"

ENV PATH="$VENV_PATH/bin:$PATH"

# Install system dependencies for gmsh, trimesh, and pyrender (EGL)
RUN apt-get update \
    && apt-get install --no-install-recommends -y \
    libglu1-mesa \
    libxcursor1 \
    libxinerama1 \
    libxft2 \
    gmsh \
    libgl1-mesa-dri \
    libegl1 \
    libgles2 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

ENV PYOPENGL_PLATFORM=egl

WORKDIR /app

COPY --from=builder $VENV_PATH $VENV_PATH
COPY src/ ./src/

# Create a non-root user
RUN useradd -m appuser && chown -R appuser /app
USER appuser

EXPOSE 8080

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8080"]
