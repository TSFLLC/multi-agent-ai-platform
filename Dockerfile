# Hosted deployment image — MA7.7B (Railway staging).
#
# Not used by local development (Section Commands: `python -m uvicorn
# app.main:app --reload` / `python -m app.worker` are run directly against
# the .venv, unchanged). This image exists only for a hosted container.
#
# Pinned to a Python version Railpack does not support (repo baseline is
# 3.8; Railpack's Python builder only supports >=3.10) — using our own
# Dockerfile bypasses that mismatch entirely rather than requiring every
# local Python 3.8 assumption in the codebase to be revisited. requirements.txt
# already only uses version ranges known to install on 3.8 through at
# least this image's 3.11 (verified locally: `pip install -r
# requirements.txt` against a 3.8 venv resolves the same pins this image
# would use).
FROM python:3.11-slim AS runtime

WORKDIR /app

# System deps: none beyond what the slim base already has -- cryptography
# ships manylinux wheels for this image's platform, and the rest of
# requirements.txt is pure-Python or also wheel-only.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./
COPY frontend ./frontend
COPY scripts ./scripts

# Railway sets $PORT at runtime; EXPOSE here is documentation only (the
# actual bind port is resolved by scripts/hosted_entrypoint.py).
EXPOSE 8000

# scripts/hosted_entrypoint.py: migrate -> confirm head -> start the
# Worker and Web processes as supervised siblings. MAP_HOSTED_MODE,
# MAP_AUTH_TOKEN, MAP_SECRET_ENCRYPTION_KEY, MAP_DATA_ROOT,
# MAP_ALLOW_REMOTE_BIND and MAP_HOST are supplied as Railway service
# variables (see .env.example) -- none are baked into this image.
CMD ["python", "-m", "scripts.hosted_entrypoint"]
