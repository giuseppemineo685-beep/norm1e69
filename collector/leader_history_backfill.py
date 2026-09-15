"""
Backfill histórico controlado del líder (Fase 1) -- separado por completo de
`backfill.py` (que sigue existiendo tal cual, sin tocar) y del poller en vivo
`leader_trades_collector.py` (tampoco se toca: sigue escribiendo únicamente en
la tabla vieja `leader_trades`).

Este módulo pagina hacia atrás con `offset` contra data-api/trades hasta una
condición terminal EVIDENCIADA (no un tope arbitrario):
  - página vacía
  - página repetida (misma huella que la anterior)
  - reintentos agotados tras backoff acotado (falla de la API, no exhaustividad)
  - safety_limit_pages alcanzado (circuit breaker, se reporta como NO probado)

Cada observación cruda (cada fila de cada página, de cada intento, incluidos
solapes y reintentos) se guarda PERMANENTEMENTE en `leader_trades_raw` -- nunca
se descarta ni se colapsa en la ingesta. La deduplicación es un paso separado
y reconstruible (`canonicalize`) que NO confía solo en una UNIQUE de SQL:
dos fills legítimos pueden compartir tx_hash+timestamp+token+side+price+shares
(ver docstring de `canonicalize`).
"""
import argparse
import hashlib
import json
import time
from collections import defaultdict

import db
import polymarket_api as pm
from config import (LEADER_WALLET, HISTORY_BACKFILL_SAFETY_LIMIT_PAGES,
                     HISTORY_BACKFILL_MAX_RETRIES_PER_PAGE)

ORIGIN = "DATA_API_HISTORY"


def _natural_key(wallet, tx_hash, condition_id, token_id, side, price, shares, ts):
    """La identidad más fuerte disponible en la respuesta pública (no hay `id`
    de fill: confirmado leyendo raw_payload real, data-api no lo expone).
    Dos fills legítimos PUEDEN compartir esta clave exacta -- eso es multiplicidad
    real, no un bug; se resuelve en canonicalize(), no acá."""
    return "|".join([
        wallet or "", tx_hash or "", condition_id or "", token_id or "", side or "",
        f"{price:.6f}", f"{shares:.6f}", f"{ts:.0f}",
    ])


def _page_fingerprint(rows):
    """Huella del CONTENIDO parseado de una página completa, en orden. Un
    repeat exacto de esta huella entre páginas consecutivas es evidencia de
    haber llegado al final de la historia (offset ya no avanza contenido
    nuevo) -- un solape PARCIAL entre páginas vecinas (normal en paginación)
    da una huella distinta y no dispara esta condición terminal."""
    keys = []
    for t in rows:
        try:
            keys.append((
                t["transactionHash"], t.get("conditionId"), t.get("asset"), t.get("side"),
                round(float(t["price"]), 6), round(float(t["size"]), 6), float(t["timestamp"]),
            ))
        except (KeyError, ValueError, TypeError):
            keys.append(("__unparsed__", json.dumps(t, sort_keys=True, default=str)))
    return hashlib.sha256(json.dumps(keys, default=str).encode()).hexdigest()


