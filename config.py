"""The template has one realm, one warehouse, and environment-based configuration."""
import os
import re
from urllib.parse import urlsplit, unquote


def required(name):
    value = os.environ.get(name, "")
    if not value:
        raise ValueError(f"Set {name} before starting the catalog")
    return value


def settings():
    database = required("DATABASE_URL")
    parsed = urlsplit(database)
    if parsed.scheme not in ("postgres", "postgresql") or not parsed.hostname or not parsed.path.strip("/"):
        raise ValueError("DATABASE_URL must be a PostgreSQL connection URL")
    secret = required("POLARIS_CLIENT_SECRET")
    signing = required("TOKEN_SIGNING_KEY")
    if len(secret) < 32 or len(signing) < 32:
        raise ValueError("POLARIS_CLIENT_SECRET and TOKEN_SIGNING_KEY must have at least 32 characters")
    client = os.environ.get("POLARIS_CLIENT_ID", "root")
    if any(c in client + secret for c in ",\r\n"):
        raise ValueError("Polaris bootstrap credentials cannot contain commas or newlines")
    name = os.environ.get("CATALOG_NAME", "warehouse")
    if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]{0,62}", name):
        raise ValueError("CATALOG_NAME must start with a letter and contain only letters, digits, _ or -")
    bucket = required("S3_BUCKET")
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket):
        raise ValueError("S3_BUCKET must be the actual S3 bucket name")
    required("AWS_ACCESS_KEY_ID")
    required("AWS_SECRET_ACCESS_KEY")
    endpoint = required("S3_ENDPOINT")
    if urlsplit(endpoint).scheme not in ("https", "http") or not urlsplit(endpoint).hostname:
        raise ValueError("S3_ENDPOINT must be an HTTP(S) URL")
    style = os.environ.get("S3_PATH_STYLE_ACCESS", "false").lower()
    if style not in ("true", "false"):
        raise ValueError("S3_PATH_STYLE_ACCESS must be true or false")
    public = os.environ.get("PUBLIC_URL") or ("https://" + os.environ["RAILWAY_PUBLIC_DOMAIN"] if os.environ.get("RAILWAY_PUBLIC_DOMAIN") else "http://localhost:8080")
    return dict(database=database, client=client, secret=secret, signing=signing,
                name=name, bucket=bucket, endpoint=endpoint,
                region=os.environ.get("AWS_REGION", "auto"), path_style=style == "true",
                public=public.rstrip("/"))


def java_environment(config):
    env = os.environ.copy()
    db = urlsplit(config["database"])
    host = f"[{db.hostname}]" if ":" in db.hostname else db.hostname
    env.update({
        "POLARIS_PERSISTENCE_TYPE": "relational-jdbc",
        "POLARIS_PERSISTENCE_RELATIONAL_JDBC_DATABASE_TYPE": "postgresql",
        "QUARKUS_DATASOURCE_JDBC_URL": f"jdbc:postgresql://{host}:{db.port or 5432}{db.path}" + (f"?{db.query}" if db.query else ""),
        "QUARKUS_DATASOURCE_USERNAME": unquote(db.username or ""),
        "QUARKUS_DATASOURCE_PASSWORD": unquote(db.password or ""),
        "POLARIS_REALM_CONTEXT_REALMS": "POLARIS",
        "POLARIS_REALM_CONTEXT_REQUIRE_HEADER": "false",
        "QUARKUS_HTTP_HOST": "127.0.0.1",
        "QUARKUS_HTTP_PORT": "8181",
        "QUARKUS_MANAGEMENT_HOST": "127.0.0.1",
        "QUARKUS_MANAGEMENT_PORT": "8182",
        "QUARKUS_OTEL_SDK_DISABLED": "true",
        "QUARKUS_LOG_FILE_ENABLED": "false",
        "POLARIS_AUTHENTICATION_TOKEN_BROKER_TYPE": "symmetric-key",
        "POLARIS_AUTHENTICATION_TOKEN_BROKER_SYMMETRIC_KEY_SECRET": config["signing"],
        "AWS_REGION": config["region"],
    })
    return env


def catalog_payload(config):
    location = f"s3://{config['bucket']}/{config['name']}/"
    return {"catalog": {"name": config["name"], "type": "INTERNAL",
            "properties": {"default-base-location": location},
            "storageConfigInfo": {"storageType": "S3", "allowedLocations": [location],
                "endpoint": config["endpoint"], "region": config["region"],
                "pathStyleAccess": config["path_style"], "stsUnavailable": True}}}
