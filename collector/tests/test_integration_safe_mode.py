"""
Integracion en modo seguro: (1) levanta el collector real ~20s sin LIVE
definido en el entorno y confirma que produce filas reales, (2) grep de
seguridad -- cero import de polymarket-client/SecureClient en todo
collector/, para que sea estructuralmente imposible que este codigo
coloque una orden.
"""
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

COLLECTOR_DIR = Path(__file__).resolve().parent.parent


def test_no_trading_client_imported_anywhere():
    forbidden = re.compile(r"\bpolymarket[_-]client\b|\bSecureClient\b|POLY_PRIVATE_KEY")
    hits = []
    for py in COLLECTOR_DIR.glob("*.py"):
        text = py.read_text()
        if forbidden.search(text):
            hits.append(py.name)
    assert not hits, f"referencias a codigo de trading encontradas en: {hits}"


def test_live_env_var_absent_is_a_noop_here():
    # Este paquete no tiene NINGUN codigo que lea LIVE -- se confirma que la
    # palabra no aparece fuera de comentarios/docstrings explicativos.
    for py in COLLECTOR_DIR.glob("*.py"):
        for i, line in enumerate(py.read_text().splitlines(), 1):
            code = line.split("#")[0]
            assert "os.environ" not in code or "LIVE" not in code, \
                f"{py.name}:{i} parece leer LIVE de env -- no deberia existir en este paquete"


def test_collector_runs_and_writes_real_rows(tmp_path):
    """Corre run_collector.py de verdad, ~20s, contra la API real de
    Polymarket (solo lectura), y confirma que aparecen filas nuevas."""
    env = os.environ.copy()
    env.pop("LIVE", None)
    env.pop("POLY_PRIVATE_KEY", None)
    db_path = tmp_path / "safe_mode.db"
    env["NORM1E69_TEST_DB_OVERRIDE"] = str(db_path)  # honored via conftest-style monkeypatch below

    # patchear config.DB_PATH del subproceso via un pequeno wrapper, ya que
    # config.py no lee env var propia para esto -- se corre con PYTHONPATH y
    # se sobreescribe el archivo tras el import usando un shim minimo.
    shim = tmp_path / "run_with_tmp_db.py"
    shim.write_text(f"""
import sys
sys.path.insert(0, {str(COLLECTOR_DIR)!r})
import config
config.DB_PATH = {str(db_path)!r}
import run_collector
sys.argv = ["run_collector.py", "--only=leader_trades,markets"]
run_collector.main()
""")

    proc = subprocess.Popen([sys.executable, str(shim)], env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    time.sleep(20)
    proc.terminate()
    try:
        out, _ = proc.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate()

    assert db_path.exists(), f"no se creo la base de datos. output:\n{out}"

    import sqlite3
    conn = sqlite3.connect(str(db_path))
    n_markets = conn.execute("SELECT count(*) FROM markets").fetchone()[0]
    n_polls = conn.execute("SELECT count(*) FROM leader_poll_log").fetchone()[0]
    conn.close()
    assert n_markets > 0, f"no se descubrio ningun mercado en 20s. output:\n{out}"
    assert n_polls > 0, f"ningun poll de leader_trades corrio en 20s. output:\n{out}"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
