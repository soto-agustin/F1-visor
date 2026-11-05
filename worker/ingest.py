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
        self.session_type = "R" # R, FP1, FP2, FP3, QUALY
        self.circuit_name = "Interlagos"
        self.lap = 1
        self.total_laps = 71
        self.flag = "GREEN"
        self.remaining_time = "" # Cronómetro oficial de sesión
        self.drivers_state = {}  # num_str -> dict
        self.driver_penalties = {}  # tla -> "INV" | "+5s" | "+10s" | "DT" | "SG"
        self.ticker = []
        self.last_update_ts = int(time.time())
        self.previous_intervals = {} # num -> prev_float
        
        # Meta de pilotos dinámica: num -> {"name", "tla", "team", "color", "num"}
        self.drivers_meta = {}
        self.grid_updated = False
        
        # Cargar base desde season_data si existe
        if SEASON_DATA_PATH.exists():
            try:
                sdata = json.loads(SEASON_DATA_PATH.read_text(encoding="utf-8"))
                for tla, info in sdata.get("drivers", {}).items():
                    num_str = str(info["num"])
                    self.drivers_meta[num_str] = {
                        "name": info.get("name", tla),
                        "tla": tla,
                        "team": info.get("team", "Unknown"),
                        "color": info.get("color", "#FFFFFF"),
                        "num": info.get("num")
                    }
            except Exception as e:
                logger.warning(f"No se pudo cargar season_data base: {e}")

    def update_driver_list(self, data):
        """Actualiza dinámicamente la parrilla desde el stream oficial DriverList"""
        with self.lock:
            changed = False
            for num_str, dinfo in data.items():
                if not isinstance(dinfo, dict):
                    continue
                tla = dinfo.get("Tla") or dinfo.get("BroadcastName", "")[:3].strip()
                if not tla:
                    continue
                full_name = dinfo.get("FullName") or f"{dinfo.get('FirstName', '')} {dinfo.get('LastName', '')}".strip()
                team_name = dinfo.get("TeamName") or "F1 Team"
                team_col = dinfo.get("TeamColour") or "FFFFFF"
                if not team_col.startswith("#"):
                    team_col = f"#{team_col}"
                
                prev = self.drivers_meta.get(num_str)
                if not prev or prev.get("tla") != tla or prev.get("team") != team_name:
                    self.drivers_meta[num_str] = {
                        "name": full_name or tla,
                        "tla": tla,
                        "team": team_name,
                        "color": team_col,
                        "num": int(num_str) if num_str.isdigit() else num_str
                    }
                    changed = True
                    logger.info(f"Parrilla actualizada: #{num_str} -> {tla} ({full_name}) [{team_name}]")

            if changed:
                self.grid_updated = True
                self.last_update_ts = int(time.time())
                # Persistir season_data.json local
                self._persist_season_data()

    def _persist_season_data(self):
        try:
            curr = {}
            if SEASON_DATA_PATH.exists():
                curr = json.loads(SEASON_DATA_PATH.read_text(encoding="utf-8"))
            
            drivers_dict = {}
            for num_str, d in self.drivers_meta.items():
                drivers_dict[d["tla"]] = {
                    "name": d["name"],
                    "num": d["num"],
                    "team": d["team"],
                    "color": d["color"]
                }
            curr["drivers"] = drivers_dict
            SEASON_DATA_PATH.write_text(json.dumps(curr, indent=2, ensure_ascii=False), encoding="utf-8")
            logger.info(f"season_data.json actualizado en disco con {len(drivers_dict)} pilotos.")
        except Exception as e:
            logger.warning(f"Error persistiendo season_data: {e}")

    def update_clock(self, data):
        """Actualiza el cronómetro oficial de sesión desde ExtrapolatedClock"""
        with self.lock:
            rem = data.get("Remaining")
            if rem:
                # Si viene formato "00:14:32", recortar a "14:32"
                if rem.startswith("00:"):
                    rem = rem[3:]
                self.remaining_time = rem
                self.last_update_ts = int(time.time())

    def update_session_info(self, data):
        with self.lock:
            # Detectar si es Practice (P1, P2, P3), Qualy o Race
            stype = (data.get("Type") or data.get("Name") or "").lower()
            if "practice 1" in stype or "fp1" in stype:
                self.session_type = "FP1"
            elif "practice 2" in stype or "fp2" in stype:
                self.session_type = "FP2"
            elif "practice 3" in stype or "fp3" in stype:
                self.session_type = "FP3"
            elif "qualifying" in stype or "qualy" in stype:
                self.session_type = "QUALY"
            elif "practice" in stype:
                self.session_type = "PRACTICE"
            else:
                self.session_type = "R"
            
            if "TotalLaps" in data:
                try:
                    self.total_laps = int(data["TotalLaps"])
                except Exception:
                    pass

            meeting = data.get("Meeting") or {}
            circuit = meeting.get("Circuit") or {}
            c_name = circuit.get("ShortName") or meeting.get("Name") or data.get("Circuit")
            if c_name:
                self.circuit_name = c_name

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

    def _parse_fia_message(self, msg):
        up = msg.upper()
        mentioned_tlas = set()
        for num_str, meta in self.drivers_meta.items():
            tla = meta.get("tla")
            num = str(meta.get("num"))
            if tla and (f"({tla})" in up or f" {tla} " in up):
                mentioned_tlas.add(tla)
            elif num and (f"CAR {num}" in up or f"CARS {num}" in up or f"#{num}" in up):
                mentioned_tlas.add(tla)

        if not mentioned_tlas:
            return

        for tla in mentioned_tlas:
            if "PENALTY SERVED" in up or "TIME PENALTY SERVED" in up:
                self.driver_penalties.pop(tla, None)
                logger.info(f"FIA: Sanción cumplida para {tla}")
            elif "NO FURTHER ACTION" in up or "NO INVESTIGATION" in up:
                if self.driver_penalties.get(tla) == "INV":
                    self.driver_penalties.pop(tla, None)
                    logger.info(f"FIA: Sin acción posterior para {tla}")
            elif "PENALTY" in up:
                if "5 SEC" in up:
                    self.driver_penalties[tla] = "+5s"
                elif "10 SEC" in up:
                    self.driver_penalties[tla] = "+10s"
                elif "DRIVE THROUGH" in up or "DRIVE-THROUGH" in up:
                    self.driver_penalties[tla] = "DT"
                elif "STOP" in up and "GO" in up:
                    self.driver_penalties[tla] = "10s SG"
                else:
                    self.driver_penalties[tla] = "+5s"
                logger.info(f"FIA Sanción: {tla} -> {self.driver_penalties[tla]}")
            elif "INVESTIGATION" in up or "UNDER INVESTIGATION" in up or "NOTED" in up:
                if not self.driver_penalties.get(tla, "").startswith("+"):
                    self.driver_penalties[tla] = "INV"
                    logger.info(f"FIA Investigación: {tla} -> INV")

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
                self._parse_fia_message(msg_txt)
            self.ticker = self.ticker[:5]
            self.last_update_ts = int(time.time())

    def update_timing_data(self, data):
        with self.lock:
            lines = data.get("Lines", {})
            for num_str, entry in lines.items():
                meta = self.drivers_meta.get(num_str, {})
                tla = meta.get("tla") or num_str
                
                cur = self.drivers_state.setdefault(num_str, {
                    "num": num_str,
                    "tla": tla,
                    "pos": 99,
                    "gap": "--",
                    "best_lap": "--",
                    "gap_p1": "--",
                    "comp": "M",
                    "age": 0,
                    "in_pit": 0,
                    "trend": 0,
                    "laps": 0
                })
                # Asegurar TLA actualizado si llegó DriverList posterior
                if tla != num_str:
                    cur["tla"] = tla

                # Posición
                if "Position" in entry:
                    try: cur["pos"] = int(entry["Position"])
                    except Exception: pass

                # Vueltas completadas
                if "NumberOfLaps" in entry:
                    try: cur["laps"] = int(entry["NumberOfLaps"])
                    except Exception: pass

                # Mejor vuelta (BestLapTime) para Prácticas y Qualy
                best_lap_info = entry.get("BestLapTime")
                if isinstance(best_lap_info, dict) and "Value" in best_lap_info:
                    cur["best_lap"] = best_lap_info["Value"]

                # Gap al líder (GapToLeader)
                if "GapToLeader" in entry:
                    cur["gap_p1"] = str(entry["GapToLeader"])

                # Gap / Interval al auto de adelante (para Carrera)
                interval = entry.get("IntervalToPositionAhead", {})
                gap_val = interval.get("Value")
                if gap_val is not None:
                    cur["gap"] = str(gap_val)
                    try:
                        clean_gap = float(str(gap_val).replace("+", ""))
                        prev = self.previous_intervals.get(num_str)
                        if prev is not None and cur["pos"] > 1:
                            diff = clean_gap - prev
                            if diff < -0.15: cur["trend"] = 1
                            elif diff > 0.15: cur["trend"] = -1
                            else: cur["trend"] = 0
                        self.previous_intervals[num_str] = clean_gap
                    except Exception:
                        pass
                elif cur["pos"] == 1:
                    cur["gap"] = "+0.000"
                    cur["gap_p1"] = "+0.000"

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
            drivers_array = []
            is_race = self.session_type in ("R", "CARRERA")
            penalties_dict = {tla: pen for tla, pen in self.driver_penalties.items() if pen}
            
            if self.drivers_state:
                # Ordenar por posición oficial en pista
                sorted_drivers = sorted(self.drivers_state.values(), key=lambda d: d["pos"])
                for d in sorted_drivers:
                    tla = d["tla"]
                    fia = penalties_dict.get(tla, "")
                    if is_race:
                        # En Carrera: [CODE, POS, GAP_ANT, TYRE, AGE, IN_PIT, TREND, FIA]
                        drivers_array.append([
                            tla,
                            d["pos"],
                            d["gap"],
                            d["comp"],
                            d["age"],
                            d["in_pit"],
                            d["trend"],
                            fia
                        ])
                    else:
                        # En FP / Qualy: [CODE, POS, BEST_LAP, GAP_P1, TYRE, LAPS_TOTAL, IN_PIT, FIA]
                        drivers_array.append([
                            tla,
                            d["pos"],
                            d["best_lap"] if d["best_lap"] != "--" else d["gap"],
                            d["gap_p1"] if d["gap_p1"] != "--" else d["gap"],
                            d["comp"],
                            d["laps"] if d["laps"] > 0 else d["age"],
                            d["in_pit"],
                            fia
                        ])
            elif self.drivers_meta:
                # Standby (antes de iniciar la tanda en pista):
                # Generar la grilla completa con los 22 pilotos oficiales 2026
                p_idx = 1
                for num_str, dmeta in self.drivers_meta.items():
                    tla = dmeta["tla"]
                    fia = penalties_dict.get(tla, "")
                    if is_race:
                        drivers_array.append([tla, p_idx, "--", "M", 0, 0, 0, fia])
                    else:
                        drivers_array.append([tla, p_idx, "--", "--", "M", 0, 0, fia])
                    p_idx += 1

            ticker_items = list(self.ticker)
            if not ticker_items:
                ticker_items = ["Sintonizando F1 Live Timing oficial...", "En espera de inicio de sesión en pista"]

            # Incluir metadatos de pilotos y sanciones
            payload = {
                "sess": self.session_type,
                "circuit": self.circuit_name,
                "lap": self.lap,
                "total": self.total_laps,
                "flag": self.flag,
                "remaining": self.remaining_time,
                "pit_delta": self.pit_loss_sec,
                "drivers": drivers_array,
                "penalties": penalties_dict,
                "ticker": ticker_items
            }
            
            # Enviar metadatos de pilotos para que el cliente siempre tenga dorsales y colores frescos
            drivers_meta_client = {}
            for n, info in self.drivers_meta.items():
                drivers_meta_client[info["tla"]] = {
                    "name": info["name"],
                    "num": info["num"],
                    "team": info["team"],
                    "color": info["color"]
                }
            payload["grid_meta"] = drivers_meta_client

            return payload, int(time.time())


