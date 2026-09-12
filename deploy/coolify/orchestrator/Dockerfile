FROM python:3.12-slim

WORKDIR /app

# Install curl for Coolify health checks
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY config.py models.py db.py docker_manager.py main.py ./

# Persistent data directory (SQLite DB lives here)
VOLUME /data

EXPOSE 3000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "3000", "--log-level", "info"]
