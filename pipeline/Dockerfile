# Pipeline image — daily ETL + train + score + agents (Cloud Run Job).
FROM python:3.14-slim

ARG GIT_SHA=unknown
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    OSS_RADAR_BACKEND=bigquery \
    OSS_RADAR_ENV=cloud \
    MLFLOW_TRACKING_URI=file:/tmp/mlruns \
    GIT_SHA=${GIT_SHA}

# libgomp1 is required at runtime by LightGBM (OpenMP).
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pipeline/requirements.txt /app/pipeline/requirements.txt
RUN pip install -r /app/pipeline/requirements.txt
COPY pipeline/ /app/pipeline/
RUN pip install --no-deps -e /app/pipeline

ENTRYPOINT ["python", "-m", "oss_radar.cli"]
CMD ["run"]
