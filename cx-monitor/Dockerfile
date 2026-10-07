FROM python:3.12-slim
WORKDIR /app
COPY cx-monitor/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY cx-monitor/monitor ./monitor
ENV PYTHONUNBUFFERED=1
CMD ["sh", "-c", "uvicorn monitor.app:app --host 0.0.0.0 --port ${PORT:-8080} --no-access-log"]
