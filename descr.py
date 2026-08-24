#!/usr/bin/env python3
"""El texto del anuncio: lo único que faltaba para decidir sin salir de Radar.

La app sabía de cada oferta el título, la empresa, la ciudad y poco más. Con eso
la ficha no puede decir DE QUÉ VA el puesto, así que para juzgarla había que
irse a la web de la empresa —y ahí se deja de usar Radar— o gastar 1-2 minutos
en "Preparar mi solicitud" adaptando el CV a algo que todavía no sabías si
querías. Esto lo arregla en el sitio donde estaba roto: la ingesta.

Va aparte de job_boards.py, y no es solo por no pisar otro repo:

  - Buscar ofertas y leerlas son dos trabajos con ritmos distintos. El scraper
    da 490 filas en un minuto largo; leer 490 anuncios son cientos de peticiones
    a cientos de dominios. Mezclarlos haría que un ATS lento se llevara por
    delante el email de la mañana.
  - Un fallo aquí se reintenta sin volver a scrapear nada.
  - Y el relleno inicial de lo que ya está en el catálogo usa exactamente el
    mismo código que la pasada de cada noche.

Cómo se saca el texto, en orden: la API del ATS cuando la hay (Workday,
Greenhouse, Lever, Ashby, SmartRecruiters — dos de cada tres ofertas salen por
aquí), después el Greenhouse escondido detrás de la web de la empresa (Jane
Street y Jump Trading son un marco alrededor de uno) y, de último, el JSON-LD
de JobPosting que Google exige para salir en Google for Jobs.

Lo que NO se puede leer se marca igual, con el motivo. amazon.jobs pinta la
oferta con JavaScript y sin navegador no hay nada que leer: son 42 ofertas que
se van a fallar siempre, y marcarlas es lo que impide que se lleven el
presupuesto de todas las pasadas siguientes.

    python3 descr.py                 # una tanda de las que nunca se intentaron
    python3 descr.py --todas         # hasta acabar o agotar el presupuesto
    python3 descr.py --reintentar 7  # y además las que fallaron hace 7+ días
    python3 descr.py --todas --reintentar 0   # reintentar TODAS las fallidas
    python3 descr.py --probe URL     # una sola, sin tocar la base
    python3 descr.py --check         # tests, sin red
"""
import contextlib
import html as _html
import json
import os
import re
import signal
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import db

try:                      # el python.org de macOS no confía en el llavero del
    import certifi        # sistema; mismo apaño que job_boards.py
    _CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _CTX = ssl.create_default_context()

# Un User-Agent de navegador y no "radar-descr": media docena de estos ATS
# devuelven 403 a cualquier cosa que no parezca Chrome. No se esconde nada —se
# piden páginas públicas, una por oferta y con pausa— pero sin esto no hay datos.
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
       " (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

MIN_TEXTO = 200          # menos que esto es un aviso de cookies, no una oferta
MAX_TEXTO = 12_000       # 490 ofertas × 12 KB = 6 MB de tope para jobs.db
PAUSA = 0.8              # segundos entre peticiones: son webs ajenas

# --- techo de reloj (ver ~/.claude/.../crons-macos-cuelgues) -----------------
# En macOS `monotonic` NO avanza mientras el Mac duerme, así que un timeout de
# socket de 25 s puede durar 12 horas de reloj de pared. Y launchd no relanza un
# trabajo mientras siga vivo el anterior: un solo cuelgue cancela todas las
# noches siguientes. Tres capas, como en job_boards.py.
TOPE_PETICION = 25       # capa 2: SIGALRM, lo único que corta un read bloqueado
PRESUPUESTO = 20 * 60    # capa 1: presupuesto de la pasada, mirado entre ofertas
TOPE_DURO = 30 * 60      # capa 3: watchdog con monotonic (tiempo TRABAJADO)
TOPE_ZOMBI = 4 * 3600    # capa 3: y con time.time(), muy ancho, anti-zombi


class _Reloj(BaseException):
    """De BaseException a propósito: los `except Exception` de los handlers se
    la tragarían y el proceso seguiría pidiendo páginas tan tranquilo."""


