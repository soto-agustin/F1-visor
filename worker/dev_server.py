#!/usr/bin/env python3
"""
Trackside Pulse - Servidor de desarrollo local (sin Redis ni Vercel).

Sirve la PWA (public/) y emula /api/live reproduciendo un replay grabado con
openf1_record.py, con la misma semántica que tendrá la Edge Function:
  - ETag calculado sobre el estado SIN el campo "ts"
  - 304 Not Modified (cuerpo vacío) si If-None-Match coincide
  - cabecera X-TS con la hora del último dato (también en los 304), para que el
    detector de desfase funcione aunque el estado no cambie

Uso:
    python worker/dev_server.py                       # replay a 1x en http://127.0.0.1:8000
    python worker/dev_server.py --speed 5 --loop
    python worker/dev_server.py --stall-at 20 --stall-for 120   # simula corte de señal (prueba >90s)
    python worker/dev_server.py --host 0.0.0.0        # accesible desde el celular en tu Wi-Fi
"""
import argparse
import hashlib
import json
import pathlib
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

ROOT = pathlib.Path(__file__).resolve().parent
PUBLIC = ROOT.parent / "public"


class Replay:
    def __init__(self, path, speed, loop, stall_at, stall_for, start_at):
        data = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
        self.meta = data["meta"]
        self.frames = data["frames"]
        self.times = [f["t"] for f in self.frames]
        self.duration = self.times[-1]
        self.speed = speed
        self.loop = loop
        self.stall_at = stall_at
        self.stall_for = stall_for
        self.start = time.time() - start_at / speed
        self.lock = threading.Lock()

    def snapshot(self):
        """Devuelve (payload_dict, ts) para el instante actual."""
        now = time.time()
        elapsed = now - self.start
        data_time = now
        if self.stall_at is not None and self.stall_for:
            if self.stall_at <= elapsed < self.stall_at + self.stall_for:
                data_time = self.start + self.stall_at
                elapsed = self.stall_at
            elif elapsed >= self.stall_at + self.stall_for:
                elapsed -= self.stall_for
        race_t = elapsed * self.speed
        if race_t > self.duration:
            race_t = race_t % self.duration if self.loop else self.duration
        idx = max(0, self._index(race_t))
        return self.frames[idx]["p"], int(data_time)

    def _index(self, t):
        lo, hi = 0, len(self.times) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self.times[mid] <= t:
                lo = mid
            else:
                hi = mid - 1
        return lo


class Handler(SimpleHTTPRequestHandler):
    replay = None
    pit_delta = 21.0

    extensions_map = {
        **SimpleHTTPRequestHandler.extensions_map,
        ".js": "text/javascript",
        ".json": "application/json",
        ".webmanifest": "application/manifest+json",
    }

    def do_GET(self):
        if self.path.split("?")[0] == "/api/live":
            return self.api_live()
        return super().do_GET()

    def api_live(self):
        payload, ts = self.replay.snapshot()
        circuit = self.replay.meta.get("circuit", "Interlagos")
        state = {"circuit": circuit, "penalties": {}, **payload, "pit_delta": self.pit_delta}
        etag = '"' + hashlib.sha1(json.dumps(state, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16] + '"'
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("X-TS", str(ts))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            return
        body = json.dumps({**state, "ts": ts}, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("ETag", etag)
        self.send_header("X-TS", str(ts))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def end_headers(self):
        if not self.path.startswith("/api/"):
            self.send_header("Cache-Control", "no-cache")  # en desarrollo, nunca servir assets viejos
        super().end_headers()

    def log_message(self, fmt, *args):
        if args and "/api/live" in str(args[0]):
            code = args[1] if len(args) > 1 else "?"
            print(f"[{time.strftime('%H:%M:%S')}] /api/live -> {code}")
        else:
            super().log_message(fmt, *args)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--replay", default=str(ROOT / "data" / "replay_sample.json"))
    ap.add_argument("--speed", type=float, default=1.0)
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--start-at", type=float, default=0.0, help="segundo de carrera donde empezar")
    ap.add_argument("--stall-at", type=float, default=None, help="segundo real en que se 'corta' la señal")
    ap.add_argument("--stall-for", type=float, default=0.0, help="duración (s) del corte")
    ap.add_argument("--pit-delta", type=float, default=21.0)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()

    Handler.replay = Replay(args.replay, args.speed, args.loop, args.stall_at, args.stall_for, args.start_at)
    Handler.pit_delta = args.pit_delta
    handler = partial(Handler, directory=str(PUBLIC))
    server = ThreadingHTTPServer((args.host, args.port), handler)
    m = Handler.replay.meta
    print(f"Replay: {m['circuit']} ({m['source']}), {len(Handler.replay.frames)} frames, x{args.speed}")
    print(f"Abrí http://{args.host}:{args.port}  (Ctrl+C para salir)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
