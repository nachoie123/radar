"""Radar — servidor local. Sirve index.html y una API de búsqueda sobre jobs.db.

Único consumidor de db.py. No importa sqlite3: si algún día esto habla con
Postgres, aquí no se toca nada.

    python3 app.py          → http://localhost:8000
"""
import json
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import db

HERE = Path(__file__).resolve().parent
PORT = 8000
CV = Path.home() / ".claude/skills/cv-adapter/references/master-cv.md"
NIGHT = Path.home() / "Projects" / "night-apply"    # tailor() + render_pdf()
BOARDS = Path.home() / "Projects" / "job-boards"    # la ingesta y sus logs

# Cuánto mira atrás la pestaña "Hoy" la PRIMERA vez, antes de que exista una
# marca de "visto". Tres días: si abre a diario nunca se llega a usar, y si
# vuelve tras el fin de semana sigue viendo lo que entró mientras no estaba.
HOY_D = 3

# Días sin que la ingesta vuelva a ver una oferta para darla por rancia. Mismo
# número que el RANCIA_D del HTML, que es quien pinta el chip de aviso.
RANCIA_D = 3

# A partir de cuántas horas sin ingesta se avisa en pantalla. El cron corre cada
# noche, así que 36 h son "se ha saltado una noche", no "hoy aún no ha tocado".
FRESCO_H = 36

# Filtro "solo verano". La ingesta (job_boards._summer_ok) ya tira lo que
# claramente NO es de verano; esto es al revés: quedarse solo con lo que lo dice
# en el título. Si cambia el _SUMMER de job_boards, cambia también aquí.
SUMMER = re.compile(r"\b(summer|verano|estiu|estival)\b", re.I)

# Las spring weeks tienen su propia pestaña y NO son de verano, así que se
# esconden del listado general: si no, ensuciarían "Para ti" y el buscador, que
# van de prácticas de verano. Sí salen con ?insight=1, en la ficha de una oferta
# (?id=) y en la ficha de su empresa (?company=). Debe coincidir con el
# INSIGHT_CAT de ingesta.py (y el de job_boards.py, la ingesta privada).
INSIGHT_CAT = "🌱 Spring weeks e insight programmes"
# El propio catálogo llama a EE. UU. "un vistazo": mirarlo, no accionarlo.
# La lista corta de "Hoy" sí es para accionar, así que va al final (ver hoy()).
US_CAT = "🇺🇸 EE. UU. · solo grandes nombres (un vistazo)"


# Lo que ve quien abre Radar por primera vez y no tiene CV puesto. Sin esto, la
# pestaña "Mi CV" es un cuadro de texto vacío que no dice qué formato quiere, y
# "Para ti" se queda sin ordenar para siempre: es el único paso de instalación
# que no se puede automatizar, porque el CV lo tiene la persona y no el programa.
#
# El prompt pide EXACTAMENTE los encabezados que lee cv_keywords() (SKILLS e
# INTERESTS). Si algún día cambian ahí, cambian aquí: es el mismo contrato visto
# desde los dos lados, y romperlo no da error, solo deja de ordenar bien.
PROMPT_CV = """Te paso mi CV. Conviértelo en un único bloque de texto en Markdown con esta estructura exacta, sin inventarte NADA que no esté en el CV:

## SUMMARY
Dos o tres líneas sobre quién soy y qué busco.

## EDUCATION
Titulación, universidad, años y nota si está en el CV.

## EXPERIENCE
Una entrada por puesto: empresa, cargo, fechas y qué hice, con las herramientas que usé escritas por su nombre.

## SKILLS
Todo lo que sé hacer, separado por comas y en palabras sueltas o expresiones cortas: lenguajes, librerías, herramientas, métodos, idiomas.

## INTERESTS
Los sectores y tipos de puesto que busco, también en palabras sueltas: por ejemplo banca de inversión, quant, consultoría, producto, startups, sostenibilidad.

Reglas:
- No añadas ni una habilidad, empresa, fecha o título que no esté en mi CV.
- SKILLS e INTERESTS son los dos apartados que importan: de ahí salen las palabras con las que se busca. Escríbelos completos aunque el resto quede corto.
- Mantén el idioma de mi CV.
- Devuélveme solo el bloque Markdown, sin explicaciones y sin comillas de código.

Mi CV es este:
"""


# El otro lado de "Preparar mi solicitud": lo que ve quien NO tiene night-apply
# en el disco, que es todo el mundo menos Nacho. En vez de adaptar el CV aquí
# —lo que pediría una suscripción, `claude -p` y reportlab—, Radar entrega el
# prompt ya montado con el anuncio y el CV dentro: se copia, se pega en Claude y
# el CV adaptado vuelve por ahí. Mismo resultado y cero dependencias, que es lo
# que hace que este repo se pueda clonar y usar tal cual.
#
# Mismo patrón que PROMPT_CV: el texto vive en el servidor y viaja por JSON. En
# la plantilla no, porque lleva ## y comillas y acabaría escapándose a mano.
PROMPT_APLICAR = """Quiero presentarme a esta oferta. Adapta mi CV a ella y prepárame las respuestas del formulario, sin inventarte NADA que no esté en mi CV.

Devuélveme, por este orden:

1. **El CV adaptado**, en Markdown y con los mismos apartados que el mío. Reordena y reescribe lo que YA está para que lo que le importa a esta oferta salga arriba y con las palabras que usa el anuncio.
2. **Lo que me falta**: los requisitos de la oferta que mi CV no cubre. Lista corta y sin adornos — me sirve para decidir si me presento.
3. **Carta de motivación** de 150-200 palabras: por qué esta empresa y qué traigo yo, apoyado en cosas concretas del CV.
4. **Respuestas cortas** a lo que suele preguntar el formulario: por qué esta empresa, disponibilidad y permiso de trabajo o visado.

Reglas:
- No añadas ni una habilidad, empresa, fecha, nota o título que no esté en mi CV. Si piden algo que no tengo, va en el punto 2 y no en el 1.
- Lo que haga falta y no esté en el CV, déjalo escrito como [COMPLETAR]. Prefiero un hueco a un dato inventado.
- Mantén el idioma de la oferta: si el anuncio está en inglés, el CV adaptado y la carta van en inglés.

## LA OFERTA

"""


