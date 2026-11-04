#!/usr/bin/env python3
"""
Trackside Pulse - Worker Principal de Ingesta (Live F1 SignalR + Replay + Upstash/Local)

Modos:
  1. En vivo (oficial SignalR autenticado con cuenta F1):
     python worker/ingest.py --live --redis-url <UPSTASH_URL> --redis-token <TOKEN>
     python worker/ingest.py --live --local-port 8000
  2. Replay (simulación offline/local):
     python worker/ingest.py --replay worker/data/replay_sample.json --speed 5 --loop
"""
import argparse
import base64
import hashlib
import json
import logging
import os
import pathlib
import sys
import threading
import time
import zlib
from datetime import datetime

import requests

ROOT = pathlib.Path(__file__).resolve().parent
PUBLIC_DIR = ROOT.parent / "public"
SEASON_DATA_PATH = PUBLIC_DIR / "static" / "data" / "season_data.json"

COMPOUNDS_MAP = {
    "SOFT": "S", "MEDIUM": "M", "HARD": "H",
    "INTERMEDIATE": "I", "WET": "W", "UNKNOWN": "U"
}

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("IngestWorker")


class LiveStateStore:
    def __init__(self, pit_loss_sec=21.0):
        self.lock = threading.Lock()
        self.pit_loss_sec = pit_loss_sec
        self.session_type = "R" # R, Q, P
        self.lap = 1
        self.total_laps = 71
        self.flag = "GREEN"
        self.drivers_state = {}  # num_str -> dict
        self.ticker = []
        self.last_update_ts = int(time.time())
        self.previous_intervals = {} # num -> prev_float
        
        # Cargar season_data si existe
        self.drivers_meta = {}
        if SEASON_DATA_PATH.exists():
            try:
                sdata = json.loads(SEASON_DATA_PATH.read_text(encoding="utf-8"))
                for tla, info in sdata.get("drivers", {}).items():
                    self.drivers_meta[str(info["num"])] = tla
            except Exception as e:
                logger.warning(f"No se pudo cargar season_data: {e}")

    def update_session_info(self, data):
        with self.lock:
            stype = data.get("Type") or data.get("Name") or ""
            if "Practice" in stype:
                self.session_type = "P"
            elif "Qualifying" in stype:
                self.session_type = "Q"
            else:
                self.session_type = "R"
            
            if "TotalLaps" in data:
                try:
                    self.total_laps = int(data["TotalLaps"])
                except Exception:
                    pass

    def update_track_status(self, data):
        with self.lock:
            # Status: "1" All Clear, "2" Yellow, "4" SC, "5" Red, "6" VSC
            status = str(data.get("Status", "1"))
            if status == "1":
                self.flag = "GREEN"
            elif status == "2":
                self.flag = "YELLOW"
            elif status == "4":
                self.flag = "SC"
            elif status == "5":
                self.flag = "RED"
            elif status == "6":
                self.flag = "VSC"
            self.last_update_ts = int(time.time())

    def update_race_control(self, data):
        with self.lock:
            messages = data.get("Messages", [])
            for m in messages:
                msg_txt = m.get("Message", "")
                lap = m.get("Lap", "")
                lap_str = f"V{lap}: " if lap else ""
                formatted = f"{lap_str}{msg_txt}"[:75]
                if formatted and formatted not in self.ticker:
                    self.ticker.insert(0, formatted)
            self.ticker = self.ticker[:5]
            self.last_update_ts = int(time.time())

    def update_timing_data(self, data):
        with self.lock:
            lines = data.get("Lines", {})
            for num_str, entry in lines.items():
                cur = self.drivers_state.setdefault(num_str, {
                    "num": num_str,
                    "tla": self.drivers_meta.get(num_str, num_str),
                    "pos": 99,
                    "gap": "--",
                    "comp": "M",
                    "age": 0,
                    "in_pit": 0,
                    "trend": 0,
                    "numeric_gap": None
                })
                
                # Posición
                if "Position" in entry:
                    try: cur["pos"] = int(entry["Position"])
                    except Exception: pass

                # Gap / Interval
                interval = entry.get("IntervalToPositionAhead", {})
                gap_val = interval.get("Value")
                if gap_val is not None:
                    cur["gap"] = str(gap_val)
                    try:
                        clean_gap = float(str(gap_val).replace("+", ""))
                        # Calcular tendencia
                        prev = self.previous_intervals.get(num_str)
                        if prev is not None and cur["pos"] > 1:
                            diff = clean_gap - prev
                            if diff < -0.15: cur["trend"] = 1   # descontando
                            elif diff > 0.15: cur["trend"] = -1 # perdiendo
                            else: cur["trend"] = 0
                        self.previous_intervals[num_str] = clean_gap
                    except Exception:
                        pass
                elif cur["pos"] == 1:
                    cur["gap"] = "+0.000"

                # Estado de Pit
                if "InPit" in entry:
                    cur["in_pit"] = 1 if entry["InPit"] else 0
                if "PitOut" in entry and entry["PitOut"]:
                    cur["in_pit"] = 0

                # Neumático / Stints
                stints = entry.get("Stints", {})
                if isinstance(stints, dict) and stints:
                    latest_stint = list(stints.values())[-1]
                    comp = latest_stint.get("Compound")
                    if comp:
                        cur["comp"] = COMPOUNDS_MAP.get(comp.upper(), "U")
                    if "TotalLaps" in latest_stint:
                        cur["age"] = int(latest_stint["TotalLaps"])

                # Vuelta del líder
                if cur["pos"] == 1 and "NumberOfLaps" in entry:
                    try: self.lap = int(entry["NumberOfLaps"])
                    except Exception: pass

            self.last_update_ts = int(time.time())

    def serialize_compact(self):
        with self.lock:
            # Ordenar por posición
            sorted_drivers = sorted(self.drivers_state.values(), key=lambda d: d["pos"])
            drivers_array = []
            for d in sorted_drivers:
                # [CODE, POS, GAP, COMP, AGE, IN_PIT, TREND]
                drivers_array.append([
                    d["tla"],
                    d["pos"],
                    d["gap"],
                    d["comp"],
                    d["age"],
                    d["in_pit"],
                    d["trend"]
                ])

            return {
                "sess": self.session_type,
                "lap": self.lap,
                "total": self.total_laps,
                "flag": self.flag,
                "pit_delta": self.pit_loss_sec,
                "drivers": drivers_array,
                "ticker": list(self.ticker)
            }, self.last_update_ts


