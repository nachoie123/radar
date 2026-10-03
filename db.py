"""Capa de datos de Radar. TODO el SQL vive aquí.

Regla del proyecto: ningún otro archivo importa sqlite3. Cuando esto migre a
Postgres se reescribe este archivo y nada más. Lo único no portable es search():
FTS5 + bm25() se cambian por tsvector + ts_rank sin tocar el resto.

Dos tablas, y la separación es lo importante (fase 5b, paso 1):

    jobs       catálogo. Una fila por oferta, global, sin dueño. Es lo que
               escribe el scraper, y lo que indexa el FTS.
    user_jobs  estado de la candidatura. Una fila por (usuario, oferta), y solo
               cuando ese usuario ha hecho algo con ella.

Antes había una sola tabla con UNIQUE (user_id, key), o sea una copia del
catálogo entero por usuario: 461 filas hoy, 461.000 con mil usuarios, y el FTS
degradándose con cada alta. Una oferta sin fila en user_jobs es simplemente
'nueva' para ese usuario — el estado por defecto sale del LEFT JOIN, no de una
fila escrita.
"""
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


def _carpeta_datos():
    """Dónde viven los datos de quien usa Radar: jobs.db, config.json, cv.md,
    descubiertos.json y los logs. Una sola carpeta, y nada fuera de ella.

      - Desde el repo: al lado del código, como siempre (el .gitignore los deja
        fuera de git).
      - En Radar.app (PyInstaller, `sys.frozen`): ~/Library/Application
        Support/Radar. El paquete es de solo lectura y se sustituye entero al
        actualizar; lo que se guardase dentro se perdería con él.
      - RADAR_DATA manda sobre las dos: el --selftest la apunta a una carpeta
        temporal para poder comprobar que no se escribe nada en otro sitio."""
    if os.environ.get("RADAR_DATA"):
        return Path(os.environ["RADAR_DATA"])
    if getattr(sys, "frozen", False):
        return Path.home() / "Library" / "Application Support" / "Radar"
    return Path(__file__).resolve().parent


DATA = _carpeta_datos()
DATA.mkdir(parents=True, exist_ok=True)
DB = DATA / "jobs.db"
ME = 1                                   # único usuario mientras esto sea local

# Fuentes que NO son del catálogo público. Una fuente que se lee con la sesión de
# una persona en una plataforma con licencia (aquí, un portal de empleo
# universitario) es SUYA: puede verla en su Radar y no puede servírsela a nadie
# más. Vive aquí y no en el HTML a propósito — un filtro en el cliente lo quita
# cualquiera con el inspector.
PRIVATE_CATS = ("🎓 Career Portal del IE",)
PRIVATE_OWNER = ME

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id         INTEGER PRIMARY KEY,
  key        TEXT    NOT NULL UNIQUE,     -- sha1 corto que ya calcula job_boards
  company    TEXT    NOT NULL,
  role       TEXT    NOT NULL DEFAULT '',
  url        TEXT    NOT NULL DEFAULT '',
  source     TEXT    NOT NULL DEFAULT '', -- id del tablero (row["repo"])
  category   TEXT    NOT NULL DEFAULT '', -- España / Europa / banca / consultoría
  board      TEXT    NOT NULL DEFAULT '', -- nombre legible de la fuente
  nospon     INTEGER NOT NULL DEFAULT 0,
  uscit      INTEGER NOT NULL DEFAULT 0,
  first_seen TEXT    NOT NULL,
  last_seen  TEXT    NOT NULL,
  kind       TEXT    NOT NULL DEFAULT 'oferta',  -- 'oferta' | 'enlace'
  owner_id   INTEGER,                            -- NULL = público; si no, su dueño
  -- Ubicación tal cual la publica la fuente ("London", "Madrid, Spain",
  -- "Chicago, IL"). Va aparte del rol porque los README de GitHub no la meten
  -- en el título: sin ella no hay forma de saber que el "Software Developer
  -- Intern" de DRW que trae vanshb03 es el de Chicago y no el de Londres.
  loc        TEXT    NOT NULL DEFAULT '',
  -- El texto del anuncio, en plano. Hasta que existió esta columna la app tenía
  -- de cada oferta el título y poco más: para saber DE QUÉ VA el puesto había
  -- que salirse a la web de la empresa, que es exactamente el momento en el que
  -- se deja de usar Radar.
  --
  -- Fuera de COLS a propósito: son kilobytes por fila y COLS viaja en todas las
  -- respuestas de la API. Se pide por separado, y solo en la ficha.
  descr      TEXT    NOT NULL DEFAULT '',
  descr_via  TEXT    NOT NULL DEFAULT '',  -- quién lo consiguió, o por qué no
  descr_at   TEXT    NOT NULL DEFAULT ''   -- último intento, con o sin suerte
);
CREATE INDEX IF NOT EXISTS jobs_seen  ON jobs (last_seen DESC);
CREATE INDEX IF NOT EXISTS jobs_owner ON jobs (owner_id);
CREATE TABLE IF NOT EXISTS user_jobs (
  user_id    INTEGER NOT NULL,
  job_id     INTEGER NOT NULL REFERENCES jobs (id),
  status     TEXT    NOT NULL DEFAULT 'nueva',
  updated_at TEXT    NOT NULL,
  PRIMARY KEY (user_id, job_id)
);
CREATE TABLE IF NOT EXISTS profiles (
  user_id    INTEGER PRIMARY KEY,
  cv_text    TEXT    NOT NULL DEFAULT '',
  updated_at TEXT    NOT NULL,
  -- Última vez que el usuario dio por vistas las novedades. La pestaña "Hoy"
  -- enseña lo que entró DESPUÉS de esta marca: sin ella, "qué hay nuevo" no
  -- tiene respuesta y hay que releer el catálogo entero cada mañana.
  seen_at    TEXT    NOT NULL DEFAULT ''
);
CREATE VIRTUAL TABLE IF NOT EXISTS jobs_fts
  USING fts5(company, role, descr, content='jobs', content_rowid='id');
