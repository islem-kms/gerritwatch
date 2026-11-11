FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/ /app/backend/

# flags.json, feedback.json, state and event logs live in /data (a Docker volume)
RUN useradd --create-home gw && mkdir /data && chown gw /data
USER gw
WORKDIR /data
ENV ENRICH_DATA_DIR=/data PYTHONUNBUFFERED=1

CMD ["python", "/app/backend/serve_cors.py"]
