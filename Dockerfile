# Bank (:8000) and the demo seller (:8001) in one container: the seeded sellers call http://localhost:8001,
# the seller calls the bank on http://localhost:8000. Only :8000 is published (behind Caddy, deploy/Caddyfile).
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 DB_PATH=/app/data/bank.db
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt deploy/constraints.txt ./
# constraints = `pip freeze` of the environment the test suite passed on
RUN pip install -r requirements.txt -c constraints.txt
COPY . .
RUN useradd -m -u 10001 app && mkdir -p /app/data && chown -R app /app/data
USER app
EXPOSE 8000
HEALTHCHECK --interval=20s --timeout=5s --start-period=40s CMD curl -fsS http://localhost:8000/health || exit 1
CMD ["sh", "deploy/entrypoint.sh"]