def prompt_aplicar(row, cv_text, texto=""):
    """El prompt de "Preparar mi solicitud", listo para copiar.

    Lleva dentro las dos cosas que Radar tiene y un chat en blanco no: el texto
    del anuncio (jobs.descr, hoy en el 75 % de las ofertas) y el CV guardado.
    Cuando el anuncio no se pudo leer se dice, con el enlace delante: "no lo
    tengo" y "aquí no hay nada que leer" son cosas distintas, y quien lo pega
    tiene que saber cuál de las dos es antes de fiarse del resultado."""
    loc, role = row["loc"] or "", row["role"] or ""
    cola = " · " + loc
    if loc and role.endswith(cola):      # misma poda que sinCola() en el HTML:
        role = role[:-len(cola)]         # la ubicación ya va en su propia línea
    url = row["url"] or ""
    campos = [f"Empresa: {row['company']}", f"Puesto: {role}"]
    if loc:
        campos.append(f"Ubicación: {loc}")
    if url:
        campos.append(f"Enlace: {url}")
    if (texto or "").strip():
        campos.append("\nTexto del anuncio:\n" + texto.strip())
    elif url:
        campos.append("\nNo tengo el texto del anuncio: está en el enlace de arriba. "
                      "Ábrelo si puedes; si no, dime qué te falta saber y te lo pego.")
    else:
        campos.append("\nNo tengo el texto del anuncio ni enlace a la oferta: "
                      "adáptalo con lo que dice el título y avísame de lo que "
                      "estás dando por hecho.")
    return PROMPT_APLICAR + "\n".join(campos) + "\n\n## MI CV\n\n" + cv_text


def modo_aplicar():
    """"local" si night-apply está en el disco, "prompt" si no.

    Se mira en cada petición y no al arrancar, por dos razones: instalar
    night-apply no debería obligar a reiniciar el servidor, y la pantalla
    necesita saberlo ANTES de que se pulse el botón —uno tarda 1-2 min y el
    otro es instantáneo, y un botón que promete la espera equivocada es la
    forma más barata de que alguien crea que se ha colgado."""
    return "local" if NIGHT.exists() else "prompt"


def cv_keywords(md):
    """Query de la pestaña "Para ti" a partir del CV del usuario, en crudo.
    db.search() ya tokeniza (\\w+) y hace OR, así que no hay nada que trocear.
    Sin IA: el ranking es el mismo bm25() del buscador con una query más larga."""
    # SKILLS solo (IA, datos, web) escoraba el ranking entero a puestos de IA;
    # INTERESTS mete lo demás que se busca de verdad: fintech, startups, producto.
    # ADDITIONAL INFORMATION es como los llama el formato oficial del IE, que es
    # el que acabó imponiéndose en el master CV.
    #
    # El "N. " del principio es opcional y no es un capricho: el 20/08/2026 el
    # master CV se renumeró a "## 6. ADDITIONAL INFORMATION" y este re dejó de
    # encontrar NADA. Sin ruido: se caía al texto entero, y "Para ti" pasó a
    # rankear con los 14.767 caracteres del documento — placeholders [PENDIENTE],
    # reglas de estilo y hasta la lista de skills que dice "no ponerlas". Lo pilló
    # el assert de --check, no la pantalla, que seguía dando resultados creíbles.
    # El "\n" del final no sobra: `^\Z` solo casa si el texto ACABA en salto de
    # línea, y el CV que devuelve la base viene sin él (`set_cv` lo recorta). Sin
    # esto, un CV cuya última sección es la de skills no casaba con nada y "Para
    # ti" se quedaba sin query — justo el CV que pega alguien que llega nuevo.
    blocks = re.findall(
        r"^##\s*(?:\d+\.\s*)?(?:SKILLS|INTERESTS|ADDITIONAL INFORMATION)\b.*?$"
        r"(.*?)^(?:## |\Z)", (md or "") + "\n", re.S | re.M | re.I)
    # Un CV pegado a mano no trae esos encabezados: entonces vale el texto entero,
    # que es peor query pero mucho mejor que "Para ti" vacío. Solo cuando NO hay
    # encabezados: si los hay y ninguno encaja, el documento cambió de forma y
    # tragárselo entero es peor que quedarse corto.
    if blocks:
        txt = "\n".join(blocks)
    elif re.search(r"^## ", md or "", re.M):
        print("app: el CV tiene encabezados pero ninguno es de skills — "
              "'Para ti' se queda sin query. Revisa cv_keywords().", file=sys.stderr)
        txt = ""
    else:
        txt = md or ""
    # Los <!-- --> del master CV listan skills *pendientes de confirmar*: fuera, o
    # el match saldría de cosas que no se saben.
    return re.sub(r"<!--.*?-->", "", txt, flags=re.S)


# Palabras que casan con cualquier cosa y no dicen nada del encaje. Solo se caen
# de lo que se ENSEÑA: el ranking sigue viéndolas, y bm25 ya las castiga solo por
# frecuentes. Quitarlas de la query cambiaría el orden; quitarlas del chip no.
_STOP = frozenset("""in of and the for with to on at de la el los las y en un una
    based using strong work working team teams new high good well non per via end
    own across within first related relevant various able using help support
    including ability strong""".split())


def cv_toks(kw):
    """Vocabulario del CV, tokenizado IGUAL que db._fts_query. Que sea el mismo
    tokenizador no es cosmético: lo que se le enseña al usuario como "coincide en
    esto" tiene que ser exactamente lo que hizo subir la oferta en el ranking."""
    return {w.lower() for w in re.findall(r"\w+", kw or "", re.U)} - _STOP


def hits(row, toks):
    """Términos del CV que están en el TÍTULO o en el nombre de la empresa, en el
    orden en que se leen. Son los que meten la oferta en el primer nivel del
    ranking (db.search): el título dice lo que el puesto es.

    El anuncio no se mira aquí a propósito, aunque desde el 2026-08-23 también
    puntúe: `descr` está fuera de db.COLS —son kilobytes por fila— así que en una
    lista de 200 ofertas no está. Para el cuerpo del anuncio está hits_txt(), que
    solo se usa en la ficha, donde el texto ya se ha cargado igualmente."""
    out = []
    for w in re.findall(r"\w+", "%s %s" % (row["role"], row["company"]), re.U):
        b = w.lower()
        if b in toks and b not in out:
            out.append(b)
    return out


def hits_txt(texto, toks, ya=(), df=None, tope=6):
    """Términos del CV que están en el CUERPO del anuncio y no en el título, **los
    más raros primero**.

    `ya` son los del título: repetirlos haría parecer que hay más encaje del que
    hay. `df` es {término: en cuántas ofertas aparece} (db.frecuencias) y es lo
    que ordena: "claude" está en 12 ofertas y "learning" en 147, así que el
    primero dice algo del encaje y el segundo no.

    Antes salían los primeros del texto y por eso la ficha enseñaba "based" y
    "structured": un anuncio de mil palabras casa con veinte términos del CV, y
    los primeros que menciona son los de la introducción, que son los genéricos.
    Ordenar por rareza es lo mismo que hace bm25 para puntuar (idf), solo que
    aquí sirve para elegir qué se lee."""
    out = []
    for w in re.findall(r"\w+", texto or "", re.U):
        b = w.lower()
        if b in toks and b not in ya and b not in out:
            out.append(b)
    df = df or {}
    # Empate (o término que no está en el índice): manda el orden del texto.
    orden = {t: i for i, t in enumerate(out)}
    return sorted(out, key=lambda t: (df.get(t, 0), orden[t]))[:tope]