def _ingest_page_rows(conn, page_id, origin, leader_wallet, rows, fetched_at):
    """Inserta CADA fila de la página en leader_trades_raw, sin deduplicar
    nada acá -- el solape entre páginas y los reintentos generan múltiples
    filas crudas a propósito; eso se reconcilia después en canonicalize()."""
    n_ok = 0
    for pos, t in enumerate(rows):
        try:
            tx_hash = t["transactionHash"]
            condition_id = t.get("conditionId")
            token_id = t.get("asset")
            side = t.get("side")
            price = float(t["price"])
            shares = float(t["size"])
            ts = float(t["timestamp"])
        except (KeyError, ValueError, TypeError) as e:
            db.log_event("leader_history_backfill", "row_parse_error",
                         {"page_id": page_id, "position": pos, "error": str(e)})
            continue
        nk = _natural_key(leader_wallet, tx_hash, condition_id, token_id, side, price, shares, ts)
        conn.execute(
            """INSERT INTO leader_trades_raw
               (page_id, origin, row_position_in_page, natural_key, parsed_payload, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (page_id, origin, pos, nk, json.dumps(t, default=str, sort_keys=True), fetched_at),
        )
        n_ok += 1
    return n_ok


def _documented_offset_limit(http_status, body_text):
    """Detecta el límite duro real de data-api/trades: HTTP 400 con un cuerpo
    que documenta explícitamente el tope de offset (confirmado en vivo:
    `{"error":"max historical trades offset of 10000 exceeded"}` a partir de
    offset=10100). Es una condición terminal EVIDENCIADA, no un fallo de red --
    reintentar no la resuelve, así que se detecta antes de agotar reintentos."""
    if http_status != 400 or not body_text:
        return None
    low = body_text.lower()
    if "offset" in low and ("exceeded" in low or "max" in low or "limit" in low):
        return body_text.strip()
    return None


def _fetch_page_with_retries(leader_wallet, run_id, page_index, offset, page_size, max_retries):
    """Reintentos acotados con backoff. Cada intento (exitoso o no) se loguea
    en backfill_pages -- así un reintento nunca queda invisible, y agotar
    los reintentos es en sí mismo una condición terminal (no un salto de página).

    La numeración de attempt_number CONTINÚA entre corridas: si esta página ya
    falló y se agotó en una corrida anterior (terminal_condition='retry_exhausted')
    y se reanuda con --resume, no se reinicia en 1 -- chocaría contra el UNIQUE
    (run_id, page_index, attempt_number) de los intentos ya guardados."""
    with db.connect() as conn:
        prior = conn.execute(
            "SELECT COALESCE(max(attempt_number), 0) m FROM backfill_pages WHERE run_id=? AND page_index=?",
            (run_id, page_index)).fetchone()["m"]
    for attempt in range(prior + 1, prior + max_retries + 1):
        raw = pm.get_leader_trades_page_raw(leader_wallet, limit=page_size, offset=offset)
        fp = _page_fingerprint(raw["rows"]) if raw["rows"] is not None else None
        headers = raw["headers"] or {}
        age = headers.get("age") or headers.get("Age")
        try:
            age = float(age) if age is not None else None
        except (TypeError, ValueError):
            age = None
        with db.connect() as conn:
            cur = conn.execute(
                """INSERT INTO backfill_pages
                   (run_id, page_index, attempt_number, offset_requested, limit_requested,
                    request_url, request_params_json, request_started_at, response_received_at,
                    http_status, cache_age_s, cache_status, response_headers_json,
                    response_body_raw, response_body_sha256, normalized_page_fingerprint,
                    n_rows_parsed, error)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (run_id, page_index, attempt, offset, page_size,
                 raw["request_url"], json.dumps(raw["request_params"]),
                 raw["request_started_at"], raw["response_received_at"],
                 raw["http_status"], age, headers.get("x-cache") or headers.get("cf-cache-status"),
                 json.dumps(dict(headers)), raw["body_text"], raw["body_sha256"], fp,
                 len(raw["rows"]) if raw["rows"] is not None else None, raw["error"]),
            )
            page_id = cur.lastrowid
        ok = raw["error"] is None and raw["rows"] is not None
        if ok:
            return page_id, raw["rows"], fp, None
        hard_limit = _documented_offset_limit(raw["http_status"], raw["body_text"])
        if hard_limit:
            db.log_event("leader_history_backfill", "documented_hard_limit",
                         {"page_index": page_index, "offset": offset, "body": hard_limit})
            return None, None, None, hard_limit
        db.log_event("leader_history_backfill", "page_retry",
                     {"page_index": page_index, "attempt": attempt, "error": raw["error"]})
        if attempt < prior + max_retries:
            time.sleep(min(2 ** (attempt - prior - 1), 30))
    return None, None, None, None


