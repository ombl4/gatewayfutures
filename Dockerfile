FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app
# UV_NO_DEV/UV_FROZEN: `uv run` inside the container must not re-sync the dev group
# (Playwright, ruff: ~56 MB) on every start; the image is built --no-dev on purpose.
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_NO_DEV=1 UV_FROZEN=1

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

COPY . .
RUN uv sync --frozen --no-dev

CMD ["uv", "run", "gf", "--help"]
