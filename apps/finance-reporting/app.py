"""finance-reporting — Phase 8 "Spend Pulse" dashboard (Plotly Dash).

Why a separate Python service (see docs/plans/phase-8-financial-insights.md):
aggregating ~2 years of transaction beads (group-by month × category) is a
pandas job. Doing it in the browser risks memory blowups; doing it here keeps
the LifeOps Console thin — it just embeds this app in an iframe under /reports/.

Data flow:  LifeOps Console  ->  finance-reporting  ->  substrate-prod (read-only)

The service is mounted behind the Console's nginx at the ``/reports/`` path.
nginx strips that prefix (``proxy_pass .../;``) so the app serves at ``/``
inside the container, but generated asset/callback URLs must carry the
``/reports/`` prefix — hence ``requests_pathname_prefix``.
"""

from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional

import dash
import pandas as pd
import plotly.express as px
import requests
from dash import Input, Output, dcc, html

# --- Config ---------------------------------------------------------------

# Brief: the loader reads the SUBSTRATE_URL env var. Default targets the
# in-cluster prod service; override per environment.
SUBSTRATE_URL = os.environ.get(
    "SUBSTRATE_URL",
    "http://substrate.platform-substrate-prod.svc.cluster.local:8000",
).rstrip("/")
# Substrate requires X-API-Key on every call. Read-only usage here.
SUBSTRATE_API_KEY = os.environ.get("SUBSTRATE_API_KEY", "")

# URL prefix the browser uses to reach this app (Console nginx proxies
# /reports/ here). routes prefix stays "/" because nginx strips /reports/.
REQUESTS_PREFIX = os.environ.get("DASH_REQUESTS_PREFIX", "/reports/")

# Rolling window + cache.
LOOKBACK_MONTHS = 24
PAGE_SIZE = 1000
CACHE_TTL_SECONDS = 300

# The 12 master categories (mirrors bank_sync FINANCE_CATEGORIES + transfer).
# We don't hard-depend on this list — categories are discovered from data —
# but it gives a stable color ordering for the stacked bar.
MASTER_CATEGORIES = [
    "dining", "groceries", "auto", "entertainment", "health", "housing",
    "utilities", "kids", "interest_fees", "misc", "income", "uncategorized",
]


# --- Substrate data loader ------------------------------------------------

_cache: Dict[str, Any] = {"ts": 0.0, "df": None}


def _fetch_all_transactions() -> List[Dict[str, Any]]:
    """Page through Substrate's /beads endpoint for finance.transaction beads.

    Substrate caps each response at ``limit`` rows, so we walk ``offset``
    until a short page comes back. Content is returned decrypted by the API.
    """
    headers = {"X-API-Key": SUBSTRATE_API_KEY} if SUBSTRATE_API_KEY else {}
    out: List[Dict[str, Any]] = []
    offset = 0
    while True:
        resp = requests.get(
            f"{SUBSTRATE_URL}/beads",
            params={
                "namespace": "finance",
                "type": "transaction",
                "limit": PAGE_SIZE,
                "offset": offset,
            },
            headers=headers,
            timeout=30,
        )
        resp.raise_for_status()
        batch = resp.json()
        out.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return out


def _build_dataframe(beads: List[Dict[str, Any]]) -> pd.DataFrame:
    """Flatten transaction beads into a tidy spend DataFrame.

    Governance (Phase 8 plan §3): transfers are dropped BEFORE any
    aggregation so credit-card payments / internal moves never inflate spend.
    """
    rows: List[Dict[str, Any]] = []
    for b in beads:
        if b.get("state") == "removed":
            continue  # Plaid retracted it — not spend
        c = b.get("content") or {}
        if c.get("is_transfer"):
            continue  # never count transfers as spend
        rows.append(
            {
                "posted_date": c.get("posted_date"),
                "amount": c.get("amount"),
                "category": c.get("our_category") or "uncategorized",
                "institution": c.get("institution"),
                "merchant": c.get("normalized_merchant") or c.get("merchant_name"),
            }
        )

    df = pd.DataFrame(rows)
    if df.empty:
        return df

    df["posted_date"] = pd.to_datetime(df["posted_date"], errors="coerce")
    df["amount"] = pd.to_numeric(df["amount"], errors="coerce")
    df = df.dropna(subset=["posted_date", "amount"])

    # Plaid convention: outflows positive, inflows negative. Spend == outflow.
    df = df[df["amount"] > 0].copy()
    df["month"] = df["posted_date"].dt.to_period("M").dt.to_timestamp()

    # Rolling 24-month window.
    cutoff = pd.Timestamp.now().to_period("M").to_timestamp() - pd.DateOffset(
        months=LOOKBACK_MONTHS - 1
    )
    df = df[df["month"] >= cutoff]
    return df