def run_live_signalr(store):
    from fastf1.livetiming.client import SignalRClient
    
    class TracksideSignalR(SignalRClient):
        def _on_message(self, msg):
            self._t_last_message = time.time()
            try:
                # msg suele ser una lista: [topic, json_string, ...]
                if isinstance(msg, list) and len(msg) >= 2:
                    topic = msg[0]
                    raw_content = msg[1]
                    parsed = json.loads(raw_content) if isinstance(raw_content, str) else raw_content
                    
                    if topic == "TimingData":
                        store.update_timing_data(parsed)
                    elif topic == "TrackStatus":
                        store.update_track_status(parsed)
                    elif topic == "RaceControlMessages":
                        store.update_race_control(parsed)
                    elif topic == "SessionInfo":
                        store.update_session_info(parsed)
            except Exception as e:
                logger.debug(f"Error procesando frame {e}")

    # Archivo dummy en memoria/temp
    client = TracksideSignalR(filename=os.devnull, timeout=0)
    logger.info("Iniciando conexión a F1 Live Timing SignalR...")
    client.start()


def push_to_upstash(redis_url, redis_token, payload, ts):
    """Guarda el payload en Upstash Redis vía REST API"""
    try:
        # Clave 'race_state'
        full_payload = {**payload, "ts": ts}
        body_str = json.dumps(full_payload, separators=(",", ":"))
        
        # Calcular hash ETag sin el campo ts
        etag_hash = hashlib.sha1(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]
        
        headers = {"Authorization": f"Bearer {redis_token}"}
        
        # SET race_state y SET race_state_hash
        requests.post(f"{redis_url}/pipeline", headers=headers, json=[
            ["SET", "race_state", body_str],
            ["SET", "race_state_hash", etag_hash],
            ["SET", "race_state_ts", str(ts)]
        ], timeout=4)
    except Exception as e:
        logger.warning(f"Error publicando en Upstash Redis: {e}")


def main():
    parser = argparse.ArgumentParser(description="Trackside Pulse - Worker de Ingesta")
    parser.add_argument("--live", action="store_true", help="Conectar a F1 Live Timing SignalR")
    parser.add_argument("--upstash-url", default=os.getenv("UPSTASH_REDIS_REST_URL"))
    parser.add_argument("--upstash-token", default=os.getenv("UPSTASH_REDIS_REST_TOKEN"))
    parser.add_argument("--pit-loss", type=float, default=21.0, help="Pérdida en boxes (ej. 21.0 para Interlagos)")
    args = parser.parse_args()

    store = LiveStateStore(pit_loss_sec=args.pit_loss)

    if args.live:
        t = threading.Thread(target=run_live_signalr, args=(store,), daemon=True)
        t.start()
        logger.info("Worker Live SignalR activo en segundo plano.")

    # Bucle de publicación a Upstash Redis cada 4 segundos
    logger.info("Iniciando bucle de publicación...")
    while True:
        try:
            payload, ts = store.serialize_compact()
            if args.upstash_url and args.upstash_token:
                push_to_upstash(args.upstash_url, args.upstash_token, payload, ts)
            
            # También persistir un archivo local para el servidor de desarrollo
            cache_file = ROOT / "data" / "live_state.json"
            cache_file.write_text(json.dumps({**payload, "ts": ts}, indent=2), encoding="utf-8")
        except Exception as e:
            logger.error(f"Error en loop de publicación: {e}")
        
        time.sleep(4)


if __name__ == "__main__":
    main()
