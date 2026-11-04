#!/usr/bin/env python3
"""
Trackside Pulse - Grabador de replay desde OpenF1 (API pública, histórico gratis, sin API key).

Descarga una sesión real ya disputada y genera un replay compacto con el formato
de payload del spec (claves abreviadas + arrays posicionales), más el campo extra
"sess" (R = carrera, Q = qualy, P = práctica).

Uso:
    python worker/openf1_record.py --session-key 11404 --step 10
    python worker/openf1_record.py --session-key 11404 --write-season   # también regenera season_data.json

Respeta el límite gratuito de OpenF1 (3 req/s, 30 req/min).
"""
import argparse
import bisect
import json
import pathlib
import sys
import time
from datetime import datetime

import requests

BASE = "https://api.openf1.org/v1/"
ROOT = pathlib.Path(__file__).resolve().parent
RAW_DIR = ROOT / "data" / "raw"
SEASON_PATH = ROOT.parent / "public" / "static" / "data" / "season_data.json"

COMPOUNDS = {"SOFT": "S", "MEDIUM": "M", "HARD": "H", "INTERMEDIATE": "I", "WET": "W"}
DEFAULT_PIT_LANE_SEC = 22.0
MAX_SANE_PIT_LANE_SEC = 120.0
TREND_LOOKBACK_FRAMES = 3
TREND_THRESHOLD_SEC = 0.15


def ts(s):
    return datetime.fromisoformat(s).timestamp()


def api_get(endpoint, **params):
    """GET con reintentos. 404 'No results found' = lista vacía."""
    for attempt in range(6):
        r = requests.get(BASE + endpoint, params=params, timeout=180)
        if r.status_code == 200:
            time.sleep(2.2)  # <= 30 req/min
            return r.json()
        if r.status_code == 404:
            time.sleep(2.2)
            return []
        wait = 5 * (attempt + 1)
        print(f"  {endpoint}: HTTP {r.status_code}, reintento en {wait}s", file=sys.stderr)
        time.sleep(wait)
    raise RuntimeError(f"OpenF1 {endpoint} falló tras varios reintentos")


def load_raw(session_key, refresh=False):
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    cache = RAW_DIR / f"{session_key}.json"
    if cache.exists() and not refresh:
        return json.loads(cache.read_text(encoding="utf-8"))
    raw = {}
    for ep in ("sessions", "drivers", "laps", "stints", "pit", "position", "intervals", "race_control"):
        print(f"Descargando {ep}...")
        raw[ep] = api_get(ep, session_key=session_key)
        print(f"  {len(raw[ep])} filas")
    cache.write_text(json.dumps(raw), encoding="utf-8")
    return raw


def fmt_delta(value, is_leader):
    if is_leader:
        return "+0.000"
    if isinstance(value, (int, float)):
        return f"+{value:.3f}"
    if isinstance(value, str) and value:
        return value if value.startswith("+") else f"+{value}"
    return "--"  # sin dato todavía (primeros segundos de carrera)