def encaje(conn, row, kw, toks, texto=""):
    """El encaje de UNA oferta con el CV, en algo que se pueda leer.

    La ficha no enseñaba encaje ninguno —db.get() no calcula score— y la lista
    enseñaba "match 23.8": bm25 en crudo, sin unidad, sin tope y sin nada con qué
    compararlo. Aquí sale el mismo orden de "Para ti" convertido en dos hechos:
    qué palabras tuyas están en la oferta y en qué puesto queda de las que casan.

    Desde el 2026-08-23 hay dos clases de encaje y se enseñan por separado, que
    es como las trata el ranking: `terminos` son los del título (nivel 1) y
    `anuncio` los que solo están en el cuerpo (nivel 2). Antes de indexar el
    anuncio, cuatro de cada cinco ofertas no casaban en NADA (397 de 490 el
    20/08/2026); ahora casan 366 de 532. Las que siguen sin casar en nada suelen
    ser las 121 cuyo anuncio no se ha podido leer, y la ficha lo dice."""
    if not kw.strip():
        return None
    orden = [r["id"] for r in db.search(conn, kw, limit=10_000)]
    tit = hits(row, toks)
    # Del cuerpo se cae lo que dice medio catálogo; del título NO (ahí "data" es
    # la etiqueta del puesto, no relleno). Ver db.GENERICO.
    cuerpo = toks - db.genericos(conn, toks)
    return {"terminos": tit, "total": len(orden),
            "anuncio": hits_txt(texto, cuerpo, tit, df=db.frecuencias(conn, cuerpo)),
            "puesto": orden.index(row["id"]) + 1 if row["id"] in orden else None}


def seed_cv(conn, path=CV):
    """Arranque: si el usuario todavía no tiene CV en la base y el master CV de
    Nacho está en el disco, se copia una vez. Es el puente entre la app local de
    una persona y el producto — en cuanto hay cuentas, este disco no existe y
    cada uno pega el suyo por la web."""
    if db.get_cv(conn):
        return False
    try:
        md = path.read_text(encoding="utf-8")
    except OSError:
        return False                         # sin CV, "Para ti" = las recientes
    db.set_cv(conn, md)
    return True


def _target(row):
    """Fila de jobs -> el dict que espera night_apply.tailor()."""
    role = row["role"]
    # job_boards deja la ubicación detrás del "·" del título, unas veces
    # "Múnich, Germany" y otras solo la ciudad.
    loc = role.split("·")[-1].strip() if "·" in role else ""
    city, _, country = (p.strip() for p in loc.partition(","))
    # El país importa: decide el sponsorship. Si el título no lo trae, de las
    # españolas se sabe por la categoría y del resto lo deduce tailor() de la
    # propia descripción de la oferta.
    if not country and "España" in row["category"]:
        country = "Spain"
    return {"company": row["company"], "role": role, "url": row["url"],
            "city": city, "country": country}


def prepare(row, cv_text, texto=""):
    """Botón "Preparar mi solicitud". Dos caminos, y el disco decide cuál:

    - Con ~/Projects/night-apply instalado (el Mac de Nacho): el pipeline de
      siempre, que adapta el CV con `claude -p` y devuelve el PDF y el dossier.
    - Sin él (cualquiera que clone Radar): un prompt listo para copiar, con el
      anuncio y el CV dentro.

    night-apply se DETECTA, no se exige: es lo que separa "esto solo va en el
    Mac de Nacho" de "esto lo clona cualquiera", y el día que alguien lo instale
    el botón cambia de camino sin tocar una línea."""
    if NIGHT.exists():
        return prepare_local(row, cv_text)
    if not cv_text:
        return {"error": "todavía no has puesto tu CV: pégalo en la pestaña Mi CV"}
    return {"prompt": prompt_aplicar(row, cv_text, texto)}


def prepare_local(row, cv_text):
    """El camino con night-apply: adapta el CV a ESTA oferta y devuelve el
    dossier con cada campo del formulario contestado. Tarda 1-2 min (`claude
    -p`) y deja el resultado en night-apply/out/, así que la segunda vez sale
    de ahí."""
    sys.path.insert(0, str(NIGHT))
    import night_apply as na                  # dentro: si no está, es un error de
    stem = na.slug(f"{row['company']} {row['role']}")   # este botón, no del server
    md, pdf = na.OUT / f"{stem}.md", na.OUT / f"{stem}.pdf"
    if md.exists():
        return {"dossier": md.read_text(), "pdf": pdf.name if pdf.exists() else None}
    if not re.match(r"https?://", row["url"] or ""):
        return {"error": "esta oferta no trae enlace: no hay descripción que leer"}
    try:
        model = json.loads((na.HERE / "config.json").read_text())["apply_model"]
    except (OSError, KeyError):
        model = "sonnet"
    if not cv_text:
        return {"error": "todavía no has puesto tu CV: pégalo en la pestaña Mi CV"}
    na.OUT.mkdir(parents=True, exist_ok=True)
    # El CV sale de la base, no del disco: aquí es donde el botón Aplicar deja de
    # ser de Nacho y pasa a ser de quien esté usando la app.
    body, dossier = na.tailor(cv_text, _target(row), model)
    md.write_text(dossier)
    # body=None: portal ilegible (Workday y compañía). Hay respuestas, no CV.
    return {"dossier": dossier, "pdf": na.render_pdf(body, stem).name if body else None}


