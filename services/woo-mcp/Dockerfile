FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY woo_mcp ./woo_mcp

# Numeric, non-root user: the pod spec can then enforce runAsNonRoot.
USER 10001:10001
EXPOSE 8000
CMD ["uvicorn", "woo_mcp.server:build_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
