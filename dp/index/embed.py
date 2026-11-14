"""Client for the `embed` service (text-embeddings-inference, bge-m3 on the CPU)."""

import httpx
import numpy as np

BATCH = 16


def embed(url: str, texts: list[str], timeout_s: float = 300) -> np.ndarray:
    """(n, dim) float32, L2-normalized (TEI normalizes by default)."""
    out: list[list[float]] = []
    for i in range(0, len(texts), BATCH):
        r = httpx.post(
            url.rstrip("/") + "/embed",
            json={"inputs": texts[i : i + BATCH], "truncate": True},
            timeout=timeout_s,
        )
        if r.status_code >= 400:
            raise RuntimeError(f"embed HTTP {r.status_code}: {r.text[:300]}")
        out.extend(r.json())
    return np.asarray(out, dtype=np.float32).reshape(len(texts), -1)
