# finance-reporting — Phase 8 Spend Pulse dashboard (Plotly Dash + gunicorn).
#
# Build:  docker build -t finance-reporting:latest apps/finance-reporting/
# Deploy: built locally on host-b and imported into k3s containerd
#         (same dance as substrate/mcp-hub; imagePullPolicy: Never).
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

# Dash serves on 8050; the Console's nginx proxies /reports/ to this port.
EXPOSE 8050

# 4 workers and 120s timeout to handle parallel data heavy aggregation.
CMD ["gunicorn", "--bind", "0.0.0.0:8050", "--workers", "4", "--timeout", "120", "app:server"]
