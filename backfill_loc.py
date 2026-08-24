"""Rellena la columna `loc` de jobs.db para las filas que YA están dentro.

Dos vías, ninguna adivina:
  1. El rol trae la ubicación detrás del último ' · ' (ATS y portal del IE).
  2. No la trae (los README de GitHub la tiran al construir el dict, pero SÍ
     entró en el sha1 de la key): se prueba cada ubicación del vocabulario del
     README y se acepta solo si el sha1 vuelve a salir idéntico. Un acierto es
     una prueba, no una estimación; lo que no cuadra se queda vacío.

Sin --write no escribe nada: enseña lo que haría.
"""
import hashlib, sqlite3, sys, urllib.request
from pathlib import Path

# La base de al lado del script: la misma que abre db.connect(). Se puede pasar
# otra como primer argumento (una copia, para probar sin tocar la de verdad).
_ARGS = [a for a in sys.argv[1:] if not a.startswith("--")]
DB = _ARGS[0] if _ARGS else str(Path(__file__).resolve().parent / "jobs.db")
WRITE = "--write" in sys.argv
README = ("https://raw.githubusercontent.com/vanshb03/Summer2026-Internships"
          "/dev/README.md")
_HDR = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}


def readme_locs():
    """Vocabulario de ubicaciones del README, para el ataque por fuerza bruta."""
    # ingesta._CTX, no un contexto nuevo: hay Pythons que no encuentran los
    # certificados del sistema, y la ingesta ya resolvió eso (certifi si está)
    # para todas sus descargas. Y su _clean/_rows son los que construyeron
    # estas filas: reusarlos es lo que hace que el sha1 vuelva a salir.
    import ingesta
    md = urllib.request.urlopen(urllib.request.Request(README, headers=_HDR),
                                timeout=30, context=ingesta._CTX).read().decode()
    return {ingesta._clean(c[2]) for c in ingesta._rows(md) if len(c) > 2} | {""}


conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row
rows = conn.execute("SELECT id, key, company, role, source FROM jobs"
                    " WHERE kind='oferta'").fetchall()

del_suffix, por_key, sin_nada = [], [], []
pendientes = []
for r in rows:
    core, sep, loc = (r["role"] or "").rpartition(" · ")
    if sep and loc.strip():
        del_suffix.append((r["id"], loc.strip()))
    else:
        pendientes.append(r)

if pendientes:
    try:
        vocab = readme_locs()
    except Exception as e:                                   # noqa: BLE001
        print("aviso: no se pudo leer el README (%s); se prueba solo ''" % e)
        vocab = {""}
    for r in pendientes:
        for loc in vocab:
            probe = f"{r['company']}¦{r['role']}¦{loc}".encode()
            if hashlib.sha1(probe).hexdigest()[:16] == r["key"]:
                (por_key if loc else sin_nada).append((r["id"], loc))
                break
        else:
            sin_nada.append((r["id"], ""))

print("ofertas: %d" % len(rows))
print("  ubicación desde el sufijo del rol : %d" % len(del_suffix))
print("  ubicación recuperada del sha1     : %d" % len(por_key))
print("  se quedan sin ubicación           : %d" % len(sin_nada))

if not WRITE:
    print("\n(simulacro: nada escrito — pasa --write para aplicarlo)")
    for i, loc in por_key[:10]:
        print("   id=%-5s <- %r" % (i, loc))
    raise SystemExit

cols = [c["name"] for c in conn.execute("PRAGMA table_info(jobs)")]
if "loc" not in cols:
    conn.execute("ALTER TABLE jobs ADD COLUMN loc TEXT NOT NULL DEFAULT ''")
conn.executemany("UPDATE jobs SET loc=? WHERE id=?",
                 [(loc, i) for i, loc in del_suffix + por_key])
conn.commit()
n = conn.execute("SELECT COUNT(*) c FROM jobs WHERE loc<>''").fetchone()["c"]
print("\nescrito: %d filas con ubicación." % n)
