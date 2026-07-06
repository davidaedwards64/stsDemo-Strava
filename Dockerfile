# Build: install deps using standard Python (has pip + wheels)
FROM python:3.12-slim AS build
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --target /app/.python-deps -r requirements.txt

# Runtime: Wolfi-based final stage as required by OAP
FROM cgr.dev/chainguard/wolfi-base@sha256:b78bb982194828b6c9c214230bf34d51944e2102ea8468f01ac21e5f99328efd
RUN apk add --no-cache python-3.12
WORKDIR /app
COPY --from=build /app/.python-deps /app/.python-deps
COPY --chown=nonroot:nonroot . /app/
ENV PYTHONPATH=/app/.python-deps
USER nonroot
EXPOSE 8080
CMD ["python3.12", "-m", "uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8080"]