def load_spend_df(force: bool = False) -> pd.DataFrame:
    """Cached entry point used by callbacks."""
    now = time.time()
    if not force and _cache["df"] is not None and (now - _cache["ts"]) < CACHE_TTL_SECONDS:
        return _cache["df"]
    try:
        df = _build_dataframe(_fetch_all_transactions())
    except Exception:  # noqa: BLE001 — surface an empty frame; UI shows "no data"
        df = _cache["df"] if _cache["df"] is not None else pd.DataFrame()
    _cache["df"] = df
    _cache["ts"] = now
    return df


# --- Figures --------------------------------------------------------------

def _empty_fig(message: str):
    fig = px.bar()
    fig.add_annotation(text=message, showarrow=False, font=dict(size=14))
    fig.update_layout(template="plotly_dark", margin=dict(l=40, r=20, t=40, b=40))
    return fig


def spend_pulse_figure(df: pd.DataFrame, categories: Optional[List[str]]):
    """The Spend Pulse: MoM stacked bar of outflow by category."""
    if df.empty:
        return _empty_fig("No spend data available.")
    if categories:
        df = df[df["category"].isin(categories)]
    grouped = (
        df.groupby(["month", "category"], as_index=False)["amount"].sum()
        .sort_values("month")
    )
    fig = px.bar(
        grouped,
        x="month",
        y="amount",
        color="category",
        title="Spend Pulse — monthly outflow by category",
        labels={"month": "Month", "amount": "Outflow (USD)", "category": "Category"},
        category_orders={"category": MASTER_CATEGORIES},
    )
    fig.update_layout(
        template="plotly_dark",
        barmode="stack",
        legend_title_text="Category",
        margin=dict(l=40, r=20, t=50, b=40),
    )
    fig.update_traces(hovertemplate="%{x|%b %Y}<br>%{fullData.name}: $%{y:,.0f}<extra></extra>")
    return fig


def category_trend_figure(df: pd.DataFrame, categories: Optional[List[str]]):
    """Micro view: line chart of the selected categories over the window."""
    if df.empty:
        return _empty_fig("No spend data available.")
    if categories:
        df = df[df["category"].isin(categories)]
    grouped = (
        df.groupby(["month", "category"], as_index=False)["amount"].sum()
        .sort_values("month")
    )
    fig = px.line(
        grouped,
        x="month",
        y="amount",
        color="category",
        markers=True,
        title="Category trend",
        labels={"month": "Month", "amount": "Outflow (USD)", "category": "Category"},
    )
    fig.update_layout(
        template="plotly_dark",
        margin=dict(l=40, r=20, t=50, b=40),
    )
    return fig


# --- App ------------------------------------------------------------------

app = dash.Dash(
    __name__,
    url_base_pathname=REQUESTS_PREFIX,
    title="Finance Reporting",
)
# Exposed for gunicorn: `gunicorn app:server`.
server = app.server


@server.route("/healthz")
def healthz():
    return "ok\n", 200, {"Content-Type": "text/plain"}


def _initial_categories(df: pd.DataFrame) -> List[str]:
    if df.empty:
        return []
    return sorted(df["category"].unique().tolist())


app.layout = html.Div(
    style={"backgroundColor": "#0d1117", "color": "#e6edf3", "minHeight": "100vh", "padding": "16px"},
    children=[
        html.H2("Spend Pulse", style={"marginBottom": "4px"}),
        html.Div(
            "Rolling 24 months · transfers excluded · sourced from substrate-prod",
            style={"fontSize": "12px", "color": "#8b949e", "marginBottom": "16px"},
        ),
        html.Div(
            style={"maxWidth": "520px", "marginBottom": "16px"},
            children=[
                html.Label("Categories", style={"fontSize": "12px", "color": "#8b949e"}),
                dcc.Dropdown(
                    id="category-filter",
                    multi=True,
                    placeholder="All categories",
                    style={"color": "#0d1117"},
                ),
            ],
        ),
        dcc.Graph(id="spend-pulse"),
        dcc.Graph(id="category-trend"),
        # Fires once on load to populate the dropdown options from live data.
        dcc.Interval(id="boot", interval=1, max_intervals=1),
    ],
)


@app.callback(
    Output("category-filter", "options"),
    Input("boot", "n_intervals"),
)
def populate_categories(_n):
    df = load_spend_df()
    return [{"label": c, "value": c} for c in _initial_categories(df)]


@app.callback(
    Output("spend-pulse", "figure"),
    Output("category-trend", "figure"),
    Input("category-filter", "value"),
    Input("boot", "n_intervals"),
)
def render_figures(categories, _n):
    df = load_spend_df()
    return spend_pulse_figure(df, categories), category_trend_figure(df, categories)


if __name__ == "__main__":
    # Local dev only; prod serves via gunicorn (see Dockerfile).
    app.run(host="0.0.0.0", port=8050, debug=True)
