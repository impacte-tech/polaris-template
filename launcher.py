"""Bootstrap once, initialize a warehouse, and supervise both local processes."""
import json
import os
import signal
import subprocess
import sys
import tempfile
import time

import httpx
import psycopg

from config import settings, java_environment, catalog_payload

children = []
stopping = False
secret_files = []


def stop(signum=None, frame=None):
    global stopping
    stopping = True
    for child in reversed(children):
        if child.poll() is None:
            child.terminate()


def initialize(config):
    with httpx.Client(base_url="http://127.0.0.1:8181", timeout=30) as client:
        response = client.post("/api/catalog/v1/oauth/tokens", data={
            "grant_type": "client_credentials", "client_id": config["client"],
            "client_secret": config["secret"], "scope": "PRINCIPAL_ROLE:ALL"})
        if response.status_code != 200:
            raise RuntimeError("Polaris login failed. Existing database credentials must match POLARIS_CLIENT_ID/SECRET; changing environment variables does not rotate them.")
        client.headers.update({"Authorization": "Bearer " + response.json()["access_token"]})
        path = "/api/management/v1/catalogs/" + config["name"]
        response = client.get(path)
        expected = catalog_payload(config)["catalog"]
        if response.status_code == 404:
            response = client.post("/api/management/v1/catalogs", json={"catalog": expected})
            response.raise_for_status()
            print("Created warehouse", config["name"], flush=True)
        else:
            response.raise_for_status()
            existing = response.json()
            if (existing["properties"].get("default-base-location") != expected["properties"]["default-base-location"]
                or any(existing["storageConfigInfo"].get(k) != v for k, v in expected["storageConfigInfo"].items())):
                raise RuntimeError("Existing warehouse differs from storage environment variables. Restore the original settings or explicitly update the catalog through the management API.")
            print("Warehouse already initialized; preserving catalog state", flush=True)


def main():
    config = settings()
    env = java_environment(config)
    signing_file = tempfile.NamedTemporaryFile(mode="w", suffix=".key")
    secret_files.append(signing_file)
    signing_file.write(config["signing"])
    signing_file.flush()
    env.pop("POLARIS_AUTHENTICATION_TOKEN_BROKER_SYMMETRIC_KEY_SECRET", None)
    env["POLARIS_AUTHENTICATION_TOKEN_BROKER_SYMMETRIC_KEY_FILE"] = signing_file.name
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    # Retry connection failures only. Never swallow bootstrap failures.
    for attempt in range(60):
        if stopping:
            return
        try:
            with psycopg.connect(config["database"], connect_timeout=3):
                break
        except psycopg.OperationalError:
            if attempt == 59:
                raise RuntimeError("PostgreSQL did not become available") from None
            time.sleep(2)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json") as credentials:
        json.dump({"POLARIS": {"client-id": config["client"], "client-secret": config["secret"]}}, credentials)
        credentials.flush()
        bootstrap = subprocess.Popen(["java", "-Xmx256m", "-jar", "/opt/polaris-admin/polaris-admin-tool.jar",
                                      "bootstrap", "--credentials-file=" + credentials.name], env=env)
        children.append(bootstrap)
        if bootstrap.wait() != 0:
            raise RuntimeError("Polaris database bootstrap failed")
    if stopping:
        return
    java = subprocess.Popen(["java", "-XX:MaxRAMPercentage=65", "-XX:+ExitOnOutOfMemoryError",
                             "-jar", "/opt/polaris/quarkus-run.jar"], env=env)
    children.append(java)
    for attempt in range(120):
        if stopping:
            return
        if java.poll() is not None:
            raise RuntimeError("Polaris exited during startup")
        try:
            # The OAuth endpoint only becomes available once the application is listening.
            response = httpx.get("http://127.0.0.1:8181/api/catalog/v1/config", timeout=2)
            if response.status_code in (200, 400, 401, 403):
                break
        except httpx.HTTPError:
            pass
        time.sleep(1)
    else:
        raise RuntimeError("Polaris did not start within 120 seconds")
    initialize(config)
    web = subprocess.Popen([sys.executable, "-m", "uvicorn", "server:app", "--host", "0.0.0.0",
                            "--port", os.environ.get("PORT", "8080"), "--no-access-log"])
    children.append(web)
    print("Catalog and dashboard started", flush=True)
    while not stopping:
        if any(p.poll() is not None for p in (java, web)):
            raise RuntimeError("A service exited; stopping the container for restart")
        time.sleep(0.5)


if __name__ == "__main__":
    try:
        main()
    finally:
        stop()
        for child in children:
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()

        for secret_file in secret_files:
            secret_file.close()
