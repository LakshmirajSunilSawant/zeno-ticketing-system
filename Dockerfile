# WHY a container rather than installing Python on the host: the image pins the exact runtime and
# dependency set, so "works on my machine" and "works on EC2" are the same artifact. It's also the
# thing that makes this portable to ECS/App Runner/Kubernetes later without a rewrite.

# WHY slim and not alpine: alpine uses musl libc, so manylinux wheels (psycopg2, bcrypt,
# cryptography) don't apply and pip falls back to compiling from source - a slow, fragile build on
# a 1 GiB free-tier instance. slim is Debian-based and installs those wheels directly.
FROM python:3.11-slim

# PYTHONUNBUFFERED: logs reach docker logs immediately instead of sitting in a buffer, which
# matters because our structured access log is the only observability on this box.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# WHY copy requirements before the source: Docker caches layers. Dependencies only reinstall when
# requirements.txt changes, so a code-only redeploy rebuilds in seconds instead of minutes.
# WHY requirements.txt and not requirements-dev.txt: pytest and locust have no business in a
# production image - slower builds, bigger image, wider attack surface.
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# WHY a non-root user: if the app is ever compromised, the attacker lands as an unprivileged user
# rather than as root inside the container. Cheap, and the first thing a security review asks about.
RUN useradd --create-home --uid 1000 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# WHY seed runs before uvicorn: seed.py is idempotent, so every boot guarantees an admin account
# and demo events exist. WHY --workers 1: the rate limiter's counter is in-process (see
# ratelimit.py), and the free-tier instance has 1 GiB of RAM shared with Postgres.
CMD ["sh", "-c", "python seed.py && exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1"]
