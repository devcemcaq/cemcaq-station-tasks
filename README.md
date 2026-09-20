# cemcaq-station-tasks

Tareas que corren **en la PC de cada estación de monitoreo** (Windows, junto al
software de Agilaire). El repo se clona una vez por estación y se actualiza solo
con `git pull` antes de cada corrida: ya no hay que copiar scripts a mano en cada
máquina cuando algo cambia.

| Tarea | Qué hace | Frecuencia |
|---|---|---|
| `telemetry/push_readings.py` | Lee los promedios horarios del datalogger Agilaire (`AVData`) y los envía al panel de control (`admin-dashboard`, módulo **Telemetría**) | cada hora |

Lo que se envía son **todos** los parámetros que expone el datalogger (NO, NO2,
NOx, SO2, CO, O3, PM2.5, `Temp Bench O3`, …). Cuáles se grafican, con qué
calibración y contra qué banda de alarma se decide en el panel (Telemetría ›
Canales), no en la estación. El backend guarda por *upsert*, así que reenviar
horas ya enviadas sólo rellena huecos.

Es independiente de `libagilaire.py` y de la base MySQL `cemcaq`: no toca nada
de lo que ya está en producción.

## Instalación en una estación (una sola vez)

Requisitos en la PC: Python 3.9+ (el de Agilaire, `C:\Users\Agilaire\AppData\Local\Programs\Python\Python39`),
el driver ODBC "SQL Server" (ya lo usa `libagilaire.py`), Git para Windows e
internet de salida por HTTPS.

1. En el panel, un administrador crea la estación (si no existe) en
   *Verificación › Estaciones* y genera una clave en *Telemetría › Canales ›
   Claves* de esa estación. La clave se muestra **una sola vez**.
2. En la PC de la estación, en una consola:

   ```bat
   cd C:\Cemcaq
   git clone https://github.com/devcemcaq/cemcaq-station-tasks.git
   cd cemcaq-station-tasks
   "C:\Users\Agilaire\AppData\Local\Programs\Python\Python39\python.exe" -m pip install -r requirements.txt
   copy .env.example .env
   notepad .env
   ```

   En `.env`: pegar la clave en `STATION_KEY`, la URL del backend en
   `DASHBOARD_API_URL` y las credenciales de SQL Server que usa `libagilaire.py`
   en `AGILAIRE_*`. `.env` no se versiona.
3. Probar sin enviar y luego enviar:

   ```bat
   scripts\run-telemetry.cmd --dry-run
   scripts\run-telemetry.cmd
   ```

   Debe terminar con `FEO: N recibidas, N guardadas, canales nuevos: …`. En el
   panel, *Telemetría › Canales* muestra los canales descubiertos; un supervisor
   asigna `Temp Bench O3` a **Temperatura interna** con desfase `-5` y lo activa.
4. Registrar la tarea programada (consola **como administrador**):

   ```bat
   scripts\install-task.cmd
   ```

   Crea la tarea "CEMCAQ Telemetria": cada hora a los 5 minutos, con el usuario
   actual (la PC se queda con sesión abierta para Agilaire). Para forzar una
   corrida: `schtasks /Run /TN "CEMCAQ Telemetria"`.

## Operación

- Bitácora: `logs\telemetry.log` (rotativa, 5 × 1 MB). Códigos de salida:
  `1` configuración, `2` datalogger/SQL Server, `3` envío (la clave rechazada
  no se reintenta; un error de red se reintenta 3 veces).
- Si la estación estuvo sin internet, no hay que hacer nada: la siguiente
  corrida reenvía las últimas `LOOKBACK_HOURS` (26) horas. Para más historia:
  `scripts\run-telemetry.cmd --hours 168`.
- Si se cambia el Python de la estación, definir la variable de entorno
  `PYTHON_EXE` o editar `scripts\run-telemetry.cmd`.
- Rotar la clave: generar una nueva en el panel, pegarla en `.env`, y revocar
  la anterior.

## Desarrollo

Sin SQL Server a la mano, el extractor se prueba con un JSON con las columnas
de la vista `ReadingAverageDataLast30Days_001H`:

```bash
python telemetry/push_readings.py --from-json fixtures/agilaire_sample.json --dry-run
python telemetry/push_readings.py --from-json fixtures/agilaire_sample.json   # POST real al DASHBOARD_API_URL del .env
```

Convenciones que hay que conservar:

- **Sello de tiempo = fin del intervalo.** Agilaire guarda el inicio de la hora;
  se le suma 1 h antes de enviar (13:00 = promedio 12:00–13:00), igual que
  `libagilaire.py` y que la base `cemcaq`.
- Se envía la hora local con `UTC_OFFSET` fijo (`-06:00`: Querétaro no tiene
  horario de verano). El backend guarda `timestamptz`.
- Se lee únicamente la vista horaria (`ReadingAverageDataLast30Days_001H`);
  no se filtra por `IntervalName` porque su texto cambia entre instalaciones.
- El contrato del `POST /api/v1/telemetry/ingest` está en
  `admin-dashboard/docs/telemetry-spec.md`.
