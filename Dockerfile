# syntax=docker/dockerfile:1.9

# ---------------------------------------------------------------- builder ---
FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /waypoint

# Dependencies first: cached until the lockfile changes.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project --no-dev --extra server

COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --extra server

# ---------------------------------------------------------------- runtime ---
FROM python:3.14-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/waypoint/.venv/bin:$PATH" \
    ENV=production \
    JSON_LOGS=true

# The WORKDIR must exist and be owned by `waypoint` *before* the copy: WORKDIR
# creates it as root, and COPY --chown only touches what it copies.
RUN groupadd --system waypoint \
    && useradd --system --gid waypoint --home /waypoint waypoint \
    && install -d -o waypoint -g waypoint /waypoint

WORKDIR /waypoint
COPY --from=builder --chown=waypoint:waypoint /waypoint /waypoint
# The documentation, for the public demo corpus (RAG__SEED_DIR=docs). A few
# hundred KB; seeding re-embeds only the pages a deploy changed.
COPY --chown=waypoint:waypoint docs ./docs

USER waypoint
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/v1/healthz', timeout=3).status==200 else 1)"

# One worker per core is wrong here: runs are I/O bound and concurrency comes
# from each worker's event loop, not from forking. Two workers gives you rolling
# restarts and a spare; raise it only if CPU-bound work appears.
#
# No --access-logfile: waypoint.middleware logs every request once, as JSON,
# with request id and tenant. uvicorn's own access line would duplicate it.
#
# --no-control-socket: gunicorn 26 opens a control socket under $HOME/.gunicorn
# by default, and nothing here uses `gunicorn ctl`. To keep it instead, point it
# somewhere writable: --control-socket /tmp/gunicorn.ctl
CMD ["gunicorn", "waypoint.asgi:app", \
     "--worker-class", "uvicorn.workers.UvicornWorker", \
     "--workers", "2", \
     "--no-control-socket", \
     "--bind", "0.0.0.0:8000", \
     "--timeout", "600", \
     "--graceful-timeout", "30", \
     "--keep-alive", "75"]
