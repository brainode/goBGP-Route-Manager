# SPDX-License-Identifier: GPL-2.0-only
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
ARG GOBGP_VERSION=4.2.0
ARG TARGETARCH=amd64

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

RUN case "${TARGETARCH}" in \
        amd64|arm64) GOBGP_ARCH="${TARGETARCH}" ;; \
        *) echo "Unsupported TARGETARCH: ${TARGETARCH}" >&2; exit 1 ;; \
    esac \
    && curl -fsSL https://github.com/osrg/gobgp/releases/download/v${GOBGP_VERSION}/gobgp_${GOBGP_VERSION}_linux_${GOBGP_ARCH}.tar.gz -o /tmp/gobgp.tar.gz \
    && tar -xzf /tmp/gobgp.tar.gz -C /tmp \
    && install -m 0755 /tmp/gobgp /usr/local/bin/gobgp \
    && rm -f /tmp/gobgp.tar.gz /tmp/gobgp

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

