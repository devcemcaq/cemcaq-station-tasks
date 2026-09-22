#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Envía los promedios horarios del datalogger Agilaire al panel de control.

Corre cada hora en la PC de cada estación (ver scripts/run-telemetry.cmd):

1. Lee la vista `ReadingAverageDataLast30Days_001H` de la base `AVData`
   (la misma que usa libagilaire.py), las últimas LOOKBACK_HOURS horas.
2. Mueve cada sello de tiempo al FIN del intervalo (Agilaire guarda el inicio;
   CEMCAQ reporta la hora terminada: 13:00 = promedio 12:00–13:00).
3. Hace POST a `/api/v1/telemetry/ingest` del backend con la clave de la
   estación. El backend upsertea, así que reenviar horas ya enviadas sólo
   rellena huecos; no hay estado local que mantener.

Se mandan TODOS los parámetros que expone el datalogger: cuáles se grafican se
decide en el panel (Telemetría › Canales), no aquí.

Uso:
    python telemetry/push_readings.py              # corrida normal
    python telemetry/push_readings.py --dry-run    # arma el lote y no lo envía
    python telemetry/push_readings.py --hours 72   # reenviar más historia
    python telemetry/push_readings.py --from-json fixtures/agilaire_sample.json
"""
import argparse
import datetime as dt
import json
import logging
import os
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / "logs"

VIEW = "[AVData].[Dashboard].[ReadingAverageDataLast30Days_001H]"
INTERVAL_MINUTES = 60
# Límite del backend por llamada (INGEST_MAX_READINGS); se manda en lotes menores.
BATCH_SIZE = 4000
RETRIES = 3
RETRY_WAIT_SECONDS = 20
HTTP_TIMEOUT_SECONDS = 60

EXIT_OK = 0
EXIT_CONFIG = 1
EXIT_DATABASE = 2
EXIT_SEND = 3

log = logging.getLogger("telemetry")


class Config:
    def __init__(self, env: Dict[str, str]):
        def need(key: str) -> str:
            value = env.get(key, "").strip()
            if not value:
                raise ValueError("falta %s en .env" % key)
            return value

        self.station_key = need("STATION_KEY")
        self.api_url = need("DASHBOARD_API_URL").rstrip("/")
        self.server = env.get("AGILAIRE_SERVER", "").strip()
        self.database = env.get("AGILAIRE_DB", "AVData").strip()
        self.user = env.get("AGILAIRE_USER", "").strip()
        self.password = env.get("AGILAIRE_PASSWORD", "")
        self.driver = env.get("AGILAIRE_DRIVER", "SQL Server").strip()
        self.lookback_hours = int(env.get("LOOKBACK_HOURS", "26"))
        self.utc_offset = env.get("UTC_OFFSET", "-06:00").strip()
        if len(self.utc_offset) != 6 or self.utc_offset[0] not in "+-" or self.utc_offset[3] != ":":
            raise ValueError("UTC_OFFSET debe verse como -06:00")


def setup_logging() -> None:
    LOG_DIR.mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_handler = RotatingFileHandler(LOG_DIR / "telemetry.log", maxBytes=1_000_000, backupCount=5, encoding="utf-8")
    file_handler.setFormatter(fmt)
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(fmt)
    log.setLevel(logging.INFO)
    log.addHandler(file_handler)
    log.addHandler(stream_handler)


def load_config() -> Config:
    load_dotenv(ROOT / ".env")
    return Config(dict(os.environ))


# ----------------------------------------------------------------- Agilaire


def query_agilaire(cfg: Config, since: dt.datetime) -> List[Dict[str, Any]]:
    """Filas crudas de la vista horaria desde `since` (hora local del logger)."""
    import pyodbc  # sólo existe en las PCs de estación; --from-json no lo necesita

    if not cfg.server or not cfg.user:
        raise ValueError("faltan AGILAIRE_SERVER / AGILAIRE_USER en .env")
    conn_str = "Driver={%s};Server=%s;Database=%s;UID=%s;PWD=%s" % (
        cfg.driver,
        cfg.server,
        cfg.database,
        cfg.user,
        cfg.password,
    )
    query = (
        "SELECT [Date] AS Date_Time, [ReportValue], [IntervalName], [IsMissing], [IsValid], "
        "[ParameterName], [ReportedUnitName], [SiteName] FROM %s WHERE [Date] >= ? ORDER BY [Date]" % VIEW
    )
    conn = pyodbc.connect(conn_str)
    try:
        cursor = conn.cursor()
        cursor.execute(query, since)
        columns = [c[0] for c in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]
    finally:
        conn.close()


def load_rows_from_json(path: Path) -> List[Dict[str, Any]]:
    """Mismas columnas que la vista, para probar sin SQL Server."""
    with path.open(encoding="utf-8") as fh:
        rows = json.load(fh)
    for row in rows:
        row["Date_Time"] = dt.datetime.fromisoformat(row["Date_Time"])
    return rows


# ------------------------------------------------------------------ payload


def to_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes")
    return bool(value)


def rows_to_readings(rows: List[Dict[str, Any]], utc_offset: str) -> List[Dict[str, Any]]:
    # La consulta ya apunta a la vista horaria (`_001H`), así que no se filtra
    # por `IntervalName`: su texto cambia entre instalaciones ("001H",
    # "1 Hour", …) y en FEO descartaba todas las filas.
    readings = []
    for row in rows:
        end = row["Date_Time"] + dt.timedelta(hours=1)  # fin del intervalo
        missing = to_bool(row.get("IsMissing"))
        raw = row.get("ReportValue")
        value = None if missing or raw is None else float(raw)
        readings.append(
            {
                "parameter": str(row["ParameterName"]).strip(),
                "unit": (row.get("ReportedUnitName") or None),
                "measuredAt": end.strftime("%Y-%m-%dT%H:%M:%S") + utc_offset,
                "value": value,
                "isValid": to_bool(row.get("IsValid", True)),
                "isMissing": missing or value is None,
            }
        )
    return readings


def batches(readings: List[Dict[str, Any]], size: int = BATCH_SIZE):
    for i in range(0, len(readings), size):
        yield readings[i : i + size]


# --------------------------------------------------------------------- envío


def send_batch(cfg: Config, readings: List[Dict[str, Any]]) -> Dict[str, Any]:
    url = cfg.api_url + "/api/v1/telemetry/ingest"
    payload = {"intervalMinutes": INTERVAL_MINUTES, "readings": readings}
    headers = {"x-station-key": cfg.station_key, "content-type": "application/json"}
    last_error: Optional[Exception] = None
    for attempt in range(1, RETRIES + 1):
        try:
            response = requests.post(url, json=payload, headers=headers, timeout=HTTP_TIMEOUT_SECONDS)
            if response.status_code in (401, 403):
                # Clave inválida o revocada: reintentar no ayuda.
                raise RuntimeError("clave rechazada por el backend (%s): %s" % (response.status_code, response.text[:200]))
            if response.status_code >= 400:
                raise RuntimeError("HTTP %s: %s" % (response.status_code, response.text[:300]))
            return response.json()
        except RuntimeError as exc:
            if "clave rechazada" in str(exc):
                raise
            last_error = exc
        except requests.RequestException as exc:
            last_error = exc
        log.warning("intento %s/%s falló: %s", attempt, RETRIES, last_error)
        if attempt < RETRIES:
            time.sleep(RETRY_WAIT_SECONDS)
    raise RuntimeError("no se pudo enviar el lote: %s" % last_error)


# ---------------------------------------------------------------------- main


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Envía promedios horarios del datalogger al panel de control.")
    parser.add_argument("--hours", type=int, help="horas hacia atrás a reenviar (default: LOOKBACK_HOURS del .env)")
    parser.add_argument("--dry-run", action="store_true", help="arma el lote y lo imprime sin enviarlo")
    parser.add_argument("--from-json", type=Path, help="lee las filas de un JSON con las columnas de la vista en vez de SQL Server")
    # La bitácora se abre ANTES de leer los argumentos: si la tarea programada
    # (o un copiar/pegar con un comentario detrás del comando) manda basura,
    # argparse aborta, y sin esto no quedaba rastro en logs\telemetry.log.
    setup_logging()
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        extra = " ".join(argv if argv is not None else sys.argv[1:])
        if extra:
            log.error(
                "argumentos no reconocidos: %s — este script sólo acepta "
                "--hours, --dry-run y --from-json; no escribas nada más "
                "después del comando",
                extra,
            )
        raise
    try:
        cfg = load_config()
    except ValueError as exc:
        log.error("configuración: %s", exc)
        return EXIT_CONFIG

    hours = args.hours or cfg.lookback_hours
    since = dt.datetime.now().replace(minute=0, second=0, microsecond=0) - dt.timedelta(hours=hours)

    try:
        if args.from_json:
            rows = load_rows_from_json(args.from_json)
            log.info("%s filas leídas de %s", len(rows), args.from_json)
        else:
            rows = query_agilaire(cfg, since)
            log.info("%s filas leídas de %s desde %s", len(rows), cfg.server, since)
    except Exception as exc:  # pyodbc.Error no está tipado fuera de Windows
        log.error("datalogger: %s", exc)
        return EXIT_DATABASE

    readings = rows_to_readings(rows, cfg.utc_offset)
    if not readings:
        log.warning("sin lecturas horarias que enviar")
        return EXIT_OK

    parameters = sorted({r["parameter"] for r in readings})
    intervals = sorted({str(row.get("IntervalName")) for row in rows})
    log.info("%s lecturas de %s parámetros: %s (IntervalName: %s)", len(readings), len(parameters), ", ".join(parameters), ", ".join(intervals))

    if args.dry_run:
        print(json.dumps({"intervalMinutes": INTERVAL_MINUTES, "readings": readings[:5]}, indent=2, ensure_ascii=False))
        print("... (%s lecturas en total; --dry-run, no se envió nada)" % len(readings))
        return EXIT_OK

    try:
        for batch in batches(readings):
            result = send_batch(cfg, batch)
            log.info(
                "%s: %s recibidas, %s guardadas%s",
                result.get("station"),
                result.get("received"),
                result.get("stored"),
                (", canales nuevos: " + ", ".join(result["discovered"])) if result.get("discovered") else "",
            )
    except Exception as exc:
        log.error("envío: %s", exc)
        return EXIT_SEND
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