-- El índice contado: qué términos existen y en cuántas ofertas aparece cada uno.
-- No es una copia, es el propio jobs_fts leído por otra puerta (fts5vocab), así
-- que no ocupa nada y no puede desincronizarse.
CREATE VIRTUAL TABLE IF NOT EXISTS jobs_vocab USING fts5vocab(jobs_fts, 'row');
"""

# Las columnas del índice, para saber si el de una base vieja se quedó atrás.
# `descr` entró el 2026-08-23: hasta entonces se buscaba solo por título y nombre
# de empresa, así que cuatro de cada cinco ofertas no casaban con el CV en NADA.
FTS_COLS = ("company", "role", "descr")

# Cuánto pesa cada columna en bm25(). El anuncio NO entra en el peso principal:
# entra como segundo nivel de orden (ver search()). Medido el 2026-08-23 sobre el
# catálogo real, sumarlo al mismo score —probado con pesos 0.3, 0.15, 0.08, 0.04,
# 0.02 y 0.01— hundía el ranking a cualquier peso: un anuncio de mil palabras
# acumula decenas de términos del CV y le gana a un título que dice justo tu
# puesto. Con 0.3, "AI Automation Engineer" (la nº 1 con el título) se caía del
# top 20 y subían becas de Legal Operations y de UX/UI. Muchas palabras genéricas
# en un texto largo no son encaje.
W_COMPANY, W_ROLE = 2.0, 1.0
# Dentro del nivel 1 el anuncio solo desempata. Es pequeño a propósito: dos
# títulos que puntúan IGUAL (pasa mucho, "AI/ML Research Intern" y "AI Operations
# Intern" empatan) los separa el cuerpo de la oferta, y nada más.
W_TIE = 0.005

# Las columnas que ve quien consume una fila. 'status' no es una columna de jobs:
# lo pone el LEFT JOIN con user_jobs.
COLS = ("id", "key", "company", "role", "url", "source", "category",
        "board", "nospon", "uscit", "first_seen", "last_seen", "loc", "status")

# Los tableros de GitHub y el ATS de la propia empresa no la llaman igual. Sin
# esto la lista por empresa enseña "Palantir" y "Palantir Technologies" aparte.
CANON = {"Palantir Technologies": "Palantir",
         "Virtu Financial": "Virtu",
         "Jump Trading Group": "Jump Trading",
         "Susquehanna Investment Group": "Susquehanna",
         "DE Shaw": "D. E. Shaw"}

# Fuentes que no son un anuncio concreto sino una convocatoria fija, mantenida a
# mano en job_boards: los programas de verano y las spring weeks. Su URL es la
# página de empleo de la empresa —no una oferta— y el resumen ya va escrito en el
# rol ("Summer Internships · verano (10-12 semanas, may-sep) · Londres"). No hay
# texto que ir a leer, así que se quedan fuera de la cola de descr.py: si no,
# cuarenta filas fallarían cada noche y la ficha diría "no se ha podido leer"
# cuando la verdad es "no hay nada que leer".
CURADAS = ("summer:", "insight:")


def es_curada(source):
    return (source or "").startswith(CURADAS)


# kind='enlace': fila-marcador de una empresa vigilada, con enlace a su web y sin
# oferta concreta. Marcas sin API pública (Goldman, Big Four…) y firmas con la
# convocatoria cerrada existen en la app gracias a ella. Fuera del buscador.
#
# Es catálogo, no estado: por eso es un `kind` de jobs y no un status de
# user_jobs. En user_jobs se quedaría sin dueño y desaparecería del listado.
LINK = "enlace"


def _loc(o):
    """Ubicación de una oferta. La manda job_boards en su propio campo; si esa
    fuente todavía no lo pone, se rescata del rol, donde varias la escriben
    detrás del último ' · ' ("Compliance Intern · London"). Que salga vacía es
    un resultado válido: significa "no sé dónde", y la dedup lo trata como tal
    en vez de dar por hecho que coincide."""
    if o.get("loc"):
        return o["loc"].strip()
    core, sep, tail = (o.get("role") or "").rpartition(" · ")
    return tail.strip() if sep else ""

# Estados de una candidatura, en orden. 'nueva' es el que devuelve el LEFT JOIN
# cuando no hay fila. LINK no está aquí: no es un estado.
STATES = ("nueva", "preparada", "enviada", "respuesta")

# Fila completa + estado del usuario. El user_id del JOIN va siempre PRIMERO en
# los parámetros; el de la visibilidad, después.
_ROW = ("j.id, j.key, j.company, j.role, j.url, j.source, j.category, j.board,"
        " j.nospon, j.uscit, j.first_seen, j.last_seen, j.loc,"
        " COALESCE(u.status, 'nueva') AS status")
_FROM = "FROM jobs j LEFT JOIN user_jobs u ON u.job_id = j.id AND u.user_id = ?"
# Una fuente privada solo la ve su dueño. Va en TODA consulta de catálogo.
_VIS = "(j.owner_id IS NULL OR j.owner_id = ?)"


def connect(path=DB):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    _migrate(conn)                       # antes del SCHEMA: si no, no lo ve venir
    conn.executescript(SCHEMA)
    _add_columns(conn)                   # después: las bases ya creadas no ven SCHEMA
    _fts_al_dia(conn)                    # y después de _add_columns: rebuild lee `descr`
    return conn


def _fts_al_dia(conn):
    """Rehace el índice si sus columnas ya no son las del SCHEMA.

    FTS5 de contenido externo no tiene ALTER: meter `descr` en el índice es
    tirarlo y reconstruirlo desde `jobs`. No es una pérdida —el contenido vive en
    la tabla, el índice solo lo apunta— y con ~600 filas el rebuild tarda
    milisegundos, así que puede correr en cada connect().

    Va aquí y no en un script suelto porque el fallo que evita es silencioso: una
    base vieja seguiría funcionando, con la misma pantalla y los mismos números,
    solo que sin mirar el anuncio. Un índice desactualizado no da error, da menos
    resultados."""
    cols = tuple(r["name"] for r in conn.execute("PRAGMA table_info(jobs_fts)"))
    if cols == FTS_COLS:
        return False
    conn.executescript("DROP TABLE IF EXISTS jobs_fts;")
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO jobs_fts (jobs_fts) VALUES ('rebuild')")
    conn.commit()
    return True


def _add_columns(conn):
    """Columnas añadidas después de que la base existiera. `CREATE TABLE IF NOT
    EXISTS` no las mete en una base ya creada, así que van por ALTER.

    Idempotente y aditivo: nunca borra ni reescribe nada, así que puede correr
    en cada connect() y una base vieja se pone al día sola."""
    nuevas = {"jobs":     (("loc", "TEXT NOT NULL DEFAULT ''"),
                           ("descr", "TEXT NOT NULL DEFAULT ''"),
                           ("descr_via", "TEXT NOT NULL DEFAULT ''"),
                           ("descr_at", "TEXT NOT NULL DEFAULT ''")),
              "profiles": (("seen_at", "TEXT NOT NULL DEFAULT ''"),)}
    for tabla, columnas in nuevas.items():
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(%s)" % tabla)}
        if not cols:                     # base recién nacida: SCHEMA ya la trae
            continue
        for name, decl in columnas:
            if name not in cols:
                conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (tabla, name, decl))
    conn.commit()


def _migrate(conn):
    """Esquema viejo (una tabla con user_id) -> jobs + user_jobs. Idempotente: si
    ya está partido no hace nada, así que puede correr en cada connect()."""
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(jobs)")]
    if not cols or "user_id" not in cols:
        return                           # base nueva, o ya migrada
    priv = ",".join("?" * len(PRIVATE_CATS))
    conn.executescript("ALTER TABLE jobs RENAME TO jobs_old;"
                       "DROP TABLE IF EXISTS jobs_fts;")
    conn.executescript(SCHEMA)
    # Dedup por key: el esquema viejo permitía una copia por usuario y el nuevo
    # no. Se queda la más antigua, que es la que conserva su first_seen.
    conn.execute(
        "INSERT INTO jobs (id, key, company, role, url, source, category, board,"
        " nospon, uscit, first_seen, last_seen, kind, owner_id)"
        " SELECT MIN(id), key, company, role, url, source, category, board,"
        " nospon, uscit, MIN(first_seen), MAX(last_seen),"
        " CASE WHEN status = ? THEN ? ELSE 'oferta' END,"
        " CASE WHEN category IN (%s) THEN ? ELSE NULL END"
        " FROM jobs_old GROUP BY key" % priv,
        (LINK, LINK) + PRIVATE_CATS + (PRIVATE_OWNER,))
    # Solo lo que el usuario tocó de verdad: 'nueva' es el default del JOIN y
    # escribirla sería justo la duplicación que esta migración viene a quitar.
    conn.execute(
        "INSERT OR IGNORE INTO user_jobs (user_id, job_id, status, updated_at)"
        " SELECT o.user_id, j.id, o.status, o.last_seen FROM jobs_old o"
        " JOIN jobs j ON j.key = o.key"
        " WHERE o.status NOT IN ('nueva', ?)", (LINK,))
    conn.executescript("INSERT INTO jobs_fts (jobs_fts) VALUES ('rebuild');"
                       "DROP TABLE jobs_old;")
    conn.commit()


def upsert(conn, offers, source="", category="", board="", kind="oferta",
           owner_id=None):
    """Mete ofertas nuevas en el CATÁLOGO; a las ya vistas solo les refresca
    last_seen. `offers` son los dicts que ya produce job_boards. Devuelve cuántas
    eran nuevas.

    No toca user_jobs: reingestar no puede pisar el estado de nadie, y ahora eso
    es estructural en vez de una condición del UPDATE.

    owner_id sale de la categoría si no se pasa: la política de qué fuente es
    privada vive aquí, no en el scraper."""
    if owner_id is None and category in PRIVATE_CATS:
        owner_id = PRIVATE_OWNER
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    new = 0
    for o in offers:
        # `loc` solo se rellena si está vacía: una fila anterior al campo se cura
        # sola la próxima vez que su fuente la vea, y nunca se pisa lo ya escrito.
        cur = conn.execute(
            "UPDATE jobs SET last_seen=?,"
            " loc=CASE WHEN loc='' THEN ? ELSE loc END WHERE key=?",
            (now, _loc(o), o["key"]))
        if cur.rowcount:
            continue
        company = CANON.get(o.get("company") or "", o.get("company") or "")
        rid = conn.execute(
            "INSERT INTO jobs (key, company, role, url, source, category,"
            " board, nospon, uscit, first_seen, last_seen, kind, owner_id, loc)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            # `or ""`, no `.get(k, "")`: job_boards deja url=None cuando la fila
            # del README no trae enlace, y las columnas son NOT NULL.
            (o["key"], company, o.get("role") or "",
             o.get("url") or "", source, category, board,
             int(bool(o.get("nospon"))), int(bool(o.get("uscit"))), now, now,
             kind, owner_id, _loc(o)),
        ).lastrowid
        # FTS de contenido externo: se sincroniza aquí porque este es el único
        # sitio del proyecto que inserta ofertas.
        # Sin `descr`: la oferta acaba de entrar y el texto lo trae descr.py más
        # tarde, que reindexa esta misma fila al guardarlo.
        conn.execute(
            "INSERT INTO jobs_fts (rowid, company, role, descr) VALUES (?,?,?,'')",
            (rid, company, o.get("role") or ""))
        new += 1
    conn.commit()
    return new


def set_descr(conn, job_id, texto, via, cuando=None):
    """Guarda el texto del anuncio de una oferta.

    Se escribe SIEMPRE, con texto o sin él. Un intento fallido deja descr='' pero
    con su `via` y su `descr_at`, y así el rellenador sabe que ya pasó por ahí.
    Sin esa marca, las que no se van a poder leer nunca —amazon.jobs se pinta con
    JavaScript, 42 ofertas— se llevarían el presupuesto de todas las pasadas."""
    # Los valores que el índice tiene AHORA de esta fila. Se leen antes de
    # pisarlos porque el comando 'delete' de fts5 exige los originales al byte: si
    # no coinciden, el índice queda corrupto en silencio (no da error, devuelve
    # menos ofertas). Leerlos de la propia tabla es la única forma de acertar
    # siempre.
    prev = conn.execute("SELECT company, role, descr FROM jobs WHERE id=?",
                        (job_id,)).fetchone()
    conn.execute(
        "UPDATE jobs SET descr=?, descr_via=?, descr_at=? WHERE id=?",
        (texto or "", via,
         cuando or datetime.now(timezone.utc).isoformat(timespec="seconds"), job_id))
    if prev is not None:
        # Un UPDATE no llega al índice: hay que sacar la fila vieja y volver a
        # meterla con el texto nuevo. Sin esto el anuncio se guardaría en la base
        # y seguiría sin contar para el ranking, que es justo lo que se venía a
        # arreglar.
        conn.execute(
            "INSERT INTO jobs_fts (jobs_fts, rowid, company, role, descr)"
            " VALUES ('delete', ?, ?, ?, ?)",
            (job_id, prev["company"], prev["role"], prev["descr"] or ""))
        conn.execute(
            "INSERT INTO jobs_fts (rowid, company, role, descr) VALUES (?,?,?,?)",
            (job_id, prev["company"], prev["role"], texto or ""))
    conn.commit()
    return conn.total_changes


def descr(conn, job_id):
    """El texto del anuncio, para la ficha. Va en su propia consulta y no en
    COLS: mete kilobytes por fila y COLS sale en TODAS las respuestas — una
    lista de 200 ofertas pasaría de 60 KB a varios megas."""
    r = conn.execute("SELECT descr, descr_via, descr_at FROM jobs WHERE id=?",
                     (job_id,)).fetchone()
    return dict(r) if r else None


def sin_descr(conn, limit=50, reintentar=None):
    """Las ofertas a las que les falta el texto, las nunca intentadas primero.

    `reintentar` (ISO) devuelve a la cola las que fallaron ANTES de esa fecha: un
    404 de hoy puede ser un despliegue a medias de la fuente, y la oferta que hoy
    no se deja leer mañana sí. Sin esa fecha solo salen las vírgenes.

    Solo ofertas con URL: la fila-marcador de una empresa vigilada no tiene
    anuncio que leer."""
    corte = " AND (descr_at='' OR descr_at < ?)" if reintentar else " AND descr_at=''"
    curadas = "".join(" AND source NOT LIKE '%s%%'" % p for p in CURADAS)
    return conn.execute(
        "SELECT id, url, company, role FROM jobs"
        " WHERE kind<>? AND url<>'' AND descr=''" + curadas + corte +
        " ORDER BY descr_at, last_seen DESC LIMIT ?",
        (LINK,) + ((reintentar,) if reintentar else ()) + (limit,)).fetchall()


def descr_stats(conn):
    """Cuántas ofertas tienen texto, cuántas se intentaron sin suerte y cuántas
    no se han tocado. Es el número que dice si vale la pena otra pasada."""
    r = conn.execute(
        "SELECT COUNT(*) AS n,"
        " SUM(descr<>'') AS con,"
        " SUM(descr='' AND descr_at<>'') AS fallidas,"
        " SUM(descr='' AND descr_at='') AS virgenes"
        " FROM jobs WHERE kind<>? AND url<>''"
        + "".join(" AND source NOT LIKE '%s%%'" % p for p in CURADAS), (LINK,)).fetchone()
    return {k: r[k] or 0 for k in r.keys()}


def set_status(conn, job_id, status, user_id=ME):
    """Cambia el estado de una candidatura. True si la oferta era suya de tocar.

    Rechaza estados desconocidos (para que un POST raro no meta basura) y no
    toca las filas-marcador de empresa: convertir una en oferta, o al revés, la
    sacaría de companies() sin que se note."""
    if status not in STATES:
        raise ValueError("estado desconocido: %r" % (status,))
    ok = conn.execute(
        "SELECT 1 FROM jobs j WHERE j.id=? AND j.kind<>? AND " + _VIS,
        (job_id, LINK, user_id)).fetchone()
    if not ok:
        return False
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO user_jobs (user_id, job_id, status, updated_at)"
        " VALUES (?,?,?,?) ON CONFLICT (user_id, job_id)"
        " DO UPDATE SET status=excluded.status, updated_at=excluded.updated_at",
        (user_id, job_id, status, now))
    conn.commit()
    return True


def purge_source(conn, source, keep_tracked=True):
    """Borra del catálogo todo lo de una fuente. Devuelve (borradas, conservadas).

    Existe porque una fuente puede MORIR. El catálogo no caduca nada por edad, y
    es a propósito: que una oferta no aparezca una noche casi siempre es un fallo
    de red, no un cierre, y expirarlas solas borraría medio catálogo cada vez que
    un scraper tose. El precio es que cuando la muerta es la FUENTE entera —el
    portal del IE dejó de responder el 13/08/2026 y sus 169 ofertas se quedaron
    ahí congeladas— nadie las quita si no se dice a mano. Esto es ese "a mano".

    `keep_tracked` deja fuera las que tienen candidatura: la oferta es la única
    traza de que te presentaste, y borrarla perdería el historial sin avisar. Se
    devuelven contadas aparte para que quien llama vea que quedó algo vivo.

    El FTS se reconstruye entero en vez de ir borrando fila a fila con el comando
    'delete' de fts5: ese comando exige repetir los valores ORIGINALES de company
    y role, y si no coinciden al byte el índice queda corrupto en silencio. Con
    ~1000 filas el rebuild es instantáneo y no puede desincronizarse."""
    filas = conn.execute("SELECT id FROM jobs WHERE source=?", (source,)).fetchall()
    ids = [r["id"] for r in filas]
    if not ids:
        return 0, 0
    if keep_tracked:
        marcadas = {r["job_id"] for r in conn.execute(
            "SELECT DISTINCT job_id FROM user_jobs")}
        borrar = [i for i in ids if i not in marcadas]
    else:
        borrar = ids
    hueco = ",".join("?" * len(borrar))
    if borrar:
        conn.execute("DELETE FROM jobs WHERE id IN (%s)" % hueco, borrar)
        conn.execute("INSERT INTO jobs_fts (jobs_fts) VALUES ('rebuild')")
    conn.commit()
    return len(borrar), len(ids) - len(borrar)


def get_cv(conn, user_id=ME):
    """El CV del usuario, en texto. '' si todavía no ha puesto ninguno.

    Es dato personal (nombre, teléfono, formación, historial): sale de aquí y de
    ningún otro sitio, y el botón de borrar cuenta (§4 del plan) tiene que vaciar
    esta tabla igual que user_jobs."""
    r = conn.execute("SELECT cv_text FROM profiles WHERE user_id=?",
                     (user_id,)).fetchone()
    return r["cv_text"] if r else ""


def set_cv(conn, cv_text, user_id=ME):
    """Guarda (o reemplaza) el CV. Devuelve cuántos caracteres quedaron."""
    cv_text = (cv_text or "").strip()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO profiles (user_id, cv_text, updated_at) VALUES (?,?,?)"
        " ON CONFLICT (user_id) DO UPDATE SET cv_text=excluded.cv_text,"
        " updated_at=excluded.updated_at", (user_id, cv_text, now))
    conn.commit()
    return len(cv_text)


# Un término que aparece en más de esta parte del catálogo no distingue nada:
# "business" está en el 34% de los anuncios, "development" en el 29%, "data" en
# el 28%. Solo se cae de lo que se ENSEÑA, y solo en el CUERPO del anuncio: en un
# título, "Data Engineering" es la etiqueta del puesto y dice mucho; en mil
# palabras de prosa, "data" es relleno. El ranking los sigue viendo igual — bm25
# ya castiga lo frecuente por su cuenta.
#
# Se calcula contra el catálogo en vez de escribirse a mano porque el umbral se
# mueve solo: cuantos más anuncios se lean, mejor sabe Radar qué es genérico.
GENERICO = 0.25


def frecuencias(conn, terms):
    """Cuántas ofertas menciona cada término, sacado del propio índice (fts5vocab).

    Es lo que separa lo informativo de lo que dice todo el mundo: "claude" está
    en 12 ofertas y "learning" en 147, y sin este número las dos se enseñan
    igual. Se pregunta solo por los términos del CV, no por el vocabulario
    entero: son unos cientos contra decenas de miles."""
    terms = tuple(terms)
    if not terms:
        return {}
    hueco = ",".join("?" * len(terms))
    return {r["term"]: r["doc"] for r in conn.execute(
        "SELECT term, doc FROM jobs_vocab WHERE term IN (%s)" % hueco, terms)}


# Por debajo de esto no hay estadística que valga: en un catálogo de 20 ofertas,
# un término que salga en 6 no es genérico, es que hay 20 ofertas. Importa de
# verdad el día que alguien instale Radar y arranque con la base casi vacía.
MINIMO_VOCAB = 40


def genericos(conn, terms, umbral=GENERICO):
    """Los términos que aparecen en tantas ofertas que no dicen nada del encaje."""
    n = conn.execute("SELECT COUNT(*) AS n FROM jobs WHERE kind<>?",
                     (LINK,)).fetchone()["n"]
    if n < MINIMO_VOCAB:
        return set()
    return {t for t, doc in frecuencias(conn, terms).items() if doc > n * umbral}


def _fts_query(text):
    """Texto libre -> consulta FTS5 segura. Entrecomilla cada palabra para que
    guiones, comillas o '*' del usuario no revienten el parser."""
    words = re.findall(r"\w+", text or "", re.UNICODE)
    return " OR ".join('"%s"' % w for w in words)


def search(conn, query="", limit=50, user_id=ME, since=None):
    """ÚNICA función no portable. bm25() devuelve negativo: menor = mejor.

    Ordena en DOS niveles desde el 2026-08-23, cuando el índice empezó a leer
    también el texto del anuncio: primero las ofertas cuyo TÍTULO (o empresa)
    casa con la consulta, y detrás las que solo casan en el cuerpo del anuncio.
    El título dice lo que el puesto ES; el anuncio, de qué habla. Mezclarlos en
    un solo número no funciona a ningún peso (medido: ver W_COMPANY arriba).

    Lo que compra el segundo nivel: con el CV entero como consulta, casaban 124
    ofertas de 532 y las otras 408 no aparecían en "Para ti" aunque el anuncio
    hablase de tu stack. Ahora casan 366. Las 166 que siguen fuera no casan en
    nada, y 121 de ellas es que su anuncio no se ha podido leer todavía.

    `since` (ISO-8601) deja solo las ofertas que ENTRARON al catálogo después de
    esa marca: es la pestaña "Hoy". Va aquí y no en una función aparte por la
    decisión 3 del proyecto — toda la búsqueda en una sola función, para que
    migrar a Postgres sea reescribir esto y nada más."""
    q = _fts_query(query)
    desde = " AND j.first_seen > ?" if since else ""
    if not q:
        # Con `since`, lo interesante es el orden de llegada; sin él, `last_seen`
        # (todo el catálogo se refresca cada noche, así que empata y desempata id).
        orden = "j.first_seen DESC, j.id DESC" if since else "j.last_seen DESC, j.id DESC"
        return conn.execute(
            "SELECT %s %s WHERE j.kind<>? AND %s%s"
            " ORDER BY %s LIMIT ?" % (_ROW, _FROM, _VIS, desde, orden),
            (user_id, LINK, user_id) + ((since,) if since else ()) + (limit,)).fetchall()
    # Dos bm25 sobre el mismo MATCH: uno mira solo el título y el nombre de la
    # empresa, el otro solo el anuncio. Una fila que no casa en el título saca
    # exactamente 0.0 en el primero, y eso es lo que parte la lista en dos
    # niveles. Los pesos van pegados al SQL y no como parámetros: fts5 los quiere
    # constantes; son constantes del módulo, no texto de nadie.
    return conn.execute(
        "SELECT %s, bm25(jobs_fts, %s, %s, 0.0) AS score,"
        " bm25(jobs_fts, 0.0, 0.0, 1.0) AS score_txt %s"
        " JOIN jobs_fts ON jobs_fts.rowid = j.id"
        " WHERE jobs_fts MATCH ? AND j.kind<>? AND %s%s"
        " ORDER BY (score = 0) ASC, score + %s * score_txt LIMIT ?"
        % (_ROW, W_COMPANY, W_ROLE, _FROM, _VIS, desde, W_TIE),
        (user_id, q, LINK, user_id) + ((since,) if since else ()) + (limit,)).fetchall()


def tracked(conn, user_id=ME):
    """Las candidaturas VIVAS del usuario: todo lo que ha tocado y no sigue en
    'nueva', lo último movido primero.

    Es la mitad que le faltaba a la fase 5: se podía marcar el estado de una
    oferta pero no había ninguna pantalla donde volver a verlas, así que marcar
    no servía de nada (user_jobs llevaba 0 filas desde que se construyó)."""
    return conn.execute(
        "SELECT %s, u.updated_at AS updated_at %s"
        " WHERE u.status IS NOT NULL AND u.status<>'nueva' AND j.kind<>? AND %s"
        # updated_at va al segundo: dos cambios en el mismo segundo empatan, y
        # sin desempate el orden lo decidiría SQLite. Desempata el id (la oferta
        # que entró después al catálogo).
        " ORDER BY u.updated_at DESC, j.id DESC" % (_ROW, _FROM, _VIS),
        (user_id, LINK, user_id)).fetchall()


def get_seen(conn, user_id=ME):
    """Marca de "hasta aquí ya lo he visto". '' si el usuario no ha marcado nunca."""
    r = conn.execute("SELECT seen_at FROM profiles WHERE user_id=?",
                     (user_id,)).fetchone()
    return (r["seen_at"] if r else "") or ""


def set_seen(conn, user_id=ME, when=None):
    """Da por vistas las novedades. Devuelve la marca guardada.

    Escribe en `profiles` con INSERT ... ON CONFLICT porque el usuario puede
    marcar vistas antes de haber pegado ningún CV: la fila puede no existir."""
    now = when or datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO profiles (user_id, cv_text, updated_at, seen_at)"
        " VALUES (?,'',?,?) ON CONFLICT (user_id) DO UPDATE SET seen_at=excluded.seen_at",
        (user_id, now, now))
    conn.commit()
    return now


def companies(conn, user_id=ME):
    """Una fila por empresa vigilada: cuántas becas tiene abiertas ahora mismo y
    el enlace a su web. Las que hoy no tienen ninguna salen con n=0 — que JP
    Morgan esté vigilado es información, no ruido.

    Del catálogo, sin estado: aquí no hay nada que sea de un usuario."""
    return conn.execute(
        "SELECT j.company, SUM(j.kind<>'enlace') n,"
        " MAX(CASE WHEN j.kind='enlace' THEN j.url END) link,"
        " MAX(j.category) category, MAX(j.board) board, MAX(j.last_seen) last_seen"
        " FROM jobs j WHERE " + _VIS + " GROUP BY j.company"
        " ORDER BY n DESC, j.company", (user_id,)).fetchall()


def get(conn, job_id, user_id=ME):
    """Una oferta por id, para su ficha. Sin filtro de kind: la fila-marcador no
    se lista en ningún sitio, así que aquí nadie llega con su id."""
    return conn.execute(
        "SELECT %s %s WHERE j.id=? AND %s" % (_ROW, _FROM, _VIS),
        (user_id, job_id, user_id)).fetchone()


def by_company(conn, company, user_id=ME):
    """Las ofertas reales de una empresa (la fila-marcador no es una oferta)."""
    return conn.execute(
        "SELECT %s %s WHERE j.company=? AND j.kind<>? AND %s"
        " ORDER BY j.last_seen DESC, j.id DESC" % (_ROW, _FROM, _VIS),
        (user_id, company, LINK, user_id)).fetchall()


def stats(conn, user_id=ME):
    r = conn.execute(
        "SELECT COUNT(*) n, MIN(first_seen) desde, MAX(last_seen) hasta"
        " FROM jobs j WHERE j.kind<>? AND " + _VIS, (LINK, user_id)).fetchone()
    return dict(r)


def _demo():
    conn = connect(":memory:")
    offers = [
        {"key": "a1", "company": "BBVA", "role": "Beca Quant Finance · Madrid"},
        {"key": "b2", "company": "Citadel", "role": "Software Engineer Intern · London",
         "url": None, "nospon": True},          # sin enlace: job_boards manda None
    ]
    assert upsert(conn, offers, source="es:BBVA", category="España") == 2
    assert upsert(conn, offers) == 0, "la misma oferta no se duplica"
    assert stats(conn)["n"] == 2

    # --- estados de candidatura ---
    jid = search(conn, "BBVA")[0]["id"]
    assert get(conn, jid)["status"] == "nueva", "arranca en 'nueva'"
    assert conn.execute("SELECT COUNT(*) c FROM user_jobs").fetchone()["c"] == 0, \
        "'nueva' es el default del JOIN: no escribe fila"
    assert set_status(conn, jid, "enviada") is True
    assert get(conn, jid)["status"] == "enviada"
    assert set_status(conn, jid, "respuesta") is True, "y se puede volver a cambiar"
    assert get(conn, jid)["status"] == "respuesta"
    try:
        set_status(conn, jid, "enlace")        # ni 'enlace' ni nada fuera de STATES
        raise AssertionError("debería rechazar un estado desconocido")
    except ValueError:
        pass
    assert set_status(conn, 9999, "enviada") is False, "id que no existe"
    assert upsert(conn, offers) == 0 and get(conn, jid)["status"] == "respuesta", \
        "reingestar no puede pisar el estado"
    # El estado es POR USUARIO: para otro, la misma oferta sigue siendo nueva.
    assert get(conn, jid, user_id=2)["status"] == "nueva"
    assert search(conn, "BBVA", user_id=2)[0]["status"] == "nueva"

    # --- filas-marcador de empresa vigilada ---
    assert upsert(conn, [{"key": "c3", "company": "Goldman Sachs", "role": "web",
                          "url": "https://gs.com"}], kind=LINK) == 1
    marker = conn.execute("SELECT id FROM jobs WHERE key='c3'").fetchone()["id"]
    assert set_status(conn, marker, "enviada") is False, "no se toca la fila-marcador"
    # Empresa vigilada sin API pública: existe en la app aunque no tenga ofertas.
    assert upsert(conn, [{"key": "gs", "company": "Goldman Sachs", "role": "ver web",
                          "url": "https://gs.com/careers"}], kind=LINK) == 1
    assert upsert(conn, [{"key": "p1", "company": "Palantir Technologies",
                          "role": "SWE Intern · London"}]) == 1

    hits = search(conn, "quant finance")
    assert hits[0]["company"] == "BBVA", [dict(h) for h in hits]
    assert search(conn, 'software -- * "')[0]["company"] == "Citadel", "query sucia"
    assert search(conn, '" OR OR *') == [], "query solo de basura no revienta"
    assert len(search(conn, "")) == 3, "sin query = las recientes, sin marcadores"
    assert search(conn, "biotecnología") == []
    assert dict(hits[0])["nospon"] == 0
    assert search(conn, "goldman") == [], "la fila-marcador no es un resultado"
    assert set(dict(hits[0])) == set(COLS) | {"score", "score_txt"}, dict(hits[0])
    # score_txt es el bm25 del anuncio: existe para partir el orden en dos
    # niveles, no para enseñarse. _fila() solo copia COLS, así que no sale.

    cs = {r["company"]: r for r in companies(conn)}
    assert cs["Goldman Sachs"]["n"] == 0, "vigilada, sin becas hoy"
    assert cs["Goldman Sachs"]["link"] == "https://gs.com/careers"
    assert "Palantir Technologies" not in cs, "CANON no ha unificado el nombre"
    assert cs["Palantir"]["n"] == 1 and cs["Palantir"]["link"] is None
    assert len(by_company(conn, "Goldman Sachs")) == 0
    bbva = by_company(conn, "BBVA")[0]
    assert bbva["role"].startswith("Beca Quant")
    assert get(conn, bbva["id"])["company"] == "BBVA"
    assert get(conn, 9999) is None, "id que no existe = None, no una excepción"

    # --- fuente privada: solo la ve su dueño ---
    assert upsert(conn, [{"key": "ie1", "company": "Blackstone",
                          "role": "Summer Analyst · London"}],
                  category=PRIVATE_CATS[0]) == 1
    assert stats(conn)["n"] == 4 and stats(conn, user_id=2)["n"] == 3
    assert search(conn, "blackstone", user_id=2) == [], "fuente privada de otro"
    assert search(conn, "blackstone")[0]["company"] == "Blackstone"
    ie_id = search(conn, "blackstone")[0]["id"]
    assert get(conn, ie_id, user_id=2) is None
    assert set_status(conn, ie_id, "enviada", user_id=2) is False, \
        "ni siquiera se le puede poner estado a lo que no se ve"
    assert "Blackstone" not in {r["company"] for r in companies(conn, user_id=2)}
    assert "Blackstone" in {r["company"] for r in companies(conn)}

    # --- perfil: el CV es de cada usuario, no del disco ---
    assert get_cv(conn) == "", "sin perfil no hay CV, y no es un error"
    assert set_cv(conn, "  ## SKILLS\npython, sql  ") == 21
    assert get_cv(conn) == "## SKILLS\npython, sql", "lo guarda sin espacios sobrantes"
    assert get_cv(conn, user_id=2) == "", "el CV de uno no es el de otro"
    set_cv(conn, "otro CV", user_id=2)
    assert get_cv(conn) == "## SKILLS\npython, sql", "y guardar el de otro no lo pisa"
    assert set_cv(conn, "CV v2") == 5 and get_cv(conn) == "CV v2", "se reemplaza"

    # --- "Hoy": novedades desde una marca, y candidaturas vivas ---
    assert get_seen(conn) == "", "sin marca, no hay 'desde'"
    # Las 4 ofertas de arriba entraron ahora, así que una marca de mañana no deja
    # ninguna y una de ayer las deja todas: eso es lo que distingue "Hoy" del
    # listado normal, que no mira first_seen.
    manana = "2999-01-01T00:00:00+00:00"
    assert search(conn, "", since=manana) == [], "nada entró después del futuro"
    assert search(conn, "BBVA", since=manana) == [], "y tampoco con query"
    assert len(search(conn, "", since="2000-01-01")) == 4, "todas son posteriores a 2000"
    assert search(conn, "BBVA", since="2000-01-01")[0]["company"] == "BBVA"

    marca = set_seen(conn)
    assert get_seen(conn) == marca and marca > "2026", "la marca se guarda"
    assert set_seen(conn, user_id=3), "se puede marcar sin haber puesto CV nunca"
    assert get_cv(conn) == "CV v2", "y marcar visto no pisa el CV de nadie"

    # tracked(): la pantalla que le faltaba a la fase 5. 'nueva' no cuenta —
    # si contara, saldría el catálogo entero en vez de lo que estás moviendo.
    vivas = {r["company"]: r["status"] for r in tracked(conn)}
    assert vivas == {"BBVA": "respuesta"}, vivas
    assert tracked(conn, user_id=2) == [], "las candidaturas son de cada usuario"
    assert set_status(conn, search(conn, "citadel")[0]["id"], "preparada")
    assert [r["company"] for r in tracked(conn)][0] == "Citadel", "lo último movido, primero"
    assert "updated_at" in tracked(conn)[0].keys(), "y trae cuándo se movió"

    # --- purga de una fuente muerta ---
    antes = stats(conn)["n"]
    assert upsert(conn, [{"key": "z1", "company": "Zombi", "role": "Beca"},
                         {"key": "z2", "company": "Zombi", "role": "Otra"}],
                  source="muerta:portal") == 2
    z2 = [r for r in search(conn, "zombi") if r["key"] == "z2"][0]["id"]
    assert set_status(conn, z2, "enviada") is True
    assert purge_source(conn, "muerta:portal") == (1, 1), \
        "se va la suelta; la que tiene candidatura se queda"
    assert len(search(conn, "zombi")) == 1, "y el FTS ya no la devuelve"
    assert purge_source(conn, "muerta:portal", keep_tracked=False) == (1, 0)
    assert search(conn, "zombi") == [] and stats(conn)["n"] == antes
    assert purge_source(conn, "muerta:portal") == (0, 0), "fuente vacía no rompe"

    # --- migración desde el esquema viejo ---
    old = sqlite3.connect(":memory:")
    old.row_factory = sqlite3.Row
    old.executescript("""
      CREATE TABLE jobs (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL DEFAULT 1,
        key TEXT NOT NULL, company TEXT NOT NULL, role TEXT NOT NULL DEFAULT '',
        url TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '',
        category TEXT NOT NULL DEFAULT '', board TEXT NOT NULL DEFAULT '',
        nospon INTEGER NOT NULL DEFAULT 0, uscit INTEGER NOT NULL DEFAULT 0,
        first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'nueva', UNIQUE (user_id, key));
      CREATE VIRTUAL TABLE jobs_fts USING fts5(company, role, content='jobs',
        content_rowid='id');
      INSERT INTO jobs (key, company, role, category, first_seen, last_seen, status)
        VALUES ('k1','BBVA','Beca Quant','España','2026-01-01','2026-02-01','enviada'),
               ('k2','Citadel','SWE Intern','España','2026-01-01','2026-02-01','nueva'),
               ('k3','Goldman Sachs','web','España','2026-01-01','2026-02-01','enlace'),
               ('k4','Blackstone','Summer Analyst','🎓 Career Portal del IE',
                '2026-01-01','2026-02-01','preparada');
    """)
    old.commit()
    _migrate(old)
    old.executescript(SCHEMA)
    assert stats(old)["n"] == 3, "3 ofertas + 1 marcador"
    assert stats(old)["desde"] == "2026-01-01", "conserva el first_seen"
    assert stats(old, user_id=2)["n"] == 2, "la del IE no es del catálogo público"
    assert {r["status"] for r in search(old, "BBVA")} == {"enviada"}, "estado migrado"
    assert search(old, "citadel")[0]["status"] == "nueva"
    assert old.execute("SELECT COUNT(*) c FROM user_jobs").fetchone()["c"] == 2, \
        "solo las tocadas: 'nueva' y 'enlace' no escriben fila"
    assert search(old, "goldman") == [] and \
        {r["company"] for r in companies(old)} >= {"Goldman Sachs"}, \
        "el marcador pasa a kind, no a user_jobs"
    _migrate(old)                          # idempotente: correr dos veces no rompe
    assert stats(old)["n"] == 3
    # --- texto del anuncio ---
    c = connect(":memory:")
    upsert(c, [{"key": "d1", "company": "Tacto", "role": "AI Intern",
                "url": "https://x/1"},
               {"key": "d2", "company": "Amazon", "role": "SDE Intern",
                "url": "https://y/2"},
               {"key": "d3", "company": "Goldman", "role": "web", "url": ""}])
    assert descr_stats(c) == {"n": 2, "con": 0, "fallidas": 0, "virgenes": 2}, \
        "sin URL no hay anuncio que leer: la fila-marcador no cuenta"
    assert [r["company"] for r in sin_descr(c)] == ["Tacto", "Amazon"]
    # Una convocatoria fija no tiene anuncio: ni entra en la cola ni cuenta en el
    # porcentaje, o el "80% con texto" mediría contra ofertas que no existen.
    upsert(c, [{"key": "d4", "company": "Jane Street", "role": "Summer · verano",
                "url": "https://js/int"}], source="summer:programas")
    assert [r["company"] for r in sin_descr(c)] == ["Tacto", "Amazon"], \
        "la convocatoria curada no se pide a la red"
    assert descr_stats(c)["n"] == 2, descr_stats(c)
    assert es_curada("summer:programas") and not es_curada("gh:celonis")
    set_descr(c, 1, "el texto de la oferta", "workday")
    set_descr(c, 2, "", "json-ld: 0 caracteres")
    assert descr_stats(c) == {"n": 2, "con": 1, "fallidas": 1, "virgenes": 0}
    # Un fallo también se apunta, y por eso deja de salir en la cola: sin esa
    # marca las que nunca se van a poder leer se llevan todas las pasadas.
    assert sin_descr(c) == [], "lo ya intentado no vuelve a la cola"
    assert [r["company"] for r in sin_descr(c, reintentar="2999-01-01")] == ["Amazon"], \
        "con fecha de reintento vuelve la fallida, no la que ya tiene texto"
    assert descr(c, 1)["descr"] == "el texto de la oferta"
    assert descr(c, 2)["descr"] == "" and descr(c, 2)["descr_via"].startswith("json-ld")
    # Y reingestar no puede pisar el texto: upsert solo toca last_seen.
    upsert(c, [{"key": "d1", "company": "Tacto", "role": "AI Intern", "url": "https://x/1"}])
    assert descr(c, 1)["descr"] == "el texto de la oferta", "upsert pisó el anuncio"
    # descr fuera de COLS: si entra, cada respuesta de la API se lleva el texto
    # de 200 ofertas por delante.
    assert "descr" not in COLS, "el texto no puede viajar en las listas"

    # --- el anuncio cuenta para el ranking (2026-08-23) ---
    # Lo que buscaba esta entrega: una palabra que solo está en el CUERPO de la
    # oferta tiene que encontrarla. Antes de indexar `descr` esto daba [].
    set_descr(c, 1, "Buscamos alguien con Kubernetes y ganas", "workday")
    assert [r["company"] for r in search(c, "kubernetes")] == ["Tacto"], \
        "una palabra que solo está en el anuncio no encuentra la oferta"
    # Y el índice se mantiene: reescribir el anuncio se lleva el término viejo.
    # Sin el 'delete' de fts5 la oferta seguiría saliendo por lo que ya no dice.
    set_descr(c, 1, "Ahora es una beca de Solidity", "workday")
    assert search(c, "kubernetes") == [], "el índice se quedó con el texto viejo"
    assert [r["company"] for r in search(c, "solidity")] == ["Tacto"]
    # El título manda sobre el cuerpo: quien lo lleva en el nombre va primero
    # aunque el otro lo repita. Es lo que compran los pesos de bm25.
    upsert(c, [{"key": "d5", "company": "Otra", "role": "Solidity Intern",
                "url": "https://z/5"}])
    set_descr(c, 5, "beca de marketing; el equipo usa Solidity a veces", "workday")
    assert [r["company"] for r in search(c, "solidity")] == ["Otra", "Tacto"], \
        "el anuncio le ganó al título"
    # Y no le gana ni repitiéndolo veinte veces: es el nivel lo que manda, no la
    # cantidad. Este es el fallo que tumbó la primera versión (un solo score con
    # el anuncio pesado), y el único test que lo habría cazado.
    set_descr(c, 1, "solidity " * 20, "workday")
    assert [r["company"] for r in search(c, "solidity")] == ["Otra", "Tacto"], \
        "un anuncio insistente se coló por delante de un título que sí casa"

    # --- índice viejo (sin `descr`) puesto al día solo ---
    v = sqlite3.connect(":memory:")
    v.row_factory = sqlite3.Row
    v.executescript(SCHEMA)
    v.executescript("DROP TABLE jobs_fts;"
                    "CREATE VIRTUAL TABLE jobs_fts USING fts5(company, role,"
                    " content='jobs', content_rowid='id');")
    v.execute("INSERT INTO jobs (key, company, role, url, first_seen, last_seen,"
              " descr) VALUES ('v1','Vieja','Beca','https://v/1','2026-01-01',"
              "'2026-01-01','proyecto de Terraform')")
    v.commit()
    assert _fts_al_dia(v) is True, "un índice sin `descr` tiene que rehacerse"
    assert [r["company"] for r in search(v, "terraform")] == ["Vieja"], \
        "el rebuild no recuperó el anuncio de las filas que ya estaban"
    assert _fts_al_dia(v) is False, "y no se rehace otra vez sin motivo"
    print("ok — esquema partido, migración, estado por usuario, fuente privada, "
          "empresas vigiladas, saneado de query, novedades desde marca, "
          "candidaturas vivas, purga de fuente muerta y anuncio indexado")


if __name__ == "__main__":
    _demo()