def build_frames(raw, step):
    session = raw["sessions"][0]
    drivers = {d["driver_number"]: d for d in raw["drivers"]}
    code = {n: d["name_acronym"] for n, d in drivers.items()}

    laps = {}
    for row in raw["laps"]:
        if row.get("date_start"):
            laps.setdefault(row["driver_number"], []).append((ts(row["date_start"]), row["lap_number"]))
    for lst in laps.values():
        lst.sort()
    lap_times = {n: [t for t, _ in lst] for n, lst in laps.items()}

    def lap_at(n, t):
        times = lap_times.get(n)
        if not times:
            return 0
        i = bisect.bisect_right(times, t) - 1
        return laps[n][i][1] if i >= 0 else 0

    positions = {}
    for row in raw["position"]:
        positions.setdefault(row["driver_number"], []).append((ts(row["date"]), row["position"]))
    pos_times = {n: [t for t, _ in lst] for n, lst in positions.items()}
    for lst in positions.values():
        lst.sort()

    intervals = {}
    for row in raw["intervals"]:
        intervals.setdefault(row["driver_number"], []).append((ts(row["date"]), row["interval"]))
    int_times = {}
    for n, lst in intervals.items():
        lst.sort(key=lambda x: x[0])
        int_times[n] = [t for t, _ in lst]

    stints = {}
    for row in raw["stints"]:
        stints.setdefault(row["driver_number"], []).append(row)

    pits = {}
    for row in raw["pit"]:
        pits.setdefault(row["driver_number"], []).append(
            (ts(row["date"]), row.get("lane_duration"), row.get("stop_duration"), row.get("lap_number"))
        )

    def latest(table, times, n, t):
        lst = table.get(n)
        if not lst:
            return None
        i = bisect.bisect_right(times[n], t) - 1
        return lst[i][1] if i >= 0 else None

    # Ventana de carrera: arranca en la vuelta 1 y termina cuando acaba la última vuelta
    lap1_starts = [lst[0][0] for lst in laps.values() if lst]
    t0 = min(lap1_starts)
    last_lap_end = 0
    for row in raw["laps"]:
        if row.get("date_start") and row.get("lap_duration"):
            last_lap_end = max(last_lap_end, ts(row["date_start"]) + row["lap_duration"])
    t_end = last_lap_end or t0 + 5400
    total_laps = max((lap for lst in laps.values() for _, lap in lst), default=0)

    # Eventos de dirección de carrera ordenados + estado de bandera
    rc = sorted(
        (r for r in raw["race_control"] if ts(r["date"]) >= t0 - 60),
        key=lambda r: ts(r["date"]),
    )
    flag_events = []  # (t, flag)
    for r in rc:
        msg = (r.get("message") or "").upper()
        cat = r.get("category")
        t = ts(r["date"])
        if cat == "Flag":
            f = (r.get("flag") or "").upper()
            if f == "RED":
                flag_events.append((t, "RED"))
            elif f in ("GREEN", "CLEAR") and r.get("scope") == "Track":
                flag_events.append((t, "GREEN"))
            elif f in ("YELLOW", "DOUBLE YELLOW") and r.get("scope") == "Track":
                flag_events.append((t, "YELLOW"))
            elif f == "CHEQUERED":
                flag_events.append((t, "FIN"))
        elif cat == "SafetyCar":
            if "DEPLOYED" in msg:
                flag_events.append((t, "VSC" if "VIRTUAL" in msg else "SC"))
            elif "ENDING" in msg:
                flag_events.append((t, "GREEN"))
    flag_times = [t for t, _ in flag_events]

    def flag_at(t):
        i = bisect.bisect_right(flag_times, t) - 1
        return flag_events[i][1] if i >= 0 else "GREEN"

    ticker_events = []  # (t, texto)
    for r in rc:
        is_noise = (r.get("flag") or "").upper() in ("BLUE", "CLEAR") and r.get("scope") != "Track"
        if not is_noise and (r.get("category") in ("Flag", "SafetyCar") or "PENALTY" in (r.get("message") or "").upper()):
            lap = r.get("lap_number")
            msg = (r.get("message") or "")[:70]
            ticker_events.append((ts(r["date"]), f"V{lap if lap else '-'}: {msg}"))
    for n, lst in pits.items():
        for t, lane, stop, lap in lst:
            if lane is None and stop is None:
                continue
            sane = lane is not None and lane <= MAX_SANE_PIT_LANE_SEC
            if stop and stop <= MAX_SANE_PIT_LANE_SEC:
                dur = f" ({stop:.1f}s)"
            elif sane:
                dur = f" ({lane:.1f}s)"
            else:
                dur = ""  # dato anómalo del feed (p. ej. coche detenido en boxes): no inventar duración
            ticker_events.append((t + (lane if sane else 0), f"V{lap}: {code.get(n, n)} pit stop{dur}"))
    ticker_events.sort()
    ticker_times = [t for t, _ in ticker_events]

    def ticker_at(t):
        i = bisect.bisect_right(ticker_times, t)
        return [txt for _, txt in reversed(ticker_events[max(0, i - 5):i])]

    frames = []
    history = []  # por frame: {numero: intervalo_numerico}
    t = t0
    while t <= t_end:
        rows = []
        numeric = {}
        for n in drivers:
            pos = latest(positions, pos_times, n, t)
            if pos is None:
                continue
            gap = latest(intervals, int_times, n, t)
            lap = lap_at(n, t)
            comp, age = "U", 0
            for st in stints.get(n, []):
                end = st["lap_end"] if st.get("lap_end") else 999
                if st["lap_start"] <= lap <= end:
                    comp = COMPOUNDS.get((st.get("compound") or "").upper(), "U")
                    age = (st.get("tyre_age_at_start") or 0) + (lap - st["lap_start"])
                    break
            in_pit = 0
            for pt, lane, _stop, _lap in pits.get(n, []):
                if lane is None:
                    continue
                window = lane if lane <= MAX_SANE_PIT_LANE_SEC else DEFAULT_PIT_LANE_SEC
                if pt <= t <= pt + window:
                    in_pit = 1
                    break
            if isinstance(gap, (int, float)):
                numeric[n] = gap
            rows.append([n, pos, gap, comp, age, in_pit])
        rows.sort(key=lambda r: r[1])
        history.append(numeric)

        prev = history[-1 - TREND_LOOKBACK_FRAMES] if len(history) > TREND_LOOKBACK_FRAMES else {}
        drivers_out = []
        for idx, (n, pos, gap, comp, age, in_pit) in enumerate(rows):
            trend = 0
            if n in numeric and n in prev and idx > 0:
                diff = numeric[n] - prev[n]
                trend = 1 if diff < -TREND_THRESHOLD_SEC else (-1 if diff > TREND_THRESHOLD_SEC else 0)
            drivers_out.append([code[n], pos, fmt_delta(gap, idx == 0), comp, age, in_pit, trend])

        leader = rows[0][0] if rows else None
        frames.append(
            {
                "t": round(t - t0, 1),
                "p": {
                    "sess": "R",
                    "lap": lap_at(leader, t) if leader else 0,
                    "total": total_laps,
                    "flag": flag_at(t),
                    "drivers": drivers_out,
                    "ticker": ticker_at(t),
                },
            }
        )
        t += step

    meta = {
        "source": f"openf1 session_key={session['session_key']}",
        "synthetic": False,
        "session": "R",
        "circuit": session["circuit_short_name"],
        "date": session["date_start"],
        "step_sec": step,
        "total_laps": total_laps,
    }
    return meta, frames


