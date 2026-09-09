import os
import unittest
from unittest.mock import patch

ENV = {
    'DATABASE_URL': 'postgresql://user:p%40ss@db:5432/catalog?sslmode=require',
    'POLARIS_CLIENT_SECRET': 'x' * 40, 'TOKEN_SIGNING_KEY': 'y' * 40,
    'S3_BUCKET': 'warehouse', 'S3_ENDPOINT': 'https://s3.example.com',
    'AWS_ACCESS_KEY_ID': 'test', 'AWS_SECRET_ACCESS_KEY': 'test',
    'PUBLIC_URL': 'http://testserver',
}
os.environ.update(ENV)
import httpx
from starlette.testclient import TestClient
from config import settings, java_environment
from server import app


class ConfigurationTests(unittest.TestCase):
    def test_database_password_is_decoded_without_leaking_into_jdbc_url(self):
        env = java_environment(settings())
        self.assertEqual(env['QUARKUS_DATASOURCE_PASSWORD'], 'p@ss')
        self.assertEqual(env['QUARKUS_DATASOURCE_JDBC_URL'], 'jdbc:postgresql://db:5432/catalog?sslmode=require')

    def test_missing_secret_fails_closed(self):
        with patch.dict(os.environ, {'TOKEN_SIGNING_KEY': ''}):
            with self.assertRaises(ValueError):
                settings()

    def test_invalid_bucket_and_path_style_rejected(self):
        for values in ({'S3_BUCKET': '../other'}, {'S3_PATH_STYLE_ACCESS': 'maybe'}):
            with patch.dict(os.environ, values):
                with self.assertRaises(ValueError):
                    settings()


class DashboardTests(unittest.TestCase):
    def test_dashboard_requires_login(self):
        with TestClient(app) as client:
            self.assertEqual(client.get('/', follow_redirects=False).status_code, 303)
            self.assertEqual(client.post('/login', data={'password': 'anything'}).status_code, 403)
            self.assertEqual(client.post('/logout').status_code, 403)

    def test_proxy_preserves_iceberg_auth_and_escaped_namespaces(self):
        seen = []
        def upstream(request):
            seen.append(request)
            return httpx.Response(401, stream=httpx.ByteStream(b'{"error":{"code":401}}'), headers={'content-type': 'application/json'})
        with TestClient(app) as client:
            original = app.state.client
            app.state.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
            try:
                result = client.get('/api/catalog/v1/warehouse/namespaces/a%1Fb/tables?limit=3',
                    headers={'Authorization': 'Bearer example', 'Cookie': 'private-cookie=secret'})
                self.assertEqual(result.status_code, 401)
                self.assertEqual(seen[0].headers['authorization'], 'Bearer example')
                self.assertNotIn('cookie', seen[0].headers)
                self.assertIn(b'a%1Fb', seen[0].url.raw_path)
                self.assertEqual(seen[0].url.query, b'limit=3')
            finally:
                app.state.client = original

    def test_health_reports_upstream_failure(self):
        def upstream(request):
            return httpx.Response(503)
        with TestClient(app) as client:
            original = app.state.client
            app.state.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
            try:
                self.assertEqual(client.get('/health').status_code, 503)
            finally:
                app.state.client = original


class BootstrapTests(unittest.TestCase):
    def test_existing_catalog_is_preserved_and_storage_mismatch_rejected(self):
        from config import catalog_payload
        from launcher import initialize
        config = settings()
        existing = catalog_payload(config)["catalog"]
        writes = []
        def upstream(request):
            if request.url.path.endswith("oauth/tokens"):
                return httpx.Response(200, json={"access_token": "test"})
            if request.method != "GET":
                writes.append(request)
            return httpx.Response(200, json=existing)
        client = httpx.Client(base_url="http://127.0.0.1:8181", transport=httpx.MockTransport(upstream))
        with patch('launcher.httpx.Client', return_value=client):
            initialize(config)
        self.assertEqual(writes, [])
        existing["properties"]["default-base-location"] = "s3://another-bucket/"
        client = httpx.Client(base_url="http://127.0.0.1:8181", transport=httpx.MockTransport(upstream))
        with patch('launcher.httpx.Client', return_value=client):
            with self.assertRaisesRegex(RuntimeError, "differs"):
                initialize(config)
        self.assertEqual(writes, [])


if __name__ == '__main__':
    unittest.main()
