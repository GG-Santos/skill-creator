#!/usr/bin/env python3
"""Shared GitHub helpers for skill install scripts."""

from __future__ import annotations

import os
import urllib.parse
import urllib.request


DEFAULT_MAX_RESPONSE_BYTES = 20 * 1024 * 1024


class ResponseTooLargeError(Exception):
    """Raised when a GitHub response exceeds the configured byte limit."""


def github_request(
    url: str,
    user_agent: str,
    max_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
) -> bytes:
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive.")
    headers = {"User-Agent": user_agent}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"token {token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req) as resp:
        content_length = resp.headers.get("Content-Length")
        if content_length:
            try:
                declared_size = int(content_length)
            except ValueError:
                declared_size = None
            if declared_size is not None and declared_size > max_bytes:
                raise ResponseTooLargeError(
                    f"GitHub response declares {declared_size} bytes; limit is {max_bytes}."
                )

        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = resp.read(min(1024 * 1024, max_bytes - total + 1))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                raise ResponseTooLargeError(
                    f"GitHub response exceeds the {max_bytes}-byte limit."
                )
        return b"".join(chunks)


def github_api_contents_url(repo: str, path: str, ref: str) -> str:
    encoded_repo = "/".join(
        urllib.parse.quote(component, safe="") for component in repo.split("/")
    )
    encoded_path = "/".join(
        urllib.parse.quote(component, safe="") for component in path.split("/")
    )
    query = urllib.parse.urlencode({"ref": ref})
    return f"https://api.github.com/repos/{encoded_repo}/contents/{encoded_path}?{query}"
