"""Download a social-login profile photo safely.

The URL comes from a third party (the provider's JSON), so it is treated as hostile:
HTTPS only, public IPs only (no SSRF into the server's network), connection pinned to the IP that was checked,
at most two redirects (each re-checked), 5 s overall, 2 MB, an image/* content type, and the bytes must open as a real
JPEG/PNG/WebP in Pillow. What gets stored is a freshly re-encoded JPEG, never the downloaded bytes.
Any failure returns None: a missing avatar must never block a sign-in.
"""
import io
import ipaddress
import logging
import os
import socket
import time
from urllib.parse import urljoin, urlparse

import httpx
from django.conf import settings
from django.core.files.base import ContentFile
from PIL import Image, UnidentifiedImageError

logger = logging.getLogger(__name__)

MAX_BYTES = 2 * 1024 * 1024
TIMEOUT_SECONDS = 5.0
MAX_REDIRECTS = 2
MAX_PIXELS = 4096 * 4096
OUTPUT_SIZE = (512, 512)
ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP"}


def public_ip(host: str) -> str | None:
    """The first address of `host` if EVERY address it resolves to is public; None otherwise (private, loopback, link-local...)."""
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError:
        return None
    addresses = []
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return None
        if not ip.is_global:
            return None
        addresses.append(str(ip))
    return addresses[0] if addresses else None


def _behind_platform_proxy() -> bool:
    """PythonAnywhere free accounts reach the internet only through their proxy; the proxy resolves the host itself."""
    return not getattr(settings, "ENABLE_PROXY_BYPASS", False) and bool(os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy"))


def _check_url(url: str):
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.port not in (None, 443):
        return None
    return parsed


def _download(url: str) -> bytes | None:
    deadline = time.monotonic() + TIMEOUT_SECONDS
    proxied = _behind_platform_proxy()
    for _hop in range(MAX_REDIRECTS + 1):
        parsed = _check_url(url)
        if parsed is None:
            return None
        ip = public_ip(parsed.hostname)
        if ip is None:
            return None
        headers = {"Accept": "image/*", "User-Agent": "Internest/1.0"}
        request_url, extensions = url, {}
        if not proxied:  # pin the connection to the address we just validated (defeats DNS rebinding)
            request_url = parsed._replace(netloc=f"[{ip}]" if ":" in ip else ip).geturl()
            headers["Host"], extensions = parsed.hostname, {"sni_hostname": parsed.hostname}
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        with httpx.Client(timeout=httpx.Timeout(remaining), follow_redirects=False, trust_env=proxied) as client:
            with client.stream("GET", request_url, headers=headers, extensions=extensions) as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        return None
                    url = urljoin(url, location)
                    continue
                if response.status_code != 200:
                    return None
                content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
                if not content_type.startswith("image/") or content_type == "image/svg+xml":
                    return None
                length = response.headers.get("content-length", "")
                if length.isdigit() and int(length) > MAX_BYTES:
                    return None
                data = bytearray()
                for chunk in response.iter_bytes():
                    data += chunk
                    if len(data) > MAX_BYTES or time.monotonic() > deadline:
                        return None
                return bytes(data)
    return None


def clean_image(data: bytes) -> ContentFile | None:
    """Verify the bytes are a real raster image and return a re-encoded JPEG (no metadata, bounded size)."""
    try:
        with Image.open(io.BytesIO(data)) as probe:
            probe.verify()
        with Image.open(io.BytesIO(data)) as image:
            if image.format not in ALLOWED_FORMATS or image.width * image.height > MAX_PIXELS:
                return None
            image.load()
            image = image.convert("RGB")
        image.thumbnail(OUTPUT_SIZE)
        out = io.BytesIO()
        image.save(out, "JPEG", quality=88, optimize=True)
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError, SyntaxError):
        return None
    return ContentFile(out.getvalue(), name="avatar.jpg")


def fetch_avatar(url: str | None) -> ContentFile | None:
    """A safe, re-encoded copy of the photo at `url`, or None."""
    if not url:
        return None
    try:
        data = _download(url)
    except (httpx.HTTPError, OSError, ValueError):
        logger.info("Social avatar download failed")
        return None
    return clean_image(data) if data else None
