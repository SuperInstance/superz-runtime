FROM python:3.12-slim

LABEL maintainer="SuperZ Runtime <runtime@superz.dev>"
LABEL description="Self-booting Pelagic fleet runtime"

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    gh \
    curl \
    make \
    && rm -rf /var/lib/apt/lists/*

# Working directory
WORKDIR /opt/superz-runtime

# Copy runtime package
COPY . .

# Install Python dependencies
RUN pip install --no-cache-dir pyyaml>=6.0

# Create instance directories
RUN mkdir -p /root/.superinstance/{agents,logs,vault,workshop,worlds}

# Clone all fleet agents during build (shallow clone)
RUN if [ -n "$GITHUB_TOKEN" ]; then \
        GIT_URL="https://${GITHUB_TOKEN}@github.com"; \
    else \
        GIT_URL="https://github.com"; \
    fi && \
    for repo in trail-agent trust-agent flux-vm-agent knowledge-agent \
               scheduler-agent edge-relay liaison-agent cartridge-agent \
               keeper-agent git-agent holodeck-studio; do \
        if [ ! -d "/root/.superinstance/agents/${repo}" ]; then \
            echo "Cloning ${repo}..." && \
            git clone --depth 1 "${GIT_URL}/SuperInstance/${repo}.git" \
                "/root/.superinstance/agents/${repo}" 2>/dev/null || \
            echo "Warning: Could not clone ${repo} — will use stub"; \
        fi; \
    done && \
    echo "Agent clone complete"

# Expose fleet ports
EXPOSE 8443 8444 7777 8501 8502 8503 8504 8505 8506 8507 8508

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "from urllib.request import urlopen; urlopen('http://127.0.0.1:8443/health', timeout=3)" || exit 1

# Runtime environment
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV SUPERZ_HEADLESS=true

# Entrypoint
ENTRYPOINT ["python", "-m", "superz_runtime"]
CMD ["--headless"]
