# Chequeo de seguridad de Radar.app

Generado por `tools/seguridad.py` el 2026-10-03 19:48 sobre `dist/Radar.app`.
Todo lo de abajo es salida real de este script, no texto escrito a mano.

| Prueba | Resultado |
|---|---|
| (a) Sin datos personales, ruta personal ni claves en el paquete | OK |
| (b) Sin ficheros de datos dentro (jobs.db, cv.md, discovered.json, config.json, .db, .env, .key, CVs) | OK |
| (c) Todos los binarios universal2 (x86_64 + arm64) | OK (65 binarios) |
| (d) Selftest de la app empaquetada (con red) | OK |
| (e) El selftest no escribe nada fuera de su carpeta de datos | OK |

## (a) Datos personales y claves

Ficheros revisados: 432 (incluye cada módulo del archivo comprimido de Python dentro del ejecutable), 65.2 MB descomprimidos. Apariciones de cada cosa buscada:

- datos personales (personal-scan/patterns.txt) (13 patrones): **0**
- ruta de la carpeta personal (/Users/nachosanbenito): **0**
- claves (AIza…, sk-…, sk-ant-, ghp_, xi-api): **0**

Se buscan los valores exactos pero no se imprimen.

## (b) Ficheros de datos

Ninguno.

## (c) Arquitecturas

- `Contents/Frameworks/AppKit/_AppKit.cpython-312-darwin.so`: x86_64 arm64
- `Contents/Frameworks/AppKit/_inlines.cpython-312-darwin.so`: x86_64 arm64
- `Contents/Frameworks/CoreFoundation/_CoreFoundation.cpython-312-darwin.so`: x86_64 arm64
- `Contents/Frameworks/CoreFoundation/_inlines.cpython-312-darwin.so`: x86_64 arm64
- `Contents/Frameworks/Foundation/_Foundation.cpython-312-darwin.so`: x86_64 arm64
- `Contents/Frameworks/Foundation/_inlines.cpython-312-darwin.so`: x86_64 arm64
- `Contents/Frameworks/Python.framework/Versions/3.12/Python`: x86_64 arm64
- `Contents/Frameworks/WebKit/_WebKit.cpython-312-darwin.so`: x86_64 arm64
- … y 57 más, todos x86_64 arm64

## (d) Selftest (`Radar --selftest`)

Arranca sin ventana con HOME falso, carpeta de trabajo vacía y RADAR_DATA temporal:

1. Servidor en 127.0.0.1 y puerto libre: `/` da 200 con el HTML.
2. Host ajeno (DNS rebinding) → 403 en GET y POST; `/jobs.db` → 404.
3. La pasada de la primera vez (catálogo vacío), en su hilo, con la fuente sin red; la barra de progreso (`/api/salud`) la ve correr y acabar.
4. Ingesta de verdad contra 2 tableros pequeños de Greenhouse (Cabify, Tide).
5. Un CV inventado pegado por la API; «Para ti» ordena con FTS5 y dice qué palabras casan.
6. Lectura de anuncios (`descr.rellena`) en un hilo, como en la app.

| Paso | Resultado |
|---|---|
| raiz_200 | OK |
| host_falso_403 | OK |
| jobs_db_404 | OK |
| primera_pasada | OK |
| ingesta_red | OK |
| fts5_cv | OK |
| descr_hilo | OK |
| sin_excepcion | OK |

```
{
 "exit": 0,
 "secs": 1.9,
 "raiz": {
  "status": 200,
  "html": true
 },
 "host_falso": 403,
 "host_falso_post": 403,
 "jobs_db": 404,
 "toca_al_abrir": "primera",
 "primera_pasada": {
  "visto_corriendo": true,
  "al_final": {
   "corriendo": false,
   "primera": true,
   "fase": "",
   "hechas": 0,
   "total": 0,
   "error": ""
  },
  "ofertas": 38
 },
 "ingesta_red": {
  "tableros": [
   {
    "empresa": "Cabify",
    "ok": true,
    "ofertas": 5,
    "error": ""
   },
   {
    "empresa": "Tide",
    "ok": true,
    "ofertas": 0,
    "error": ""
   }
  ],
  "nuevas": 5,
  "total": 43
 },
 "sin_red": false,
 "cv_post": 200,
 "para_ti": {
  "n": 7,
  "primera": "Python Data Analyst Intern · Madrid",
  "casa_en": [
   "python",
   "data",
   "analyst"
  ]
 },
 "busqueda": 1,
 "descr_hilo": {
  "con": 2,
  "sin": 0,
  "por": {
   "greenhouse": 2
  }
 },
 "ficheros": [
  "ingesta.log",
  "jobs.db"
 ]
}
```

## (e) Nada fuera de la carpeta de datos

- HOME falso: vacío
- Carpeta de trabajo: vacía
- TMPDIR: vacío
- Ficheros del .app cambiados: ninguno
- Lo que sí escribió, todo dentro de RADAR_DATA: ['ingesta.log', 'jobs.db']