def run_backfill(leader_wallet=LEADER_WALLET, page_size=100,
                  safety_limit_pages=HISTORY_BACKFILL_SAFETY_LIMIT_PAGES,
                  max_retries_per_page=HISTORY_BACKFILL_MAX_RETRIES_PER_PAGE,
                  resume_run_id=None):
    db.init_db()
    now = time.time()

    if resume_run_id:
        with db.connect() as conn:
            row = conn.execute("SELECT * FROM backfill_runs WHERE run_id=?", (resume_run_id,)).fetchone()
        if not row:
            raise ValueError(f"run_id desconocido: {resume_run_id}")
        run_id = resume_run_id
        start_page_index = (row["last_offset_completed"] // page_size + 1) \
            if row["last_offset_completed"] is not None else 0
        pages_succeeded, pages_failed = row["pages_succeeded"], row["pages_failed"]
        with db.connect() as conn:
            last = conn.execute(
                """SELECT normalized_page_fingerprint FROM backfill_pages
                   WHERE run_id=? AND error IS NULL
                   ORDER BY page_index DESC, attempt_number DESC LIMIT 1""",
                (run_id,)).fetchone()
        prev_fingerprint = last["normalized_page_fingerprint"] if last else None
        print(f"reanudando run {run_id} desde pagina {start_page_index} (offset {start_page_index*page_size})")
    else:
        run_id = f"hist_{leader_wallet[-6:]}_{int(now)}"
        start_page_index = 0
        pages_succeeded = pages_failed = 0
        prev_fingerprint = None
        with db.connect() as conn:
            conn.execute(
                """INSERT INTO backfill_runs (run_id, origin, leader_wallet, started_at, status,
                   page_size, safety_limit_pages, pages_requested, pages_succeeded, pages_failed)
                   VALUES (?, ?, ?, ?, 'running', ?, ?, 0, 0, 0)""",
                (run_id, ORIGIN, leader_wallet, now, page_size, safety_limit_pages))
        print(f"nuevo run {run_id}")

    status = terminal_condition = None
    page_index = start_page_index
    while True:
        if page_index >= safety_limit_pages:
            status, terminal_condition = "completed_safety_limit", None
            break

        offset = page_index * page_size
        page_id, rows, fp, hard_limit = _fetch_page_with_retries(
            leader_wallet, run_id, page_index, offset, page_size, max_retries_per_page)

        if page_id is None:
            pages_failed += 1
            if hard_limit:
                status, terminal_condition = "completed_hard_limit", "hard_limit"
                notes = hard_limit
            else:
                status, terminal_condition = "failed", "retry_exhausted"
                notes = None
            with db.connect() as conn:
                conn.execute(
                    "UPDATE backfill_runs SET pages_failed=?, notes=? WHERE run_id=?",
                    (pages_failed, notes, run_id))
            break

        pages_succeeded += 1
        with db.connect() as conn:
            n_ingested = _ingest_page_rows(conn, page_id, ORIGIN, leader_wallet, rows, time.time())
            conn.execute(
                """UPDATE backfill_runs SET last_offset_completed=?, pages_requested=?,
                   pages_succeeded=?, pages_failed=? WHERE run_id=?""",
                (offset, page_index + 1, pages_succeeded, pages_failed, run_id))
        print(f"pagina {page_index} (offset {offset}): {len(rows)} filas, {n_ingested} ingresadas")

        if not rows:
            status, terminal_condition = "completed_exhausted", "empty_page"
            break
        if prev_fingerprint is not None and fp == prev_fingerprint:
            status, terminal_condition = "completed_exhausted", "repeated_page"
            break
        prev_fingerprint = fp
        page_index += 1

    with db.connect() as conn:
        conn.execute(
            "UPDATE backfill_runs SET finished_at=?, status=?, terminal_condition=? WHERE run_id=?",
            (time.time(), status, terminal_condition, run_id))
    print(f"run {run_id} terminado: status={status} terminal_condition={terminal_condition} "
          f"paginas_ok={pages_succeeded} paginas_fallidas={pages_failed}")

    n_canonical, n_ambiguous = canonicalize(leader_wallet, ORIGIN)
    print(f"canonicalizacion: {n_canonical} trades canonicos, {n_ambiguous} grupos ambiguos (multiplicidad>1)")
    return run_id, status, terminal_condition


def canonicalize(leader_wallet=LEADER_WALLET, origin=ORIGIN):
    """Reconstruye leader_trades_v2 + leader_trade_raw_links desde leader_trades_raw
    (el ledger crudo es la fuente de verdad; esto es DERIVADO y se puede borrar y
    recalcular con seguridad, a diferencia de todo lo demás en el schema).

    Reconciliación multiset (no una UNIQUE de SQL):
      1. Se agrupan las filas crudas por natural_key.
      2. Dentro de cada grupo, se cuenta cuántas veces aparece esa natural_key
         DENTRO DE UNA MISMA respuesta (una página/intento) -- multiplicidad local.
      3. multiplicity_total = el MÁXIMO de esa multiplicidad local visto en
         cualquier respuesta individual. Esto es clave: si la misma natural_key
         aparece 1 vez en la página A y 1 vez en la página B (solape de
         paginación), multiplicity_total sigue siendo 1 -- el solape NUNCA
         infla el conteo canónico. Si aparece 2 veces DENTRO de la misma
         respuesta, multiplicity_total=2: son fills legítimos distintos que
         comparten todos los campos observables, y no hay forma de diferenciarlos
         con certeza -- se crean 2 trades canónicos y ambos quedan
         dedup_ambiguous=1.
      4. La página que alcanzó ese máximo ("testigo") ancla la multiplicidad;
         sus filas se enlazan 1:1 (por posición) a los N trades canónicos con
         link_reason='witness'. Cualquier otra página/intento que también haya
         observado esa natural_key enlaza sus filas como 'repeat_observation' --
         no crean trades nuevos, solo prueban que la observación se repitió.
      5. NINGUNA fila cruda queda sin enlazar: cada una termina apuntando a
         al menos un trade canónico.
    """
    now = time.time()
    with db.connect() as conn:
        raw_rows = conn.execute(
            """SELECT id, page_id, natural_key, parsed_payload, row_position_in_page
               FROM leader_trades_raw WHERE origin = ?""", (origin,)).fetchall()
        page_meta = {
            r["id"]: {"page_index": r["page_index"], "attempt_number": r["attempt_number"], "run_id": r["run_id"]}
            for r in conn.execute("SELECT id, page_index, attempt_number, run_id FROM backfill_pages").fetchall()
        }

        by_key = defaultdict(list)
        for r in raw_rows:
            by_key[r["natural_key"]].append(dict(r))

        conn.execute(
            """DELETE FROM leader_trade_raw_links WHERE canonical_trade_id IN
               (SELECT id FROM leader_trades_v2 WHERE origin=? AND leader_wallet=?)""",
            (origin, leader_wallet))
        conn.execute(
            "DELETE FROM leader_trades_v2 WHERE origin=? AND leader_wallet=?",
            (origin, leader_wallet))

        n_canonical = 0
        n_ambiguous_groups = 0
        for natural_key, rows in by_key.items():
            per_page = defaultdict(list)
            for r in rows:
                per_page[r["page_id"]].append(r)
            multiplicity_total = max(len(v) for v in per_page.values())
            witness_page_id = min(
                (pid for pid, v in per_page.items() if len(v) == multiplicity_total),
                key=lambda pid: (page_meta[pid]["page_index"], page_meta[pid]["attempt_number"]),
            )
            ambiguous = 1 if multiplicity_total > 1 else 0
            if ambiguous:
                n_ambiguous_groups += 1

            sample = json.loads(rows[0]["parsed_payload"])
            market_slug = sample.get("slug") or sample.get("eventSlug")
            asset_symbol = market_slug.split("-")[0].upper() if market_slug else None
            run_id = page_meta[witness_page_id]["run_id"]

            canonical_ids = []
            for idx in range(1, multiplicity_total + 1):
                cur = conn.execute(
                    """INSERT INTO leader_trades_v2
                       (leader_wallet, transaction_hash, condition_id, market_slug, market_title,
                        asset_symbol, token_id, outcome, side, price, shares, usdc_amount,
                        source_timestamp_utc, natural_key, multiplicity_index, multiplicity_total,
                        dedup_ambiguous, origin, run_id, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (leader_wallet, sample.get("transactionHash"), sample.get("conditionId"),
                     market_slug, sample.get("title"), asset_symbol, sample.get("asset"),
                     sample.get("outcome"), sample.get("side"), float(sample["price"]),
                     float(sample["size"]), float(sample["price"]) * float(sample["size"]),
                     float(sample["timestamp"]), natural_key, idx, multiplicity_total,
                     ambiguous, origin, run_id, now),
                )
                canonical_ids.append(cur.lastrowid)
                n_canonical += 1

            witness_rows = sorted(per_page[witness_page_id], key=lambda r: r["row_position_in_page"])
            for slot_idx, raw_row in zip(range(multiplicity_total), witness_rows):
                conn.execute(
                    """INSERT OR IGNORE INTO leader_trade_raw_links
                       (canonical_trade_id, raw_row_id, link_reason) VALUES (?, ?, 'witness')""",
                    (canonical_ids[slot_idx], raw_row["id"]))

            for pid, prows in per_page.items():
                if pid == witness_page_id:
                    continue
                prows_sorted = sorted(prows, key=lambda r: r["row_position_in_page"])
                for slot_idx, raw_row in zip(range(multiplicity_total), prows_sorted):
                    conn.execute(
                        """INSERT OR IGNORE INTO leader_trade_raw_links
                           (canonical_trade_id, raw_row_id, link_reason) VALUES (?, ?, 'repeat_observation')""",
                        (canonical_ids[slot_idx], raw_row["id"]))

    return n_canonical, n_ambiguous_groups


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--resume", help="run_id a continuar desde su checkpoint")
    p.add_argument("--page-size", type=int, default=100)
    p.add_argument("--safety-limit-pages", type=int, default=HISTORY_BACKFILL_SAFETY_LIMIT_PAGES)
    p.add_argument("--max-retries", type=int, default=HISTORY_BACKFILL_MAX_RETRIES_PER_PAGE)
    p.add_argument("--canonicalize-only", action="store_true", help="solo recalcular v2 desde el crudo")
    args = p.parse_args()

    db.init_db()
    if args.canonicalize_only:
        n, amb = canonicalize()
        print(f"{n} trades canonicos, {amb} grupos ambiguos")
    else:
        run_backfill(page_size=args.page_size, safety_limit_pages=args.safety_limit_pages,
                     max_retries_per_page=args.max_retries, resume_run_id=args.resume)
