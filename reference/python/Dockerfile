FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir ".[all]" \
 && useradd --system --uid 10001 --home /app jarvis

USER jarvis
EXPOSE 8080
# Konfiguration, Policies, Persona und Schemas werden nach /app/config und /app/schemas gemountet
CMD ["python", "-m", "jarvis.app"]
