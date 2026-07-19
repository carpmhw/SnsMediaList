"""Validation and canonicalization for supported public post URLs."""

from dataclasses import dataclass
from urllib.parse import SplitResult, parse_qsl, urlencode, urlsplit, urlunsplit

from .errors import AppError

_INSTAGRAM_HOSTS = {"instagram.com", "www.instagram.com"}
_X_HOSTS = {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}
_TRACKING_KEYS = {"ref", "s", "t", "cxt", "src"}


@dataclass(frozen=True, slots=True)
class ValidatedPostUrl:
    """Represent a validated public post URL and its platform identity."""

    platform: str
    canonical_url: str
    post_id: str


def validate_post_url(url: str) -> ValidatedPostUrl:
    """Validate and canonicalize one supported public post URL."""
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as error:
        raise AppError("unsupported_url", "The URL is not supported.") from error

    if parsed.scheme.lower() != "https" or parsed.username or parsed.password:
        raise AppError("unsupported_url", "Only supported HTTPS post URLs are accepted.")
    if parsed.fragment or port not in (None, 443):
        raise AppError("unsupported_url", "The URL is not supported.")

    hostname = (parsed.hostname or "").lower().rstrip(".")
    if hostname in _INSTAGRAM_HOSTS:
        post_id = _match_instagram_post(parsed.path)
        platform = "instagram"
    elif hostname in _X_HOSTS:
        post_id = _match_x_post(parsed.path)
        platform = "x"
    else:
        raise AppError("unsupported_url", "The URL is not supported.")

    if post_id is None:
        raise AppError("unsupported_url", "Only single public post URLs are supported.")

    return ValidatedPostUrl(platform, _canonicalize(parsed, hostname), post_id)


def validate_platform_redirect_target(source_url: str, target_url: str) -> ValidatedPostUrl:
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
) -> ValidatedPostUrl:
    """Validate a bounded chain of platform redirects and return its final post."""
    if len(targets) > max_redirects:
        raise AppError("unsupported_url", "The URL redirected too many times.")
    source_parts = urlsplit(source_url)
    source_host = (source_parts.hostname or "").lower().rstrip(".")
    source_platform = _platform_for_host(source_host)
    final: ValidatedPostUrl | None = None
    for target_url in targets:
        final = validate_post_url(target_url)
        if source_platform is not None and final.platform != source_platform:
            raise AppError("unsupported_url", "Platform redirects cannot change platform.")
    if final is None:
        raise AppError("unsupported_url", "The URL did not resolve to a post.")
    return final


def _match_instagram_post(path: str) -> str | None:
    """Extract an Instagram post identifier from an allowed path."""
    parts = [part for part in path.split("/") if part]
    if len(parts) == 2 and parts[0] in {"p", "reel", "tv"}:
        return parts[1]
    if len(parts) == 3 and parts[1] in {"p", "reel", "tv"}:
        return parts[2]
    return None


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
