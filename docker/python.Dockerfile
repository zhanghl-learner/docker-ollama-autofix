FROM python:3.11-slim

LABEL org.opencontainers.image.title="AutoFix Python Builder"
LABEL org.opencontainers.image.description="Offline-ready Python test environment for AutoFix"

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN python -m pip install \
    httpx==0.28.1 \
    pytest==9.0.2 \
    PyYAML==6.0.2 \
    rich==13.7.1 \
    typer==0.24.1

WORKDIR /workspace

