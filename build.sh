#!/bin/bash
# Empaqueta Radar.app (universal2: Apple Silicon + Intel) en dist/ y pasa el chequeo de seguridad.
#   ./build.sh            → exige el árbol de git limpio (nada sin commitear que no esté ignorado)
#   PERMITIR_SUCIO=1 ./build.sh → solo para probar el empaquetado antes de commitear; NO publicar ese .dmg
set -e
cd "$(dirname "$0")"
# Lo que no está en git no puede colarse en el paquete: ni un cv.md, ni un jobs.db
# copiado a mano, ni un script de pruebas con rutas de una carpeta de trabajo.
SUCIO="$(git status --porcelain)"
if [ -n "$SUCIO" ]; then
  if [ "${PERMITIR_SUCIO:-}" = "1" ]; then
    echo "AVISO: árbol sin commitear (PERMITIR_SUCIO=1). Este paquete es de prueba, no se publica:"; echo "$SUCIO"
  else
    echo "Hay ficheros sin commitear; commitéalos o ignóralos antes de empaquetar:"; echo "$SUCIO"; exit 1
  fi
fi
PY=/Library/Frameworks/Python.framework/Versions/3.12/bin/python3.12; [ -x $PY ] || PY=python3
[ -x .venv/bin/python ] || $PY -m venv .venv
.venv/bin/pip install -q pywebview==6.2.1 pyinstaller==6.22.3 certifi==2026.7.22
# Los tests propios, sin red, antes de empaquetar nada.
for f in db app descr ingesta; do .venv/bin/python $f.py --check > /dev/null; done
# Icono: build_assets/icon.png (1024 px) -> .icns con las herramientas de macOS.
mkdir -p build/icon.iconset
for s in 16 32 128 256 512; do
  sips -z $s $s build_assets/icon.png --out build/icon.iconset/icon_${s}x${s}.png > /dev/null
  sips -z $((s*2)) $((s*2)) build_assets/icon.png --out build/icon.iconset/icon_${s}x${s}@2x.png > /dev/null
done
iconutil -c icns build/icon.iconset -o build/icon.icns
rm -rf build/Radar dist/Radar dist/Radar.app
.venv/bin/pyinstaller --noconfirm --clean Radar.spec > build/pyinstaller.log 2>&1 || { tail -30 build/pyinstaller.log; exit 1; }
tail -2 build/pyinstaller.log
# macOS mínimo de verdad: el mayor `minos` (LC_BUILD_VERSION, o LC_VERSION_MIN_MACOSX en los
# binarios viejos) de cualquier binario del .app, en las dos arquitecturas. Si Info.plist
# declara menos, la app se instalaría en un macOS donde no arranca: se para aquí.
PLIST_MIN="$(plutil -extract LSMinimumSystemVersion raw dist/Radar.app/Contents/Info.plist)"
REAL_MIN="$(find dist/Radar.app -type f \( -name '*.so' -o -name '*.dylib' -o -perm -111 \) -print0 \
  | xargs -0 otool -arch all -l 2>/dev/null \
  | awk '/LC_BUILD_VERSION/{f=1} f&&$1=="minos"{print $2; f=0} /LC_VERSION_MIN_MACOSX/{g=1} g&&$1=="version"{print $2; g=0}' \
  | sort -V | tail -1)"
echo "macOS mínimo: Info.plist $PLIST_MIN, binarios $REAL_MIN"
if [ "$(printf '%s\n%s\n' "$PLIST_MIN" "$REAL_MIN" | sort -V | tail -1)" != "$PLIST_MIN" ]; then
  echo "LSMinimumSystemVersion ($PLIST_MIN) es menor que lo que piden los binarios ($REAL_MIN): súbelo en Radar.spec"; exit 1
fi
.venv/bin/python tools/seguridad.py dist/Radar.app
