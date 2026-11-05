"""Send authenticated Azure REST requests with retry and timeout limits."""

from __future__ import annotations

import json
import logging
import math
import random
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

from azure.core.credentials import TokenCredential
from azure.identity import AzureCliCredential, get_bearer_token_provider

OPENAI_API_VERSION = "2024-10-21"
SEARCH_API_VERSION = "2026-08-01-preview"
SEARCH_SCOPE = "https://search.azure.com/.default"
OPENAI_SCOPE = "https://cognitiveservices.azure.com/.default"
TRANSIENT_STATUSES = {408, 429, 500, 502, 503, 504}
HeaderSource = Mapping[str, str] | Callable[[], Mapping[str, str]]
logger = logging.getLogger(__name__)


class AzureHttpError(RuntimeError):
    """An Azure HTTP request failure with its status code."""

    def __init__(self, method: str, status_code: int, body: str) -> None:
        super().__init__(f"{method} failed with HTTP {status_code}: {body[:2000]}")
        self.status_code = status_code


def access_token(scope: str, credential: TokenCredential | None = None) -> str:
    """Return an access token using the supplied credential or Azure CLI."""
    if credential is not None:
        return credential.get_token(scope).token
    with AzureCliCredential() as local_credential:
        return local_credential.get_token(scope).token


def authorization_headers(
    credential: TokenCredential, scope: str
) -> Callable[[], dict[str, str]]:
    """
    Return a header factory that refreshes expired Entra access tokens.

    Parameters
    ----------
    credential : TokenCredential
        Credential used to request Entra access tokens.
    scope : str
        Token scope required by the service, such as
        ``https://search.azure.com/.default``.

    Returns
    -------
    callable
        Function that returns an Authorization header using a current
        access token.
    """
    token_provider = get_bearer_token_provider(credential, scope)
    return lambda: {"Authorization": "Bearer " + token_provider()}


def retry_delay(headers: Mapping[str, str], attempt: int) -> float:
    """
    Return a retry delay in seconds from service headers or backoff.

    The attempt number is one-based. Use exponential backoff with jitter
    when no valid retry header is present.
    """
    normalized = {name.lower(): value for name, value in headers.items()}
    milliseconds = normalized.get("retry-after-ms") or normalized.get(
        "x-ms-retry-after-ms"
    )
    retry_after = normalized.get("retry-after")
    if milliseconds or retry_after:
        try:
            delay = float(milliseconds) / 1000 if milliseconds else float(retry_after)
        except ValueError:
            try:
                retry_time = parsedate_to_datetime(retry_after or "")
                delay = (retry_time - datetime.now(UTC)).total_seconds()
            except (ValueError, TypeError, OverflowError):
                logger.warning("Invalid Retry-After header; using exponential backoff")
            else:
                return max(0.0, delay)
        else:
            if math.isfinite(delay):
                return max(0.0, delay)
            logger.warning("Non-finite Retry-After header; using exponential backoff")
    return min(2 ** (attempt - 1), 30) + random.uniform(0, 1)


def _send_once(
    method: str, url: str, body: bytes | None, headers: HeaderSource, timeout: float
) -> tuple[int, dict[str, str], bytes]:
    request_headers = dict(headers() if callable(headers) else headers)
    request_object = urllib.request.Request(
        url, data=body, headers=request_headers, method=method
    )
    with urllib.request.urlopen(request_object, timeout=timeout) as response:
        return (
            response.status,
            {key.lower(): value for key, value in response.headers.items()},
            response.read(),
        )


