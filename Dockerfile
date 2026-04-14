FROM python:3.12-slim

LABEL maintainer="SuperZ Fleet <fleet@superinstance.dev>"
LABEL description="Self-booting Pelagic fleet runtime"

RUN apt-get update && \
    apt-get install -y --no-install-recommends git && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY . /app/superz-runtime/

RUN pip install --no-cache-dir pyyaml

# Fleet agents are cloned at runtime, not build time (they may be private).
# The runtime will auto-clone from GitHub when first booted.

EXPOSE 8443 8444 7777 8501 8502 8503 8504 8505 8506 8507 8508 8509 8510 8511 8512

# Non-root user for security
RUN useradd --create-home --shell /bin/bash superz
USER superz
ENV HOME=/home/superz

CMD ["python", "-m", "superz_runtime"]
