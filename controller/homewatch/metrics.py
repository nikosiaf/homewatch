"""A small VictoriaMetrics client: instant and range queries, and writes in Influx line protocol."""

from __future__ import annotations

import time

import httpx


class MetricsError(RuntimeError):
    pass


def _escape_tag(v: str) -> str:
    return v.replace("\\", "\\\\").replace(",", "\\,").replace("=", "\\=").replace(" ", "\\ ")


def line(measurement: str, tags: dict[str, str], fields: dict[str, float], ts: float | None = None) -> str:
    tag_s = "".join(f",{_escape_tag(k)}={_escape_tag(str(v))}" for k, v in sorted(tags.items()) if v != "")
    field_s = ",".join(f"{k}={float(v)}" for k, v in fields.items() if v is not None)
    ns = int((ts if ts is not None else time.time()) * 1e9)
    return f"{_escape_tag(measurement)}{tag_s} {field_s} {ns}"


def lines(measurement: str, rows: list[tuple[dict[str, str], dict[str, float | None]]], ts: float) -> list[str]:
    """Several rows of one measurement; rows whose fields are all missing are left out."""
    out = []
    for tags, fields in rows:
        fields = {k: v for k, v in fields.items() if isinstance(v, (int, float)) and not isinstance(v, bool)
                  and v == v and abs(v) != float("inf")}
        if fields:
            out.append(line(measurement, tags, fields, ts))
    return out


def num(v) -> float | None:
    """A device's value as a number, or None when it is missing or not a number."""
    if isinstance(v, bool):
        return float(v)
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class Metrics:
    def __init__(self, base_url: str, client: httpx.AsyncClient | None = None):
        self.base = base_url
        self.http = client or httpx.AsyncClient(timeout=15)

    async def query(self, q: str, at: float | None = None) -> list[tuple[dict[str, str], float, float]]:
        """Instant query → [(labels, timestamp, value)]."""
        params = {"query": q}
        if at is not None:
            params["time"] = str(at)
        data = await self._get("/api/v1/query", params)
        out = []
        for r in data.get("result", []):
            ts, val = r["value"]
            out.append((r.get("metric", {}), float(ts), float(val)))
        return out

    async def query_range(self, q: str, start: float, end: float, step: int) -> list[tuple[dict[str, str], list[tuple[float, float]]]]:
        data = await self._get("/api/v1/query_range", {"query": q, "start": str(start), "end": str(end), "step": f"{step}s"})
        return [(r.get("metric", {}), [(float(t), float(v)) for t, v in r["values"]]) for r in data.get("result", [])]

    async def write(self, lines: list[str]) -> None:
        if not lines:
            return
        try:
            r = await self.http.post(f"{self.base}/write", content="\n".join(lines).encode())
        except httpx.HTTPError as e:
            raise MetricsError(f"VictoriaMetrics write failed: {e}") from e
        if r.status_code >= 300:
            raise MetricsError(f"VictoriaMetrics write failed: HTTP {r.status_code} {r.text[:200]}")

    async def delete_site(self, site: str) -> None:
        try:
            r = await self.http.post(f"{self.base}/api/v1/admin/tsdb/delete_series",
                                     data={"match[]": f'{{site="{site}"}}'})
        except httpx.HTTPError as e:
            raise MetricsError(f"VictoriaMetrics unreachable: {e}") from e
        if r.status_code >= 300:
            raise MetricsError(f"VictoriaMetrics delete failed: HTTP {r.status_code} {r.text[:200]}")

    async def delete(self, match: str) -> None:
        """Delete every series matching a selector, e.g. {__name__=~"hw_ha_entity_.*",entity_id="sensor.x"}."""
        try:
            r = await self.http.post(f"{self.base}/api/v1/admin/tsdb/delete_series", data={"match[]": match})
        except httpx.HTTPError as e:
            raise MetricsError(f"VictoriaMetrics unreachable: {e}") from e
        if r.status_code >= 300:
            raise MetricsError(f"VictoriaMetrics delete failed: HTTP {r.status_code} {r.text[:200]}")

    async def healthy(self) -> bool:
        try:
            r = await self.http.get(f"{self.base}/health")
            return r.status_code == 200
        except httpx.HTTPError:
            return False

    async def _get(self, path: str, params: dict) -> dict:
        try:
            r = await self.http.get(f"{self.base}{path}", params=params)
        except httpx.HTTPError as e:
            raise MetricsError(f"VictoriaMetrics unreachable: {e}") from e
        if r.status_code != 200:
            raise MetricsError(f"VictoriaMetrics query failed: HTTP {r.status_code} {r.text[:200]}")
        body = r.json()
        if body.get("status") != "success":
            raise MetricsError(f"VictoriaMetrics query failed: {body.get('error', body)}")
        return body["data"]
