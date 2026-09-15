"""
tag_vpn_recovery_cohort.py: marca PRE_VPN_RECOVERY sin borrar nada, y las
filas nuevas (insertadas DESPUES de la migracion, por el proceso que sigue
corriendo) deben caer en OFFICIAL por default sin que nadie las toque.
"""
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _fresh_paper_conn(monkeypatch):
    import paper_validation_db as pdb
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    monkeypatch.setattr(pdb, "DB_PATH", tmp.name)
    pdb.reset_connection()
    pdb.init_db()
    return pdb


def _insert_market(pdb, cid, open_ts):
    with pdb.connect() as conn:
        conn.execute("""INSERT INTO paper_markets (condition_id, market_title, asset_symbol,
            token_up_id, token_down_id, open_time_utc, close_time_utc, decision_time_utc, discovered_at)
            VALUES (?,?,?,?,?,?,?,?,?)""",
            (cid, "t", "BTC", "up", "down", open_ts, open_ts + 300, open_ts + 60, 0))


def test_migration_tags_pre_and_post_recovery_correctly(monkeypatch):
    pdb = _fresh_paper_conn(monkeypatch)
    _insert_market(pdb, "0xbefore", open_ts=1000)
    _insert_market(pdb, "0xduring", open_ts=1200)   # abrio antes del corte, aunque cerca
    _insert_market(pdb, "0xafter", open_ts=1800)

    import tag_vpn_recovery_cohort as tagger
    monkeypatch.setattr(tagger, "OFFICIAL_COHORT_START_TS", 1500.0)
    monkeypatch.setattr(tagger, "VPN_OUTAGE_START_TS", 900.0)
    monkeypatch.setattr(tagger, "VPN_RECOVERY_TS", 1400.0)
    monkeypatch.setattr(tagger, "pdb", pdb)
    tagger.run()

    with pdb.connect() as conn:
        rows = {r["condition_id"]: r["validation_cohort"]
                for r in conn.execute("SELECT condition_id, validation_cohort FROM paper_markets")}
    assert rows["0xbefore"] == "PRE_VPN_RECOVERY"
    assert rows["0xduring"] == "PRE_VPN_RECOVERY"
    assert rows["0xafter"] == "OFFICIAL"
    # nada se borro
    with pdb.connect() as conn:
        assert conn.execute("SELECT count(*) c FROM paper_markets").fetchone()["c"] == 3


def test_new_rows_after_migration_default_to_official(monkeypatch):
    pdb = _fresh_paper_conn(monkeypatch)
    _insert_market(pdb, "0xold", open_ts=1000)

    import tag_vpn_recovery_cohort as tagger
    monkeypatch.setattr(tagger, "OFFICIAL_COHORT_START_TS", 1500.0)
    monkeypatch.setattr(tagger, "VPN_OUTAGE_START_TS", 900.0)
    monkeypatch.setattr(tagger, "VPN_RECOVERY_TS", 1400.0)
    monkeypatch.setattr(tagger, "pdb", pdb)
    tagger.run()

    # una fila insertada DESPUES de la migracion, con el INSERT original de
    # discover_markets (que nunca menciona validation_cohort) -- no hace
    # falta tocar run_paper_validation.py para que caiga en OFFICIAL.
    _insert_market(pdb, "0xnew", open_ts=2000)
    with pdb.connect() as conn:
        row = conn.execute("SELECT validation_cohort FROM paper_markets WHERE condition_id='0xnew'").fetchone()
    assert row["validation_cohort"] == "OFFICIAL"


def test_migration_is_idempotent(monkeypatch):
    """Correrlo dos veces no debe fallar ni duplicar nada -- por si algun
    dia se re-ejecuta sin querer."""
    pdb = _fresh_paper_conn(monkeypatch)
    _insert_market(pdb, "0xa", open_ts=1000)

    import tag_vpn_recovery_cohort as tagger
    monkeypatch.setattr(tagger, "OFFICIAL_COHORT_START_TS", 1500.0)
    monkeypatch.setattr(tagger, "pdb", pdb)
    tagger.run()
    tagger.run()  # segunda vez, no debe explotar

    with pdb.connect() as conn:
        n = conn.execute("SELECT count(*) c FROM paper_markets").fetchone()["c"]
    assert n == 1


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
