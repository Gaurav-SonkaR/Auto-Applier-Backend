# AutoApply AI backend.
#
# Built for Oracle Cloud Always Free (ARM64) per the architecture document, but
# the base image is multi-arch so it builds on x86 dev machines unchanged.
#
# Two stages: the builder compiles wheels, the runtime carries none of the build
# toolchain. That matters here because Chromium and a TeX Live
# subset already make this image large.

# ── Stage 1: build Python wheels ─────────────────────────────────────────────
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# build-essential is needed for any sdist that lacks an ARM wheel.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements.txt .
RUN pip wheel --wheel-dir /wheels -r requirements.txt


# ── Stage 2: runtime ─────────────────────────────────────────────────────────
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    CHROME_BINARY=/usr/bin/chromium

# texlive-* : pdflatex for resume compilation (FR-003).
#   We install only the collections the template needs (geometry, titlesec,
#   enumitem, hyperref, lmodern, microtype) rather than texlive-full, which is
#   ~5GB and would blow the free tier's 100GB storage budget (NFR-004) for no
#   benefit — the template deliberately avoids exotic packages.
# Chromium and its matching driver are installed from the same Debian repo.
RUN apt-get update && apt-get install -y --no-install-recommends \
        texlive-latex-base \
        texlive-latex-recommended \
        texlive-fonts-recommended \
        texlive-latex-extra \
    chromium chromium-driver \
        libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
        libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 \
        libxrandr2 libgbm1 libasound2 libpango-1.0-0 libcairo2 \
        fonts-liberation ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /wheels /wheels
COPY requirements.txt .
RUN pip install --no-index --find-links=/wheels -r requirements.txt \
    && rm -rf /wheels requirements.txt

WORKDIR /app
COPY backend/ ./backend/
COPY templates/ ./templates/
COPY scripts/ ./scripts/
COPY master_profile.json ./

# Non-root. Volumes are chowned so the container can write its DB and PDFs.
RUN useradd --create-home --uid 1000 autoapply \
    && mkdir -p /app/data /app/resumes /app/screenshots /app/logs \
    && chown -R autoapply:autoapply /app
USER autoapply

EXPOSE 8000

# The DB is a file — a container that answers /health but has a corrupt schema
# is worse than one that restarts, so the check exercises a real endpoint.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"

# 0.0.0.0 inside the container is required for Docker port publishing to reach
# it. Exposure is controlled by docker-compose binding to 127.0.0.1 on the host.
CMD ["uvicorn", "backend.app.main:app", "--host", "0.0.0.0", "--port", "8000"]