def request(
    method: str,
    url: str,
    *,
    headers: HeaderSource | None = None,
    json_body: Any | None = None,
    raw_body: bytes | None = None,
    timeout: float = 180,
    retries: int = 4,
    max_retry_seconds: float = 120,
) -> tuple[int, dict[str, str], bytes]:
    """
    Send a request and return its status, headers, and response bytes.

    Only call this for operations that can be repeated safely. Retry transport
    errors and transient HTTP failures only.

    Parameters
    ----------
    method : str
        HTTP verb, such as GET or POST.
    url : str
        Full Azure REST URL, including any API-version query parameter.
    headers : HeaderSource or None, default=None
        Header mapping or zero-argument factory called per attempt;
        use a factory when authentication tokens may need refreshing.
    json_body : Any or None, default=None
        Optional JSON-serializable request body.
    raw_body : bytes or None, default=None
        Optional raw bytes; cannot be combined with ``json_body``.
    timeout : float, default=180
        Initial attempt's timeout in seconds. Later attempts use the
        smaller of this value and the remaining retry budget.
    retries : int, default=4
        Maximum total attempts, including the initial request.
    max_retry_seconds : float, default=120
        Retry time budget measured from the first attempt.
        Zero disables retries but still permits the initial request.

    Returns
    -------
    status : int
        HTTP response status code.
    response_headers : dict
        Response headers with lowercase names.
    body : bytes
        Response content before decoding.
    """
    if timeout <= 0 or not math.isfinite(timeout) or retries < 1:
        raise ValueError(
            "timeout must be finite and positive; retries must be at least one"
        )
    if max_retry_seconds < 0 or not math.isfinite(max_retry_seconds):
        raise ValueError("max_retry_seconds must be finite and nonnegative")
    if json_body is not None and raw_body is not None:
        raise ValueError("Specify json_body or raw_body, not both")
    body = (
        json.dumps(json_body, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if json_body is not None
        else raw_body
    )
    header_source = headers or {}

    def request_headers() -> dict[str, str]:
        values = dict(header_source() if callable(header_source) else header_source)
        if json_body is not None:
            values.setdefault("Content-Type", "application/json")
        return values

    deadline = time.monotonic() + max_retry_seconds
    for attempt in range(1, retries + 1):
        attempt_timeout = (
            timeout if attempt == 1 else min(timeout, deadline - time.monotonic())
        )
        if attempt_timeout <= 0:
            raise TimeoutError("Azure REST retry budget exhausted")
        try:
            return _send_once(method, url, body, request_headers, attempt_timeout)
        except urllib.error.HTTPError as error:
            failure = AzureHttpError(
                method, error.code, error.read().decode("utf-8", errors="replace")
            )
            delay = retry_delay(error.headers, attempt)
            error.close()
            if error.code not in TRANSIENT_STATUSES:
                raise failure from error
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            failure = RuntimeError(f"{method} transport failure: {error}")
            delay = retry_delay({}, attempt)
        remaining = deadline - time.monotonic()
        if attempt == retries or remaining <= delay:
            raise failure
        logger.warning(
            "Retrying %s after transient failure (attempt %d): %s",
            method,
            attempt,
            failure,
        )
        time.sleep(delay)
    raise RuntimeError("Azure REST request exhausted its attempts")


def request_json(
    method: str,
    url: str,
    *,
    headers: HeaderSource | None = None,
    json_body: Any | None = None,
    raw_body: bytes | None = None,
    timeout: float = 180,
    retries: int = 4,
    max_retry_seconds: float = 120,
) -> tuple[int, dict[str, str], dict[str, Any]]:
    """
    Send a request and return its status, headers, and JSON object.

    Parameters
    ----------
    method : str
        HTTP verb for an operation safe to retry.
    url : str
        Full Azure REST URL, including its API version where required.
    headers : HeaderSource or None, default=None
        Static headers or a per-attempt factory for refreshed tokens.
    json_body : Any or None, default=None
        JSON-serializable request body. Use either this or ``raw_body``.
    raw_body : bytes or None, default=None
        Request body supplied as bytes.
    timeout : float, default=180
        Initial request timeout in seconds; retries respect the budget.
    retries : int, default=4
        Maximum total attempts, including the initial request.
    max_retry_seconds : float, default=120
        Time budget for retries, measured from the first attempt.
        Set to zero to make only the initial request.

    Returns
    -------
    status : int
        HTTP response status code.
    response_headers : dict
        Response headers with lowercase names.
    payload : dict
        Decoded JSON object, or an empty dictionary for an empty response body.

    Raises
    ------
    json.JSONDecodeError
        A nonempty response body is not valid JSON.
    RuntimeError
        The response decodes to a value other than a JSON object.
    """
    status, response_headers, body = request(
        method,
        url,
        headers=headers,
        json_body=json_body,
        raw_body=raw_body,
        timeout=timeout,
        retries=retries,
        max_retry_seconds=max_retry_seconds,
    )
    payload = json.loads(body.decode("utf-8-sig")) if body else {}
    if not isinstance(payload, dict):
        raise RuntimeError(f"{method} returned a JSON value other than an object")
    return status, response_headers, payload
