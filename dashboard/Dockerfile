# Dashboard image — FastAPI read-only API + SPA (Cloud Run Service, scales to zero).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    OSS_RADAR_BACKEND=bigquery \
    OSS_RADAR_ENV=cloud \
    PORT=8080

WORKDIR /app
COPY dashboard/requirements.txt /app/dashboard/requirements.txt
RUN pip install -r /app/dashboard/requirements.txt
# the package provides oss_radar.{config,warehouse}; --no-deps avoids pulling ML libs
COPY pipeline/ /app/pipeline/
COPY dashboard/ /app/dashboard/
RUN pip install --no-deps -e /app/pipeline

EXPOSE 8080
CMD exec uvicorn dashboard.app.main:app --host 0.0.0.0 --port ${PORT}
