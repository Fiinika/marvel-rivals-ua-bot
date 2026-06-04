# Container image for the Marvel Rivals UA submission bot.
#
# A single long-polling process: no inbound ports, no web server. Runtime
# configuration comes from the environment (see .env.example). The SQLite
# database lives at DATABASE_PATH, which docker-compose.prod.yml points at the
# mounted /data volume so it survives container recreation.

FROM python:3.12-slim

# Predictable runtime: unbuffered logs, no .pyc clutter, lean pip.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install dependencies first so this layer is cached across code-only changes.
# All deps ship manylinux wheels (incl. pydantic-core), so no build toolchain
# is needed. tzdata is a pip dependency, so timezones work without OS packages.
COPY requirements.txt ./
RUN pip install -r requirements.txt

# Application code.
COPY . .

# Run as an unprivileged user with a writable data dir. Docker seeds a fresh
# named volume from this directory, so the volume inherits app's ownership and
# the bot can create the SQLite file there.
RUN useradd --create-home --uid 10001 app \
    && mkdir -p /data \
    && chown -R app:app /app /data
USER app

# Default DB location; compose mounts the persistent volume here.
ENV DATABASE_PATH=/data/bot.db

CMD ["python", "main.py"]
