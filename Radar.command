#!/bin/zsh
# Doble clic = abrir Radar, esté como esté el servidor.
#   - si el agente está cargado pero muerto  -> kickstart
#   - si está descargado (tras un bootout)   -> bootstrap
# El agente debería tenerlo siempre vivo; esto es el rescate.
cd "$(dirname "$0")"
UID_="$(id -u)"
# La etiqueta la deja instalar.command al cargar el agente. Sin ese fichero
# (repo recién clonado, o instalación anterior a las plantillas) se busca la que
# haya en el disco antes de rendirse.
LABEL="$(cat .radar-label 2>/dev/null)"
if [[ -z "$LABEL" ]]; then
  [[ -f "$HOME/Library/LaunchAgents/com.radar.app.plist" ]] && LABEL="com.radar.app"
fi
if [[ -z "$LABEL" ]]; then
  echo "No hay ningún agente de Radar instalado: pasa antes por instalar.command."
  exit 1
fi
if ! curl -s -o /dev/null --max-time 2 http://127.0.0.1:8000/; then
  if launchctl print "gui/$UID_/$LABEL" >/dev/null 2>&1; then
    launchctl kickstart -k "gui/$UID_/$LABEL"
  else
    launchctl bootstrap "gui/$UID_" "$HOME/Library/LaunchAgents/$LABEL.plist"
  fi
  # El servidor tarda un instante en escuchar; se espera a que responda.
  for i in 1 2 3 4 5 6 7 8 9 10; do
    curl -s -o /dev/null --max-time 1 http://127.0.0.1:8000/ && break
    sleep 0.5
  done
fi
open http://localhost:8000
