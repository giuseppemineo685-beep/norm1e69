"""
Reconciliación multiset del backfill histórico: el solape entre páginas NUNCA
debe crear trades canónicos de más, pero una multiplicidad real DENTRO de una
misma página NUNCA debe colapsarse en una sola fila. También cubre checkpoint
de reanudación y condición terminal por reintentos agotados.
"""
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _fresh_db(monkeypatch):
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    import config
    monkeypatch.setattr(config, "DB_PATH", tmp.name)
    import db
    monkeypatch.setattr(db, "DB_PATH", tmp.name)
    db.reset_connection()
    db.init_db()
    return db


def _trade(tx, ts, price, size, side="BUY", outcome="Up", cond="0xc1", tok="tok1",
           slug="btc-updown-5m-1700000000"):
    return {"transactionHash": tx, "timestamp": ts, "price": str(price), "size": str(size),
            "side": side, "outcome": outcome, "conditionId": cond, "asset": tok,
            "slug": slug, "eventSlug": slug, "title": "Bitcoin Up or Down - test"}


def _fake_response(rows):
    return {"request_url": "https://fake/trades", "request_params": {},
            "request_started_at": time.time(), "response_received_at": time.time(),
            "http_status": 200, "headers": {}, "body_text": json.dumps(rows),
            "body_sha256": "x", "rows": rows, "error": None}


def test_duplicate_fill_within_same_page_preserved_as_multiplicity(monkeypatch):
    db = _fresh_db(monkeypatch)
    import leader_history_backfill as hb
    import polymarket_api as pm

    # el mismo fill aparece DOS veces en la misma respuesta: multiplicidad real
    page0 = [_trade("0xaaa", 1000, 0.5, 10), _trade("0xaaa", 1000, 0.5, 10)]

    def fake_fetch(wallet, limit=100, offset=0, bust_cache=True):
        return _fake_response(page0 if offset == 0 else [])
    monkeypatch.setattr(pm, "get_leader_trades_page_raw", fake_fetch)

    run_id, status, terminal = hb.run_backfill(leader_wallet="0xleader", page_size=100,
                                                safety_limit_pages=10)
    assert status == "completed_exhausted" and terminal == "empty_page"

    with db.connect() as conn:
        groups = conn.execute(
            """SELECT natural_key, multiplicity_total, dedup_ambiguous, count(*) c
               FROM leader_trades_v2 GROUP BY natural_key""").fetchall()
    assert len(groups) == 1
    g = groups[0]
    assert g["multiplicity_total"] == 2
    assert g["c"] == 2
    assert g["dedup_ambiguous"] == 1


def test_page_overlap_does_not_inflate_canonical_count(monkeypatch):
    db = _fresh_db(monkeypatch)
    import leader_history_backfill as hb
    import polymarket_api as pm

    tA = _trade("0xaaa1", 2000, 0.4, 5)
    tB = _trade("0xbbb1", 2001, 0.6, 3)
    tC = _trade("0xccc1", 2002, 0.3, 7)

    def fake_fetch(wallet, limit=100, offset=0, bust_cache=True):
        if offset == 0:
            rows = [tA, tB]
        elif offset == 100:
            rows = [tB, tC]  # tB se repite por solape real de paginación
        else:
            rows = []
        return _fake_response(rows)
    monkeypatch.setattr(pm, "get_leader_trades_page_raw", fake_fetch)

    run_id, status, terminal = hb.run_backfill(leader_wallet="0xleader", page_size=100,
                                                safety_limit_pages=10)
    assert status == "completed_exhausted" and terminal == "empty_page"

    with db.connect() as conn:
        n_canonical = conn.execute("SELECT count(*) c FROM leader_trades_v2").fetchone()["c"]
        n_raw = conn.execute("SELECT count(*) c FROM leader_trades_raw").fetchone()["c"]
        n_ambiguous = conn.execute(
            "SELECT count(*) c FROM leader_trades_v2 WHERE dedup_ambiguous=1").fetchone()["c"]
        n_links_tB = conn.execute(
            """SELECT count(*) c FROM leader_trade_raw_links l
               JOIN leader_trades_v2 v ON v.id = l.canonical_trade_id
               WHERE v.transaction_hash = ?""", ("0xbbb1",)).fetchone()["c"]

    assert n_canonical == 3       # tA, tB, tC -- tB no se duplica por aparecer en 2 páginas
    assert n_raw == 4             # tA + tB(pagina0) + tB(pagina1) + tC: ninguna observación se pierde
    assert n_ambiguous == 0       # tB nunca aparece 2 veces DENTRO de una misma respuesta
    assert n_links_tB == 2        # pero sus 2 observaciones crudas SÍ quedan ambas enlazadas


