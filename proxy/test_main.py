import os
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

os.environ["PROXY_TOKEN"] = "test-token"
os.environ["ALLOW_PRIVATE_TARGETS"] = "true"

import main


def upstream(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/created":
        return httpx.Response(201, text="created", headers={"x-test": "yes"})
    if request.url.path == "/inspect":
        return httpx.Response(
            200,
            json={
                "method": request.method,
                "authorization": request.headers.get("authorization"),
                "body": request.content.decode(),
            },
        )
    return httpx.Response(404, text="missing")


class ProxyTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)
        self.client.__enter__()

        original_client = main.app.state.http
        self.client.portal.call(original_client.aclose)
        main.app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(upstream))

    def tearDown(self):
        self.client.__exit__(None, None, None)

    def proxy(self, path: str, token: str = "test-token") -> httpx.Response:
        return self.client.post(
            "/proxy",
            json={
                "version": 1,
                "token": token,
                "request": {
                    "url": f"https://target.test{path}",
                    "method": "GET",
                },
            },
        )

    def test_dotenv_is_loaded_from_explicit_path(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text(
                "PROXY_TOKEN=from-dotenv\nALLOW_PRIVATE_TARGETS=true\n",
                encoding="utf-8",
            )

            with patch.dict(os.environ, {}, clear=True):
                loaded = main.load_environment(env_file)
                self.assertTrue(loaded)
                self.assertEqual(os.environ["PROXY_TOKEN"], "from-dotenv")
                self.assertEqual(os.environ["ALLOW_PRIVATE_TARGETS"], "true")

    def test_process_environment_has_priority_over_dotenv(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text("PROXY_TOKEN=from-dotenv\n", encoding="utf-8")

            with patch.dict(os.environ, {"PROXY_TOKEN": "from-process"}, clear=True):
                main.load_environment(env_file)
                self.assertEqual(os.environ["PROXY_TOKEN"], "from-process")

    def test_successful_upstream_response(self):
        response = self.proxy("/created")
        payload = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["success"])
        self.assertEqual(payload["statusCode"], 201)
        self.assertEqual(payload["body"], "created")

    def test_upstream_error_status_is_wrapped_in_http_200(self):
        response = self.proxy("/missing")
        payload = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertFalse(payload["success"])
        self.assertEqual(payload["statusCode"], 404)
        self.assertEqual(payload["body"], "missing")

    def test_method_headers_and_body_are_forwarded(self):
        response = self.client.post(
            "/proxy",
            json={
                "version": 1,
                "token": "test-token",
                "request": {
                    "url": "https://target.test/inspect",
                    "method": "PATCH",
                    "headers": {"Authorization": "Bearer target-token"},
                    "body": "payload",
                },
            },
        )
        upstream_payload = json.loads(response.json()["body"])

        self.assertEqual(response.status_code, 200)
        self.assertEqual(upstream_payload["method"], "PATCH")
        self.assertEqual(upstream_payload["authorization"], "Bearer target-token")
        self.assertEqual(upstream_payload["body"], "payload")

    def test_empty_luau_headers_array_is_accepted(self):
        response = self.client.post(
            "/proxy",
            json={
                "version": 1,
                "token": "test-token",
                "request": {
                    "url": "https://target.test/created",
                    "method": "GET",
                    "headers": [],
                },
            },
        )

        payload = response.json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["statusCode"], 201)

    def test_invalid_token_is_wrapped_in_http_200(self):
        response = self.proxy("/created", token="wrong")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["statusCode"], 401)

    def test_invalid_payload_is_wrapped_in_http_200(self):
        response = self.client.post("/proxy", content=b"not-json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["statusCode"], 400)


if __name__ == "__main__":
    unittest.main()
