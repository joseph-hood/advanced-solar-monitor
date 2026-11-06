FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install ".[dashboard,mqtt]"
COPY config ./config
COPY app ./app
COPY .streamlit ./.streamlit
# Non-editable install: point the config paths at /app explicitly.
ENV MODEL_PARAMS_FILE=/app/config/model_params.yaml \
    PIPELINE_FILE=/app/config/pipeline.yaml \
    EG4_FIELD_MAP=/app/config/eg4_cloud.yaml \
    MQTT_TOPICS_FILE=/app/config/topics.yaml \
    DB_PATH=/data/eg4mon.sqlite
VOLUME /data
EXPOSE 8000 8501
CMD ["eg4mon", "collect"]
