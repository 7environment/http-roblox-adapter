from __future__ import annotations

import base64
import asyncio
import hmac
import ipaddress
import os
import socket
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


ENV_FILE = Path(__file__).resolve().with_name(".env")


def load_environment(path: str | Path = ENV_FILE) -> bool:
    """Load a dotenv file without replacing variables set by the process."""
    return load_dotenv(dotenv_path=path, override=False)


load_environment()
PROXY_TOKEN = os.getenv("PROXY_TOKEN", os.getenv("PROXY_SECRET", ""))
ALLOW_PRIVATE_TARGETS = os.getenv("ALLOW_PRIVATE_TARGETS", "false").lower() == "true"
MAX_TIMEOUT = float(os.getenv("MAX_TIMEOUT", "60"))
MAX_REDIRECTS = int(os.getenv("MAX_REDIRECTS", "5"))
MAX_REQUEST_BYTES = int(os.getenv("MAX_REQUEST_BYTES", str(2 * 1024 * 1024)))

ALLOWED_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
BLOCKED_HEADERS = {
    "connection",
    "content-length",
    "host",
    "proxy-authorization",
    "proxy-connection",
    "transfer-encoding",
    "upgrade",
}


class TargetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str
    method: str = "GET"
    headers: dict[str, str] = Field(default_factory=dict)
    body: str | None = None
    timeout: float = Field(default=30, gt=0)
    followRedirects: bool = False

    @field_validator("headers", mode="before")
    @classmethod
    def normalize_empty_luau_headers(cls, value: object) -> object:
        # Roblox HttpService:JSONEncode represents an empty Luau table as [].
        # Accept only that empty-list case; non-empty arrays remain invalid.
        if value == []:
            return {}
        return value

    @field_validator("method")
    @classmethod
    def validate_method(cls, value: str) -> str:
        method = value.upper()
        if method not in ALLOWED_METHODS:
            raise ValueError("unsupported HTTP method")
        return method


class ProxyPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = 1
    token: str | None = None
    request: TargetRequest


def proxy_response(
    *,
    success: bool,
    status_code: int = 0,
    status_message: str | None = None,
    headers: dict[str, str] | None = None,
    body: str = "",
    body_encoding: str = "text",
    error: str | None = None,
) -> JSONResponse:
    """Return the stable envelope. Its real HTTP status is always 200."""
    return JSONResponse(
        status_code=200,
        content={
            "success": success,
            "statusCode": status_code,
            "statusMessage": status_message,
            "headers": headers or {},
            "body": body,
            "bodyEncoding": body_encoding,
            "error": error,
        },
    )


def token_is_valid(payload_token: str | None, authorization: str | None) -> bool:
    if not PROXY_TOKEN:
        return True

    header_token = ""
    if authorization and authorization.startswith("Bearer "):
        header_token = authorization[7:]

    supplied = payload_token or header_token
    return bool(supplied) and hmac.compare_digest(supplied, PROXY_TOKEN)


async def validate_target_url(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("only absolute http:// and https:// URLs are allowed")
    if parsed.username or parsed.password:
        raise ValueError("credentials in target URLs are not allowed")
    if ALLOW_PRIVATE_TARGETS:
        return

    loop = asyncio.get_running_loop()
    addresses = await loop.getaddrinfo(
        parsed.hostname,
        parsed.port or (443 if parsed.scheme == "https" else 80),
        type=socket.SOCK_STREAM,
    )
    if not addresses:
        raise ValueError("target host could not be resolved")

    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if not ip.is_global:
            raise ValueError("private and non-global target addresses are blocked")


def clean_headers(headers: dict[str, str]) -> dict[str, str]:
    return {
        name: value
        for name, value in headers.items()
        if name.lower() not in BLOCKED_HEADERS
    }


def encode_response_body(content: bytes) -> tuple[str, str]:
    try:
        return content.decode("utf-8"), "text"
    except UnicodeDecodeError:
        return base64.b64encode(content).decode("ascii"), "base64"


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.http = httpx.AsyncClient(
        follow_redirects=False,
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
        trust_env=False,
    )
    try:
        yield
    finally:
        await app.state.http.aclose()


app = FastAPI(title="Roblox HTTP Proxy", lifespan=lifespan)


@app.exception_handler(Exception)
async def unhandled_exception_handler(_: Request, error: Exception) -> JSONResponse:
    return proxy_response(success=False, error=f"internal proxy error: {type(error).__name__}")


@app.get("/health")
async def health() -> dict[str, bool]:
    return {"ok": True}


async def send_target_request(client: httpx.AsyncClient, target: TargetRequest) -> httpx.Response:
    outgoing = client.build_request(
        method=target.method,
        url=target.url,
        headers=clean_headers(target.headers),
        content=target.body,
        timeout=min(target.timeout, MAX_TIMEOUT),
    )

    for redirect_number in range(MAX_REDIRECTS + 1):
        await validate_target_url(str(outgoing.url))
        response = await client.send(outgoing, follow_redirects=False)

        if not target.followRedirects or not response.has_redirect_location:
            return response
        if redirect_number == MAX_REDIRECTS:
            raise httpx.TooManyRedirects("maximum redirect count exceeded")

        outgoing = response.next_request
        if outgoing is None:
            return response

    raise httpx.TooManyRedirects("maximum redirect count exceeded")


@app.post("/proxy")
async def handle_proxy(request: Request) -> JSONResponse:
    try:
        raw_body = await request.body()
        if not raw_body:
            return proxy_response(success=False, status_code=400, error="empty payload")
        if len(raw_body) > MAX_REQUEST_BYTES:
            return proxy_response(success=False, status_code=413, error="payload is too large")

        try:
            payload = ProxyPayload.model_validate_json(raw_body)
        except ValidationError as error:
            return proxy_response(success=False, status_code=400, error=str(error))

        if payload.version != 1:
            return proxy_response(success=False, status_code=400, error="unsupported protocol version")
        if not token_is_valid(payload.token, request.headers.get("authorization")):
            return proxy_response(success=False, status_code=401, error="unauthorized")

        try:
            response = await send_target_request(request.app.state.http, payload.request)
        except httpx.TimeoutException:
            return proxy_response(success=False, status_code=408, error="target request timed out")
        except (httpx.RequestError, OSError, ValueError) as error:
            return proxy_response(success=False, error=str(error))

        body, body_encoding = encode_response_body(response.content)
        return proxy_response(
            success=response.is_success,
            status_code=response.status_code,
            status_message=response.reason_phrase,
            headers=dict(response.headers),
            body=body,
            body_encoding=body_encoding,
        )
    except Exception as error:
        # Keep executor HTTP implementations away from non-200 proxy responses.
        return proxy_response(success=False, error=f"internal proxy error: {type(error).__name__}")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=10998)
