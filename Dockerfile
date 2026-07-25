# Standalone container — for Home Assistant Container/Core users, who cannot install
# apps, and for anyone who would rather not run this on the host.
#
#   docker compose up -d      (see docker-compose.yml)
#
# config.yaml and the gallery live in mounted volumes, so both survive image updates.
FROM python:3.12-slim

# InsightFace caches its model pack under $HOME/.insightface — pointing HOME into the
# data volume keeps the ~300 MB out of the image and avoids a re-download after updates.
ENV PYTHONUNBUFFERED=1 \
    HOME=/opt/faceid/data/model-cache

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libglib2.0-0 libgl1 libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/faceid

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt \
    && apt-get purge -y build-essential && apt-get autoremove -y

COPY app app
COPY static static
COPY scripts scripts
COPY docs/example-config.yaml docs/example-config.yaml

# Gallery, settings and the downloaded model pack — mount this.
VOLUME ["/opt/faceid/data"]
EXPOSE 8600

HEALTHCHECK --interval=30s --timeout=5s --start-period=180s \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8600/api/health',timeout=4).status==200 else 1)"

CMD ["python", "-m", "app.main"]