def test_resume_continues_from_checkpoint_without_reprocessing(monkeypatch):
    db = _fresh_db(monkeypatch)
    import leader_history_backfill as hb
    import polymarket_api as pm

    tA = _trade("0xaaa2", 3000, 0.5, 1)
    tB = _trade("0xbbb2", 3001, 0.5, 1)

    def fake_fetch(wallet, limit=100, offset=0, bust_cache=True):
        if offset == 0:
            rows = [tA]
        elif offset == 100:
            rows = [tB]
        else:
            rows = []
        return _fake_response(rows)
    monkeypatch.setattr(pm, "get_leader_trades_page_raw", fake_fetch)

    run_id, status, terminal = hb.run_backfill(leader_wallet="0xleader", page_size=100,
                                                safety_limit_pages=1)
    assert status == "completed_safety_limit"

    with db.connect() as conn:
        n_raw_before = conn.execute("SELECT count(*) c FROM leader_trades_raw").fetchone()["c"]
    assert n_raw_before == 1  # solo la pagina 0 (tA) se pidio

    run_id2, status2, terminal2 = hb.run_backfill(leader_wallet="0xleader", page_size=100,
                                                   safety_limit_pages=10, resume_run_id=run_id)
    assert run_id2 == run_id
    assert status2 == "completed_exhausted" and terminal2 == "empty_page"

    with db.connect() as conn:
        n_pages_0 = conn.execute(
            "SELECT count(*) c FROM backfill_pages WHERE run_id=? AND page_index=0", (run_id,)
        ).fetchone()["c"]
        n_raw_total = conn.execute("SELECT count(*) c FROM leader_trades_raw").fetchone()["c"]
    assert n_pages_0 == 1       # la pagina 0 NO se volvio a pedir al reanudar
    assert n_raw_total == 2     # tA (pagina0) + tB (pagina1)


def test_retries_exhausted_marks_run_failed(monkeypatch):
    db = _fresh_db(monkeypatch)
    import leader_history_backfill as hb
    import polymarket_api as pm

    def fake_fetch(wallet, limit=100, offset=0, bust_cache=True):
        return {"request_url": "https://fake", "request_params": {"offset": offset},
                "request_started_at": time.time(), "response_received_at": time.time(),
                "http_status": 503, "headers": {}, "body_text": None,
                "body_sha256": None, "rows": None, "error": "HTTP 503"}
    monkeypatch.setattr(pm, "get_leader_trades_page_raw", fake_fetch)
    monkeypatch.setattr(hb.time, "sleep", lambda s: None)

    run_id, status, terminal = hb.run_backfill(leader_wallet="0xleader", page_size=100,
                                                safety_limit_pages=10, max_retries_per_page=2)
    assert status == "failed"
    assert terminal == "retry_exhausted"


def test_documented_offset_limit_stops_without_burning_all_retries(monkeypatch):
    """HTTP 400 + 'max historical trades offset of N exceeded' (confirmado en
    vivo contra data-api) es una condición terminal evidenciada, no un fallo
    transitorio: debe detenerse en el primer intento, sin agotar reintentos,
    y marcar el run como completed_hard_limit (no 'failed')."""
    db = _fresh_db(monkeypatch)
    import leader_history_backfill as hb
    import polymarket_api as pm

    calls = {"n": 0}

    def fake_fetch(wallet, limit=100, offset=0, bust_cache=True):
        calls["n"] += 1
        if offset == 0:
            return _fake_response([_trade("0xaaa3", 4000, 0.5, 1)])
        return {"request_url": "https://fake", "request_params": {"offset": offset},
                "request_started_at": time.time(), "response_received_at": time.time(),
                "http_status": 400, "headers": {}, "body_text": '{"error":"max historical trades offset of 100 exceeded"}',
                "body_sha256": "x", "rows": None, "error": "HTTP 400"}
    monkeypatch.setattr(pm, "get_leader_trades_page_raw", fake_fetch)

    run_id, status, terminal = hb.run_backfill(leader_wallet="0xleader", page_size=100,
                                                safety_limit_pages=10, max_retries_per_page=5)
    assert status == "completed_hard_limit"
    assert terminal == "hard_limit"
    # pagina 0 (ok) + 1 solo intento en la pagina 1 (no 5) porque se detecto el limite documentado
    assert calls["n"] == 2


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
