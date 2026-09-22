# Credit Union Ops Simulator — the application under test.
#
# This image is deliberately unaware of the automation system: no policy, no model,
# no discovery code. It serves HTML and responds to HTTP, nothing more.
# Built natively for the host architecture (arm64 here) — never pin --platform.

# ---------- builder: resolve dependencies from the lockfile ----------
FROM python:3.12-slim AS builder

ENV POETRY_VERSION=2.1.3 \
    POETRY_NO_INTERACTION=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN pip install "poetry==${POETRY_VERSION}"

WORKDIR /app

# Manifests only, so the dependency layer caches independently of application code.
COPY pyproject.toml poetry.lock ./

# --no-root: this repo has no importable root package (apps/ and src/ are plain directories).
# --only main: pins the intent now, before a dev group exists.
RUN poetry config virtualenvs.in-project true \
 && poetry install --only main --no-root

# ---------- runtime: the venv plus the app, no build tooling ----------
FROM python:3.12-slim AS runtime

# WORKDIR is load-bearing, not cosmetic. server.py mounts
# StaticFiles(directory="apps/cred_union_sim/static") and
# Jinja2Templates(directory="apps/cred_union_sim/templates") — both CWD-relative.
# Any other WORKDIR and the app boots but every page fails on template lookup.
WORKDIR /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CRED_UNION_SIM_DB=":memory:"

COPY --from=builder /app/.venv /app/.venv
COPY apps/ /app/apps/

# Non-root is safe here: the DB is in-memory and templates/static are read-only.
RUN useradd --create-home --uid 10001 app \
 && chown -R app:app /app
USER app

EXPOSE 8001

# --host 0.0.0.0 is required: uvicorn's default 127.0.0.1 would be unreachable
# from the sandbox container.
CMD ["uvicorn", "apps.cred_union_sim.server:app", "--host", "0.0.0.0", "--port", "8001"]
