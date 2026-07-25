"""Validation and canonicalization for supported public media targets."""

import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import SplitResult, parse_qsl, urlencode, urlsplit, urlunsplit

from .errors import AppError

_INSTAGRAM_HOSTS = {"instagram.com", "www.instagram.com"}
_X_HOSTS = {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}
_TRACKING_KEYS = {"ref", "s", "t", "cxt", "src"}
_INSTAGRAM_STORY_PATH = re.compile(
    r"/stories/(?P<username>[A-Za-z0-9._]{1,30})/(?P<target_id>[0-9]+)/"
)

type TargetKind = Literal["post", "story"]


@dataclass(frozen=True, slots=True)
class ValidatedExtractionTarget:
    """Represent a validated public media target and its platform identity."""

    platform: str
    kind: TargetKind
    canonical_url: str
    target_id: str


def validate_post_url(url: str) -> ValidatedExtractionTarget:
    """Validate and canonicalize one supported public media target URL."""
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as error:
        raise AppError("unsupported_url", "The URL is not supported.") from error

    if (
        parsed.scheme.lower() != "https"
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise AppError("unsupported_url", "Only supported HTTPS media target URLs are accepted.")
    if "#" in url or port not in (None, 443):
        raise AppError("unsupported_url", "The URL is not supported.")

    hostname = (parsed.hostname or "").lower().rstrip(".")
    if hostname in _INSTAGRAM_HOSTS:
        platform = "instagram"
        if parsed.path.startswith("/stories/"):
            target_id = _match_instagram_story(parsed.path)
            kind: TargetKind = "story"
            canonical_url = urlunsplit(("https", "www.instagram.com", parsed.path, "", ""))
        else:
            target_id = _match_instagram_post(parsed.path)
            kind = "post"
            canonical_url = _canonicalize(parsed, hostname)
    elif hostname in _X_HOSTS:
        platform = "x"
        target_id = _match_x_post(parsed.path)
        kind = "post"
        canonical_url = _canonicalize(parsed, hostname)
    else:
        raise AppError("unsupported_url", "The URL is not supported.")

    if target_id is None:
        raise AppError("unsupported_url", "Only supported single media target URLs are accepted.")

    return ValidatedExtractionTarget(platform, kind, canonical_url, target_id)


def validate_platform_redirect_target(
    source_url: str, target_url: str
) -> ValidatedExtractionTarget:
    """Validate that a platform redirect ends at a supported same-platform post."""
    source = validate_post_url(source_url) if "/status/" in source_url else None
    target = validate_post_url(target_url)
    if source is not None and source.platform != target.platform:
        raise AppError("unsupported_url", "Platform redirects cannot change platform.")
    return target


def validate_redirect_chain(
    source_url: str,
    targets: list[str],
    *,
    max_redirects: int,
) -> ValidatedExtractionTarget:
    """Validate a bounded chain of platform redirects and return its final post."""
    if len(targets) > max_redirects:
        raise AppError("unsupported_url", "The URL redirected too many times.")
    source_parts = urlsplit(source_url)
    source_host = (source_parts.hostname or "").lower().rstrip(".")
    source_platform = _platform_for_host(source_host)
    final: ValidatedExtractionTarget | None = None
    for target_url in targets:
        final = validate_post_url(target_url)
        if source_platform is not None and final.platform != source_platform:
            raise AppError("unsupported_url", "Platform redirects cannot change platform.")
    if final is None:
        raise AppError(
            "unsupported_url",
            "The URL did not resolve to a supported media target.",
        )
    return final


def _match_instagram_post(path: str) -> str | None:
    """Extract an Instagram post identifier from an allowed path."""
    parts = [part for part in path.split("/") if part]
    if len(parts) == 2 and parts[0] in {"p", "reel", "tv"}:
        return parts[1]
    if len(parts) == 3 and parts[1] in {"p", "reel", "tv"}:
        return parts[2]
    return None


def _match_instagram_story(path: str) -> str | None:
    """Extract an identifier from an exact single-Story Instagram path."""
    match = _INSTAGRAM_STORY_PATH.fullmatch(path)
    if match is None:
        return None
    username = match.group("username")
    if (
        username.lower() == "highlights"
        or username.startswith(".")
        or username.endswith(".")
        or ".." in username
    ):
        return None
    return match.group("target_id")


def _match_x_post(path: str) -> str | None:
    """Extract an X status identifier from an allowed path."""
    parts = [part for part in path.split("/") if part]
    if len(parts) == 3 and parts[1] == "status" and parts[2].isdigit():
        return parts[2]
    if len(parts) == 3 and parts[0] == "i" and parts[1] == "web" and parts[2].isdigit():
        return parts[2]
    return None


def _platform_for_host(hostname: str) -> str | None:
    """Return the platform owning an allowed host, if any."""
    if hostname in _INSTAGRAM_HOSTS:
        return "instagram"
    if hostname in _X_HOSTS:
        return "x"
    return None


def _canonicalize(parsed: SplitResult, hostname: str) -> str:
    """Build a canonical URL while retaining only non-tracking query values."""
    source = parsed
    query = parse_qsl(source.query, keep_blank_values=True)
    filtered = [
        (key, value)
        for key, value in query
        if key.lower() not in _TRACKING_KEYS and not key.lower().startswith("utm_")
    ]
    clean_query = urlencode(filtered)
    return urlunsplit(("https", hostname, source.path.rstrip("/") + "/", clean_query, ""))
