# BarPoint Staff Bot — one image, three roles (migrate / bot / scheduler).
# Python 3.12 exactly as TZ 3.1 requires; the local venv may be newer, the image is not.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # Everything is stored in UTC (TZ 3.4); venue-local time is computed in the code.
    TZ=UTC

WORKDIR /app

# Project metadata and sources. `tests/` and `docs/` stay out of the image (.dockerignore):
# in particular the reference workbook must never reach a production container.
COPY pyproject.toml ./
COPY src ./src
COPY alembic ./alembic
COPY alembic.ini ./
COPY docker ./docker

# No build toolchain is installed: every dependency (asyncpg, pydantic-core) publishes
# manylinux wheels, so the image stays small and the build stays fast.
RUN pip install --no-cache-dir . \
    && chmod +x /app/docker/entrypoint.sh \
    && useradd --system --create-home --home-dir /home/app app \
    && chown -R app:app /app

USER app

ENTRYPOINT ["/app/docker/entrypoint.sh"]
CMD ["python", "-m", "src.bot"]
