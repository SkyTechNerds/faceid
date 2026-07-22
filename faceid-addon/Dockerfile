ARG BUILD_FROM
FROM $BUILD_FROM

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3-venv python3-dev build-essential \
        libglib2.0-0 libgl1 libxcb1 libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/faceid
COPY requirements.txt .
RUN python3 -m venv venv \
    && venv/bin/pip install --no-cache-dir --upgrade pip \
    && venv/bin/pip install --no-cache-dir -r requirements.txt \
    && apt-get purge -y build-essential python3-dev && apt-get autoremove -y

COPY app app
COPY static static
COPY run.sh /
RUN chmod a+x /run.sh

CMD ["/run.sh"]
