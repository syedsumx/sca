FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install .

COPY alembic.ini ./
COPY migrations ./migrations
COPY tests ./tests

EXPOSE 8000
CMD ["sh", "-c", "tether migrate && uvicorn tether.api.app:app --host 0.0.0.0 --port 8000"]
