# Trackside Pulse

PWA de ultra-bajo ancho de banda para seguir una carrera desde el autódromo. Spec completo en [TRACKSIDE_PULSE_SPEC.md](TRACKSIDE_PULSE_SPEC.md).

## Estado: prototipo local (replay)

Sin Redis ni Vercel todavía. Todo corre en tu PC con Python 3.11+.

```powershell
pip install requests

# 1. Grabar un replay REAL desde OpenF1 (gratis, histórico, sin API key). Ya hay uno generado.
python worker/openf1_record.py --session-key 11404 --step 10 --write-season --season-sessions 11396

# 2. Levantar PWA + /api/live con el replay
python worker/dev_server.py --speed 5 --loop            # http://127.0.0.1:8000
python worker/dev_server.py --speed 5 --stall-at 20 --stall-for 200   # simula corte de señal (>90 s)
python worker/dev_server.py --host 0.0.0.0              # accesible desde el celular por Wi-Fi
```

`worker/make_icons.py` regenera los íconos PWA.

## Datos

- Fuente: [OpenF1](https://openf1.org). El histórico es gratis; el tiempo real es de pago. Las sesiones "en vivo" (desde 30 min antes hasta 30 min después) también requieren cuenta de pago.
- El replay actual es la **carrera de Ciudad de México 2026** (`session_key=11404`, 71 vueltas).
- `season_data.json` (dorsales, siglas, equipos y colores) sale del mismo feed. Los datos de circuito (`pit_loss_sec`) son estimaciones.

## Desviaciones respecto al spec (a revisar)

- `/api/live` envía el timestamp en la cabecera `X-TS` (también en los 304). Con `ts` dentro del cuerpo, el ETag cambiaría en cada actualización y nunca habría 304.
- El payload agrega `sess` (`R`, `Q`, `P`). Por ahora solo se implementó `R`.
