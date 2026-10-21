FROM python:3.12-slim
ARG VERSION=0.1.0
LABEL org.opencontainers.image.title="WB2Api" \
      org.opencontainers.image.version=$VERSION \
      org.opencontainers.image.source="https://github.com/Likhixang/WB2Api"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 WB2API_DATA_DIR=/data
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid 10001 wb2api \
    && useradd --uid 10001 --gid wb2api --no-create-home wb2api \
    && mkdir /data && chown wb2api:wb2api /data
COPY --chown=wb2api:wb2api wb2api ./wb2api
USER 10001:10001
EXPOSE 8080
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)"]
CMD ["uvicorn", "wb2api.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1", "--no-access-log"]
