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
from utils import verify_access_key, check_rate_limit, UnexpectedFinishReasonError
from request_logger import RequestLogger

# Create router
router = APIRouter()

# Initialize request logger
request_logger = RequestLogger(log_file="requests.log", max_entries=100)

# Initialize key manager
key_manager = KeyManager(
    keys=config["openrouter"]["keys"],
    cooldown_seconds=config["openrouter"]["rate_limit_cooldown"],
    strategy=config["openrouter"]["key_selection_strategy"],
    opts=config["openrouter"]["key_selection_opts"],
)


MAX_PROXY_RETRIES = 3


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
    is_stream: bool = False,
):
    # too big or small for error
  
    has_rate_limit_error, reset_time_ms = await check_rate_limit(
        body,
        request_body=request_body,
        response_status=response_status,
        is_stream=is_stream,
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


async def _proxy_with_httpx_once(
    request: Request,
    *,
    path: str,
    api_key: str,
    is_stream: bool,
    free_only: bool,
    request_body_bytes: bytes,
) -> Response:
    """Execute a single attempt forwarding the request to OpenRouter."""

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
    openrouter_resp: Optional[httpx.Response] = None
    should_close = True
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
        headers.pop("content-encoding", None)
        headers.pop("Content-Encoding", None)

        if not is_stream:
            body = openrouter_resp.content

            # Log request and response before check_httpx_err
            await request_logger.log_request(
                request_body=request_body_bytes,
                response_body=body,
                status_code=openrouter_resp.status_code,
                path=path,
            )

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
            last_finish_json = ""
            full_response = []
            try:
                async for line in openrouter_resp.aiter_lines():
                    if line.startswith("data: {"):
                        last_json = line[6:]
                        full_response.append(last_json)
                        # Track the last chunk with a valid finish_reason
                        try:
                            data = json.loads(last_json)
                            if data.get("choices", [{}])[0].get("finish_reason"):
                                last_finish_json = last_json
                        except:
                            pass
                    yield f"{line}\n\n".encode("utf-8")
            except Exception as err:
                logger.error("sse_stream error: %s", err)
            finally:
                await openrouter_resp.aclose()

            # Log streaming request and response before check_httpx_err
            await request_logger.log_request(
                request_body=request_body_bytes,
                response_body="\n".join(full_response),
                status_code=openrouter_resp.status_code,
                path=path,
            )

            # Use the chunk with finish_reason if available, otherwise use last chunk
            check_json = last_finish_json or last_json
            await check_httpx_err(
                check_json,
                api_key,
                request_body=request_body_bytes,
                response_status=openrouter_resp.status_code,
                is_stream=True,
            )

        should_close = False
        return StreamingResponse(
            sse_stream(),
            status_code=openrouter_resp.status_code,
            media_type="text/event-stream",
            headers=headers,
        )
    except UnexpectedFinishReasonError:
        raise
    except httpx.HTTPStatusError as e:
        try:
            await check_httpx_err(
                e.response.content,
                api_key,
                request_body=request_body_bytes,
                response_status=e.response.status_code,
            )
        except UnexpectedFinishReasonError:
            raise
        # Log error response
        await request_logger.log_request(
            request_body=request_body_bytes,
            response_body=e.response.content,
            status_code=e.response.status_code,
            path=path,
        )
        logger.error("Request error: %s", str(e))
        raise HTTPException(e.response.status_code, str(e.response.content)) from e
    except httpx.ConnectError as e:
        # Log connection error
        await request_logger.log_request(
            request_body=request_body_bytes,
            response_body=f"Connection error: {str(e)}",
            status_code=503,
            path=path,
        )
        logger.error("Connection error to OpenRouter: %s", str(e))
        raise HTTPException(503, "Unable to connect to OpenRouter API") from e
    except httpx.TimeoutException as e:
        # Log timeout error
        await request_logger.log_request(
            request_body=request_body_bytes,
            response_body=f"Timeout: {str(e)}",
            status_code=504,
            path=path,
        )
        logger.error("Timeout connecting to OpenRouter: %s", str(e))
        raise HTTPException(504, "OpenRouter API request timed out") from e
    except Exception as e:
        # Log internal error
        await request_logger.log_request(
            request_body=request_body_bytes,
            response_body=f"Internal error: {str(e)}",
            status_code=500,
            path=path,
        )
        logger.error("Internal error: %s", str(e))
        raise HTTPException(status_code=500, detail="Internal Proxy Error") from e
    finally:
        if should_close and openrouter_resp is not None:
            await openrouter_resp.aclose()


async def proxy_with_httpx(
    request: Request,
    path: str,
    api_key: str,
    is_stream: bool,
) -> Response:
    """Proxy request with retries when forwarding to OpenRouter fails."""

    free_only = (any(f"/api/v1{path}" == ep for ep in MODELS_ENDPOINTS) and
                 config["openrouter"]["free_only"])
    request_body_bytes = await request.body()

    total_attempts = MAX_PROXY_RETRIES + 1
    current_api_key = api_key
    rotate_keys = bool(api_key)
    last_exception: Optional[Exception] = None
    attempts_made = 0

    for attempt in range(total_attempts):
        attempt_number = attempt + 1
        attempts_made = attempt_number
        if attempt > 0:
            if rotate_keys:
                previous_key = current_api_key
                try:
                    current_api_key = await key_manager.get_next_key()
                    logger.info(
                        "Retrying request (%s/%s) with API key %s (previous %s)",
                        attempt_number,
                        total_attempts,
                        mask_key(current_api_key),
                        mask_key(previous_key),
                    )
                except HTTPException as err:
                    last_exception = err
                    logger.error(
                        "Unable to acquire replacement API key for retry (%s/%s): %s",
                        attempt_number,
                        total_attempts,
                        err.detail,
                    )
                    break
            else:
                logger.info(
                    "Retrying request (%s/%s) without API key rotation",
                    attempt_number,
                    total_attempts,
                )

        try:
            return await _proxy_with_httpx_once(
                request,
                path=path,
                api_key=current_api_key,
                is_stream=is_stream,
                free_only=free_only,
                request_body_bytes=request_body_bytes,
            )
        except UnexpectedFinishReasonError as err:
            last_exception = err
            logger.warning(
                "Attempt %s/%s failed due to unexpected finish_reason: %s",
                attempt_number,
                total_attempts,
                err,
            )
        except HTTPException as err:
            last_exception = err
            logger.warning(
                "Attempt %s/%s failed with HTTPException %s: %s",
                attempt_number,
                total_attempts,
                err.status_code,
                err.detail,
            )
        except Exception as err:
            last_exception = err
            logger.warning(
                "Attempt %s/%s failed with error: %s",
                attempt_number,
                total_attempts,
                err,
            )

    if last_exception is None:
        raise HTTPException(status_code=500, detail="Internal Proxy Error")

    logger.error(
        "Proxy request to %s failed after %s attempt(s) (max %s).",
        path,
        attempts_made,
        total_attempts,
    )

    if isinstance(last_exception, UnexpectedFinishReasonError):
        raise HTTPException(
            status_code=502,
            detail="Unexpected finish reason from upstream response",
        ) from last_exception
    if isinstance(last_exception, HTTPException):
        raise last_exception
    raise HTTPException(status_code=500, detail="Internal Proxy Error") from last_exception


@router.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "ok"}
