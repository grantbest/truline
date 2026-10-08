import os
from datetime import UTC, datetime

import httpx
from kubernetes import client, config
from src.tools.finance import substrate_beads_url, substrate_headers

_SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}
_LITELLM_SPEND_METRIC = "litellm_spend_metric_total"

def get_platform_status() -> str:
    """Current rollout status of all K8s deployments."""
    try:
        config.load_incluster_config()
    except config.ConfigException:
        return "Could not load in-cluster config (running outside cluster?)"
    
    apps = client.AppsV1Api()
    try:
        deployments = apps.list_deployment_for_all_namespaces().items
        lines = [f"{d.metadata.namespace}/{d.metadata.name}: {d.status.ready_replicas or 0}/{d.spec.replicas}"
                 for d in deployments]
        return "\n".join(lines)
    except Exception as e:
        return f"Error listing deployments: {str(e)}"

def get_grafana_alerts() -> list[dict]:
    """Active alerts from Alertmanager, most severe first.

    Watchdog (the always-firing heartbeat) is filtered out, so an empty list
    means "no active alerts". Unreachable Alertmanager returns an explicit
    error entry rather than raising — a broken digest must never crash
    MorningBrief (same best-effort semantics as bank_sync notifications).
    """
    base = os.environ.get(
        "ALERTMANAGER_URL", "http://alertmanager-operated.monitoring.svc:9093"
    )
    try:
        resp = httpx.get(
            f"{base}/api/v2/alerts",
            params={"active": "true", "silenced": "false", "inhibited": "false"},
            timeout=5.0,
        )
        resp.raise_for_status()
        raw = resp.json()
    except Exception as e:
        return [{"error": "Failed to reach Alertmanager", "detail": str(e)}]

    alerts = []
    for a in raw:
        labels = a.get("labels", {})
        name = labels.get("alertname", "unknown")
        if name == "Watchdog":
            continue
        alerts.append(
            {
                "alert": name,
                "severity": labels.get("severity", "none"),
                "status": a.get("status", {}).get("state", "active"),
                "summary": a.get("annotations", {}).get("summary", ""),
                "since": a.get("startsAt", ""),
            }
        )
    alerts.sort(key=lambda x: (_SEVERITY_ORDER.get(x["severity"], 3), x["alert"]))
    return alerts

def query_beads(
    namespace: str,
    type: str | None = None,
    limit: int = 10,
    state: str | None = None,
    trust_tier: str | None = None,
    parent_id: str | None = None,
    created_after: str | None = None,
    offset: int = 0,
) -> list[dict]:
    """Query beads from Substrate.

    Same filter surface as the console's substrateClient.listBeads
    (namespace, type, state, trust_tier, parent_id, created_after, limit,
    offset) against the same /beads endpoint — not the narrower
    namespace/type/limit-only shape this used to hand-roll.
    """
    params = {
        "namespace": namespace,
        "type": type,
        "state": state,
        "trust_tier": trust_tier,
        "parent_id": parent_id,
        "created_after": created_after,
        "limit": limit,
        "offset": offset,
    }
    # httpx serializes None params as `key=` rather than omitting them, so
    # unset filters must be stripped before the request goes out.
    params = {k: v for k, v in params.items() if v is not None}
    try:
        resp = httpx.get(
            substrate_beads_url(),
            params=params,
            headers=substrate_headers(),
            timeout=5.0,
        )
        if resp.status_code == 200:
            return resp.json()
        return [{"error": f"Substrate returned {resp.status_code}", "detail": resp.text}]
    except Exception as e:
        return [{"error": "Failed to connect to Substrate", "detail": str(e)}]

def _current_utc_month_window() -> tuple[datetime, int]:
    now = datetime.now(UTC).replace(microsecond=0)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return now, max(1, int((now - month_start).total_seconds()))


def get_litellm_spend() -> dict:
    """Current UTC calendar month-to-date token spend per model."""
    base = os.environ.get(
        "PROMETHEUS_URL", "http://prometheus-operated.monitoring.svc:9090"
    )
    query_time, window_seconds = _current_utc_month_window()
    query = (
        "sum by (model) "
        f"(increase({_LITELLM_SPEND_METRIC}[{window_seconds}s]))"
    )

    try:
        resp = httpx.get(
            f"{base}/api/v1/query",
            params={"query": query, "time": query_time.timestamp()},
            timeout=5.0,
        )
        resp.raise_for_status()
        raw = resp.json()
    except Exception as e:
        return {"error": "Failed to reach Prometheus", "detail": str(e)}

    if not isinstance(raw, dict):
        return {
            "error": "Malformed Prometheus response",
            "detail": "Prometheus response was not a JSON object",
        }

    if raw.get("status") != "success":
        return {
            "error": "Prometheus query failed",
            "detail": raw.get("error", "unknown error"),
        }

    result = raw.get("data", {}).get("result", [])
    if not isinstance(result, list):
        return {
            "error": "Malformed Prometheus response",
            "detail": "Prometheus result is not a vector",
        }
    if not result:
        return {
            "error": "LiteLLM spend metric absent",
            "detail": f"No {_LITELLM_SPEND_METRIC} series returned",
        }

    spend = {}
    for series in result:
        model = series.get("metric", {}).get("model")
        value_pair = series.get("value") or []
        value = value_pair[1] if len(value_pair) > 1 else None
        if not model:
            return {
                "error": "Malformed Prometheus response",
                "detail": "LiteLLM spend series missing model label",
            }
        try:
            spend[model] = float(value)
        except (TypeError, ValueError):
            return {
                "error": "Malformed Prometheus response",
                "detail": f"LiteLLM spend value for {model} is not numeric",
            }

    return spend
