"""The web app's API on the recorded Phase 2 measurement: no fork, no network."""

import pytest
from fastapi.testclient import TestClient

from lapeg.app.api import app

client = TestClient(app)


def test_overview_states_the_measured_capacity() -> None:
    o = client.get("/api/overview").json()
    assert o["market"]["borrowers"] > 0
    assert 0 < o["capacity"]["low_usd"] < o["capacity"]["high_usd"]
    losses = [p["loss"] for p in o["depth"]]
    assert losses == sorted(losses)  # one coherent curve: selling more never loses less
    assert o["aug25"]["liquidations"] == 16


def test_whatif_accounts_for_every_loan() -> None:
    r = client.post("/api/whatif", json={"drop": 0.05, "refill": 0.005, "lag": True}).json()
    s = r["summary"]
    assert s["safe"] + s["rescued"] + s["stuck"] + s["loss"] == len(r["loans"])
    assert r["refill_half_life_min"] == pytest.approx(27.66, abs=0.05)


def test_no_shock_leaves_every_loan_safe() -> None:
    s = client.post("/api/whatif", json={"refill": 0, "lag": True}).json()["summary"]
    assert s["safe"] > 0 and s["rescued"] == s["stuck"] == s["loss"] == 0


def test_guard_passes_a_small_sale_and_holds_a_large_one() -> None:
    assert client.post("/api/guard", json={"sale_usd": 5e5}).json()["action"] == "PASS"
    big = client.post("/api/guard", json={"sale_usd": 2e7}).json()
    assert big["action"] == "DELAY" and big["added_risk_usd"] > 0


def test_lab_actions_need_a_running_copy() -> None:
    assert client.get("/api/lab").json() == {"running": False}
    assert client.post("/api/lab/drop", json={"drop": 0.05}).status_code == 409


def test_inputs_are_validated() -> None:
    assert client.post("/api/whatif", json={"drop": 2}).status_code == 422
    assert client.post("/api/guard", json={"sale_usd": -1}).status_code == 422
