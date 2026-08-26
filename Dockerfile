FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY consola_latam ./consola_latam

ENV SCRAPER_APP_DATA_DIR=/data
VOLUME ["/data"]

EXPOSE 8000

CMD ["python", "-m", "consola_latam.webapp", "--host", "0.0.0.0", "--port", "8000", "--no-browser"]
