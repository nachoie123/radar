#!/usr/bin/env python3
"""Chequeo de seguridad de dist/Radar.app, ejecutado de verdad. Escribe tools/seguridad.md.

  .venv/bin/python tools/seguridad.py dist/Radar.app

(a) Datos personales: abre TODO el paquete (también el archivo comprimido de
    Python que va dentro del ejecutable) y cuenta cuántas veces aparece:
    cada línea de ~/.config/personal-scan/patterns.txt (correos, teléfono,
    dirección, cadenas del CV… de quien empaqueta; el fichero no está en el
    repo), la ruta de su carpeta personal y cualquier cosa con forma de clave.
    Ningún valor se imprime: solo el número de apariciones.
(b) Ficheros de datos que no pueden ir dentro: jobs.db y cualquier .db/.sqlite,
    cv.md, discovered.json/descubiertos.json, config.json, .env, *.key y CVs.
(c) Arquitecturas de cada binario con `lipo -archs` (universal2 = x86_64 + arm64).
(d) Lanza el binario empaquetado en `--selftest` (ver app.selftest) con HOME
    falso, carpeta de trabajo vacía y RADAR_DATA en una carpeta temporal, y
    comprueba además que no ha escrito NADA fuera de RADAR_DATA (ni en el HOME
    falso, ni en la carpeta de trabajo, ni dentro del propio .app).
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import zlib
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
PATTERNS = Path.home() / ".config" / "personal-scan" / "patterns.txt"


def bundle_blobs(app: Path):
    """(nombre, bytes) de cada fichero del .app, con los archivos de PyInstaller abiertos."""
    from PyInstaller.archive.readers import CArchiveReader, ZlibArchiveReader
    for f in app.rglob("*"):
        if not f.is_file() or f.is_symlink():
            continue
        data = f.read_bytes()
        yield str(f.relative_to(app)), data
        if f.name == "Radar" and f.parent.name == "MacOS":
            try:
                ca = CArchiveReader(str(f))
            except Exception:
                continue
            for name in ca.toc:
                try:
                    blob = ca.extract(name)
                except Exception:
                    continue
                yield f"exe!{name}", blob or b""
                if name.endswith(".pyz") or name.startswith("PYZ"):
                    tmp = HERE / "build" / "_pyz.tmp"
                    tmp.write_bytes(blob)
                    z = ZlibArchiveReader(str(tmp))
                    for mod in z.toc:
                        try:
                            code = z.extract(mod, raw=True)
                        except TypeError:
                            code = z.extract(mod)
                        if isinstance(code, bytes):
                            try:
                                code = zlib.decompress(code)
                            except zlib.error:
                                pass
                            yield f"pyz!{mod}", code
                        else:
                            import marshal
                            yield f"pyz!{mod}", marshal.dumps(code) if code else b""
                    tmp.unlink()


def to_find():
    """{etiqueta: [patrones]}. Los valores reales no se imprimen nunca."""
    s = {}
    if PATTERNS.exists():
        s["datos personales (personal-scan/patterns.txt)"] = [
            l.strip().encode() for l in PATTERNS.read_text().splitlines() if l.strip()]
    else:
        s["datos personales (personal-scan/patterns.txt NO ENCONTRADO)"] = []
    s[f"ruta de la carpeta personal (/Users/{Path.home().name})"] = [str(Path.home()).encode()]
    s["claves (AIza…, sk-…, sk-ant-, ghp_, xi-api)"] = [
        re.compile(rb"AIza[0-9A-Za-z_\-]{35}"), re.compile(rb"sk-[A-Za-z0-9_\-]{20,}"),
        re.compile(rb"sk-ant-"), re.compile(rb"ghp_[A-Za-z0-9]{20,}"), re.compile(rb"xi-api")]
    return s


# Por el NOMBRE del fichero (también dentro del ejecutable). config.example.json sí va.
DATAFILES = re.compile(r"(^|[/!])(jobs\.db[^/]*|[^/!]*\.(db|sqlite3?)|cv\.md|master-cv[^/]*|[^/!]*cv[^/!]*\.pdf"
                       r"|discovered\.json|descubiertos\.json|config\.json|\.env|[^/!]*\.key)$", re.I)


def scan(app):
    pats = to_find()
    hits = {k: {} for k in pats}
    datos, files, total = {}, 0, 0
    for name, data in bundle_blobs(app):
        files += 1
        total += len(data)
        if DATAFILES.search(name):
            datos[name] = len(data)
        for label, plist in pats.items():
            n = sum(len(p.findall(data)) if hasattr(p, "findall") else data.count(p) for p in plist)
            if n:
                hits[label][name] = n
    return files, total, hits, datos


def archs(app):
    out = {}
    for f in app.rglob("*"):
        if f.is_file() and not f.is_symlink() and (f.suffix in (".so", ".dylib") or f.parent.name == "MacOS"
                                                   or f.name == "Python"):
            r = subprocess.run(["lipo", "-archs", str(f)], capture_output=True, text=True)
            if r.returncode == 0:
                out[str(f.relative_to(app))] = r.stdout.strip()
    return out


def foto(carpeta):
    """{ruta: (tamaño, mtime)} de todo lo que hay debajo."""
    return {str(p.relative_to(carpeta)): (p.lstat().st_size, p.lstat().st_mtime_ns)
            for p in carpeta.rglob("*")}


def selftest(app):
    raiz = Path(tempfile.mkdtemp(prefix="radar-seguridad-"))
    home, cwd, data = raiz / "home", raiz / "cwd", raiz / "data"
    for d in (home, cwd, data):
        d.mkdir()
    antes = foto(app)
    env = {"PATH": "/usr/bin:/bin", "HOME": str(home), "RADAR_DATA": str(data), "TMPDIR": str(raiz / "tmp")}
    (raiz / "tmp").mkdir()
    try:
        p = subprocess.run([str(app / "Contents" / "MacOS" / "Radar"), "--selftest"], cwd=cwd, env=env,
                           capture_output=True, text=True, timeout=300)
        code = p.returncode
    except subprocess.TimeoutExpired:
        code = "timeout"
    out = data / "selftest.json"
    r = json.loads(out.read_text()) if out.exists() else {"exception": "la app no escribió selftest.json"}
    r["exit"] = code
    r["fuera_de_data"] = {"home": sorted(foto(home)), "cwd": sorted(foto(cwd)),
                          "tmp": sorted(foto(raiz / "tmp")),
                          "app_cambiada": sorted(k for k in set(antes) | set(foto(app))
                                                 if antes.get(k) != foto(app).get(k))}
    return r


def main():
    app = Path(sys.argv[1] if len(sys.argv) > 1 else HERE / "dist" / "Radar.app").resolve()
    files, size, hits, datos = scan(app)
    ar = archs(app)
    not_universal = {k: v for k, v in ar.items() if set(v.split()) != {"x86_64", "arm64"}}
    st = selftest(app)
    ok_st = st.get("ok") or {}
    fuera = st["fuera_de_data"]
    patrones_ok = PATTERNS.exists()

    ok_a = patrones_ok and not any(v for v in hits.values())
    ok_b = not datos
    ok_c = bool(ar) and not not_universal
    ok_d = st.get("exit") == 0 and st.get("todo_ok") is True
    ok_e = not any(fuera.values())
    mark = lambda b: "OK" if b else "FALLA"
    red = "sin red (no cuenta como fallo)" if st.get("sin_red") else "con red"

    L = ["# Chequeo de seguridad de Radar.app", "",
         f"Generado por `tools/seguridad.py` el {datetime.now():%Y-%m-%d %H:%M} sobre `{app.relative_to(HERE)}`.",
         "Todo lo de abajo es salida real de este script, no texto escrito a mano.", "",
         "| Prueba | Resultado |", "|---|---|",
         f"| (a) Sin datos personales, ruta personal ni claves en el paquete | {mark(ok_a)} |",
         f"| (b) Sin ficheros de datos dentro (jobs.db, cv.md, discovered.json, config.json, .db, .env, .key, CVs) | {mark(ok_b)} |",
         f"| (c) Todos los binarios universal2 (x86_64 + arm64) | {mark(ok_c)} ({len(ar)} binarios) |",
         f"| (d) Selftest de la app empaquetada ({red}) | {mark(ok_d)} |",
         f"| (e) El selftest no escribe nada fuera de su carpeta de datos | {mark(ok_e)} |",
         "", "## (a) Datos personales y claves", "",
         f"Ficheros revisados: {files} (incluye cada módulo del archivo comprimido de Python dentro del ejecutable),"
         f" {size / 1e6:.1f} MB descomprimidos. Apariciones de cada cosa buscada:", ""]
    for k, v in hits.items():
        n = len(to_find()[k]) if "patterns" in k else None
        L.append(f"- {k}{f' ({n} patrones)' if n is not None else ''}: **{sum(v.values())}**"
                 + (f" en {', '.join(list(v)[:5])}" if v else ""))
    L += ["", "Se buscan los valores exactos pero no se imprimen.", "",
          "## (b) Ficheros de datos", "",
          (f"Encontrados: {', '.join(datos)}" if datos else "Ninguno."), "",
          "## (c) Arquitecturas", ""]
    L += [f"- `{k}`: {v}" for k, v in sorted(ar.items())[:8]]
    if len(ar) > 8:
        L.append(f"- … y {len(ar) - 8} más" + (f"; NO universales: {not_universal}" if not_universal
                                                 else ", todos x86_64 arm64"))
    L += ["", "## (d) Selftest (`Radar --selftest`)", "",
          "Arranca sin ventana con HOME falso, carpeta de trabajo vacía y RADAR_DATA temporal:", "",
          "1. Servidor en 127.0.0.1 y puerto libre: `/` da 200 con el HTML.",
          "2. Host ajeno (DNS rebinding) → 403 en GET y POST; `/jobs.db` → 404.",
          "3. La pasada de la primera vez (catálogo vacío), en su hilo, con la fuente sin red;"
          " la barra de progreso (`/api/salud`) la ve correr y acabar.",
          "4. Ingesta de verdad contra 2 tableros pequeños de Greenhouse (Cabify, Tide).",
          "5. Un CV inventado pegado por la API; «Para ti» ordena con FTS5 y dice qué palabras casan.",
          "6. Lectura de anuncios (`descr.rellena`) en un hilo, como en la app.", "",
          "| Paso | Resultado |", "|---|---|"]
    L += [f"| {k} | {mark(v)} |" for k, v in ok_st.items()]
    L += ["", "```", json.dumps({k: st.get(k) for k in ("exit", "secs", "raiz", "host_falso", "host_falso_post",
                                                         "jobs_db", "toca_al_abrir", "primera_pasada",
                                                         "ingesta_red", "sin_red", "cv_post", "para_ti",
                                                         "busqueda", "descr_hilo", "ficheros", "exception")
                                 if k in st}, ensure_ascii=False, indent=1), "```", "",
          "## (e) Nada fuera de la carpeta de datos", "",
          f"- HOME falso: {fuera['home'] or 'vacío'}",
          f"- Carpeta de trabajo: {fuera['cwd'] or 'vacía'}",
          f"- TMPDIR: {fuera['tmp'] or 'vacío'}",
          f"- Ficheros del .app cambiados: {fuera['app_cambiada'] or 'ninguno'}",
          f"- Lo que sí escribió, todo dentro de RADAR_DATA: {st.get('ficheros')}"]
    (HERE / "tools" / "seguridad.md").write_text("\n".join(L) + "\n")
    print("\n".join(L[5:12]))
    sys.exit(0 if ok_a and ok_b and ok_c and ok_d and ok_e else 1)


if __name__ == "__main__":
    main()