def _cola(path, n=8):
    """Últimas n líneas no vacías de un log, o [] si no se puede leer. Lee solo
    el final del fichero: un log que crezca no puede volver lenta la búsqueda."""
    try:
        with path.open("rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 4096))
            txt = f.read().decode("utf-8", "replace")
    except OSError:
        return []
    return [l for l in txt.splitlines() if l.strip()][-n:]


def salud(conn, ahora=None):
    """Estado de la INGESTA: lo único que la app no puede ver por sí sola.

    La app enseña jobs.db y no sabe si alguien la sigue llenando. Si el cron de
    job_boards deja de escribir, la pantalla se ve exactamente igual con el
    catálogo de anteayer. Pasó entre el 13 y el 15 de agosto de 2026: dos noches
    sin ingesta y el único rastro estaba en un .log que no lee nadie.

    Devuelve la edad del catálogo y los avisos del banner. No levanta nunca: un
    fallo leyendo logs no puede tumbar el listado."""
    ahora = ahora or datetime.now(timezone.utc)
    hasta = (db.stats(conn) or {}).get("hasta")
    horas = None
    if hasta:
        try:
            t = datetime.fromisoformat(hasta)
            horas = max(0.0, (ahora - (t if t.tzinfo else t.replace(tzinfo=timezone.utc))
                              ).total_seconds() / 3600)
        except ValueError:
            horas = None
    avisos = []
    if horas is not None and horas >= FRESCO_H:
        dias = int(horas // 24)
        cuanto = f"{dias} días" if dias >= 2 else f"{int(horas)} horas"
        avisos.append({"txt": f"Catálogo de hace {cuanto}: la ingesta lleva desde "
                              f"entonces sin escribir nada.", "url": ""})
    # Aquí vivía el aviso de "sesión del portal del IE caducada". Fuera desde el
    # 2026-08-16: la fuente está apagada y un banner pidiendo renovar una sesión
    # que ya no se usa es ruido permanente. Las 169 ofertas del IE que quedan en
    # jobs.db son de esa fecha y no se refrescan. Si se reactiva la fuente,
    # recuperar este bloque del backup `app.py.bak-20260816-corte-ie`.
    # Ídem con el cron nocturno: manda lo último que pasó. Una pasada que llega a
    # escribir en jobs.db ("jobs.db: +N nuevas") cancela el aviso de la anterior,
    # que si no se quedaría pegado para siempre en el log.
    for linea in reversed(_cola(BOARDS / "run.log", 12)):
        if "jobs.db:" in linea:
            break
        if "watchdog" in linea:
            avisos.append({"txt": "La última pasada de ingesta se cortó por el watchdog "
                                  "antes de escribir nada.", "url": ""})
            break
    return {"hasta": hasta, "horas": None if horas is None else round(horas, 1),
            "avisos": avisos}


# Rango CGNAT que usa Tailscale para las IP de tu tailnet (100.64.0.0/10). Es
# lo que distingue "mi red privada" de "el wifi en el que estoy": una IP 100.64-127
# solo la tienen tus propios dispositivos, no el resto de la cafetería.
_TAILNET = re.compile(r"\binet (100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d+\.\d+)\b")
_TS_CLI = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"


def tailnet_ip():
    """IP del Mac dentro de tu tailnet, o None si Tailscale no está levantado.

    Se mira la interfaz de red y no el CLI de Tailscale a propósito: si un día
    cambia el nombre del binario o la app, esto sigue funcionando."""
    try:
        out = subprocess.run(["/sbin/ifconfig"], capture_output=True, text=True,
                             timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = _TAILNET.search(out)
    return m.group(1) if m else None


def radar_url():
    """La URL con la que abrir Radar desde OTRO dispositivo tuyo (el móvil), o la
    local si Tailscale no está. Es la que escribe el email nocturno.

    Prefiere el nombre MagicDNS (`mac.tunombre.ts.net`) a la IP: la IP puede
    cambiar y el nombre no."""
    ip = tailnet_ip()
    if not ip:
        return f"http://localhost:{PORT}"
    try:
        est = json.loads(subprocess.run([_TS_CLI, "status", "--json"],
                                        capture_output=True, text=True,
                                        timeout=10).stdout)
        nombre = (est.get("Self") or {}).get("DNSName", "").rstrip(".")
    except (OSError, subprocess.SubprocessError, ValueError):
        nombre = ""
    return f"http://{nombre or ip}:{PORT}"


def hoy(conn, ahora=None):
    """Pantalla de entrada: qué ha cambiado desde la última vez que estuviste.

    Dos preguntas, y ninguna tenía respuesta en la app hasta ahora:
      1. Qué ha entrado nuevo — `first_seen` posterior a tu marca de visto, y
         ordenado por lo que encaja con tu CV, no por orden de llegada.
      2. Qué tienes a medias — las candidaturas que no están en 'nueva'. La
         fase 5 sabía guardar el estado pero no lo enseñaba en ningún sitio.

    Sin la 1 hay que releer el catálogo entero cada mañana (y el email ya hace
    eso mejor); sin la 2, marcar el estado de una oferta no sirve para nada."""
    ahora = ahora or datetime.now(timezone.utc)
    desde = db.get_seen(conn) or (ahora - timedelta(days=HOY_D)).isoformat(timespec="seconds")
    seed_cv(conn)
    cv = db.get_cv(conn)
    # Con CV, bm25 ordena las novedades por encaje; sin él, por orden de llegada.
    kw = cv_keywords(cv) if cv else ""
    toks = cv_toks(kw)
    rows = db.search(conn, kw, limit=200, since=desde)
    # Las spring weeks tienen pestaña propia y otro calendario: fuera de "Hoy",
    # igual que del listado general (INSIGHT_CAT).
    rows = [r for r in rows if r["category"] != INSIGHT_CAT]
    nuevas = [_fila(r, toks) for r in rows]
    curso = [_fila(r, toks) for r in db.tracked(conn)]
    # Casi ningún día entran ofertas nuevas (el catálogo ya está maduro: 3 en los
    # últimos 3 días). Sin esto, "Hoy" saldría vacía la mayoría de mañanas y no
    # habría razón para abrir la app. La lista corta es lo que más encaja con el
    # CV y todavía no has tocado — el trabajo pendiente, no una novedad.
    yaestan = {j["id"] for j in nuevas} | {j["id"] for j in curso}
    # Y sin ofertas rancias: la lista corta es una recomendación de qué hacer
    # ahora, y mandar a alguien a una oferta que la ingesta no ve desde hace días
    # (las 169 congeladas del portal del IE, por ejemplo) es hacerle perder el
    # rato. En el buscador sí salen, con su chip de aviso — ahí las pide él.
    tope = (ahora - timedelta(days=RANCIA_D)).isoformat(timespec="seconds")
    sugeridas = []
    if cv:
        cand = [_fila(r, toks) for r in db.search(conn, kw, limit=60)
                if r["id"] not in yaestan and r["category"] != INSIGHT_CAT
                and r["last_seen"] >= tope]
        # bm25 a secas colaba Redmond (WA) y Montréal en el top 6: los títulos de
        # tech americanos son los que más casan con el CV, y ganan a cualquier
        # oferta europea. Pero la lista corta es "qué hago hoy" y EE. UU. es un
        # vistazo — así que las europeas primero y las de allí solo si sobra
        # hueco. Se despriorizan, no se borran: si un día no hay nada en Europa,
        # es mejor enseñar Redmond que una lista vacía.
        sugeridas = ([j for j in cand if j["category"] != US_CAT]
                     + [j for j in cand if j["category"] == US_CAT])[:6]
    return {"desde": desde, "marcado": bool(db.get_seen(conn)),
            "nuevas": nuevas, "curso": curso, "sugeridas": sugeridas}


def _fila(r, toks=frozenset()):
    """Fila de la base -> dict que entiende el HTML.

    `hits` sustituye al `score` que se mandaba antes. El score era el bm25 y
    salía a pantalla tal cual —"match 23.8"—, que no se puede leer: es relativo a
    la query, no tiene tope y cambia de pestaña a pestaña. Las palabras del CV
    que están en la oferta sí se leen, y valen igual en cualquier pestaña porque
    no dependen del orden con el que se pidió la lista.

    updated_at solo sale cuando la consulta lo trae (las candidaturas en curso)."""
    j = {k: r[k] for k in db.COLS}
    if "updated_at" in r.keys():
        j["updated_at"] = r["updated_at"]
    j["hits"] = hits(r, toks)
    # `score` es el bm25 del título: vale 0.0 exacto cuando la oferta entró en la
    # lista por el anuncio y no por el título. Sin esta marca, la columna de la
    # derecha caería al tablero ("Greenhouse") justo donde antes ponía el motivo,
    # y eso se lee como "no casa contigo" cuando lo cierto es "casa, pero en el
    # cuerpo". El texto no viaja en las listas (está fuera de COLS), así que esta
    # marca es todo lo que se puede decir sin traerse megas por delante.
    if not j["hits"] and "score" in r.keys() and r["score"] == 0:
        j["anuncio"] = True
    return j


# Lo único que se sirve del disco. Todo lo demás de esta carpeta —la base, el
# config, los logs, el propio código— responde 404, y los listados de directorio
# desaparecen con ello.
ESTATICOS = {"/": "index.html", "/index.html": "index.html"}


class Radar(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(HERE), **kw)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path.startswith("/cv/"):        # el PDF adaptado vive en night-apply/out
            f = NIGHT / "out" / Path(u.path).name   # .name: no se sube de directorio
            if f.suffix != ".pdf" or not f.exists():
                return self.send_error(404)
            return self._send(f.read_bytes(), "application/pdf")
        if u.path not in ("/api/jobs", "/api/companies", "/api/profile", "/api/hoy"):
            # Lista blanca, no "la carpeta entera". `SimpleHTTPRequestHandler`
            # sirve el directorio en el que vive el código, y ahí está `jobs.db`
            # —con tu CV dentro—, tu `config.json` y los logs: `GET /jobs.db`
            # devolvía la base entera con un 200. El servidor escucha también en
            # tu tailnet, así que eso lo alcanzaba cualquier trasto tuyo con
            # Tailscale. La app es UN html: no hay nada más que servir.
            nombre = ESTATICOS.get(u.path)
            if not nombre:
                return self.send_error(404)
            self.path = "/" + nombre
            return super().do_GET()
        q = parse_qs(u.query)
        term = q.get("q", [""])[0]
        conn = db.connect()
        try:
            if u.path == "/api/hoy":
                d = hoy(conn)
                d.update(total=db.stats(conn)["n"], salud=salud(conn))
                return self._json(json.dumps(d).encode())
            if u.path == "/api/profile":
                seed_cv(conn)
                cv = db.get_cv(conn)
                # El prompt viaja siempre, no solo cuando el CV está vacío: quien
                # quiera rehacerlo con otro CV lo tiene a mano sin recargar.
                return self._json(json.dumps(
                    {"cv": cv, "n": len(cv), "prompt": PROMPT_CV}).encode())
            if u.path == "/api/companies":
                # Todas las vigiladas, incluidas las que hoy tienen 0 becas.
                body = json.dumps({"companies": [dict(r) for r in db.companies(conn)],
                                   "salud": salud(conn)}).encode()
                return self._json(body)
            # Sin este dato el contador decia "189 de 490" con el chip "Todas"
            # puesto, y eso se lee como un filtro que esconde 301 ofertas. No lo
            # es: es el tope de la consulta. El cliente no puede deducirlo solo
            # —no sabe con que limite se le respondio—, asi que se le dice.
            # El vocabulario del CV hace falta en las dos: ordena "Para ti", y
            # en cualquier pestaña marca qué palabras tuyas trae cada oferta.
            seed_cv(conn)
            kw = cv_keywords(db.get_cv(conn))
            toks = cv_toks(kw)
            if q.get("mine"):
                term = f"{term} {kw}"
            truncado, enc, texto = False, None, None
            if q.get("id"):                  # ficha de oferta: solo esa
                rows = [r for r in [db.get(conn, int(q["id"][0]))] if r]
                if rows:
                    # El texto del anuncio solo viaja en la ficha, nunca en una
                    # lista: son kilobytes por oferta (por eso `descr` está
                    # fuera de db.COLS) y en la lista no se leería igualmente.
                    # Se carga antes del encaje porque el encaje ya lo mira.
                    texto = db.descr(conn, rows[0]["id"])
                    enc = encaje(conn, rows[0], kw, toks,
                                 (texto or {}).get("descr", ""))
                    # Las convocatorias fijas no tienen anuncio que leer y su
                    # resumen ya va en el título. La ficha tiene que decir eso y
                    # no "no se ha podido leer", que suena a avería.
                    if texto:
                        texto["curada"] = db.es_curada(rows[0]["source"])
            elif q.get("company"):           # ficha de empresa: sus ofertas
                rows = db.by_company(conn, q["company"][0])
            else:
                tope = int(q.get("limit", [200])[0])
                rows = db.search(conn, term, limit=tope)
                truncado = len(rows) >= tope
            cat = q.get("cat", [""])[0]
            if cat:
                rows = [r for r in rows if r["category"] == cat]
            if q.get("insight"):                 # pestaña de spring weeks: solo ellas
                rows = [r for r in rows if r["category"] == INSIGHT_CAT]
            elif not (q.get("id") or q.get("company")):
                rows = [r for r in rows if r["category"] != INSIGHT_CAT]
            if q.get("summer"):
                rows = [r for r in rows if SUMMER.search(r["role"])]
            jobs = [_fila(r, toks) for r in rows]
            body = json.dumps({"total": db.stats(conn)["n"], "jobs": jobs,
                               "truncado": truncado, "encaje": enc,
                               "descr": texto, "salud": salud(conn),
                               "aplicar": modo_aplicar()}).encode()
        finally:
            conn.close()
        self._json(body)

    def do_POST(self):
        path = urlparse(self.path).path
        if path not in ("/api/apply", "/api/status", "/api/profile", "/api/visto"):
            return self.send_error(404)
        # Cualquier web que visites puede hacerle un POST a este servidor: sin
        # cabecera de tipo, `fetch` manda una "petición simple" y no hay preflight
        # que la frene. Leer la respuesta no podría, pero el efecto ya habría
        # ocurrido — pisarte el CV, cambiarte estados o disparar el pipeline de
        # aplicar. Un POST del propio Radar trae `Origin` igual al `Host`; uno de
        # fuera trae el suyo. Sin `Origin` no es una página (curl, un script), y
        # eso no es lo que hay que parar aquí.
        origen = self.headers.get("Origin")
        sitio = self.headers.get("Sec-Fetch-Site")
        if (origen and urlparse(origen).netloc != self.headers.get("Host")) or \
                (sitio and sitio != "same-origin"):
            return self.send_error(403, "origen cruzado")
        n = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(n) or "{}")
        job_id = payload.get("id")

        if path == "/api/visto":             # "ya he visto las novedades"
            conn = db.connect()
            try:
                return self._json(json.dumps({"desde": db.set_seen(conn)}).encode())
            finally:
                conn.close()

        if path == "/api/profile":           # guardar el CV pegado en la web
            conn = db.connect()
            try:
                n = db.set_cv(conn, payload.get("cv", ""))
            finally:
                conn.close()
            return self._json(json.dumps({"ok": True, "n": n}).encode())

        if path == "/api/status":            # cambio de estado a mano desde la ficha
            conn = db.connect()
            try:
                ok = db.set_status(conn, int(job_id), payload.get("status", ""))
                out = {"ok": ok, "status": payload.get("status")}
            except (ValueError, TypeError) as e:
                out = {"error": str(e)}
            finally:
                conn.close()
            return self._json(json.dumps(out).encode())

        conn = db.connect()
        try:
            row = db.get(conn, int(job_id))
            seed_cv(conn)
            cv_text = db.get_cv(conn)
            # El anuncio solo lo necesita el camino del prompt, pero se lee aquí
            # con la misma conexión: abrir otra para una columna no compensa.
            texto = (db.descr(conn, int(job_id)) or {}).get("descr", "") if row else ""
        finally:
            conn.close()
        try:
            out = prepare(row, cv_text, texto) if row else {"error": "esa oferta ya no está"}
        except Exception as e:               # que el fallo se vea en la ficha,
            out = {"error": f"{type(e).__name__}: {e}"}   # no en la terminal
        if row is not None and "dossier" in out:
            # Preparar la solicitud la deja lista para enviar: el estado sube solo.
            # Por "dossier" y no por "no hay error": con el prompt lo único que ha
            # pasado es que se ha copiado un texto, y darla por preparada ahí sería
            # la app contando por ti algo que todavía no has hecho. El estado es lo
            # único de la base que no se puede reconstruir solo.
            conn = db.connect()
            try:
                db.set_status(conn, int(job_id), "preparada")
                out["status"] = "preparada"
            finally:
                conn.close()
        self._json(json.dumps(out).encode())

    def _json(self, body):
        self._send(body, "application/json; charset=utf-8")

    def _send(self, body, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass                                  # sin ruido en la terminal


if __name__ == "__main__":
    if "--check" in sys.argv:
        # El master CV es de Nacho y no viaja con el repo. Donde esté, se
        # comprueba contra él de verdad: que `cv_keywords` siga encontrando su
        # bloque de skills es lo ÚNICO que avisa de que "Para ti" se ha quedado
        # sin query. Donde no esté (cualquiera que clone esto), el resto del
        # bloque se prueba igual, con CVs de mentira escritos aquí mismo.
        if CV.exists():
            kw = cv_keywords(CV.read_text(encoding="utf-8"))
            assert "clustering" in kw, "no lee el bloque ## SKILLS del master CV"
            assert "scikit" not in kw, "no ha quitado los <!-- pendiente confirmar -->"
            # Centinelas del "se ha caido al documento entero": estas dos cadenas
            # solo existen en secciones que NO son de skills. Si aparecen, el
            # master CV ha vuelto a cambiar de encabezados y "Para ti" esta
            # rankeando con su propio manual de estilo. Pasó el 20/08/2026 con la
            # renumeración a "## 6. ...".
            assert "PENDIENTE" not in kw, "cv_keywords se ha tragado el CV entero"
            assert len(kw) < 3000, "query sospechosamente larga: %d caracteres" % len(kw)
        assert cv_keywords("## 6. ADDITIONAL INFORMATION\n- clustering\n") .strip() \
            == "- clustering", "el encabezado numerado del formato del IE"
        assert cv_keywords("## OTRA COSA\nhola") == "", \
            "con encabezados pero sin skills se queda corto, no se traga todo"
        # Sin salto de línea al final y con las skills de últimas: es como sale
        # de la base un CV pegado a mano.
        assert "clustering" in cv_keywords("## SKILLS\n- clustering"), \
            "el CV de la base viene sin \\n final y ahí es donde se perdía"
        # Un CV pegado a pelo no tiene encabezados: vale el texto entero.
        assert cv_keywords("python, sql y algo de valoración") .strip() \
            == "python, sql y algo de valoración"
        assert cv_keywords("") == "" and cv_keywords(None) == ""
        # El CV vive en la base, no en el disco: seed_cv() solo hace de puente.
        # Con un fichero de mentira, no con el master CV: así esta parte se
        # prueba igual en una máquina que no tenga el de Nacho.
        import tempfile
        falso = Path(tempfile.mkdtemp()) / "master-cv.md"
        falso.write_text("## SKILLS\n- clustering, python, sql\n", encoding="utf-8")
        c = db.connect(":memory:")
        assert seed_cv(c, falso) is True and "clustering" in cv_keywords(db.get_cv(c))
        assert seed_cv(c, falso) is False, "solo siembra una vez: no pisa lo que pegue el usuario"
        db.set_cv(c, "mi CV nuevo")
        assert seed_cv(c, falso) is False and db.get_cv(c) == "mi CV nuevo"
        # Y sin fichero: no revienta, simplemente no siembra ("Para ti" se queda
        # con las recientes hasta que el usuario pegue el suyo).
        assert seed_cv(db.connect(":memory:"), falso.with_name("no-existe.md")) is False
        # --- encaje: lo que se enseña es lo que puntúa ---
        oferta = {"role": "AI Automation Engineer Intern", "company": "Tacto"}
        assert hits(oferta, cv_toks("python, ai y automation")) == ["ai", "automation"], \
            hits(oferta, cv_toks("python, ai y automation"))
        assert hits({"role": "Data Intern", "company": "Data Corp"},
                    cv_toks("data")) == ["data"], "un término repetido se enseña una vez"
        assert "in" not in cv_toks("machine learning in production"), \
            "las palabras vacías casarían con medio catálogo"
        assert hits({"role": "Intern", "company": "X"}, cv_toks("")) == []
        c = db.connect(":memory:")
        db.upsert(c, [{"key": "e1", "company": "Tacto", "role": "AI Automation Intern"},
                      {"key": "e2", "company": "Nestlé", "role": "Controlling Intern"}])
        kw2 = "ai automation"
        e = encaje(c, db.get(c, 1), kw2, cv_toks(kw2))
        assert (e["terminos"], e["puesto"], e["total"]) == (["ai", "automation"], 1, 1), e
        # La que no casa en NADA no está en el ranking: puesto None, y la ficha lo
        # dice. Con el anuncio indexado ya no es el caso corriente, pero sigue
        # siéndolo de las ofertas cuyo texto no se ha podido leer.
        e = encaje(c, db.get(c, 2), kw2, cv_toks(kw2))
        assert (e["terminos"], e["puesto"], e["anuncio"]) == ([], None, []), e
        # --- el anuncio, en la ficha y en la lista (2026-08-23) ---
        # Lo que está en el cuerpo y no en el título se enseña aparte: son dos
        # cosas distintas para el ranking y también para quien lee.
        db.set_descr(c, 2, "buscamos a alguien con ganas de automation", "workday")
        e = encaje(c, db.get(c, 2), kw2, cv_toks(kw2), db.descr(c, 2)["descr"])
        assert (e["terminos"], e["anuncio"], e["puesto"]) == ([], ["automation"], 2), e
        assert hits_txt("ai y automation", cv_toks(kw2), ya=["ai"]) == ["automation"], \
            "un término que ya sale en el título no se repite en el anuncio"
        assert hits_txt("", cv_toks(kw2)) == [] and hits_txt(None, cv_toks(kw2)) == []
        # Y en la lista, la que entra por el anuncio lo dice. Sin la marca, la
        # columna caería al tablero y se leería como "no casa contigo".
        porcv = {j["company"]: j for j in [_fila(r, cv_toks(kw2))
                                          for r in db.search(c, kw2)]}
        assert porcv["Nestlé"].get("anuncio") is True and porcv["Nestlé"]["hits"] == []
        assert "anuncio" not in porcv["Tacto"], "la que casa en el título no la lleva"
        assert encaje(c, db.get(c, 1), "", frozenset()) is None, "sin CV no hay encaje"
        assert "score" not in _fila(db.get(c, 1)), "el bm25 crudo ya no sale a pantalla"
        assert _fila(db.get(c, 1), cv_toks(kw2))["hits"] == ["ai", "automation"]
        t = _target({"company": "BBVA", "role": "Beca Quant Finance · Madrid",
                     "url": "u", "category": "🇪🇸 España · empresas top"})
        assert (t["city"], t["country"]) == ("Madrid", "Spain"), t
        t = _target({"company": "TEC", "role": "AIT Intern · Munich, Germany",
                     "url": "u", "category": "🇪🇺 Europa · empresas con buen nombre"})
        assert (t["city"], t["country"]) == ("Munich", "Germany"), t
        t = _target({"company": "N26", "role": "Software Intern", "url": "u",
                     "category": "🇪🇺 Europa · empresas con buen nombre"})
        assert (t["city"], t["country"]) == ("", ""), t   # sin país: lo deduce la JD
        # --- "Preparar mi solicitud" sin night-apply: el prompt ---
        c3 = db.connect(":memory:")
        db.upsert(c3, [{"key": "p1", "company": "Alantra",
                        "role": "Quant Intern · Madrid", "url": "https://x.test/1"}])
        fila = db.get(c3, 1)
        pr = prompt_aplicar(fila, "## SKILLS\npython", " Buscamos alguien con Python. ")
        assert "Empresa: Alantra" in pr and "Enlace: https://x.test/1" in pr, pr
        # La ubicación va en su línea y no repetida en el puesto.
        assert "Puesto: Quant Intern\n" in pr and "Ubicación: Madrid" in pr, pr
        assert "Buscamos alguien con Python." in pr and pr.endswith("## SKILLS\npython")
        # Sin anuncio leído se DICE, y con el enlace delante: quien pega el prompt
        # tiene que saber que Claude no ha visto la oferta.
        pr = prompt_aplicar(fila, "mi cv", "")
        assert "No tengo el texto del anuncio" in pr and "https://x.test/1" in pr, pr
        db.upsert(c3, [{"key": "p2", "company": "Curada", "role": "Beca"}])
        pr = prompt_aplicar(db.get(c3, 2), "mi cv")
        assert "ni enlace" in pr and "Ubicación" not in pr, pr
        # El disco elige el camino. Se falsea NIGHT para probar los dos aquí:
        # en el Mac de Nacho existe y en cualquier otro sitio no.
        real, globals()["NIGHT"] = NIGHT, Path("/no/existe/night-apply")
        assert modo_aplicar() == "prompt"
        assert "prompt" in prepare(fila, "mi cv"), "sin night-apply, un prompt"
        assert "dossier" not in prepare(fila, "mi cv"), \
            "y sin dossier: es la clave por la que el POST decide NO marcarla preparada"
        assert prepare(fila, "")["error"].startswith("todavía no has puesto tu CV")
        globals()["NIGHT"] = real
        assert modo_aplicar() == ("local" if NIGHT.exists() else "prompt")
        yes = ["ML Research Intern - Summer 2027", "Beca de verano · Madrid",
               "KPMG Blue Summer Experience · programa de verano"]
        no = ["Software Engineer Intern · Munich", "Trainee - 6 months from September",
              "Summerfield Analyst"]           # 'summer' pegado a otra palabra no cuenta
        assert all(SUMMER.search(r) for r in yes), yes
        assert not any(SUMMER.search(r) for r in no), no
        # La categoría tiene que escribirse igual en los dos ficheros: si no, la
        # pestaña de spring weeks sale vacía sin dar ningún error. `ingesta` vive
        # en este repo, así que esta comprobación corre en cualquier máquina.
        import ingesta
        assert ingesta.INSIGHT_CAT == INSIGHT_CAT, (ingesta.INSIGHT_CAT, INSIGHT_CAT)
        # Y ninguna puede decir "verano" en el título: el email se queda solo con
        # lo que casa con _SUMMER, así que ese es el segundo cinturón por si algún
        # día se cuela una fila de spring week en las que van al correo.
        assert not any(SUMMER.search(r) for _, r, _ in ingesta.INSIGHT_PROGRAMS), \
            "una spring week dice 'verano' en el título: acabaría en el email"
        # La ingesta privada de Nacho (job_boards.py) escribe en la MISMA base,
        # así que su categoría también tiene que cuadrar. Solo donde exista: es
        # suya y no viaja con el repo.
        if BOARDS.exists():
            sys.path.insert(0, str(BOARDS))
            import job_boards
            assert job_boards.INSIGHT_CAT == INSIGHT_CAT, (job_boards.INSIGHT_CAT, INSIGHT_CAT)
            assert not any(SUMMER.search(r) for _, r, _ in job_boards.INSIGHT_PROGRAMS), \
                "una spring week dice 'verano' en el título: acabaría en el email"
        # --- pestaña Hoy ---
        # El CV se pone a mano en la base: la lista corta solo existe si hay CV,
        # y esperar a que `seed_cv` encuentre el master CV en el disco ataría
        # estos asserts al Mac de Nacho (donde pasaban) y los dejaría sin
        # comprobar nada en cualquier otro (donde salían vacíos y pasaban igual).
        CV_TEST = "## SKILLS\n- python, sql, ai\n"
        c = db.connect(":memory:")
        db.set_cv(c, CV_TEST)
        db.upsert(c, [{"key": "n1", "company": "Celonis", "role": "AI Intern · Madrid"}],
                  category="🇪🇺 Europa · empresas con buen nombre")
        db.upsert(c, [{"key": "s1", "company": "JP Morgan", "role": "Spring Week · London"}],
                  category=INSIGHT_CAT)
        d = hoy(c)
        assert d["marcado"] is False, "sin marca todavía"
        assert [j["company"] for j in d["nuevas"]] == ["Celonis"],             "las spring weeks tienen pestaña propia: fuera de Hoy"
        assert d["curso"] == [], "nada movido todavía"
        # Marcar visto vacía "Nuevas": es el único sitio donde la app recuerda
        # por dónde ibas.
        db.set_seen(c)
        assert hoy(c)["nuevas"] == [] and hoy(c)["marcado"] is True
        jid = d["nuevas"][0]["id"]
        db.set_status(c, jid, "enviada")
        assert [j["company"] for j in hoy(c)["curso"]] == ["Celonis"]
        assert jid not in {j["id"] for j in hoy(c)["sugeridas"]},             "lo que ya estás moviendo no se recomienda otra vez"
        # Y una oferta que la ingesta no ve desde hace días no se recomienda:
        # la lista corta es para actuar hoy.
        c.execute("UPDATE jobs SET last_seen='2020-01-01T00:00:00+00:00'")
        c.commit()
        assert hoy(c)["sugeridas"] == [], "una oferta rancia no entra en la lista corta"
        # Y EE. UU. no adelanta a Europa en la lista corta. La de Redmond casa
        # MEJOR con el CV a propósito (repite términos): con bm25 a secas salía
        # la primera, que es justo lo que pasaba con Microsoft y DRW.
        c2 = db.connect(":memory:")
        db.set_cv(c2, CV_TEST)
        db.upsert(c2, [{"key": "eu", "company": "Alantra",
                        "role": "Python Intern · Madrid"}],
                  category="🇪🇺 Europa · empresas con buen nombre")
        db.upsert(c2, [{"key": "us", "company": "Microsoft",
                        "role": "Python Python SQL Intern · Redmond, WA"}],
                  category=US_CAT)
        # Sin marca de visto las dos serían "Nuevas" y la lista corta iría vacía:
        # `desde` y el tope de rancia son el mismo día (HOY_D == RANCIA_D).
        db.set_seen(c2)
        orden = [j["company"] for j in hoy(c2)["sugeridas"]]
        assert orden == ["Alantra", "Microsoft"], \
            f"EE. UU. es 'un vistazo': no adelanta a Europa en la lista corta: {orden}"
        # --- tailnet: qué IP cuenta como "mi red privada" y cuál no ---
        muestra = ("\tinet 192.168.1.40 netmask 0xffffff00\n"
                   "\tinet 100.101.102.103 --> 100.101.102.103 netmask 0xff000000\n")
        assert _TAILNET.search(muestra).group(1) == "100.101.102.103"
        assert not _TAILNET.search("\tinet 100.200.1.1 netmask 0xff000000"), \
            "100.200 está fuera del rango CGNAT: no es una IP de tailnet"
        assert not _TAILNET.search("\tinet 10.0.0.5 netmask 0xff000000"), \
            "una IP de wifi normal no puede confundirse con la tailnet"
        assert radar_url().startswith("http://"), radar_url()
        # --- lo que el servidor deja bajar del disco ---
        # De verdad, con un servidor levantado: es un control de seguridad, y la
        # forma de que se caiga es que alguien "arregle" el 404 sin saber que
        # `jobs.db` (tu CV dentro) está en esta misma carpeta.
        import urllib.error
        import urllib.request
        srv = ThreadingHTTPServer(("127.0.0.1", 0), Radar)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = "http://127.0.0.1:%d" % srv.server_address[1]
        try:
            assert b"<title>" in urllib.request.urlopen(base + "/", timeout=5).read(), \
                "el html sí se sirve"
            # Y un POST desde otra web se para ANTES de tocar la base.
            req = urllib.request.Request(base + "/api/profile", data=b"{}",
                                         headers={"Origin": "https://evil.example"})
            try:
                urllib.request.urlopen(req, timeout=5)
            except urllib.error.HTTPError as e:
                assert e.code == 403, e.code
            else:
                raise AssertionError("una web cualquiera puede pisarte el CV")
            for ruta in ("/jobs.db", "/config.json", "/app.py", "/radar.log", "/docs/"):
                try:
                    urllib.request.urlopen(base + ruta, timeout=5)
                except urllib.error.HTTPError as e:
                    assert e.code == 404, (ruta, e.code)
                else:
                    raise AssertionError("%s se está sirviendo: la carpeta lleva "
                                         "dentro la base y el config" % ruta)
        finally:
            srv.shutdown()
        print("ok — keywords del master CV, encaje legible, target de Aplicar, "
              "prompt de Aplicar sin night-apply, filtro de verano, categoría de "
              "spring weeks, pestaña Hoy, detección de tailnet y lista blanca "
              "de estáticos")
        raise SystemExit
    # Threading porque Aplicar tarda 1-2 min: con un solo hilo la app entera se
    # queda congelada mientras claude piensa.
    def _sirve(host):
        srv = ThreadingHTTPServer((host, PORT), Radar)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv

    # Dos sockets, y la diferencia es de seguridad, no de comodidad:
    #   127.0.0.1  -> el Mac. Siempre.
    #   100.x.y.z  -> TU tailnet (el móvil), solo si Tailscale está levantado.
    # Lo que NUNCA se hace es escuchar en 0.0.0.0: eso serviría jobs.db —con tu
    # CV y tu historial de candidaturas, sin contraseña delante— a cualquiera
    # que esté en el mismo wifi. Atarse a la IP de la tailnet deja fuera al wifi.
    _sirve("127.0.0.1")
    print(f"Radar → http://localhost:{PORT}   (Ctrl-C para parar)")
    activos = set()

    def _vigila_tailnet():
        """Tailscale puede levantarse DESPUÉS que el servidor (al iniciar sesión,
        o al volver de una red donde estaba caído). Sin esto habría que reiniciar
        Radar a mano justo el día que lo instalas."""
        while True:
            ip = tailnet_ip()
            if ip and ip not in activos:
                try:
                    _sirve(ip)
                    activos.add(ip)
                    print(f"Radar en tu tailnet → {radar_url()}")
                except OSError as e:                 # IP recién retirada, o puerto
                    print(f"tailnet {ip}: {e}")      # ocupado: se reintenta luego
            time.sleep(60)

    threading.Thread(target=_vigila_tailnet, daemon=True).start()
    while True:                                      # los servidores van en hilos
        time.sleep(3600)