@contextlib.contextmanager
def _alarma(seg):
    if not hasattr(signal, "SIGALRM"):
        yield
        return
    def _salta(*_):
        raise _Reloj("la petición pasó de %d s" % seg)
    viejo = signal.signal(signal.SIGALRM, _salta)
    signal.alarm(seg)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, viejo)


def _watchdog():
    """os._exit() y no sys.exit(): SystemExit se levantaría en ESTE hilo mientras
    el principal sigue colgado en su read, y no saldría nadie."""
    m0, w0 = time.monotonic(), time.time()
    def mira():
        while True:
            time.sleep(20)
            if time.monotonic() - m0 > TOPE_DURO or time.time() - w0 > TOPE_ZOMBI:
                print("descr: tope duro alcanzado, salgo", file=sys.stderr, flush=True)
                os._exit(2)
    threading.Thread(target=mira, daemon=True).start()


# ---------- descarga ----------

def _bytes(url, accept=None):
    req = urllib.request.Request(url, headers={
        "User-Agent": _UA, "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
        "Accept": accept or "text/html,application/xhtml+xml,*/*;q=0.8"})
    with _alarma(TOPE_PETICION):
        with urllib.request.urlopen(req, timeout=TOPE_PETICION, context=_CTX) as r:
            return r.read(4_000_000).decode("utf-8", "replace")


def _json(url):
    return json.loads(_bytes(url, accept="application/json"))


# ---------- HTML -> texto ----------

