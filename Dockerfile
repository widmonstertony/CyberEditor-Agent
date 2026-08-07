FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CYBEREDITOR_STORAGE=/data

WORKDIR /app

COPY src /app/src
COPY web /app/web
COPY control_plane.py /app/control_plane.py

RUN addgroup --system cybereditor \
    && adduser --system --ingroup cybereditor cybereditor \
    && mkdir -p /data \
    && chown -R cybereditor:cybereditor /app /data

USER cybereditor
EXPOSE 8765

CMD ["python", "control_plane.py", "--host", "0.0.0.0", "--port", "8765"]
