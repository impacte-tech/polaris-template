FROM apache/polaris:1.7.0 AS polaris
FROM apache/polaris-admin-tool:1.7.0 AS admin
FROM eclipse-temurin:21-jre-noble
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-venv tini ca-certificates && rm -rf /var/lib/apt/lists/*
COPY --from=polaris /deployments /opt/polaris
COPY --from=admin /deployments /opt/polaris-admin
WORKDIR /app
COPY requirements.txt .
RUN python3 -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt && useradd --uid 10001 --create-home catalog
COPY . /app
ENV PATH="/opt/venv/bin:$PATH" PORT=8080 PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER catalog
EXPOSE 8080
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "launcher.py"]
