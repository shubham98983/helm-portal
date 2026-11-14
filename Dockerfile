FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HELM_PORT=8100
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --system --uid 10001 --create-home helm \
    && mkdir -p /data
COPY helm ./helm
COPY frontend ./frontend
COPY config ./config
RUN chown -R helm:helm /app /data
USER helm
VOLUME ["/data"]
EXPOSE 8100
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8100/health', timeout=2)"
CMD ["python", "-m", "helm"]