_FUERA = re.compile(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>")
# Sin </li>: la apertura de cada <li> ya mete su salto, y contar los dos deja
# una línea en blanco entre viñeta y viñeta.
_SALTO = re.compile(r"(?i)</(p|div|ul|ol|h[1-6]|tr|table|section|article)>"
                    r"|<br\s*/?>|</?hr\s*/?>")


# Una etiqueta de verdad: letra o barra pegadas al '<'. Así "x < y" no cuenta.
_ETIQUETA = re.compile(r"</?[a-z][a-z0-9]{0,14}(\s[^>]{0,300})?/?>", re.I)


def _pasada(t):
    t = _FUERA.sub(" ", t)
    t = re.sub(r"(?i)<li[^>]*>", "\n· ", t)      # antes de comerse los cierres
    t = _SALTO.sub("\n", t)
    t = re.sub(r"<[^>]+>", " ", t)
    return _html.unescape(t)


def plano(h):
    """HTML de una oferta -> texto legible. Sin parser: son cuatro etiquetas de
    formato y lo que importa es que las listas de requisitos sigan pareciendo
    listas, no que el árbol esté bien.

    Y en bucle, hasta tres veces: Greenhouse manda su HTML ESCAPADO dentro del
    JSON, así que al deshacer las entidades aparecen etiquetas nuevas que la
    pasada anterior no podía ver. Con una sola pasada la ficha de Point72
    enseñaba "<h3>Job Description</h3><p>We are seeking…" tal cual."""
    if not h:
        return ""
    t = h
    for _ in range(3):
        t = _pasada(t)
        # Y también "&lt;": una etiqueta escapada todavía no lo parece, pero lo
        # será en cuanto la pasada siguiente deshaga las entidades.
        if not (_ETIQUETA.search(t) or "&lt;" in t):
            break
    return limpia(t)


def limpia(t):
    """Espacios de sobra fuera y tope de tamaño. Vale igual para el texto que ya
    viene en plano de la API (Lever, Ashby) que para el que sale de plano()."""
    t = (t or "").replace(" ", " ").replace("\r", "")
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r" *\n *", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t).strip()
    if len(t) > MAX_TEXTO:
        # Se corta por el último salto de línea para no dejar una frase a medias.
        corte = t.rfind("\n", 0, MAX_TEXTO)
        t = t[:corte if corte > MAX_TEXTO // 2 else MAX_TEXTO].rstrip() + "\n\n[…]"
    return t


# ---------- handlers, uno por ATS ----------

def _workday(url, cache):
    """La web de Workday es una cáscara de JavaScript, pero por debajo llama a
    su propio JSON (`/wday/cxs/…`) con la misma ruta. Dos formas de URL:
    tenant en el dominio (bbva.wd3.myworkdayjobs.com/en-US/BBVA/job/…) o en la
    ruta detrás de "recruiting" (wd3.myworkdaysite.com/en-US/recruiting/havas/…)."""
    u = urlparse(url)
    seg = [s for s in u.path.split("/") if s]
    if seg and re.fullmatch(r"[a-z]{2}(-[A-Za-z]{2,4})?", seg[0]):
        seg = seg[1:]                              # el idioma es opcional
    if seg and seg[0] == "recruiting":             # myworkdaysite: tenant en ruta
        seg = seg[1:]
        tenant, seg = seg[0], seg[1:]
    else:
        tenant = u.netloc.split(".")[0]
    if not seg:
        raise ValueError("ruta de Workday sin sitio")
    cxs = "https://%s/wday/cxs/%s/%s" % (u.netloc, tenant, "/".join(seg))
    return plano(_json(cxs).get("jobPostingInfo", {}).get("jobDescription", ""))


def _greenhouse(board, jid):
    return plano(_json("https://boards-api.greenhouse.io/v1/boards/%s/jobs/%s"
                       % (board, jid)).get("content", ""))


def _greenhouse_url(url, cache):
    m = re.search(r"greenhouse\.io/(?:embed/job_app\?for=)?([^/?#]+)/jobs/(\d+)", url)
    if not m:
        raise ValueError("no es una URL de Greenhouse")
    return _greenhouse(*m.groups())


def _greenhouse_incrustado(url, cache):
    """Jane Street, Jump Trading, HelloFresh… su página de empleo es un marco
    alrededor de un Greenhouse, y el id va en `?gh_jid=` o al final de la ruta.
    El nombre del tablero casi siempre es el dominio sin el .com."""
    u = urlparse(url)
    jid = parse_qs(u.query).get("gh_jid", [""])[0]
    if not jid:
        m = re.search(r"/(?:position|job|listings?)/[^/]*?(\d{6,})/?$", u.path)
        jid = m.group(1) if m else ""
    if not jid:
        raise ValueError("sin gh_jid")
    marca = re.sub(r"[^a-z0-9]", "", u.netloc.split(".")[-2].lower())
    return _greenhouse(marca, jid)


def _lever(url, cache):
    m = re.search(r"lever\.co/([^/?#]+)/([0-9a-f-]{36})", url)
    if not m:
        raise ValueError("no es una URL de Lever")
    d = _json("https://api.lever.co/v0/postings/%s/%s" % m.groups())
    # `description` es la intro y `lists` los bloques de requisitos: sin ellos se
    # pierde justo la mitad que dice qué piden.
    trozos = [plano(d.get("description", ""))]
    for b in d.get("lists", []):
        trozos.append(plano("<h3>%s</h3>%s" % (b.get("text", ""), b.get("content", ""))))
    trozos.append(plano(d.get("additional", "")))
    return limpia("\n\n".join(t for t in trozos if t))


def _ashby(url, cache):
    """Ashby no sirve una oferta suelta: da el tablero entero de la empresa (más
    de un mega). Se cachea por empresa, que si no una tanda de 20 ofertas de
    Alan se baja el mismo mega veinte veces."""
    m = re.search(r"ashbyhq\.com/([^/?#]+)/([0-9a-f-]{36})", url)
    if not m:
        raise ValueError("no es una URL de Ashby")
    empresa, jid = m.groups()
    cache = cache if cache is not None else {}
    if empresa not in cache:
        cache[empresa] = {j["id"]: j for j in _json(
            "https://api.ashbyhq.com/posting-api/job-board/%s" % empresa).get("jobs", [])}
    j = cache[empresa].get(jid)
    if not j:
        raise ValueError("ya no está en el tablero de Ashby")
    return limpia(j.get("descriptionPlain") or plano(j.get("descriptionHtml", "")))


def _smartrecruiters(url, cache):
    m = re.search(r"smartrecruiters\.com/([^/?#]+)/(\d+)", url)
    if not m:
        raise ValueError("no es una URL de SmartRecruiters")
    d = _json("https://api.smartrecruiters.com/v1/companies/%s/postings/%s" % m.groups())
    secciones = (d.get("jobAd", {}).get("sections", {}) or {}).values()
    return limpia("\n\n".join(plano(s.get("text", "")) for s in secciones if s.get("text")))


def jsonld(pagina):
    """El JobPosting que Google exige para salir en Google for Jobs. Es el único
    handler que vale para una web cualquiera, y por eso va el último."""
    for m in re.finditer(r'(?is)<script[^>]+application/ld\+json[^>]*>(.*?)</script>', pagina):
        try:
            d = json.loads(m.group(1).strip())
        except ValueError:
            continue
        # A veces es una lista, y a veces un @graph con el JobPosting dentro.
        for o in (d if isinstance(d, list) else d.get("@graph", [d]) if isinstance(d, dict) else []):
            if isinstance(o, dict) and "JobPosting" in str(o.get("@type", "")):
                t = plano(o.get("description", ""))
                if t:
                    return t
    return ""


def _jsonld(url, cache):
    return jsonld(_bytes(url))


# El contenedor donde media web de empleo mete la descripción. Es una heurística
# —de ahí que vaya la última— pero es la que rescata a Nestlé y L'Oréal, que
# sirven el texto en el HTML y no publican ni JSON-LD ni API.
_MARCA = re.compile(r'<(\w+)[^>]*\bclass="[^"]*'
                    r'(?:job-?description|job-?details?|jobdesc)[^"]*"[^>]*>', re.I)


def bloque(pagina, m):
    """El contenido del elemento que empieza en `m`, equilibrando su etiqueta.

    Sin equilibrar, "de la marca al final del documento" se trae el pie de
    página, el aviso de cookies y el menú entero detrás de la oferta."""
    abre = re.compile(r"<%s\b" % m.group(1), re.I)
    cierra = re.compile(r"</%s\s*>" % m.group(1), re.I)
    i, nivel = m.end(), 1
    while nivel:
        c = cierra.search(pagina, i)
        if not c:
            return pagina[m.end():]          # HTML roto: mejor de más que nada
        a = abre.search(pagina, i)
        if a and a.start() < c.start():
            nivel, i = nivel + 1, a.end()
        else:
            nivel, i = nivel - 1, c.end()
    return pagina[m.end():i - len(c.group(0))]


# Un contenedor que ocupa media página no es la descripción: es el armazón de la
# página, con su menú y su pie dentro. Pasó con L'Oréal, cuyo texto salía
# empezando por "Skip to content".
MAX_BLOQUE = 0.5


def _marcador(url, cache):
    pagina = _bytes(url)
    # De todos los contenedores que encajan se queda el MÁS PEQUEÑO que aun así
    # tenga texto suficiente: el más ajustado a la oferta y el que menos arrastra.
    cand = []
    for m in _MARCA.finditer(pagina):
        crudo = bloque(pagina, m)
        if len(crudo) > MAX_BLOQUE * len(pagina):
            continue
        t = plano(crudo)
        if len(t) >= MIN_TEXTO:
            cand.append((len(crudo), t))
    if not cand:
        raise ValueError("sin contenedor de descripción")
    return min(cand)[1]


def _handlers(url):
    """Qué probar y en qué orden. El JSON-LD siempre de último: es el que peor
    texto da, pero es el único que no depende de conocer el ATS."""
    h = urlparse(url).netloc.lower()
    if "myworkdayjobs.com" in h or "myworkdaysite.com" in h:
        yield "workday", _workday
    if "greenhouse.io" in h:
        yield "greenhouse", _greenhouse_url
    if "lever.co" in h:
        yield "lever", _lever
    if "ashbyhq.com" in h:
        yield "ashby", _ashby
    if "smartrecruiters.com" in h:
        yield "smartrecruiters", _smartrecruiters
    if "gh_jid=" in url or re.search(r"/(?:position|job|listings?)/[^/]*?\d{6,}/?$",
                                     urlparse(url).path):
        yield "greenhouse incrustado", _greenhouse_incrustado
    yield "json-ld", _jsonld
    yield "marcador", _marcador


def texto_de(url, cache=None):
    """(texto, via). Nunca levanta una excepción: un fallo es un `via` que dice
    quién lo intentó y con qué se encontró, y eso se guarda igual que un acierto
    — es lo que evita volver mañana a por la misma URL muerta."""
    # Se apuntan TODOS los intentos, no solo el último: el último siempre es el
    # json-ld, y "json-ld: 0 caracteres" esconde que lo que falló de verdad fue
    # el 404 de la API del ATS, que es lo que dice si la oferta ya está cerrada.
    # Solo http(s). La URL sale del anuncio que publica un tercero, y `urlopen`
    # también entiende `file://`: una oferta con esa URL haría que la ingesta se
    # leyera un fichero del disco y lo guardara como "el anuncio". No ha pasado,
    # y no tiene por qué poder pasar.
    if not re.match(r"https?://", url or "", re.I):
        return "", "esquema no permitido"
    porques = []
    for nombre, fn in _handlers(url):
        try:
            t = fn(url, cache)
        except urllib.error.HTTPError as e:
            porques.append("%s: HTTP %s" % (nombre, e.code))
        except _Reloj:
            porques.append("%s: se pasó de tiempo" % nombre)
            break                                  # el reloj no mejora esperando
        except Exception as e:                     # noqa: BLE001 — da igual qué
            porques.append("%s: %s" % (nombre, type(e).__name__))
        else:
            if len(t) >= MIN_TEXTO:
                return t, nombre
            porques.append("%s: %d caracteres" % (nombre, len(t)))
    return "", " · ".join(porques) or "sin handler"


# ---------- la pasada ----------

def rellena(conn, limite=60, reintentar=None, presupuesto=PRESUPUESTO, ruido=True):
    """Rellena el texto de las ofertas que no lo tienen. Devuelve el recuento.

    El presupuesto se mira ENTRE ofertas y con monotonic: es tiempo TRABAJADO,
    no de calendario. Al agotarse se deja lo conseguido en la base en vez de
    perderlo, que es la diferencia entre una pasada corta y una pasada inútil."""
    filas = db.sin_descr(conn, limit=limite, reintentar=reintentar)
    cache, t0 = {}, time.monotonic()
    hechas = {"con": 0, "sin": 0, "por": {}}
    for i, f in enumerate(filas):
        if time.monotonic() - t0 > presupuesto:
            if ruido:
                print("descr: presupuesto agotado en %d/%d" % (i, len(filas)),
                      file=sys.stderr)
            break
        if i:
            time.sleep(PAUSA)                      # son webs ajenas
        try:
            t, via = texto_de(f["url"], cache)
        except _Reloj as e:                        # la alarma saltó fuera del try
            t, via = "", "se pasó de tiempo: %s" % e
        db.set_descr(conn, f["id"], t, via)
        hechas["con" if t else "sin"] += 1
        hechas["por"][via.split(":")[0]] = hechas["por"].get(via.split(":")[0], 0) + 1
        if ruido:
            print("%s %-22.22s %-42.42s %s" % ("✓" if t else "·", f["company"],
                                               f["role"], via), flush=True)
    return hechas


def repara(conn):
    """Devuelve a la cola los textos que se guardaron con etiquetas dentro.

    Hubo una versión de plano() que deshacía las entidades DESPUÉS de quitar las
    etiquetas, y Greenhouse manda su HTML escapado dentro del JSON: al
    desescaparlo aparecían etiquetas que ya nadie iba a quitar, y la ficha
    enseñaba "<h3>Job Description</h3>" tal cual en 96 ofertas.

    Se detecta por el RESULTADO y no por la fecha del arreglo: así vale igual
    para el próximo handler que devuelva algo a medio limpiar."""
    n = 0
    for r in conn.execute("SELECT id, descr FROM jobs WHERE descr<>''").fetchall():
        if _ETIQUETA.search(r["descr"]):
            conn.execute("UPDATE jobs SET descr='', descr_via='', descr_at=''"
                         " WHERE id=?", (r["id"],))
            n += 1
    conn.commit()
    return n


def _resumen(conn):
    s = db.descr_stats(conn)
    pct = 100 * s["con"] / s["n"] if s["n"] else 0
    return ("%d de %d ofertas con texto (%.0f%%) · %d intentadas sin suerte · "
            "%d sin tocar" % (s["con"], s["n"], pct, s["fallidas"], s["virgenes"]))


# Vigilante de crons opcional, fuera de este repo: si no está, no hay nada que
# avisar y `_latido` no hace nada. La ruta se puede mover con RADAR_HEARTBEAT.
HEARTBEAT = Path(os.environ.get("RADAR_HEARTBEAT",
                                Path.home() / "Projects" / "cron-heartbeat"))


def _latido(detalle):
    """Aviso de vida para el vigilante de crons, si lo hay. Sin esto, que esta
    pasada NO ocurra no deja rastro: el catálogo simplemente se queda sin texto
    nuevo y no hay nada que lo diga.

    Que el vigilante no exista es lo normal fuera del Mac de Nacho, así que en
    ese caso se calla: un aviso en cada pasada por algo que nadie ha instalado
    es ruido que enseña a ignorar el log."""
    if not (HEARTBEAT / "heartbeat.py").exists():
        return
    try:
        sys.path.insert(0, str(HEARTBEAT))
        from heartbeat import beat
        beat(os.environ.get("RADAR_LABEL_DESCR", "com.nacho.radardescr"),
             detalle=detalle)
    except Exception as e:                         # noqa: BLE001
        print("[heartbeat] no pude dejar el latido: %s" % e, flush=True)


def main(argv):
    if "--probe" in argv:
        url = argv[argv.index("--probe") + 1]
        t, via = texto_de(url)
        print("via:", via, "| %d caracteres\n" % len(t))
        print(t[:2000])
        return 0
    _watchdog()
    conn = db.connect()
    try:
        if "--repara" in argv:
            print("%d textos con etiquetas devueltos a la cola" % repara(conn))
        dias = int(argv[argv.index("--reintentar") + 1]) if "--reintentar" in argv else 0
        # Con --reintentar 0 el corte es "ahora", o sea: vuelven TODAS las que
        # fallaron alguna vez. Es lo que se usa después de arreglar un handler.
        rein = ((datetime.now(timezone.utc) - timedelta(days=dias))
                .isoformat(timespec="seconds")) if "--reintentar" in argv else None
        limite = int(argv[argv.index("--limit") + 1]) if "--limit" in argv else 60
        presu = (int(argv[argv.index("--presupuesto") + 1])
                 if "--presupuesto" in argv else PRESUPUESTO)
        if "--todas" in argv:
            limite = 10_000
        r = rellena(conn, limite=limite, reintentar=rein, presupuesto=presu)
        print("\n%d con texto, %d sin. %s" % (r["con"], r["sin"], _resumen(conn)))
        for via, n in sorted(r["por"].items(), key=lambda x: -x[1]):
            print("   %4d  %s" % (n, via))
        _latido("%d con texto, %d sin · %s" % (r["con"], r["sin"], _resumen(conn)))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    if "--check" in sys.argv:
        # Sin red: lo que se prueba es el troceo de URLs y el paso a texto, que
        # es donde están los errores que no dan la cara (una ruta de Workday mal
        # partida devuelve 404 y parece que la oferta se cerró).
        lista = plano("<p>Hola</p><ul><li>uno</li><li>dos</li></ul>")
        assert lista == "Hola\n\n· uno\n· dos", repr(lista)
        assert plano("<script>var x='<b>no</b>';</script><p>sí</p>") == "sí"
        # HTML escapado dentro del JSON (Greenhouse): hace falta más de una
        # pasada, y sin ellas las etiquetas salían a pantalla.
        assert plano("&lt;h3&gt;Perfil&lt;/h3&gt;&lt;p&gt;Python&lt;/p&gt;") \
            == "Perfil\nPython", repr(plano("&lt;h3&gt;Perfil&lt;/h3&gt;"))
        assert plano("&amp;lt;p&amp;gt;hola&amp;lt;/p&amp;gt;") == "hola"
        # Pero "x < y" no es una etiqueta y no se puede tragar el resto.
        assert "y > z" in plano("<p>si x &lt; y &gt; z</p>") or \
            plano("<p>si x &lt; y &gt; z</p>") == "si x < y > z", \
            repr(plano("<p>si x &lt; y &gt; z</p>"))
        assert plano("a &amp; b&nbsp;c") == "a & b c"
        assert plano("") == "" and plano(None) == ""
        assert limpia("  a  \n\n\n  b  ") == "a\n\nb"
        largo = limpia("x" * (MAX_TEXTO + 500))
        assert len(largo) <= MAX_TEXTO + 5 and largo.endswith("[…]"), len(largo)
        # Orden de handlers: el ATS primero, el JSON-LD siempre de último.
        n = lambda u: [k for k, _ in _handlers(u)]
        assert texto_de("file:///etc/passwd") == ("", "esquema no permitido"), \
            "la URL la publica un tercero: `file://` no puede llegar a urlopen"
        assert texto_de("") == ("", "esquema no permitido")
        assert n("https://bbva.wd3.myworkdayjobs.com/en-US/BBVA/job/x/y_1") \
            == ["workday", "json-ld", "marcador"]
        assert n("https://job-boards.greenhouse.io/celonis/jobs/777") \
            == ["greenhouse", "json-ld", "marcador"]
        assert n("https://jobs.lever.co/aircall/" + "a" * 8 + "-1234-1234-1234-" + "b" * 12) \
            == ["lever", "json-ld", "marcador"]
        assert n("https://www.jumptrading.com/hr/job?gh_jid=7362318") \
            == ["greenhouse incrustado", "json-ld", "marcador"], n("https://www.jumptrading.com/hr/job?gh_jid=7362318")
        assert n("https://www.janestreet.com/join-jane-street/position/8599644002/") \
            == ["greenhouse incrustado", "json-ld", "marcador"]
        assert n("https://www.amazon.jobs/en/jobs/3120058/algo") == ["json-ld", "marcador"]
        # La ruta CXS de Workday: el idioma es opcional y el tenant está en el
        # dominio o detrás de "recruiting". Equivocarse aquí da un 404 que parece
        # una oferta cerrada.
        vistas = []
        globals()["_json"] = lambda u: vistas.append(u) or {"jobPostingInfo": {}}
        for u, esperado in [
            ("https://bbva.wd3.myworkdayjobs.com/en-US/BBVA/job/Madrid/Beca_JR1",
             "https://bbva.wd3.myworkdayjobs.com/wday/cxs/bbva/BBVA/job/Madrid/Beca_JR1"),
            ("https://santander.wd3.myworkdayjobs.com/SantanderCareers/job/TORINO/X_R1",
             "https://santander.wd3.myworkdayjobs.com/wday/cxs/santander/SantanderCareers/job/TORINO/X_R1"),
            ("https://wd3.myworkdaysite.com/en-US/recruiting/havas/GroupSite/job/Madrid/X_JR1",
             "https://wd3.myworkdaysite.com/wday/cxs/havas/GroupSite/job/Madrid/X_JR1")]:
            _workday(u, None)
            assert vistas[-1] == esperado, "\n%s\n%s" % (vistas[-1], esperado)
        # JSON-LD: lista, @graph y objeto suelto son las tres formas que se ven.
        pag = ('<script type="application/ld+json">{"@type":"JobPosting",'
               '"description":"<p>%s</p>"}</script>' % ("hola " * 60))
        assert jsonld(pag).startswith("hola hola")
        assert jsonld('<script type="application/ld+json">{"@graph":[{"@type":'
                      '"JobPosting","description":"<b>x</b>"}]}</script>') == "x"
        assert jsonld("<html>nada</html>") == ""
        assert jsonld('<script type="application/ld+json">{roto</script>') == ""
        # El recorte por contenedor tiene que equilibrar los <div> anidados: si
        # se para en el primer cierre corta la oferta por la mitad, y si no se
        # para nunca se trae el pie de página entero.
        pag = ('<body><nav>menú</nav><div class="job-description">'
               '<div><p>uno</p></div><p>dos</p></div><footer>pie</footer></body>')
        recorte = plano(bloque(pag, _MARCA.search(pag)))
        assert recorte == "uno\n\ndos", repr(recorte)   # ni el menú ni el pie
        roto = '<div class="jobdescription"><p>sin cerrar</p>'
        assert "sin cerrar" in plano(bloque(roto, _MARCA.search(roto)))
        # El detector de texto a medio limpiar, que es lo que decide qué vuelve
        # a la cola con --repara.
        assert _ETIQUETA.search("<h3>Job Description</h3>")
        assert _ETIQUETA.search('<div class="content-intro">')
        assert not _ETIQUETA.search("si x < y el filtro > 3"), \
            "una desigualdad no es una etiqueta: repararía ofertas sanas"
        assert not _ETIQUETA.search("Python, C++ y <3 por el producto")
        print("ok — troceo de URLs por ATS, ruta CXS de Workday, JSON-LD y "
              "HTML a texto")
        raise SystemExit
    raise SystemExit(main(sys.argv[1:]))
