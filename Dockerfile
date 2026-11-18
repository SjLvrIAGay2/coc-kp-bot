FROM python:3.14-slim
ENV PYTHONUNBUFFERED=1 PYTHONUTF8=1 COCKP_DATABASE=/app/data/cockp.sqlite3
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends fonts-noto-cjk && rm -rf /var/lib/apt/lists/*
COPY requirements.lock pyproject.toml ./
RUN pip install --no-cache-dir -r requirements.lock
COPY src ./src
COPY scripts/qq_official_session.py scripts/qq_personal_admin_bridge.py ./scripts/
RUN pip install --no-cache-dir --no-deps --no-build-isolation . && useradd --uid 10001 --create-home kp && mkdir -p /app/data && chown -R kp:kp /app
USER kp
EXPOSE 8080
VOLUME ["/app/data"]
CMD ["cockp", "serve", "--host", "0.0.0.0", "--port", "8080"]
