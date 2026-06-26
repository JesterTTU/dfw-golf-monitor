FROM python:3.11-slim

WORKDIR /app

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Python deps first (for layer cache)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Source
COPY *.py ./
COPY *.html ./
COPY *.json ./

# Data directory (DB lives here, mounted as a volume)
RUN mkdir -p /app/data

ENV DB_PATH=/app/data/tee_times.db
ENV PORT=8080

EXPOSE 8080

CMD ["python", "web.py"]
