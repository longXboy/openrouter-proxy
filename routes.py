#!/usr/bin/env python3
"""
API routes for OpenRouter API Proxy.
"""

import json
from contextlib import asynccontextmanager
from typing import Optional

import httpx
from fastapi import APIRouter, Request, Header, HTTPException, FastAPI
from fastapi.responses import StreamingResponse, Response

from config import config, logger
from constants import MODELS_ENDPOINTS
from key_manager import KeyManager, mask_key
from utils import verify_access_key, check_rate_limit

# Create router
router = APIRouter()

# Initialize key manager
key_manager = KeyManager(
    keys=config["openrouter"]["keys"],
    cooldown_seconds=config["openrouter"]["rate_limit_cooldown"],
    strategy=config["openrouter"]["key_selection_strategy"],
    opts=config["openrouter"]["key_selection_opts"],
)


@asynccontextmanager
async def lifespan(app_: FastAPI):
    client_kwargs = {"timeout": 600.0}  # Increase default timeout
    # Add proxy configuration if enabled
    if config["requestProxy"]["enabled"]:
        proxy_url = config["requestProxy"]["url"]
        client_kwargs["proxy"] = proxy_url
        logger.info("Using proxy for httpx client: %s", proxy_url)
    app_.state.http_client = httpx.AsyncClient(**client_kwargs)
    yield
    await app_.state.http_client.aclose()


async def get_async_client(request: Request) -> httpx.AsyncClient:
    return request.app.state.http_client


async def check_httpx_err(
    body: str | bytes,
    api_key: Optional[str],
    *,
    request_body: str | bytes | None = None,
    response_status: Optional[int] = None,
):
    # too big or small for error
    if 10 > len(body) > 4000 or not api_key:
        return
    has_rate_limit_error, reset_time_ms = await check_rate_limit(
        body,
        request_body=request_body,
        response_status=response_status,
    )
    if has_rate_limit_error:
        await key_manager.disable_key(api_key, reset_time_ms)


def remove_paid_models(body: bytes) -> bytes:
    # {'prompt': '0', 'completion': '0', 'request': '0', 'image': '0', 'web_search': '0', 'internal_reasoning': '0'}
    prices = ['prompt', 'completion', 'request', 'image', 'web_search', 'internal_reasoning']
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning("Error models deserialize: %s", str(e))
    else:
        if isinstance(data.get("data"), list):
            clear_data = []
            for model in data["data"]:
                if all(model.get("pricing", {}).get(k, "1") == "0" for k in prices):
                    clear_data.append(model)
            if clear_data:
                data["data"] = clear_data
                body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    return body


def prepare_forward_headers(request: Request) -> dict:
    return {
        k: v
        for k, v in request.headers.items()
        if k.lower()
           not in ["host", "content-length", "connection", "authorization"]
    }


@router.api_route("/api/v1{path:path}", methods=["GET", "POST"])
async def proxy_endpoint(
    request: Request, path: str, authorization: Optional[str] = Header(None)
):
    """Main proxy endpoint for handling all requests to OpenRouter API."""
    is_public = any(f"/api/v1{path}".startswith(ep) for ep in config["openrouter"]["public_endpoints"])

    # Verify authorization for non-public endpoints
    if not is_public:
        await verify_access_key(authorization=authorization)

    # Log the full request URL including query parameters
    full_url = str(request.url).replace(str(request.base_url), "/")

    # Get API key to use
    api_key = "" if is_public else await key_manager.get_next_key()

    logger.info("Proxying request to %s (Public: %s, key: %s)", full_url, is_public, mask_key(api_key))

    is_stream = False
    if request.method == "POST":
        try:
            if body_bytes := await request.body():
                request_body = json.loads(body_bytes)
                if is_stream := request_body.get("stream", False):
                    logger.info("Detected streaming request")
                if model := request_body.get("model"):
                    logger.info("Using model: %s", model)
        except Exception as e:
            logger.debug("Could not parse request body: %s", str(e))

    return await proxy_with_httpx(request, path, api_key, is_stream)


async def proxy_with_httpx(
    request: Request,
    path: str,
    api_key: str,
    is_stream: bool,
) -> Response:
    """Core logic to proxy requests."""
    free_only = (any(f"/api/v1{path}" == ep for ep in MODELS_ENDPOINTS) and
                 config["openrouter"]["free_only"])
    request_body_bytes = await request.body()
    req_kwargs = {
        "method": request.method,
        "url": f"{config['openrouter']['base_url']}{path}",
        "headers": prepare_forward_headers(request),
        "content": request_body_bytes,
        "params": request.query_params,
    }
    if api_key:
        req_kwargs["headers"]["Authorization"] = f"Bearer {api_key}"

    client = await get_async_client(request)
    try:
        openrouter_req = client.build_request(**req_kwargs)
        openrouter_resp = await client.send(openrouter_req, stream=is_stream)

        if openrouter_resp.status_code >= 400:
            if is_stream:
                try:
                    await openrouter_resp.aread()
                except Exception as e:
                    await openrouter_resp.aclose()
                    raise e
            openrouter_resp.raise_for_status()

        headers = dict(openrouter_resp.headers)
        # Content has already been decoded
        headers.pop("content-encoding", None)
        headers.pop("Content-Encoding", None)

        if not is_stream:
            body = openrouter_resp.content
            await check_httpx_err(
                body,
                api_key,
                request_body=request_body_bytes,
                response_status=openrouter_resp.status_code,
            )
            if free_only:
                body = remove_paid_models(body)
            return Response(
                content=body,
                status_code=openrouter_resp.status_code,
                media_type="application/json",
                headers=headers,
            )

        async def sse_stream():
            last_json = ""
            try:
                async for line in openrouter_resp.aiter_lines():
                    if line.startswith("data: {"): # get json only
                        last_json = line[6:]
                    yield f"{line}\n\n".encode("utf-8")
            except Exception as err:
                logger.error("sse_stream error: %s", err)
            finally:
                await openrouter_resp.aclose()
            await check_httpx_err(
                last_json,
                api_key,
                request_body=request_body_bytes,
                response_status=openrouter_resp.status_code,
            )


        return StreamingResponse(
            sse_stream(),
            status_code=openrouter_resp.status_code,
            media_type="text/event-stream",
            headers=headers,
        )
    except httpx.HTTPStatusError as e:
        await check_httpx_err(
            e.response.content,
            api_key,
            request_body=request_body_bytes,
            response_status=e.response.status_code,
        )
        logger.error("Request error: %s", str(e))
        raise HTTPException(e.response.status_code, str(e.response.content)) from e
    except httpx.ConnectError as e:
        logger.error("Connection error to OpenRouter: %s", str(e))
        raise HTTPException(503, "Unable to connect to OpenRouter API") from e
    except httpx.TimeoutException as e:
        logger.error("Timeout connecting to OpenRouter: %s", str(e))
        raise HTTPException(504, "OpenRouter API request timed out") from e
    except Exception as e:
        logger.error("Internal error: %s", str(e))
        raise HTTPException(status_code=500, detail="Internal Proxy Error") from e


@router.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "ok"}
