#!/bin/zsh
# Doble clic = dejar Radar instalado en este Mac.
#
#   - comprueba que hay un Python que sirva (3.9+ y con FTS5, que es lo que usa
#     la búsqueda: sin él la app arranca y luego no encuentra nada),
#   - rellena las plantillas de launchd/ con las rutas DE ESTA máquina,
#   - carga los agentes: el servidor (siempre vivo) y las dos pasadas nocturnas,
#   - y abre la app.
#
# Se puede volver a ejecutar sin miedo: si un agente ya está cargado, se
# recarga, y de cualquier .plist anterior se guarda una copia antes de pisarlo.
#
# En Linux/Windows no hay launchd: ahí es `python3 ingesta.py` una vez y
# `python3 app.py` cuando quieras abrirla (o el cron/tarea programada de tu
# sistema con esos dos comandos).
set -u
cd "$(dirname "$0")" || exit 1
DIR="$PWD"

if [[ "$(uname)" != "Darwin" ]]; then
  echo "Esto es un instalador de macOS (launchd)."
  echo "En tu sistema:  python3 ingesta.py   (llena el catálogo)"
  echo "                python3 app.py       (abre http://localhost:8000)"
  exit 1
fi

# ---- Python ----------------------------------------------------------------
PY="$(command -v python3 || true)"
if [[ -z "$PY" ]]; then
  echo "No encuentro python3. Instálalo desde https://www.python.org/downloads/"
  echo "(macOS trae uno solo dentro de las Command Line Tools de Xcode)."
  exit 1
fi
# La ruta real, no el enlace del PATH: launchd arranca sin PATH de usuario, y un
# 'python3' de shim (pyenv, asdf) fuera de él dejaría el agente sin intérprete.
# Sale con la versión dentro (…/3.12/bin/python3), así que si algún día actualizas
# de Python, vuelve a ejecutar este instalador para que los agentes lo apunten.
PY="$("$PY" -c 'import sys; print(sys.executable)')"
"$PY" - <<'PYCHK' || exit 1
import sqlite3, sys
if sys.version_info < (3, 9):
    sys.exit("Radar necesita Python 3.9 o más nuevo; este es %d.%d"
             % sys.version_info[:2])
try:
    sqlite3.connect(":memory:").execute("CREATE VIRTUAL TABLE t USING fts5(x)")
except sqlite3.OperationalError:
    sys.exit("Este Python trae un SQLite sin FTS5 y la búsqueda de Radar lo usa.\n"
             "Prueba con el instalador de python.org, que sí lo trae.")
PYCHK
echo "python3: $PY"

# Descargar es TODO lo que hace la ingesta, así que se comprueba aquí y no la
# primera noche a las 02:30. El Python de python.org llega a macOS sin confiar
# en las raíces del sistema: sin esto, la ingesta falla en las 89 fuentes a la
# vez y el error no dice por qué.
if ! "$PY" - <<'TLSCHK'
import sys, urllib.error, urllib.request
import ingesta                       # su _CTX es el que usará la ingesta de verdad
try:
    urllib.request.urlopen(
        urllib.request.Request("https://boards-api.greenhouse.io/v1/boards/stripe/jobs",
                               headers=ingesta._HDR), timeout=20, context=ingesta._CTX)
except urllib.error.HTTPError:
    pass                             # un 404/429 ya demuestra que el TLS va bien
except Exception as e:                                       # noqa: BLE001
    if "certificate" in str(e).lower():
        sys.exit(1)                  # el problema es el llavero, no la red
    print(f"aviso: no he podido comprobar la conexión ({e}); sigo de todos modos.")
sys.exit(0)
TLSCHK
then
  echo
  echo "Este Python no confía en ningún certificado todavía, así que no podría"
  echo "descargar ninguna oferta. Arréglalo con UNA de las dos y vuelve a pasar"
  echo "por aquí:"
  echo "    open \"/Applications/Python 3.x/Install Certificates.command\""
  echo "    $PY -m pip install --user certifi"
  exit 1
fi

# ---- etiquetas de los agentes ----------------------------------------------
LA="$HOME/Library/LaunchAgents"
mkdir -p "$LA"
L_APP="com.radar.app"
L_DESCR="com.radar.descr"
L_ING="com.radar.ingesta"

# ---- rellenar y cargar ------------------------------------------------------
instala() {  # $1 = plantilla, $2 = etiqueta
  local plist="$LA/$2.plist"
  if [[ -f "$plist" ]]; then
    cp "$plist" "$plist.bak-$(date +%Y%m%d-%H%M%S)"
  fi
  sed -e "s|@@LABEL@@|$2|g" -e "s|@@PYTHON@@|$PY|g" -e "s|@@DIR@@|$DIR|g" \
      -e "s|@@PATH@@|$PATH|g" -e "s|@@HOME@@|$HOME|g" \
      "launchd/$1" > "$plist" || return 1
  plutil -lint "$plist" >/dev/null || { echo "plist inválido: $plist"; return 1; }
  launchctl bootout "gui/$UID/$2" 2>/dev/null
  launchctl bootstrap "gui/$UID" "$plist" || return 1
  echo "cargado: $2"
}

instala radar.plist.plantilla       "$L_APP"   || exit 1
instala radar-descr.plist.plantilla "$L_DESCR" || exit 1
instala radar-ingesta.plist.plantilla "$L_ING" || exit 1

# Radar.command necesita saber qué etiqueta acabó teniendo el servidor.
echo "$L_APP" > .radar-label

# ---- primera ingesta --------------------------------------------------------
# El agente no corre hasta las 02:30 y una app vacía no se entiende: si la base
# no tiene ofertas, se ofrece llenarla ahora.
N="$("$PY" -c "import db,sys
try:
    c=db.connect()
    print(c.execute(\"SELECT COUNT(*) FROM jobs WHERE kind='oferta'\").fetchone()[0])
except Exception:
    print(0)" 2>/dev/null || echo 0)"
if [[ "$N" == "0" ]]; then
  echo
  echo "El catálogo está vacío. La primera ingesta tarda unos 5-10 minutos."
  read -r "?¿La lanzo ahora? [S/n] " R
  if [[ "$R" != "n" && "$R" != "N" ]]; then
    "$PY" ingesta.py
  else
    echo "Cuando quieras:  python3 ingesta.py"
  fi
fi

# ---- abrir ------------------------------------------------------------------
for i in {1..20}; do
  curl -s -o /dev/null --max-time 1 http://127.0.0.1:8000/ && break
  sleep 0.5
done
if curl -s -o /dev/null --max-time 2 http://127.0.0.1:8000/; then
  echo "Radar está en http://localhost:8000 — se abre solo al iniciar sesión."
  open http://localhost:8000
else
  echo "El servidor no responde todavía. Mira radar.log en esta carpeta."
  exit 1
fi
