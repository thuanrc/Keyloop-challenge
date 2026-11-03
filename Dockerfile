FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim
ENV UV_PYTHON_DOWNLOADS=never UV_COMPILE_BYTECODE=1 PATH="/app/.venv/bin:$PATH"
WORKDIR /app
COPY . .
RUN uv sync --frozen --no-dev
EXPOSE 8000
CMD ["sh", "-c", "python -m keyloop_test.seed && keyloop-test"]