def run_live_signalr(store):
    from fastf1.livetiming.client import SignalRClient
    from signalrcore.hub_connection_builder import HubConnectionBuilder
    
    class TracksideSignalR(SignalRClient):
        def _run(self):
            # Pre-negociar cookie AWSALBCORS
            r = requests.options(self._negotiate_url, headers=self.headers)
            if 'AWSALBCORS' in r.cookies:
                self.headers.update({"Cookie": f"AWSALBCORS={r.cookies['AWSALBCORS']}"})

            options = {
                "verify_ssl": True,
                "access_token_factory": self._access_token_factory(),
                "headers": self.headers
            }

            # Configurar con auto-reconnect infinito en caso de microcortes
            self._connection = HubConnectionBuilder() \
                .with_url(self._connection_url, options=options) \
                .with_automatic_reconnect({
                    "type": "raw",
                    "keep_alive_interval": 15,
                    "reconnect_interval": 5,
                    "max_attempts": None  # Reintentos infinitos
                }) \
                .configure_logging(logging.WARNING) \
                .build()

            self._connection.on_open(self._on_connect)
            self._connection.on_close(self._on_close)
            self._connection.on_reconnect(lambda: logger.info("SignalR reconectado automáticamente."))
            self._connection.on("feed", self._on_message)

            self._connection.start()

            while not self._is_connected:
                time.sleep(0.1)

            logger.info("Suscrito a todos los tópicos de F1 Live Timing.")
            self._connection.send("Subscribe", [self.topics], on_invocation=self._on_message)

        def _on_message(self, msg):
            self._t_last_message = time.time()
            try:
                if isinstance(msg, list) and len(msg) >= 2:
                    topic = msg[0]
                    raw_content = msg[1]
                    parsed = json.loads(raw_content) if isinstance(raw_content, str) else raw_content
                    
                    if topic == "TimingData":
                        store.update_timing_data(parsed)
                    elif topic == "DriverList":
                        store.update_driver_list(parsed)
                    elif topic == "ExtrapolatedClock":
                        store.update_clock(parsed)
                    elif topic == "TrackStatus":
                        store.update_track_status(parsed)
                    elif topic == "RaceControlMessages":
                        store.update_race_control(parsed)
                    elif topic == "SessionInfo":
                        store.update_session_info(parsed)
            except Exception as e:
                logger.debug(f"Error procesando frame: {e}")

    client = TracksideSignalR(filename=os.devnull, timeout=0)
    logger.info("Iniciando conexión a F1 Live Timing SignalR con Auto-Reconnect...")
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
    # Cargar .env prioritariamente antes de parsear argumentos
    env_file = ROOT.parent / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

    parser = argparse.ArgumentParser(description="Trackside Pulse - Worker de Ingesta")
    parser.add_argument("--live", action="store_true", help="Conectar a F1 Live Timing SignalR")
    parser.add_argument("--upstash-url", default=os.getenv("UPSTASH_REDIS_REST_URL"))
    parser.add_argument("--upstash-token", default=os.getenv("UPSTASH_REDIS_REST_TOKEN"))
    parser.add_argument("--pit-loss", type=float, default=21.0, help="Pérdida en boxes (ej. 21.0 para Interlagos)")
    parser.add_argument("--replay", type=str, default=None, help="Ruta al archivo JSON de replay a simular en Upstash")
    parser.add_argument("--speed", type=float, default=5.0, help="Velocidad del replay (ej: 5.0)")
    parser.add_argument("--loop", action="store_true", help="Repetir replay al terminar")
    args = parser.parse_args()

    url = args.upstash_url or os.getenv("UPSTASH_REDIS_REST_URL")
    token = args.upstash_token or os.getenv("UPSTASH_REDIS_REST_TOKEN")

    store = LiveStateStore(pit_loss_sec=args.pit_loss)

    # Modo Replay transmitido directo a Upstash Redis para pruebas en vivo
    if args.replay:
        replay_path = pathlib.Path(args.replay)
        if not replay_path.exists():
            logger.error(f"Archivo de replay no encontrado: {replay_path}")
            sys.exit(1)
        data = json.loads(replay_path.read_text(encoding="utf-8"))
        frames = data["frames"]
        logger.info(f"Iniciando simulación de carrera hacia Upstash Redis: {len(frames)} frames a x{args.speed}...")

        if not url or not token:
            logger.error("Se requiere UPSTASH_REDIS_REST_URL y UPSTASH_REDIS_REST_TOKEN")
            sys.exit(1)

        step_delay = (frames[1]["t"] - frames[0]["t"]) / args.speed if len(frames) > 1 else 1.0

        while True:
            for f in frames:
                payload = dict(f["p"])
                payload["pit_delta"] = args.pit_loss
                if "circuit" not in payload:
                    payload["circuit"] = data.get("meta", {}).get("circuit", "Interlagos")
                if "penalties" not in payload:
                    payload["penalties"] = {}
                ts = int(time.time())
                push_to_upstash(url, token, payload, ts)
                n_d = len(payload.get("drivers", []))
                logger.info(f"[SIMULACIÓN] Vuelta {payload.get('lap')}/{payload.get('total')} -> Upstash OK ({n_d} pilotos)")
                time.sleep(step_delay)
            if not args.loop:
                break
        return

    if args.live:
        t = threading.Thread(target=run_live_signalr, args=(store,), daemon=True)
        t.start()
        logger.info("Worker Live SignalR activo en segundo plano.")

    # Bucle de publicación a Upstash Redis cada 4 segundos
    if url and token:
        logger.info(f"Conectado a Upstash Redis: {url}")
    else:
        logger.warning("ATENCIÓN: UPSTASH_REDIS_REST_URL o TOKEN no configurados.")

    logger.info("Iniciando bucle de publicación...")
    while True:
        try:
            payload, ts = store.serialize_compact()
            if url and token:
                push_to_upstash(url, token, payload, ts)
                n_drv = len(payload.get("drivers", []))
                s_name = payload.get("sess", "LIVE")
                logger.info(f"[NUBE] Upstash sincronizado -> {s_name} | {n_drv} pilotos en grilla | ts={ts}")
            
            # También persistir un archivo local para el servidor de desarrollo
            cache_file = ROOT / "data" / "live_state.json"
            cache_file.write_text(json.dumps({**payload, "ts": ts}, indent=2), encoding="utf-8")
        except Exception as e:
            logger.error(f"Error en loop de publicación: {e}")
        
        time.sleep(4)


if __name__ == "__main__":
    main()
