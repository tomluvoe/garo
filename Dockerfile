FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements-mcp.txt ./
RUN python -m pip install --no-cache-dir --disable-pip-version-check -r requirements-mcp.txt

COPY garo_bridge.py ./
COPY dashboard/ ./dashboard/

USER 10001:10001
EXPOSE 8765
ENTRYPOINT ["python", "/app/garo_bridge.py"]
CMD ["dashboard", "--listen-host", "0.0.0.0"]