def write_season(raw, extra_sessions=()):
    """Regenera drivers de season_data.json desde el feed (acrónimos, dorsales y colores reales).

    Une los pilotos de varias sesiones: en una sola carrera pueden faltar titulares
    (p. ej. un suplente que corrió en su lugar).
    """
    season = json.loads(SEASON_PATH.read_text(encoding="utf-8")) if SEASON_PATH.exists() else {"circuits": {}}
    out = {}
    all_drivers = list(raw["drivers"])
    for sk in extra_sessions:
        print(f"Descargando drivers de sesión {sk}...")
        all_drivers = api_get("drivers", session_key=sk) + all_drivers  # la sesión principal prevalece
    seen = {}
    for d in all_drivers:
        seen[d["name_acronym"]] = d
    for d in sorted(seen.values(), key=lambda d: d["name_acronym"]):
        first = (d.get("first_name") or "").strip()
        last = (d.get("last_name") or "").strip().title()
        out[d["name_acronym"]] = {
            "name": f"{first[:1]}. {last}" if first else last,
            "num": d["driver_number"],
            "team": d.get("team_name"),
            "color": "#" + (d.get("team_colour") or "FFFFFF"),
        }
    season["season"] = 2026
    season["drivers"] = out
    SEASON_PATH.write_text(json.dumps(season, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"season_data.json actualizado con {len(out)} pilotos")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--session-key", type=int, required=True)
    ap.add_argument("--step", type=float, default=10.0, help="segundos de carrera entre frames")
    ap.add_argument("--refresh", action="store_true", help="ignorar caché y volver a descargar")
    ap.add_argument("--write-season", action="store_true")
    ap.add_argument("--season-sessions", type=int, nargs="*", default=[], help="sesiones extra para completar la lista de pilotos")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    raw = load_raw(args.session_key, args.refresh)
    if args.write_season:
        write_season(raw, args.season_sessions)
    meta, frames = build_frames(raw, args.step)
    out = pathlib.Path(args.out) if args.out else ROOT / "data" / "replay_sample.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"meta": meta, "frames": frames}, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
    print(f"{len(frames)} frames -> {out} ({out.stat().st_size / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
