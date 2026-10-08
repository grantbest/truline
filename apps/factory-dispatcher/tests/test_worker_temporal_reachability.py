"""Temporal reachability must be judged by a real RPC, not an open port.

On 2026-08-25 a dead `kubectl port-forward` left its local listener open for
13h23m: `nc -z 127.0.0.1 7233` succeeded the whole time while every real
Temporal call failed with "connection reset by peer." A TCP-level liveness
check would have read healthy for the entire outage. `worker.py` must ask the
already-connected Temporal client to make a call, not open a second socket.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import worker  # noqa: E402


class FakeServiceClient:
    def __init__(self, *, healthy: bool = True, exc: Exception | None = None):
        self.healthy = healthy
        self.exc = exc
        self.calls = 0

    async def check_health(self):
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        return self.healthy


class FakeClient:
    def __init__(self, *, healthy: bool = True, exc: Exception | None = None):
        self.service_client = FakeServiceClient(healthy=healthy, exc=exc)


def run(coro):
    return asyncio.run(coro)


def test_reachable_client_reports_true_via_check_health():
    client = FakeClient(healthy=True)

    assert run(worker.temporal_connection_reachable(client)) is True
    assert client.service_client.calls == 1


def test_broken_tunnel_reports_false_when_the_rpc_raises():
    # This is the observed failure shape: the local listener stays open, but
    # the forward behind it is dead, so any real Temporal call raises.
    client = FakeClient(exc=ConnectionResetError("connection reset by peer"))

    assert run(worker.temporal_connection_reachable(client)) is False


def test_unhealthy_service_response_reports_false():
    client = FakeClient(healthy=False)

    assert run(worker.temporal_connection_reachable(client)) is False


def test_reachability_check_does_not_open_a_socket(monkeypatch):
    def fail_if_called(*args, **kwargs):
        raise AssertionError("temporal_connection_reachable must not open a raw socket")

    monkeypatch.setattr(asyncio, "open_connection", fail_if_called)
    client = FakeClient(healthy=True)

    assert run(worker.temporal_connection_reachable(client)) is True


def test_worker_has_no_tcp_based_reachability_check_left_behind():
    # The finding was specific: a TCP-only liveness check on the port stayed
    # healthy through the whole outage. Nothing in worker.py should still
    # offer that as the tunnel-monitoring primitive.
    assert not hasattr(worker, "temporal_address_reachable")
    assert not hasattr(worker, "temporal_host_port")
