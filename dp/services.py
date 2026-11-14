"""Read-only reachability checks for the shared whisper and llama services."""

import httpx

from dp.config import Settings


def check_services(s: Settings, timeout: float = 5.0) -> dict[str, str]:
    """GET lightweight endpoints only; never sends inference requests."""
    probes = {
        "whisper": f"{s.whisper_url}/",
        "llama /health": f"{s.llama_url}/health",
        "llama /v1/models": f"{s.llama_url}/v1/models",
    }
    results: dict[str, str] = {}
    for name, url in probes.items():
        try:
            r = httpx.get(url, timeout=timeout)
            results[name] = f"HTTP {r.status_code} {r.text[:120]!r}"
        except httpx.HTTPError as e:
            results[name] = f"ERROR {type(e).__name__}: {e}"
    return results
