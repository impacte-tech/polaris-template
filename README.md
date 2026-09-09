# Apache Polaris · Railway template

A small, self-hosted Apache Iceberg REST catalog powered by **Apache Polaris 1.7.0**, with PostgreSQL persistence and S3-compatible storage. Includes a password-protected dashboard, namespace/table browsing, connection instructions, and automatic warehouse initialization.

Built by impacte.tech. The packaging follows our [Hermes template](https://github.com/impacte-tech/hermes-agent-template): a lightweight Python front end around an upstream application. This is an independent template, not an Apache Software Foundation product.

## Deploy on Railway

Create a Railway project with **two services and a bucket**:

1. Add PostgreSQL.
2. Add a Railway Bucket (or use an existing S3-compatible bucket).
3. Add a service from [impacte-tech/polaris-template](https://github.com/impacte-tech/polaris-template). Railway builds the Dockerfile and uses `railway.toml` for the health check.
4. Configure the variables below and generate a public domain for the catalog service. The public domain supplies the dashboard's connection URL automatically.
5. Open the domain and sign in with `POLARIS_CLIENT_SECRET`.

No volume is needed on the catalog service. PostgreSQL holds catalog state; the bucket holds Iceberg metadata, manifests, and data files. Keep both when redeploying.

### Railway variables

These references assume your services are named `Postgres` and `Bucket`. Substitute their actual names in Railway.

| Variable | Value |
|---|---|
| `DATABASE_URL` | `${{Postgres.DATABASE_URL}}` |
| `POLARIS_CLIENT_ID` | `root` |
| `POLARIS_CLIENT_SECRET` | Generate a unique random secret, at least 32 characters |
| `TOKEN_SIGNING_KEY` | Generate a second unique random secret, at least 32 characters |
| `CATALOG_NAME` | `warehouse` |
| `S3_BUCKET` | `${{Bucket.BUCKET}}` |
| `S3_ENDPOINT` | `${{Bucket.ENDPOINT}}` |
| `AWS_REGION` | `${{Bucket.REGION}}` |
| `AWS_ACCESS_KEY_ID` | `${{Bucket.ACCESS_KEY_ID}}` |
| `AWS_SECRET_ACCESS_KEY` | `${{Bucket.SECRET_ACCESS_KEY}}` |
| `S3_PATH_STYLE_ACCESS` | `false` for current Railway buckets; follow the bucket Credentials tab |
| `PUBLIC_URL` | Optional: `https://your-custom-domain` |

Railway supplies `PORT` and, after creating a Railway domain, `RAILWAY_PUBLIC_DOMAIN`. For a custom domain, set `PUBLIC_URL` explicitly. Generate secrets with:

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

Run this twice. Keep these values across redeploys. Do not use the local example secrets on Railway.

The bucket must already exist. The warehouse uses `s3://<bucket>/<CATALOG_NAME>/`. This template sets `stsUnavailable: true`: Polaris and each client use direct S3 credentials. Temporary credential vending is not enabled. PyIceberg examples explicitly set `header.X-Iceberg-Access-Delegation` to an empty string to disable its default delegation request. You can also use AWS S3 with its regional endpoint and appropriate direct credentials.

### Publish as a reusable Railway template

After verifying the deployed project, use Railway's **Create Template** flow to capture the catalog service, PostgreSQL, bucket, and variable references. Configure the two secrets as generated values in the template editor and exclude any real credentials. Publish it, then add the generated Deploy on Railway button here. `railway.toml` configures the application service; it does not provision PostgreSQL or a bucket by itself.

## Local development

Docker with Compose is required. Local S3 uses **Moto**, an in-memory S3 emulator for testing only.

```bash
cp .env.example .env
docker compose up -d --build --wait --wait-timeout 300
```

Open [localhost:8080](http://localhost:8080). If port 8080 is occupied, set `HOST_PORT=18080` and `PUBLIC_URL=http://localhost:18080` in `.env`. The password is the `POLARIS_CLIENT_SECRET` from `.env`. The local port is bound to loopback.

Run an actual Iceberg create/write/read test:

```bash
docker compose --profile test run --build --rm smoke
docker compose restart catalog
docker compose up -d --wait --wait-timeout 300
docker compose --profile test run --rm smoke
```

The second run checks that the table and rows survived a catalog restart. Moto storage lasts only for the lifetime of its process: restarting/recreating **storage** discards test objects, while PostgreSQL still contains their catalog entries. For a clean test environment, remove this Compose project's volumes with `docker compose down -v`, then start again. This deletes the local test database.

Stop without deleting the PostgreSQL volume:

```bash
docker compose down
```

Python checks:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
```

## Connect an Iceberg client

The dashboard includes a PyIceberg example. The general settings are:

| Setting | Value |
|---|---|
| REST URI | `https://<your-domain>/api/catalog` |
| OAuth token endpoint | `https://<your-domain>/api/catalog/v1/oauth/tokens` |
| Warehouse | `warehouse` (or your `CATALOG_NAME`) |
| Credential | `<client-id>:<client-secret>` |
| Scope | `PRINCIPAL_ROLE:ALL` |
| S3 credentials | Configure directly in the client |

[`examples/smoke.py`](examples/smoke.py) is a runnable PyIceberg example. From outside Compose, use a bucket endpoint reachable by your client; `http://storage:5000` resolves only inside the local Docker network.

Spark uses the same REST URI, warehouse, credential, and scope with `spark.sql.catalog.<name>.type=rest`; configure its S3 endpoint, region, access key, secret key, and path-style setting separately. For Trino, use its Iceberg REST catalog connector with OAuth2, and **disable vended credentials** for this template's direct-key storage mode.

The bootstrap principal is an administrator. Use it to set up the catalog; provision dedicated principals and roles through the [Polaris management API](https://polaris.apache.org/releases/1.7.0/managing-security/access-control/) for applications. The dashboard logs in as the bootstrap principal. It is not a multi-user administration console.

## How it works

```text
Railway HTTPS / $PORT
        │
Python (Starlette + Uvicorn)
        ├── /                 dashboard and namespace/table browser
        ├── /login            native Polaris OAuth login, signed session cookie
        ├── /health           upstream readiness check (no credentials exposed)
        └── /api/*            streaming proxy; Polaris validates API credentials
                 │
         Polaris on 127.0.0.1:8181
                 ├── PostgreSQL: catalog and authorization state
                 └── S3: Iceberg metadata and table files
```

`launcher.py` validates configuration, waits for PostgreSQL, runs the official admin bootstrap tool, starts Polaris, and creates the warehouse if absent. Repeated bootstrap preserves existing credentials. Existing warehouse settings are checked against the environment; mismatches fail startup instead of silently moving data. The launcher supervises both processes and exits if either dies so Railway can restart the service. Polaris's metrics/health port stays on loopback.

The Python dashboard uses native OAuth access tokens in signed HttpOnly cookies, CSRF protection on login/logout, and a bounded login rate limit. Catalog API requests use native Polaris OAuth independently of dashboard sessions.

## Operations

- **Credentials:** Changing `POLARIS_CLIENT_SECRET` does not rotate credentials in an existing database. Rotate/reset them through Polaris's management API and update the Railway variable to match. Changing `TOKEN_SIGNING_KEY` invalidates existing tokens and dashboard sessions.
- **Backups:** Back up PostgreSQL and object storage. One without the other is insufficient to restore your tables.
- **Upgrades:** Both Polaris images are pinned to 1.7.0. Test upgrades with a database backup and run any schema migrations required by the release; bootstrap is not an automatic upgrade system.
- **Resources:** Start with room for a Java service plus PostgreSQL. Benchmark your workload before setting tight memory limits. The server JVM can use up to 65% of container memory for its heap.
- **Scope:** This is a catalog, not a SQL query engine. Spark, Trino, or Python run externally. Table compaction and snapshot cleanup are separate maintenance tasks.
- **Storage:** Local automated tests use simulated S3. Validate against your actual Railway bucket before publishing the template.

## References

- [Apache Polaris](https://polaris.apache.org/)
- [Polaris 1.7.0](https://polaris.apache.org/releases/1.7.0/)
- [Railway Buckets](https://docs.railway.com/storage-buckets)
- [Creating Railway templates](https://docs.railway.com/templates/create)

Template code is Apache-2.0 licensed. Upstream Polaris licenses and notices are retained in `/opt/polaris` and `/opt/polaris-admin` inside the image.
