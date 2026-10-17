# Image of the pipeline Jobs: `python -m jobs train|predict|report|promote ...`.
# Two stages: uv builds the virtualenv from uv.lock, the runtime stage gets
# only the venv (no uv, no build cache). torch is the CPU build (see
# pyproject.toml), ~1 GB of the image.

FROM python:3.13-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.13.0 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app

# Dependencies first: this layer is rebuilt only when the lock file changes.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.13-slim
RUN useradd --uid 10001 --create-home app
COPY --from=build /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    # Model downloads go through the MLflow server, like uploads: MLflow 3
    # would otherwise hand out presigned URLs straight to the object store,
    # and the Job would need network access to it.
    MLFLOW_ENABLE_PROXY_MULTIPART_DOWNLOAD=false \
    MLFLOW_DISABLE_AGENT_HINT=1
USER 10001
ENTRYPOINT ["python", "-m", "jobs"]
