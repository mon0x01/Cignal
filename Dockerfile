FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv/cignal
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl gcc libpq-dev postgresql-client \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./requirements.txt
RUN pip install --upgrade pip && pip install -r requirements.txt
COPY . .
RUN useradd --create-home --uid 10001 cignal \
    && mkdir -p /data/evidence /data/backups /data/models \
    && chown -R cignal:cignal /srv/cignal /data
USER cignal
EXPOSE 8000 8080
CMD ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
