"""Create, append, read, and verify durable Iceberg state. Safe to rerun."""
import os
import httpx
import pyarrow as pa
from pyiceberg.catalog import load_catalog

uri = os.environ.get("CATALOG_URI", "http://localhost:8080/api/catalog")
name = os.environ.get("CATALOG_NAME", "warehouse")
base = uri.removesuffix("/api/catalog")
assert httpx.get(base + "/health").status_code == 200
assert httpx.get(uri + "/v1/config", params={"warehouse": name}).status_code == 401
assert httpx.get(base + "/api/management/v1/catalogs").status_code == 401
catalog = load_catalog(name, type="rest", uri=uri, warehouse=name,
    credential=os.environ.get("POLARIS_CLIENT_ID", "root") + ":" + os.environ["POLARIS_CLIENT_SECRET"],
    scope="PRINCIPAL_ROLE:ALL", **{
        "header.X-Iceberg-Access-Delegation": "",
        "oauth2-server-uri": uri + "/v1/oauth/tokens",
        "s3.endpoint": os.environ["S3_ENDPOINT"], "s3.region": os.environ["AWS_REGION"],
        "s3.access-key-id": os.environ["AWS_ACCESS_KEY_ID"],
        "s3.secret-access-key": os.environ["AWS_SECRET_ACCESS_KEY"],
        "s3.force-virtual-addressing": "false" if os.environ.get("S3_PATH_STYLE_ACCESS", "false") == "true" else "true",
    })
catalog.create_namespace_if_not_exists("template_test")
identifier = "template_test.roundtrip"
data = pa.table({"id": pa.array([1, 2], type=pa.int64()), "message": ["hello", "polaris"]})
if catalog.table_exists(identifier):
    table = catalog.load_table(identifier)
    assert table.scan().to_arrow().sort_by("id").equals(data)
    print("PASS: existing table and rows survived restart")
else:
    table = catalog.create_table(identifier, schema=data.schema)
    table.append(data)
    assert catalog.load_table(identifier).scan().to_arrow().equals(data)
    print("PASS: namespace, table creation, append, and S3 read")
print("PASS: unauthenticated requests rejected")
