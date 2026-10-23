from datetime import datetime, timezone
import re

EXCLUDED_PATHS = {"/healthz", "/readyz", "/openapi.json", "/docs", "/redoc"}
OMITTED_SEGMENTS = {"account"}


def export_filename(path: str, now: datetime | None = None) -> str | None:
    """Name an exported report after its endpoint plus a UTC timestamp."""
    if path in EXCLUDED_PATHS or path.startswith("/docs/"):
        return None
    segments = [segment for segment in path.strip("/").split("/")
                if segment and segment not in OMITTED_SEGMENTS]
    if not segments:
        return None
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", "_".join(segments)).strip("-_")
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    return f"{name}_{stamp}.json" if name else None
