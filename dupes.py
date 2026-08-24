"""Reproduce el recuento de duplicados de jobs.db con una normalización
conservadora. NO escribe nada: solo lee y clasifica.

Regla: dos filas son la MISMA oferta si coinciden empresa + núcleo del puesto
y sus ubicaciones son compatibles. "Compatibles" = una es prefijo de la otra
(Milano Bicocca ~ Milan) o alguna de las dos no dice ubicación Y el grupo solo
apunta a una ciudad. Si el grupo apunta a varias ciudades y hay una fila sin
ubicación, se marca AMBIGUO y no se toca: es el caso de DRW/Amazon/IMC.
"""
import re, sqlite3, sys, unicodedata
from collections import defaultdict
from pathlib import Path

# Por defecto la base de al lado del script; con un argumento, la que se pase
# (normalmente una copia: este script solo lee, pero el de al lado sí escribe).
DB = sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).resolve().parent / "jobs.db")

# Fichas de programa escritas a mano: no son ofertas concretas, describen un
# programa entero ("Summer Analyst Programme · verano · Londres"). Nunca son
# duplicado de una oferta del ATS ni entre sí.
CURATED = {"summer:programas", "insight:programas"}

# Sufijo de duración que mete el portal del IE, pegado al puesto.
_DUR = re.compile(r"\s*\((?:duración sin especificar|verano|"
                  r"\d+(?:[-–]\d+)?\s*(?:mes|meses|semana|semanas))\)\s*$", re.I)
_NOISE = re.compile(r"\((?:f/m/x|m/f|m/w/d|h/f|f/m/d|w/m/d|x/f/m)\)", re.I)
_YEAR = re.compile(r"\b20\d\d\b")


def _fold(s):
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


def split_role(role):
    """role -> (núcleo normalizado, ubicación cruda). La ubicación va detrás del
    último ' · ', que es de donde app._target saca ciudad y país."""
    core, _, loc = (role or "").rpartition(" · ")
    if not core:                       # sin ' · ': la fila entera es el puesto
        core, loc = loc, ""
    core = _DUR.sub("", core)
    core = _NOISE.sub(" ", core)
    core = _YEAR.sub(" ", _fold(core))
    core = re.sub(r"[^a-z0-9]+", " ", core).strip()
    core = re.sub(r"\binternships?\b", "intern", core)   # intern ~ internship
    core = re.sub(r"\s+", " ", core)
    return core, loc


def norm_loc(loc):
    """Ubicación -> ciudad comparable. Se queda con el primer tramo antes de la
    coma (Barcelona, Catalonia, ESP -> barcelona) y corta en el primer número,
    que en Deutsche Bank es donde empieza la calle."""
    city = _fold(loc).split(",")[0]
    city = re.sub(r"[^a-z0-9 ]+", " ", city)
    out = []
    for tok in city.split():
        if any(c.isdigit() for c in tok):
            break
        out.append(tok)
    city = " ".join(out).strip()
    if city in ("multiple locations", "remote telecommute", "flexible negotiable",
                "various", "europe", ""):
        return ""                      # no dice ciudad: no vale para desempatar
    return city


STRICT = "--strict" in sys.argv


def compatible(a, b):
    """Dos ciudades son la misma si una es prefijo de la otra (milan/milano).

    En modo --strict, una fila sin ciudad no casa con NADA: es la regla que
    hay que llevar a producción, porque las filas sin ciudad de vanshb03 son
    ofertas de EE.UU. y casarlas con el ATS europeo inventa duplicados."""
    if not a or not b:
        return not STRICT              # una no dice nada: no contradice
    lo, hi = sorted((a, b), key=len)
    return len(lo) >= 4 and hi.startswith(lo)


conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row
has_loc = any(c["name"] == "loc" for c in conn.execute("PRAGMA table_info(jobs)"))
rows = conn.execute(
    "SELECT id, key, company, role, url, source, board, first_seen%s"
    "  FROM jobs WHERE kind='oferta' ORDER BY id"
    % (", loc" if has_loc else ", '' AS loc")).fetchall()

groups = defaultdict(list)
for r in rows:
    if r["source"] in CURATED:
        continue
    core, del_rol = split_role(r["role"])
    # La columna manda: es la ubicación de origen. El sufijo del rol solo se usa
    # si la columna está vacía, que es lo que pasaba antes del backfill.
    loc = r["loc"] or del_rol
    groups[(_fold(r["company"]).strip(), core)].append(
        dict(r, core=core, loc=loc, city=norm_loc(loc)))

dupes, ambiguous, intra = [], [], []
for gk, items in groups.items():
    if len(items) < 2:
        continue
    cities = {i["city"] for i in items if i["city"]}
    # Reparte el grupo en subgrupos por ciudad compatible.
    buckets = []
    for it in sorted(items, key=lambda i: (not i["city"], i["id"])):
        for b in buckets:
            if all(compatible(it["city"], x["city"]) for x in b):
                b.append(it)
                break
        else:
            buckets.append([it])
    for b in buckets:
        if len(b) < 2:
            continue
        srcs = {i["source"] for i in b}
        blind = [i for i in b if not i["city"]]
        if blind and len(cities) > 1:
            ambiguous.append(b)        # DRW/Amazon/IMC: no se puede saber cuál
        elif len(srcs) > 1:
            dupes.append(b)
        else:
            intra.append(b)            # misma fuente: dos ciudades escritas igual


def show(title, groups_):
    print("\n=== %s: %d grupos, %d filas ===" % (
        title, len(groups_), sum(len(g) for g in groups_)))
    for g in sorted(groups_, key=lambda g: g[0]["company"].lower()):
        print("  %s — %r" % (g[0]["company"], g[0]["core"]))
        for i in g:
            print("      [%-32s] id=%-5s %s" % (i["source"], i["id"], i["role"]))


show("DUPLICADOS entre fuentes", dupes)
show("AMBIGUOS (fila sin ciudad + varias ciudades) — NO tocar", ambiguous)
show("Repetidos dentro de la MISMA fuente", intra)

pairs = defaultdict(int)
for g in dupes:
    for a in range(len(g)):
        for b in range(a + 1, len(g)):
            pairs[tuple(sorted((g[a]["source"], g[b]["source"])))] += 1
print("\n=== Pares de fuentes que chocan ===")
for (a, b), n in sorted(pairs.items(), key=lambda kv: -kv[1]):
    print("  %3d  %s  <->  %s" % (n, a, b))
print("\nofertas totales: %d · sobrantes a fusionar: %d"
      % (len(rows), sum(len(g) - 1 for g in dupes)))
