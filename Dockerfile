# Production image — targets Kubernetes Engine (Linux).
# This same image is what Aspire builds locally via AddDockerfile, so local and
# production environments are identical (python:3.12-slim, OSMesa, Xvfb, gmsh).
#
# Stage 1: Build
FROM python:3.12-slim AS builder

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
FROM python:3.12-slim AS production

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYSETUP_PATH="/opt/pysetup" \
    VENV_PATH="/opt/pysetup/.venv"

ENV PATH="$VENV_PATH/bin:$PATH"

# Install system dependencies for gmsh, headless rendering (OSMesa), and virtual framebuffer (xvfb)
RUN apt-get update \
    && apt-get install --no-install-recommends -y \
    libgl1 \
    libglu1-mesa \
    libx11-6 \
    libxext6 \
    libxrender1 \
    libxcursor1 \
    libxinerama1 \
    libxft2 \
    libxi6 \
    libxmu6 \
    libxt6 \
    gmsh \
    libosmesa6 \
    xvfb \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

ENV PYVISTA_OFF_SCREEN=true
ENV MESA_GL_VERSION_OVERRIDE=3.3
ENV PYTHONPATH=/app
ENV OTEL_EXPORTER_OTLP_ENDPOINT=
# Xvfb display — must match the display started in CMD below.
# Worker subprocesses inherit this env var so they know which display to use.
ENV DISPLAY=:99

WORKDIR /app

COPY --from=builder $VENV_PATH $VENV_PATH
COPY src/ ./src/

# Verify cadquery/OCP imports work — fail build if they don't
RUN python -c "import cadquery; from OCP.BRep import BRep_Tool; print('cadquery + OCP OK')"

# Create a non-root user
RUN useradd -m appuser && chown -R appuser /app
USER appuser

EXPOSE 8081

# Start Xvfb (virtual framebuffer) before uvicorn so PyVista/VTK off-screen
# rendering works without a real GPU or display.  Without Xvfb, pv.Plotter()
# hangs indefinitely, blocking all ProcessPoolExecutor workers.
CMD ["sh", "-c", "Xvfb :99 -screen 0 1024x768x24 -ac +extension GLX +render -noreset & sleep 1 && exec uvicorn src.main:app --host 0.0.0.0 --port 8081"]
