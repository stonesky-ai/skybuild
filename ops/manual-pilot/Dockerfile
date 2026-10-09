FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml /app/
COPY ops/manual-pilot/requirements.txt /app/requirements.txt
COPY src /app/src
RUN python -m pip install --root-user-action=ignore --no-cache-dir -r requirements.txt \
    && python -m pip install --root-user-action=ignore --no-cache-dir --no-deps . \
    && useradd --uid 10001 --no-create-home skybuild
USER skybuild
CMD ["python", "-m", "skybuild", "serve", "--host", "0.0.0.0", "--port", "8000"]
