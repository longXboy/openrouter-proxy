#!/usr/bin/env python3
"""
Utility functions for OpenRouter API Proxy.
"""

import asyncio
import json
import socket
from typing import Optional, Tuple

from fastapi import Header, HTTPException

from config import config, logger
from constants import RATE_LIMIT_ERROR_CODE, GOOGLE_LIMIT_ERROR, GLOBAL_LIMIT_ERROR, GLOBAL_LIMIT_PATTERN


def _format_for_log(value: str | bytes | None, limit: int = 1000) -> str:
    if value is None:
        return "None"
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            value = value.decode("utf-8", "ignore")
    text = str(value)
    if len(text) > limit:
        return f"{text[:limit]}... (truncated)"
    return text


def get_local_ip() -> str:
    """Get local IP address for displaying in logs."""
    try:
        # Create a socket that connects to a public address
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # No actual connection is made
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
        return local_ip
    except Exception:
        return "localhost"


async def verify_access_key(
    authorization: Optional[str] = Header(None),
) -> bool:
    """
    Verify the local access key for authentication.

    Args:
        authorization: Authorization header

    Returns:
        True if authentication is successful

    Raises:
        HTTPException: If authentication fails
    """

    if not authorization:
        raise HTTPException(status_code=401, detail="Authorization header missing")

    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="Invalid authentication scheme")

    if token != config["server"]["access_key"]:
        raise HTTPException(status_code=401, detail="Invalid access key")

    return True


def check_global_limit(data: str) -> Optional[str]:
    """
    Checks for a global rate limit error message from OpenRouter.

    Example message:
    "google/gemini-2.0-flash-exp:free is temporarily rate-limited upstream.
    Please retry shortly, or add your own key to accumulate your rate limits:
    https://openrouter.ai/settings/integrations"
    """
    if isinstance(data, str) and GLOBAL_LIMIT_PATTERN in data:
        logger.warning("Model %s is overloaded.", data.split(' ', 1)[0])
        return GLOBAL_LIMIT_ERROR
    return None


def check_google_error(data: str) -> Optional[str]:
    # data = {
    #     'error': {
    #         'code': 429,
    #         'message': 'You exceeded your current quota, please check your plan and billing details.',
    #         'status': 'RESOURCE_EXHAUSTED',
    #         'details': [
    #             {'@type': 'type.googleapis.com/google.rpc.QuotaFailure', 'violations': [
    #                 {'quotaMetric': 'generativelanguage.googleapis.com/generate_content_paid_tier_input_token_count',
    #                  'quotaId': 'GenerateContentPaidTierInputTokensPerModelPerMinute',
    #                  'quotaDimensions': {'model': 'gemini-2.0-pro-exp', 'location': 'global'},
    #                  'quotaValue': '10000000'}
    #             ]},
    #             {'@type': 'type.googleapis.com/google.rpc.Help', 'links': [
    #                 {'description': 'Learn more about Gemini API quotas',
    #                  'url': 'https://ai.google.dev/gemini-api/docs/rate-limits'}
    #             ]},
    #             {'@type': 'type.googleapis.com/google.rpc.RetryInfo', 'retryDelay': '5s'}
    #         ]
    #     }
    # }
    if data:
        try:
            data = json.loads(data)
        except Exception as e:
            logger.info("Json.loads error %s", e)
        else:
            if data.get("error", {}).get("status", "") == "RESOURCE_EXHAUSTED":
                return GOOGLE_LIMIT_ERROR
    return None


async def check_rate_limit(
    data: str | bytes,
    *,
    request_body: str | bytes | None = None,
    response_status: Optional[int] = None,
) -> Tuple[bool, Optional[int]]:
    """
    Check for rate limit error.

    Args:
        data: response line

    Returns:
        Tuple (has_rate_limit_error, reset_time_ms)
    """
    has_rate_limit_error = False
    reset_time_ms = None
    err = None
    for attempt in range(5):
        try:
            err = json.loads(data)
        except Exception as exc:  # retry on transient decode issues
            if attempt < 4:
                continue
            logger.warning('Json.loads error %s', exc)
            logger.warning(
                'Json decode failed. status=%s request_body=%s response_body=%s',
                response_status,
                _format_for_log(request_body),
                _format_for_log(data),
            )
        else:
            break
    if isinstance(err, dict) and "error" in err:
        code = err["error"].get("code", 0)
        try:
            x_rate_limit = int(err["error"]["metadata"]["headers"]["X-RateLimit-Reset"])
        except (TypeError, KeyError):
            if code == RATE_LIMIT_ERROR_CODE and (raw := err["error"].get("metadata", {}).get("raw", "")):
                issue = check_global_limit(raw) or check_google_error(raw)
                if issue:
                    if config["openrouter"]["global_rate_delay"]:
                        logger.info("%s, waiting %s seconds.", issue, config["openrouter"]["global_rate_delay"])
                        await asyncio.sleep(config["openrouter"]["global_rate_delay"])
                    return False, None
            x_rate_limit = 0

        if x_rate_limit > 0:
            has_rate_limit_error = True
            reset_time_ms = x_rate_limit
        elif code == RATE_LIMIT_ERROR_CODE:
            has_rate_limit_error = True

    return has_rate_limit_error, reset_time_ms
