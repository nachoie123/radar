"""Radar — ingesta del catálogo. Llena `jobs.db` desde los ATS públicos.

    python3 ingesta.py            → una pasada completa
    python3 ingesta.py --dry-run  → igual, pero sin escribir en la base
    python3 ingesta.py --check    → tests propios, sin red
    python3 ingesta.py --fuentes  → lista las fuentes y sale

Sin dependencias: todo es librería estándar. No hay API keys ni cuentas; cada
fuente es la API o el listado PÚBLICO del ATS de la empresa (Greenhouse, Lever,
Ashby, Workday, SmartRecruiters, Workable, Recruitee, Teamtailor,
SuccessFactors, Avature) más los tableros comunitarios de GitHub.

Qué se busca y para quién sale de `config.json` (año de graduación, regiones,
solo verano, qué fuentes). Si no existe, se usa `config.example.json`, que trae
los valores por defecto — así la primera pasada funciona recién clonado.

Escribir en la base es SIEMPRE por `db.upsert()`, que es idempotente: repetir la
pasada no duplica nada y solo refresca `last_seen`. Por eso esto puede correr
tantas veces como quieras, y por eso puede convivir con cualquier otra
ingesta escribiendo en la misma base.
"""
import hashlib
import html
import json
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import db

try:                                    # certifi es OPCIONAL: solo hace falta si
    import certifi                      # el Python del sistema no trae raíces CA
    _CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:                       # noqa: BLE001
    _CTX = ssl.create_default_context()

HERE = Path(__file__).resolve().parent
# El ejemplo va con el código (también dentro de Radar.app); lo que es de cada
# uno —su config y lo que encuentre probe.py— va en la carpeta de datos (db.DATA).
CONFIG = db.DATA / "config.json"
EJEMPLO = HERE / "config.example.json"
# probe.py escribe aquí los tableros que encuentra vivos y con becas. Opcional:
# sin el fichero, se ingesta solo la lista curada a mano.
DESCUBIERTOS = db.DATA / "descubiertos.json"

# Por dónde va la pasada, para la barra de progreso de la app (app.py la lee
# desde otro hilo). Fuentes miradas de las que hay que mirar.
PROGRESO = {"hechas": 0, "total": 0}

HTTP_TIMEOUT = 25            # timeout de cada petición HTTP
MAX_SECONDS = 900            # techo de reloj de la pasada entera (override en config)


# ---------- configuración ----------

def cargar_config(path=CONFIG):
    """Config del usuario, con `config.example.json` de respaldo.

    El ejemplo NO es solo documentación: es el valor por defecto de verdad, así
    que recién clonado (sin `config.json`, que está en el .gitignore porque es
    de cada uno) la ingesta ya sabe qué buscar. Las claves que falten en el
    `config.json` del usuario también caen al ejemplo, para que añadir una clave
    nueva a Radar no rompa las configuraciones que ya existen."""
    base = {}
    try:
        base = json.loads(EJEMPLO.read_text())
    except Exception as e:                              # noqa: BLE001
        print(f"aviso: no pude leer {EJEMPLO.name} ({e}); tiro de los valores del código.")
    try:
        base.update(json.loads(Path(path).read_text()))
    except FileNotFoundError:
        pass                                            # normal: config.json es opcional
    except Exception as e:                              # noqa: BLE001
        print(f"aviso: {Path(path).name} no es JSON válido ({e}); uso los valores por defecto.")
    base.pop("_ayuda", None)                            # la ayuda del ejemplo no es config
    return base


# ---------- regiones ----------
# `_EU_LOC` de la ingesta original era UNA regex con toda Europa dentro. Aquí va
# troceada por regiones para que `config.json` pueda elegir: quien busque en
# Londres no quiere Varsovia, y quien esté en EE. UU. no quiere ninguna de las
# dos. La regex de verdad se compone en `_compilar_loc()` con lo que se pida.
REGIONES = {
    "es": r"spain|espa[ñn]a|madrid|barcelona|valencia|sevilla|bilbao|m[aá]laga|zaragoza",
    "pt": r"portugal|lisbon|lisboa|porto",
    "uk": r"united kingdom|england|london|manchester|edinburgh|glasgow",
    "ie": r"ireland|dublin",
    "de": r"germany|deutschland|berlin|munich|m[uü]nchen|hamburg|frankfurt|cologne",
    "fr": r"france|paris|lyon|toulouse|lille",
    "it": r"italy|italia|milan|milano|rome|roma|turin|torino",
    "pl": r"poland|polska|warsaw|warszawa|krak[oó]w|wroc[lł]aw|gdansk",
    "benelux": r"netherlands|amsterdam|rotterdam|belgium|brussels|luxembourg",
    "alpes": r"switzerland|zurich|z[uü]rich|geneva|austria|vienna|wien",
    "nordicos": r"denmark|copenhagen|sweden|stockholm|norway|oslo|finland|helsinki",
    "europa": r"europe|emea",           # ofertas que solo dicen "Europe"/"EMEA"
    "remoto": r"remote|teletrabajo|en remoto",
    "us": (r"united states|u\.?s\.?a\.?|new york|nyc|san francisco|bay area|"
           r"seattle|boston|chicago|austin|los angeles|atlanta|denver|"
           r"california|texas|new jersey|washington, ?d\.?c\.?"),
    "latam": r"m[eé]xico|mexico city|cdmx|bogot[aá]|colombia|santiago|chile|"
             r"buenos aires|argentina|lima|per[uú]|s[aã]o paulo|brasil|brazil",
}


def _compilar_loc(regiones):
    """Regex de ubicación con las regiones pedidas. Sin regiones válidas, casa
    con todo: mejor un catálogo con ruido que un catálogo vacío por una errata
    en el config (que es justo lo que no se ve hasta que la app está vacía)."""
    trozos = [REGIONES[r] for r in regiones if r in REGIONES]
    desconocidas = [r for r in regiones if r not in REGIONES]
    if desconocidas:
        print(f"aviso: regiones que no conozco, las ignoro: {', '.join(desconocidas)}"
              f" (válidas: {', '.join(REGIONES)})")
    if not trozos:
        return re.compile(r"")           # todo pasa
    return re.compile(r"\b(" + "|".join(trozos) + r")\b", re.I)


# Título de becario en los idiomas de los mercados que se cubren. Sin esto se
# perdía Francia entera (allí las prácticas se titulan "Stage ...") y Alemania
# ("Praktikum").
_TITULO_BECA = re.compile(
    r"\b(intern(ship)?s?|becari[oa]s?|becas?|pr[aá]cticas|trainee|"
    r"graduate program(me)?|talent program(me)?|working student|werkstudent|"
    r"placement|apprentice|early career|programa de talento|"
    r"stage|stagiaire|praktikum|praktikant(in)?|stagiair|tirocinio|"
    r"summer analyst|off.?cycle|co.?op)\b", re.I)


# ---------- filtros ----------
# Los cuatro filtros de la ingesta original, con sus umbrales sacados a config:
# año de graduación, "solo verano", "solo grado" y la lista de empresas top.

_GRAD_PATS = [
    re.compile(r"class of\s+(20\d{2})", re.I),
    re.compile(r"graduat\w*\s+(?:in\s+|by\s+|before\s+)?(20\d{2})", re.I),
    re.compile(r"(20\d{2})\s+grad(?:uate|uating|s)?\b", re.I),
    re.compile(r"\bgrad(?:uating)?\s+(20\d{2})", re.I),
    re.compile(r"c/o\s*(20\d{2})", re.I),
]
_GRAD_OPEN = re.compile(r"or later|onwards?|and beyond|or after|\+", re.I)


def _grad_ok(role, grad_year):
    """False solo si el título EXIGE graduarse en un año anterior a grad_year.
    Sin mención de graduación -> True (no lo sabemos, se conserva)."""
    if not role:
        return True
    years = [int(y) for p in _GRAD_PATS for y in p.findall(role)]
    if not years:
        return True
    if _GRAD_OPEN.search(role):            # "Class of 2026 or later" -> sí califica
        return True
    return max(years) >= grad_year


# Señales de que NO es una práctica de verano: otra temporada, working student /
# werkstudent (a tiempo parcial durante el curso), off-cycle (6 meses en curso),
# graduate programme (empleo full-time post-grado), placement year, apprenticeship.
# 'summer'/'verano' en el título gana a todas.
_OFFSEASON = re.compile(
    r"\b(fall|autumn|spring|winter|off[- ]?season|off[- ]?cycle|"
    r"working student|werkstudent|graduate programme|graduate program|"
    r"apprentice(ship)?|placement year|industrial placement|year[- ]?round|"
    r"oto[ñn]o|primavera|invierno)\b", re.I)
_SUMMER = re.compile(r"\b(summer|verano|estiu|estival|d['e]?\s?et[eé])\b", re.I)


def _summer_ok(role):
    """Solo prácticas de verano. Sin señal de temporada se conserva: el título
    casi nunca dice el mes, y tirar por defecto vacía el catálogo."""
    if not role:
        return True
    if _SUMMER.search(role):
        return True
    return not _OFFSEASON.search(role)


# Perfiles que un estudiante de grado no puede cubrir: PhD, máster/MBA exigido,
# veteranos, security clearance. 'master' solo cuenta si va con degree/student/etc
# (para no tirar "Master Data" ni "Scrum Master").
_UNREAL = re.compile(
    r"\bph\.?\s?d\.?\b|\bdoctora(?:l|te)\b|\bmba\b|"
    r"master'?s?\s+(?:degree|student|candidate|graduate|program|level|required)|"
    r"\bveterans?\b|\bclearance\b|\bpolygraph\b|\bts/sci\b", re.I)


def _realistic(o, solo_grado=True, necesito_visado_us=True):
    """False si el título pide un perfil que no tienes.

    `necesito_visado_us` tira las que exigen ciudadanía estadounidense (🇺🇸 en los
    tableros de GitHub). Ponlo a false si YA puedes trabajar en EE. UU.: si no,
    con `regiones: ["us"]` el catálogo sale medio vacío sin decir por qué.

    Solo mira el TÍTULO y el flag de visado: los requisitos que únicamente están
    en la descripción no se ven desde estos tableros."""
    if necesito_visado_us and o.get("uscit"):
        return False
    return not (solo_grado and _UNREAL.search(o.get("role", "")))


# Empresas "de buen nombre": si sale una oferta suya, la app la destaca aunque no
# sea nueva. Match por palabra completa (evita "Citizens" -> "Citi"). Se puede
# sustituir entera desde config.json con "empresas_top", o ampliar con
# "empresas_top_extra" (lo normal: añadir las tuyas sin perder estas).
PRESTIGE = [
    "banco santander", "santander", "bbva", "caixabank", "banco sabadell",
    "bankinter", "telefónica", "telefonica", "iberdrola", "repsol", "inditex",
    "zara", "ferrovial", "indra", "minsait", "naturgy", "mapfre", "acciona",
    "cellnex", "aena", "grifols", "amadeus", "cabify",
    "spotify", "adyen", "revolut", "wise", "klarna", "booking", "n26",
    "celonis", "doctolib", "sumup", "aircall", "hellofresh", "blablacar",
    "asml", "sap", "siemens", "lvmh", "louis vuitton", "nestlé", "nestle", "ferrari",
    "goldman sachs", "goldman", "morgan stanley", "jp morgan", "jpmorgan",
    "j.p. morgan", "mckinsey", "boston consulting", "bcg", "bain", "jane street",
    "citadel", "blackstone", "blackrock", "kkr", "google", "amazon", "apple",
    "microsoft", "nvidia", "meta",
    "bank of america", "merrill", "citigroup", "citibank", "citi", "barclays",
    "ubs", "deutsche bank", "credit suisse", "hsbc", "bnp paribas",
    "nomura", "lazard", "evercore", "centerview", "moelis", "perella weinberg",
    "pjt partners", "rothschild", "jefferies", "houlihan lokey", "greenhill",
    "guggenheim", "wells fargo", "raymond james", "apollo", "carlyle", "vanguard",
    "fidelity", "alantra", "ardian", "julius baer", "arcano", "pai partners",
    "two sigma", "hudson river trading", "optiver", "imc", "susquehanna",
    "de shaw", "point72", "millennium", "akuna", "five rings", "virtu",
    "flow traders", "squarepoint", "marshall wace", "aqr", "bridgewater",
    "man group", "drw", "old mission", "pdt partners", "qube", "wintermute",
    "jane street capital", "jump trading", "jump", "quadrature",
    "oliver wyman", "kearney", "deloitte", "pwc", "kpmg", "ernst & young",
    "accenture", "lek consulting",
    "alphabet", "facebook", "netflix", "openai", "anthropic", "tesla", "stripe",
    "databricks", "airbnb", "coinbase", "uber", "linkedin", "salesforce", "adobe",
    "palantir", "snowflake", "bloomberg",
]


def _compilar_top(nombres):
    """Regex de empresas top: alias más largos primero, para que "citigroup" gane
    a "citi" y el match no dependa del orden de la lista."""
    nombres = [n for n in nombres if n]
    if not nombres:
        return re.compile(r"(?!)")       # lista vacía: no destaca ninguna
    return re.compile(r"\b(" + "|".join(sorted((re.escape(a) for a in nombres),
                                               key=len, reverse=True)) + r")\b", re.I)


# ---------- techo de reloj ----------
# Una pasada normal son ~4 min. El techo solo salta si algo va mal de verdad: una
# fuente que no cierra la conexión, la red del portátil que se cae a mitad. Se
# mira en `_get_json`/`_get_html`, que es por donde pasa TODA la red, así que no
# hace falta hilarlo por la firma de cada scraper.
#
# Se usa `time.time()` (reloj de pared) a propósito, no `time.monotonic()`: el
# monotónico se PARA mientras el ordenador duerme, así que un techo de 15 minutos
# tomado antes de cerrar la tapa no vence hasta 15 minutos de uso después — y la
# pasada se queda colgada horas.
_FIN = None


class SinTiempo(Exception):
    """Se agotó el techo de reloj de la pasada."""


def _queda_tiempo():
    return _FIN is None or time.time() < _FIN


def _exigir_tiempo():
    if not _queda_tiempo():
        raise SinTiempo("se acabó el techo de reloj de la pasada")


# ---------- tableros de GitHub: descarga y parseo ----------

_EMOJI = re.compile(r"[\U0001F000-\U0001FAFF←-⇿⌀-➿️✅‍]")
_HEADERWORDS = {"company", "role", "job title", "location", "links", "date posted",
                "application/link", "work model", "company name", "date", "apply",
                "position", "link", "notes"}


# ---------- descarga ----------

def _api(url, cfg):
    _exigir_tiempo()
    headers = {"User-Agent": "job-boards-radar", "Accept": "application/vnd.github+json"}
    tok = (cfg or {}).get("github_token", "")
    if tok and "PEGA_AQUI" not in tok:
        headers["Authorization"] = "Bearer " + tok
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT, context=_CTX) as r:
        return json.load(r)


def _raw(owner, repo, branch, path):
    _exigir_tiempo()
    url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{path}"
    req = urllib.request.Request(url, headers={"User-Agent": "job-boards-radar"})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT, context=_CTX) as r:
        return r.read().decode("utf-8", "replace")


def _parse_url(url):
    """owner, repo, branch|None, path — soporta /blob/<branch>/<path>#anchor."""
    seg = url.split("github.com/", 1)[1].split("#", 1)[0].split("/")
    owner, repo = seg[0], seg[1]
    if len(seg) > 2 and seg[2] == "blob":
        return owner, repo, seg[3], "/".join(seg[4:])
    return owner, repo, None, "README.md"


# ---------- parseo de ofertas ----------

def _clean(cell):
    """Texto legible de una celda markdown/html (sin imágenes, enlaces, tags, emoji)."""
    c = re.sub(r"<img[^>]*>", "", cell)
    c = re.sub(r"<details>.*?</details>", " ", c, flags=re.S)      # bloque "N locations"
    c = re.sub(r"<a[^>]*>(.*?)</a>", r"\1", c, flags=re.S)
    c = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", c)                      # ![alt](img)
    c = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", c)                  # [txt](url) -> txt
    c = re.sub(r"<[^>]+>", " ", c)                                  # tags sueltos
    c = c.replace("*", "").replace("`", "")
    c = _EMOJI.sub("", c)
    return re.sub(r"\s+", " ", c).strip()


def _first_url(cells):
    """Primer enlace de aplicación de la fila: ignora la 1a celda (logo/web empresa)."""
    for cell in cells[1:]:
        m = re.search(r'href="([^"]+)"', cell) or re.search(r"\]\((https?://[^)]+)\)", cell)
        if m:
            return m.group(1)
    return None


def _rows(md):
    """Filas de tabla como listas de celdas crudas. Soporta tablas markdown (| .. |)
    y tablas HTML (<tr><td>..</td></tr>) — SimplifyJobs y off-season usan HTML."""
    for line in md.splitlines():                                     # markdown
        s = line.strip()
        if not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if len(cells) >= 2 and not all(re.fullmatch(r"[-:\s]*", c) for c in cells):
            yield cells
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", md, re.I | re.S):    # html
        if re.search(r"<th[^>]*>", tr, re.I):
            continue
        cells = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.I | re.S)
        if len(cells) >= 2:
            yield cells


def parse_offers(md):
    """Filas de tabla del README -> lista de ofertas {key, company, role, url, loc}.
    Arrastra la empresa en filas '↳' (mismo empleador, otra vacante).

    `loc` viaja como campo propio porque estos README NO la meten en el título:
    sin ella, "Software Developer Intern" de DRW Chicago y el de DRW Londres
    llegan a jobs.db indistinguibles y la dedup los fusiona. Entra en la key
    desde siempre; lo que faltaba era no tirarla al construir el dict."""
    offers = []
    last_company = ""
    for cells in _rows(md):
        if any(c.strip().lower() in _HEADERWORDS for c in cells):    # cabecera
            continue
        company = _clean(cells[0])
        if company in ("", "↳"):
            company = last_company
        else:
            last_company = company
        role = _clean(cells[1]) if len(cells) > 1 else ""
        loc = _clean(cells[2]) if len(cells) > 2 else ""
        if not company and not role:
            continue
        raw = " ".join(cells)                    # emojis de visado antes de limpiar
        key = hashlib.sha1(f"{company}¦{role}¦{loc}".encode()).hexdigest()[:16]
        offers.append({"key": key, "company": company, "role": role,
                       "url": _first_url(cells), "loc": loc,
                       "nospon": "🛂" in raw,      # no ofrece sponsorship
                       "uscit": "🇺🇸" in raw})    # requiere ciudadanía US
    # dedup por key conservando orden (una tabla puede repetir la misma fila)
    seen, uniq = set(), []
    for o in offers:
        if o["key"] not in seen:
            seen.add(o["key"])
            uniq.append(o)
    return uniq


# ---------- fuentes directas (ATS de las propias empresas) ----------
# Los tableros de GitHub son tech/quant y NO traen banca. Aquí tiramos de la API
# pública del ATS de cada firma top (Greenhouse/Lever = JSON abierto; Workday =
# endpoint cxs). Sin scraping. Cada entrada de DIRECT: company (para el ranking),
# name (rótulo), y UNA de gh/lever/wd. Probadas en vivo antes de meterlas.
_INTERN = re.compile(r"\bintern(ship)?s?\b", re.I)
# Algún ATS deja la oferta publicada y anuncia el cierre en el propio título.
_CLOSED = re.compile(r"\bCLOSED\b")
_HDR = {"User-Agent": "job-boards-radar", "Accept": "application/json"}

# Todas las fuentes directas se filtran a UBICACIÓN EUROPEA (_EU_LOC) + título de
# becario (_EU_INTERN). Cada entrada: {company, name, url, y uno de
def _get_json(url, data=None):
    _exigir_tiempo()
    req = urllib.request.Request(url, data=data, headers=_HDR)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT, context=_CTX) as r:
        return json.load(r)


def _offer(company, role, url, loc=""):
    """`loc` NO entra en la key a propósito. Las fuentes que ya traen ubicación
    la llevan dentro de `role` ("título · ciudad"), así que la key ya las
    distingue; meterla otra vez cambiaría todos los hashes y seen_boards.json
    daría por NUEVAS todas las ofertas en el email siguiente. Aquí solo se
    guarda aparte para que jobs.db pueda comparar ciudades sin parsear el rol."""
    key = hashlib.sha1(f"{company}¦{role}".encode()).hexdigest()[:16]
    return {"key": key, "company": company, "role": role, "url": url,
            "loc": loc, "nospon": False, "uscit": False}


# Términos que busca Workday: 'intern' (EN) + español, porque las grandes
# empresas de España publican "becas"/"prácticas", no "internship".
_WD_TERMS = ("intern", "beca", "prácticas", "graduate", "talent program")


def _wd_prefix(host, tenant, site):
    """Prefijo de las URL de oferta. Los hosts `*.myworkdaysite.com` son
    MULTITENANT: la ruta lleva `/recruiting/<tenant>/` en medio (Havas). Los
    `<tenant>.myworkdayjobs.com` ya identifican al tenant por el subdominio."""
    if "myworkdaysite.com" in host:
        return f"https://{host}/en-US/recruiting/{tenant}/{site}"
    return f"https://{host}/en-US/{site}"


def _workday(host, tenant, site, pages=2):
    """Endpoint cxs de Workday: por cada término pagina buscando becarios.
    Devuelve (title, url_oferta, loc). ponytail: tope de `pages`*20 por término
    (bajo para acotar la red: muchas firmas ES se consultan por noche); una firma
    con >40 becas por término perdería la cola (rarísimo para becarios).
    Workday busca por SUBCADENA — 'intern' casa con 'International' e 'Internal' —
    así que en tenants enormes (Marsh McLennan, Havas) las becas de verdad caen
    detrás del ruido: esos llevan `pages` propio, 4º elemento de la tupla `wd`."""
    base, out = f"https://{host}", []
    pref = _wd_prefix(host, tenant, site)
    for term in _WD_TERMS:
        for off in range(0, pages * 20, 20):
            payload = json.dumps({"appliedFacets": {}, "limit": 20, "offset": off,
                                  "searchText": term}).encode()
            try:
                d = _get_json(f"{base}/wday/cxs/{tenant}/{site}/jobs", payload)
            except Exception:                   # noqa: BLE001
                break
            jps = d.get("jobPostings", [])
            for jp in jps:
                out.append((jp.get("title", ""),
                            f"{pref}{jp.get('externalPath', '')}",
                            jp.get("locationsText", "")))
            if len(jps) < 20:
                break
    return out


# --- Los otros tres ATS del portal del IE. Los tres exponen JSON público. ---
# SmartRecruiters pagina y busca por SUBCADENA igual que Workday ('intern' casa
# con 'International'), así que va por términos con offset. Workable y Recruitee
# devuelven el tablero entero de una vez: no hace falta ni buscar ni paginar, el
# filtro europeo de fetch_eu se encarga del resto.

def _smartrecruiters(cid, pages=2):
    """API pública de SmartRecruiters. Devuelve (title, url, loc)."""
    out = []
    for term in _WD_TERMS:
        for off in range(0, pages * 100, 100):
            url = (f"https://api.smartrecruiters.com/v1/companies/{cid}/postings"
                   f"?limit=100&offset={off}&q={urllib.parse.quote(term)}")
            try:
                d = _get_json(url)
            except Exception:                   # noqa: BLE001
                break
            items = d.get("content", [])
            for j in items:
                loc = j.get("location") or {}
                out.append((j.get("name", ""),
                            f"https://jobs.smartrecruiters.com/{cid}/{j.get('id')}",
                            ", ".join(x for x in (loc.get("city"), loc.get("region"),
                                                  loc.get("country")) if x)))
            if len(items) < 100:
                break
    return out


def _workable(account):
    d = _get_json(f"https://apply.workable.com/api/v1/widget/accounts/{account}?details=true")
    return [(j.get("title", ""), j.get("url") or j.get("application_url", ""),
             ", ".join(x for x in (j.get("city"), j.get("country")) if x))
            for j in d.get("jobs", [])]


def _recruitee(account):
    d = _get_json(f"https://{account}.recruitee.com/api/offers/")
    return [(j.get("title", ""), j.get("careers_url") or j.get("careers_apply_url", ""),
             ", ".join(x for x in (j.get("city"), j.get("country")) if x))
            for j in d.get("offers", [])]


# Teamtailor no tiene API pública, pero SÍ publica el tablero entero en un RSS
# con ciudad y país en su propio namespace (`tt:`). Se lee con regex, como el
# resto de fuentes de texto de este fichero. La clave es el DOMINIO, no un slug:
# cada cliente de Teamtailor sirve el suyo (talento.arcanopartners.com…).
_TT_ITEM = re.compile(r"<item>(.*?)</item>", re.S)
_TT_TITLE = re.compile(r"<title>(.*?)</title>", re.S)
_TT_LINK = re.compile(r"<link>(.*?)</link>", re.S)
_TT_CITY = re.compile(r"<tt:city>(.*?)</tt:city>", re.S)
_TT_COUNTRY = re.compile(r"<tt:country>(.*?)</tt:country>", re.S)


def _teamtailor(host):
    _exigir_tiempo()
    req = urllib.request.Request(f"https://{host}/jobs.rss", headers=_HDR)
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT, context=_CTX) as r:
        xml = r.read().decode("utf-8", "replace")
    out = []
    for item in _TT_ITEM.findall(xml):
        t, l = _TT_TITLE.search(item), _TT_LINK.search(item)
        if not (t and l):
            continue
        loc = ", ".join(m.group(1).strip() for m in
                        (_TT_CITY.search(item), _TT_COUNTRY.search(item)) if m)
        out.append((html.unescape(t.group(1).strip()), l.group(1).strip(),
                    html.unescape(loc)))
    return out


def _get_html(url):
    _exigir_tiempo()
    req = urllib.request.Request(url, headers=_HDR)
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT, context=_CTX) as r:
        return r.read().decode("utf-8", "replace")


# --- SuccessFactors y Avature: sin JSON, pero SÍ sirven el listado en el HTML ---
# Sondeo del 2026-08-20 sobre las 15 empresas de "propio/otro" (ATS_TARGETS.md):
# de las tres que se dejaron leer sin navegador, dos van por aquí (Nestlé y
# L'Oréal), y de propina EY. El resto (Goldman, IBM, Uber, Revolut, Glovo, Bain,
# A&M, BNP, Crédit Agricole, Natixis, EIB, KPMG España) pinta las ofertas en
# cliente o vive detrás de un WAF: descartadas, replicarlas exigiría navegador.

# SuccessFactors (Nestlé, EY). El buscador `q=` es por relevancia, NO un filtro:
# pide "intern" y devuelve de todo, así que la criba de verdad la hace fetch_eu.
# Lo que sí filtra es `locationsearch`, y por eso se consulta PAÍS A PAÍS: sin
# ese bucle no hay forma de sacar Europa de un tablero mundial. El tamaño de
# página lo elige cada cliente (Nestlé 10, EY 25), así que el salto de `startrow`
# se toma de la primera página en vez de fijarlo: con el número equivocado se
# saltan ofertas enteras.
_SF_ROW = re.compile(r'<tr class="data-row">(.*?)</tr>', re.S)
_SF_LINK = re.compile(r'<a\b(?=[^>]*class="jobTitle-link")(?=[^>]*href="([^"]+)")[^>]*>([^<]*)</a>')
_SF_LOC = re.compile(r'class="jobLocation"[^>]*>\s*([^<]*)')
_SF_EU = ("Spain", "United Kingdom", "France", "Germany", "Italy", "Portugal",
          "Netherlands", "Ireland", "Switzerland", "Poland", "Belgium")


def _successfactors(base, countries=None, pages=2):
    """Listado HTML de un portal SuccessFactors (jobs2web). (title, url, loc).

    `base` es el host, o host+prefijo cuando el portal no cuelga de la raíz
    ("careers.ey.com/ey"). A la ubicación se le PEGA el país consultado: el
    portal la escribe con el código ISO ("Esplugues Llobregat, B, ES, 08950") y
    _LOC busca nombres de país o de ciudad, así que sin esto España entera
    se caía del filtro.

    `countries` sale de las regiones del config (ver _PAISES_REGION): este
    portal solo entiende nombres de país, no la regex."""
    countries = countries or _SF_PAISES
    out, vistos = [], set()
    leidas, fallo = 0, None
    for pais in countries:
        paso, row = 0, 0
        for _ in range(pages):
            url = (f"https://{base}/search/?q=intern&startrow={row}"
                   f"&locationsearch={urllib.parse.quote(pais)}")
            try:
                page = _get_html(url)
            except Exception as e:              # noqa: BLE001
                fallo = e
                break
            leidas += 1
            filas = _SF_ROW.findall(page)
            for fila in filas:
                m = _SF_LINK.search(fila)
                if not m:
                    continue
                href = html.unescape(m.group(1))   # el href viene con &amp; dentro
                if href in vistos:              # cada fila se pinta dos veces (web y móvil)
                    continue
                vistos.add(href)
                loc = _SF_LOC.search(fila)
                loc = html.unescape(loc.group(1).strip()) if loc else ""
                # el país solo se pega si hace falta: los portales de un solo
                # país (carreras.kpmg.es) IGNORAN `locationsearch` y devuelven
                # lo mismo para todos, así que pegarlo a ciegas etiquetaba una
                # oferta de Madrid como "France"
                if loc and _LOC.search(loc):
                    pass
                elif loc:
                    loc = f"{loc} · {pais}"
                else:
                    loc = pais
                out.append((html.unescape(m.group(2).strip()),
                            urllib.parse.urljoin(f"https://{base}/", href), loc))
            paso = paso or len(filas)            # tamaño de página: el de la 1.ª
            if not paso or len(filas) < paso:    # última página (o ninguna): a otro país
                break
            row += paso
    if not leidas and fallo:        # ni una página: la fuente está caída, no vacía
        raise fallo                 # (un 0 en silencio se lee como "no hay becas")
    return out


# Avature (L'Oréal). Aquí el buscador SÍ filtra (`SearchJobs/<término>`), así que
# basta un término y paginar con `jobOffset` de 20 en 20. La ubicación es el
# primer <span> del subtítulo; el segundo es la fecha de publicación.
_AV_ART = re.compile(r'<article class="[^"]*article--result[^"]*"(.*?)</article>', re.S)
_AV_LINK = re.compile(r'<a href="(https://[^"]*/JobDetail/[^"]+)"[^>]*>\s*(.*?)\s*</a>', re.S)
_AV_SPAN = re.compile(r"<span>\s*(.*?)\s*</span>", re.S)


def _avature(host, path="/en_US/careers/SearchJobs", term="intern", pages=7):
    """Listado HTML de un portal Avature. Devuelve (title, url, loc)."""
    out, leidas, fallo = [], 0, None
    for off in range(0, pages * 20, 20):
        url = (f"https://{host}{path}/{urllib.parse.quote(term)}"
               f"?listFilterMode=1&jobOffset={off}")
        try:
            page = _get_html(url)
        except Exception as e:                  # noqa: BLE001
            fallo = e
            break
        leidas += 1
        arts = _AV_ART.findall(page)
        for art in arts:
            m = _AV_LINK.search(art)
            if not m:
                continue
            spans = _AV_SPAN.findall(art)
            loc = re.sub(r"\s+", " ", html.unescape(spans[0])).strip() if spans else ""
            out.append((re.sub(r"\s+", " ", html.unescape(m.group(2))).strip(),
                        html.unescape(m.group(1)), loc))
        if len(arts) < 20:
            break
    if not leidas and fallo:        # ídem: caída != tablero vacío
        raise fallo
    return out


def _direct_url(entry):
    if "gh" in entry:
        return f"https://boards.greenhouse.io/{entry['gh']}"
    if "lever" in entry:
        return f"https://jobs.lever.co/{entry['lever']}"
    if "ashby" in entry:
        return f"https://jobs.ashbyhq.com/{entry['ashby']}"
    if "sr" in entry:
        return f"https://jobs.smartrecruiters.com/{entry['sr']}"
    if "workable" in entry:
        return f"https://apply.workable.com/{entry['workable']}/"
    if "recruitee" in entry:
        return f"https://{entry['recruitee']}.recruitee.com/"
    if "teamtailor" in entry:
        return f"https://{entry['teamtailor']}/jobs"
    if "sf" in entry:
        return f"https://{entry['sf']}/search/?q=intern"
    if "avature" in entry:
        return f"https://{entry['avature']}/en_US/careers/SearchJobs/intern"
    host, tenant, site = entry["wd"][:3]
    return _wd_prefix(host, tenant, site)


_AMZ_CC = ["ESP", "GBR", "DEU", "IRL", "FRA", "ITA", "POL", "PRT", "NLD", "LUX", "BEL", "CHE", "AUT"]


# ---------- fuentes ----------
# Cada entrada de las listas DIRECT_*: `company` (para el ranking y la ficha),
# `name` (el rótulo que se ve) y UNA clave de ATS — gh / lever / ashby / sr /
# workable / recruitee / teamtailor / sf / avature / wd / amazon / jpm — o
# `link_only` para las marcas que no exponen listado público y de las que solo
# se guarda el enlace a su web. Todas comprobadas en vivo.
#
# Los nombres de categoría los usa la app tal cual (app.py, index.html): si
# cambias uno aquí, cámbialo allí. Llevan emoji porque son lo que se ve en
# pantalla, no una clave interna.
EU_CAT = "🇪🇺 Europa · empresas con buen nombre"
ES_CAT = "🇪🇸 España · empresas top"
DIRECT_CAT = "🏦 Banca de inversión & quant · Europa"
CONSULT_CAT = "💼 Consultoría & auditoría · Europa"
SUMMER_CAT = "☀️ Programas de verano"
INSIGHT_CAT = "🌱 Spring weeks e insight programmes"
US_CAT = "🇺🇸 EE. UU. · solo grandes nombres (un vistazo)"

# Empresas con buen nombre que publican prácticas en Europa (Londres, Berlín,
# París, Milán, Dublín, Ámsterdam…), en inglés o español. Da igual el puesto:
# lo que importa es la marca, así que se listan TODAS sus prácticas.
DIRECT_EU = [
    {"company": "Amazon", "name": "Amazon · Europa (prácticas)", "amazon": True,
     "url": "https://www.amazon.jobs/en/search?base_query=intern&country=ESP"},
    {"company": "Celonis", "name": "Celonis · Múnich / Europa", "gh": "celonis",
     "url": "https://www.celonis.com/careers/jobs/"},
    {"company": "Doctolib", "name": "Doctolib · París / Milán", "gh": "doctolib",
     "url": "https://careers.doctolib.com/"},
    {"company": "SumUp", "name": "SumUp · Berlín / Europa", "gh": "sumup",
     "url": "https://www.sumup.com/careers/positions/"},
    {"company": "Aircall", "name": "Aircall · París", "lever": "aircall",
     "url": "https://jobs.lever.co/aircall"},
    {"company": "HelloFresh", "name": "HelloFresh · Berlín / Ámsterdam", "gh": "hellofresh",
     "url": "https://careers.hellofresh.com/global/en/search-results"},
    {"company": "Cabify", "name": "Cabify · Madrid", "gh": "cabify",
     "url": "https://cabify.com/en/jobs"},
    {"company": "BlaBlaCar", "name": "BlaBlaCar · París", "lever": "blablacar",
     "url": "https://jobs.lever.co/blablacar"},
    # --- Ampliación 2026-08-10: slugs probados en vivo (probe de 122 candidatos,
    # 41 tableros vivos). Muchos dan 0 becas HOY; se dejan para avisar en cuanto
    # abran convocatoria, igual que se hace con las españolas de DIRECT_ES. ---
    {"company": "Stripe", "name": "Stripe · Dublín / Londres", "gh": "stripe"},
    {"company": "Databricks", "name": "Databricks · Ámsterdam / Londres", "gh": "databricks"},
    {"company": "Datadog", "name": "Datadog · París", "gh": "datadog"},
    {"company": "Adyen", "name": "Adyen · Ámsterdam", "gh": "adyen"},
    {"company": "N26", "name": "N26 · Berlín", "gh": "n26"},
    {"company": "Monzo", "name": "Monzo · Londres", "gh": "monzo"},
    {"company": "GoCardless", "name": "GoCardless · Londres", "gh": "gocardless"},
    {"company": "Tide", "name": "Tide · Londres", "gh": "tide"},
    {"company": "GetYourGuide", "name": "GetYourGuide · Berlín", "gh": "getyourguide"},
    {"company": "Dataiku", "name": "Dataiku · París", "gh": "dataiku"},
    {"company": "Alan", "name": "Alan · París", "ashby": "alan"},
    {"company": "Qonto", "name": "Qonto · París", "ashby": "qonto"},
    {"company": "Pennylane", "name": "Pennylane · París", "ashby": "pennylane"},
    {"company": "Poolside", "name": "Poolside · París", "ashby": "poolside"},
    {"company": "ElevenLabs", "name": "ElevenLabs · Londres", "ashby": "elevenlabs"},
    {"company": "Synthesia", "name": "Synthesia · Londres", "ashby": "synthesia"},
    {"company": "Harvey", "name": "Harvey · Londres", "ashby": "harvey"},
    {"company": "Legora", "name": "Legora · Estocolmo / Londres", "ashby": "legora"},
    {"company": "Lovable", "name": "Lovable · Estocolmo", "ashby": "lovable"},
    {"company": "n8n", "name": "n8n · Berlín", "ashby": "n8n"},
    {"company": "Tacto", "name": "Tacto · Múnich", "ashby": "tacto"},
    {"company": "The Exploration Company", "name": "The Exploration Company · Múnich / Burdeos",
     "ashby": "the-exploration-company"},
    # --- Alta 2026-08-12: del portal del IE (ATS_TARGETS.md), sondeadas en vivo. ---
    # Havas va en un Workday MULTITENANT (`wd3.myworkdaysite.com`): la URL de la
    # oferta lleva `/recruiting/havas/` (ver _wd_prefix). Sus becas españolas son
    # el programa "Havas FirstGen" en Madrid y Barcelona; pages=8 porque el
    # tablero es del grupo entero.
    {"company": "Havas", "name": "Havas · Madrid / Barcelona / París",
     "wd": ("wd3.myworkdaysite.com", "havas", "GroupExternalCareerSite", 8)},
    {"company": "Sandoz", "name": "Sandoz · Madrid / Europa",
     "wd": ("sandoz.wd103.myworkdayjobs.com", "sandoz", "Sandoz_Careers", 8)},
    {"company": "Aptura", "name": "Aptura · Londres", "ashby": "aptura"},
    {"company": "Radisson", "name": "Radisson Hotel Group · Madrid / Europa", "sr": "RHG"},
    {"company": "Destinus", "name": "Destinus · Suiza / Europa (aeroespacial)",
     "workable": "destinusgroup"},
    # --- Alta 2026-08-20: las dos únicas de la lista de "propio/otro" que se
    # dejan leer sin navegador (ver _successfactors / _avature). ---
    {"company": "Nestlé", "name": "Nestlé · Europa (SuccessFactors)", "sf": "jobdetails.nestle.com"},
    {"company": "L'Oréal", "name": "L'Oréal · París / Europa (Avature)", "avature": "careers.loreal.com"},
    # Sin API pública fácil (ATS propio) -> tarjeta de enlace a su web de prácticas EU.
    {"company": "Revolut", "name": "Revolut · Europa", "link_only": True,
     "url": "https://www.revolut.com/careers/?department=Internships"},
]

# Grandes empresas españolas (IBEX-35 y bancos): publican "becas"/"prácticas"
# (por eso el fetch de Workday busca también en español). Endpoints Workday
# verificados en vivo; muchas dan 0 becas hoy pero se dejan para AVISAR en cuanto
# abran convocatoria: aquí pesa la MARCA, no el puesto concreto.
DIRECT_ES = [
    {"company": "Santander", "name": "Banco Santander · España",
     "wd": ("santander.wd3.myworkdayjobs.com", "santander", "SantanderCareers"),
     "url": "https://santander.wd3.myworkdayjobs.com/SantanderCareers"},
    {"company": "BBVA", "name": "BBVA · España",
     "wd": ("bbva.wd3.myworkdayjobs.com", "bbva", "BBVA"),
     "url": "https://bbva.wd3.myworkdayjobs.com/BBVA"},
    {"company": "Iberdrola", "name": "Iberdrola · España",
     "wd": ("iberdrola.wd3.myworkdayjobs.com", "iberdrola", "Iberdrola"),
     "url": "https://iberdrola.wd3.myworkdayjobs.com/Iberdrola"},
    {"company": "Repsol", "name": "Repsol · España",
     "wd": ("repsol.wd3.myworkdayjobs.com", "repsol", "Repsol"),
     "url": "https://repsol.wd3.myworkdayjobs.com/Repsol"},
    {"company": "Telefónica", "name": "Telefónica · España",
     "wd": ("telefonica.wd3.myworkdayjobs.com", "telefonica", "TelefonicaTalento"),
     "url": "https://telefonica.wd3.myworkdayjobs.com/TelefonicaTalento"},
    {"company": "Amadeus", "name": "Amadeus · España",
     "wd": ("amadeus.wd3.myworkdayjobs.com", "amadeus", "Amadeus"),
     "url": "https://amadeus.wd3.myworkdayjobs.com/Amadeus"},
    {"company": "NTT Data", "name": "NTT Data · España",
     "wd": ("nttdata.wd3.myworkdayjobs.com", "nttdata", "NTTDATA"),
     "url": "https://nttdata.wd3.myworkdayjobs.com/NTTDATA"},
    {"company": "Indra", "name": "Indra / Minsait · España",
     "wd": ("indracompany.wd3.myworkdayjobs.com", "indracompany", "Indra"),
     "url": "https://indracompany.wd3.myworkdayjobs.com/Indra"},
    {"company": "Ferrovial", "name": "Ferrovial · España",
     "wd": ("ferrovial.wd3.myworkdayjobs.com", "ferrovial", "Ferrovial"),
     "url": "https://ferrovial.wd3.myworkdayjobs.com/Ferrovial"},
    {"company": "Mapfre", "name": "Mapfre · España",
     "wd": ("mapfre.wd3.myworkdayjobs.com", "mapfre", "Mapfre"),
     "url": "https://mapfre.wd3.myworkdayjobs.com/Mapfre"},
    # Tech española en Greenhouse/Lever (probado en vivo 2026-08-10).
    {"company": "Ebury", "name": "Ebury · Madrid", "gh": "ebury"},
    {"company": "Typeform", "name": "Typeform · Barcelona", "gh": "typeform"},
    {"company": "Jobandtalent", "name": "Jobandtalent · Madrid", "lever": "jobandtalent"},
    # Del portal del IE: se anunciaban con dominio propio, pero por debajo son
    # ATS conocidos (ver ATS_TARGETS.md, sonda del 2026-08-12).
    {"company": "Fever", "name": "Fever · Madrid", "gh": "feverup"},
    {"company": "Dcycle", "name": "Dcycle · Madrid", "teamtailor": "jobs.dcycle.io"},
    # CaixaBank usa ATS propio (sin API pública) -> tarjeta de enlace.
    {"company": "CaixaBank", "name": "CaixaBank · España", "link_only": True,
     "url": "https://jobs.caixabank.com/es/ofertas-empleo/?keyword=becario"},
]

# Banca de inversión y quant con oficinas en Europa. JP Morgan vía API pública de
# Oracle (incluye Investment Banking Off-Cycle/Summer Analyst en Londres, Fráncfort,
# París, Ámsterdam). Los demás vía Greenhouse/Lever/Workday, filtrados a Europa.
_JPM_HOST, _JPM_SITE = "jpmc.fa.oraclecloud.com", "CX_1001"
DIRECT_BANK = [
    # Con listado en vivo (API pública): se muestran TODAS sus prácticas en Europa.
    {"company": "JP Morgan", "name": "J.P. Morgan · banca de inversión (Europa)", "jpm": True,
     "url": f"https://{_JPM_HOST}/hcmUI/CandidateExperience/en/sites/{_JPM_SITE}/requisitions"},
    {"company": "Morgan Stanley", "name": "Morgan Stanley · Europa", "wd": ("ms.wd5.myworkdayjobs.com", "ms", "External")},
    {"company": "Deutsche Bank", "name": "Deutsche Bank · Europa", "wd": ("db.wd3.myworkdayjobs.com", "db", "DBWebsite")},
    {"company": "Citi", "name": "Citi · Europa", "wd": ("citi.wd5.myworkdayjobs.com", "citi", "2")},
    {"company": "Point72", "name": "Point72 · Europa", "gh": "point72"},
    {"company": "Jump Trading", "name": "Jump Trading · Europa", "gh": "jumptrading"},
    {"company": "IMC", "name": "IMC Trading · Ámsterdam", "gh": "imc"},
    {"company": "Optiver", "name": "Optiver · Ámsterdam", "gh": "optiver"},
    {"company": "DRW", "name": "DRW · Londres", "gh": "drweng"},
    {"company": "Squarepoint", "name": "Squarepoint · Londres / París", "gh": "squarepointcapital"},
    {"company": "Virtu", "name": "Virtu Financial · Europa", "gh": "virtu"},
    {"company": "Old Mission", "name": "Old Mission · Europa", "gh": "oldmissioncapital"},
    {"company": "Quadrature", "name": "Quadrature Capital · Londres", "gh": "quadraturecapital"},
    {"company": "Palantir", "name": "Palantir · Europa", "lever": "palantir"},
    {"company": "Flow Traders", "name": "Flow Traders · Ámsterdam", "gh": "flowtraders"},
    {"company": "Marshall Wace", "name": "Marshall Wace · Londres", "gh": "marshallwace"},
    {"company": "Schonfeld", "name": "Schonfeld · Londres", "gh": "schonfeld"},
    # --- Firmas que publican en Workday. Sondeadas en vivo: sitio, tenant y
    # número de becas europeas comprobados antes de escribirlas aquí. Nunca
    # adivines una URL de Workday: o la has visto responder, o no entra. ---
    {"company": "Alantra", "name": "Alantra · Madrid / Europa",
     "wd": ("alantra.wd3.myworkdayjobs.com", "alantra", "Alantra")},
    {"company": "Ardian", "name": "Ardian · París / Londres / Luxemburgo",
     "wd": ("ardian.wd103.myworkdayjobs.com", "ardian", "ArdianCareers", 8)},
    {"company": "Blackstone", "name": "Blackstone · Londres / Fráncfort (campus)",
     "wd": ("blackstone.wd1.myworkdayjobs.com", "blackstone", "Blackstone_Campus_Careers")},
    {"company": "Julius Baer", "name": "Julius Baer · Zúrich / Europa",
     "wd": ("juliusbaer.wd3.myworkdayjobs.com", "juliusbaer", "External")},
    {"company": "AFS Group", "name": "AFS Group · Ámsterdam (bróker interdealer)",
     "recruitee": "afsgroup"},
    {"company": "Arcano", "name": "Arcano Partners · Madrid",
     "teamtailor": "talento.arcanopartners.com"},
    {"company": "Pai Partners", "name": "PAI Partners · Londres / París",
     "recruitee": "paipartners"},
    # --- Alta 2026-08-20: única de la lista de "propio/otro" con JSON público
    # de verdad (Workday). Tenant enorme -> `pages` propio, como Havas/Ardian.
    # `BlackRock_Professional` es su ÚNICO sitio (probados Students/Campus/Early
    # Careers: 404). Hoy da 182 ofertas y 0 becas europeas: se deja igual que las
    # españolas, para avisar en cuanto abran convocatoria. ---
    {"company": "BlackRock", "name": "BlackRock · Londres / Europa",
     "wd": ("blackrock.wd1.myworkdayjobs.com", "blackrock", "BlackRock_Professional", 8)},
    # Marcas icónicas sin API pública (alojan en su propia web) -> tarjeta de enlace.
    {"company": "Citadel", "name": "Citadel / Citadel Securities · Europa", "link_only": True,
     "url": "https://www.citadel.com/careers/students-and-graduates/"},
    {"company": "Jane Street", "name": "Jane Street · Londres", "link_only": True,
     "url": "https://www.janestreet.com/join-jane-street/open-roles/?type=internship&location=london"},
    {"company": "Goldman Sachs", "name": "Goldman Sachs · Europa", "link_only": True,
     "url": "https://www.goldmansachs.com/careers/students/"},
    {"company": "Bloomberg", "name": "Bloomberg · Londres", "link_only": True,
     "url": "https://careers.bloomberg.com/job/search?q=intern"},
]

# Consultoría estratégica + Big Four. PwC SÍ tiene API pública (Workday campus, con
# muchas becas en España) -> se lista entera. EY también, desde el 2026-08-20: no
# da JSON, pero su SuccessFactors sirve el listado en el HTML y se lee con
# _successfactors (la nota vieja decía "render JS"; no era cierto).
# Deloitte se queda en enlace: su Avature público (apply.deloitte.com) es el
# portal de EE. UU. y escribe "Deloitte US" en la ubicación, así que no hay
# Europa que filtrar ahí. KPMG (fragmentado) y McKinsey (web propia), igual.
DIRECT_CONSULT = [
    {"company": "PwC", "name": "PwC · Europa (becas)",
     "wd": ("pwc.wd3.myworkdayjobs.com", "pwc", "Global_Campus_Careers"),
     "url": "https://pwc.wd3.myworkdayjobs.com/Global_Campus_Careers"},
    # Oliver Wyman comparte el Workday del grupo (tenant `mmc` = Marsh McLennan),
    # así que este tablero trae también Marsh y Mercer — de ahí el rótulo: la
    # mayoría de lo que sale en España son "Beca ..." de Marsh, no de Oliver
    # Wyman. Tenant gigantesco (>1000 resultados para 'intern', casi todo ruido
    # de "International"/"Internal") -> pages=8 para llegar a las becas de verdad.
    {"company": "Oliver Wyman", "name": "Marsh McLennan (Oliver Wyman · Marsh · Mercer) · España / Europa",
     "wd": ("mmc.wd1.myworkdayjobs.com", "mmc", "MMC", 8)},
    {"company": "Deloitte", "name": "Deloitte · España / Europa", "link_only": True,
     "url": "https://apply.deloitte.com/en_US/careers/SearchJobs/intern?21178=%5B586%5D&21178_format=6027"},
    # El portal de EY no cuelga de la raíz del host: va bajo /ey (ver _successfactors).
    {"company": "EY", "name": "EY (Ernst & Young) · Europa", "sf": "careers.ey.com/ey"},
    # KPMG España va por SuccessFactors en carreras.kpmg.es (NO careers.kpmg.es,
    # que no existe: el host bueno estaba en ATS_TARGETS.md todo el tiempo).
    {"company": "KPMG", "name": "KPMG · España", "sf": "carreras.kpmg.es"},
    {"company": "McKinsey", "name": "McKinsey & Company · Europa", "link_only": True,
     "url": "https://www.mckinsey.com/careers/search-jobs#/?query=intern"},
]



def _cargar_descubiertos():
    """Tableros que probe.py haya encontrado vivos y con becas (`descubiertos.json`).

    Es opcional: sin el fichero no pasa nada. Se descartan las empresas que ya
    están en las listas de arriba — una entrada curada a mano siempre gana a una
    descubierta, porque lleva el rótulo y la categoría bien puestos."""
    try:
        found = json.loads(DESCUBIERTOS.read_text())
    except Exception:                                        # noqa: BLE001
        return []                                            # sin fichero aún, o corrupto
    known = {d["company"].lower()
             for d in DIRECT_EU + DIRECT_ES + DIRECT_BANK + DIRECT_CONSULT}
    return [d for d in found
            if isinstance(d, dict) and d.get("company", "").lower() not in known
            and ({"gh", "lever", "ashby"} & set(d))]


DIRECT_EU += _cargar_descubiertos()

# Tableros comunitarios de GitHub. `solo_top` deja pasar únicamente las empresas
# de la lista de arriba: son tableros de EE. UU. con miles de ofertas, y sin ese
# corte se comen el catálogo entero. Quien esté EN Estados Unidos puede quitarlo
# con "github_solo_top": false en config.json. Aquí NO se filtra por ubicación:
# el tablero es de un país entero y la columna de ciudad viene como venga.
TABLEROS = [
    {"cat": US_CAT, "name": "Summer 2027 Internships (empresas top)",
     "url": "https://github.com/vanshb03/Summer2026-Internships"},
]
def fetch_amazon_eu():
    """Prácticas de Amazon en países europeos (usa el flag is_intern de su API)."""
    out, seen = [], set()
    for cc in _AMZ_CC:
        try:
            d = _get_json("https://www.amazon.jobs/en/search.json?base_query=intern"
                          f"&country={cc}&result_limit=100")
        except Exception:                       # noqa: BLE001
            continue
        for j in d.get("jobs", []):
            title = j.get("title", "")
            if not (j.get("is_intern") or _TITULO_BECA.search(title)):
                continue
            loc = j.get("normalized_location", "")
            url = "https://www.amazon.jobs" + (j.get("job_path", "") or "")
            o = _offer("Amazon", f"{title} · {loc}" if loc else title, url, loc)
            if o["key"] in seen:
                continue
            seen.add(o["key"])
            out.append(o)
    return out


# JP Morgan publica en Oracle Recruiting (CE). Su API pública devuelve
# requisitionList; paginamos por varias keywords (incluye banca de inversión:
# 'off-cycle', 'summer analyst') y filtramos a Europa + becario. Host/site arriba.
_JPM_KW = ("intern", "summer analyst", "working student", "off-cycle", "graduate", "apprentice")


def fetch_jpm():
    uniq = {}
    for kw in _JPM_KW:
        for off in range(0, 150, 25):
            url = (f"https://{_JPM_HOST}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
                   f"?onlyData=true&expand=requisitionList.secondaryLocations"
                   f"&finder=findReqs;siteNumber={_JPM_SITE},keyword={urllib.parse.quote(kw)},"
                   f"limit=25,offset={off}")
            try:
                d = _get_json(url)
            except Exception:                   # noqa: BLE001
                break
            reqs = (d.get("items") or [{}])[0].get("requisitionList", [])
            if not reqs:
                break
            for r in reqs:
                title, loc = r.get("Title", ""), r.get("PrimaryLocation", "") or ""
                if not (_TITULO_BECA.search(title) and _LOC.search(loc)):
                    continue
                job_url = (f"https://{_JPM_HOST}/hcmUI/CandidateExperience/en/sites/"
                           f"{_JPM_SITE}/job/{r.get('Id')}")
                o = _offer("JP Morgan", f"{title} · {loc}", job_url, loc)
                uniq[o["key"]] = o
            if len(reqs) < 25:
                break
    return list(uniq.values())


def fetch_eu(entry):
    """Prácticas EN EUROPA de la firma vía su ATS: ubicación europea (_LOC) +
    título de becario en ES o EN (_TITULO_BECA). La ubicación se muestra en el rol."""
    if entry.get("amazon"):
        return fetch_amazon_eu()
    if entry.get("jpm"):
        return fetch_jpm()
    company = entry["company"]
    if "gh" in entry:
        d = _get_json(f"https://boards-api.greenhouse.io/v1/boards/{entry['gh']}/jobs")
        items = [(j.get("title", ""), j.get("absolute_url", ""),
                  (j.get("location") or {}).get("name", "")) for j in d.get("jobs", [])]
    elif "lever" in entry:
        d = _get_json(f"https://api.lever.co/v0/postings/{entry['lever']}?mode=json")
        items = [(j.get("text", ""), j.get("hostedUrl", ""),
                  (j.get("categories") or {}).get("location", "")) for j in (d if isinstance(d, list) else [])]
    elif "ashby" in entry:
        d = _get_json(f"https://api.ashbyhq.com/posting-api/job-board/{entry['ashby']}")
        items = [(j.get("title", ""), j.get("jobUrl", ""), j.get("location", ""))
                 for j in (d.get("jobs", []) if isinstance(d, dict) else [])]
    elif "sr" in entry:
        items = _smartrecruiters(entry["sr"])
    elif "workable" in entry:
        items = _workable(entry["workable"])
    elif "recruitee" in entry:
        items = _recruitee(entry["recruitee"])
    elif "teamtailor" in entry:
        items = _teamtailor(entry["teamtailor"])
    elif "sf" in entry:
        items = _successfactors(entry["sf"])
    elif "avature" in entry:
        items = _avature(entry["avature"])
    else:
        items = _workday(*entry["wd"])
    offers, seenk = [], set()
    for title, url, loc in items:
        if not title or not _TITULO_BECA.search(title) or not _LOC.search(loc):
            continue
        if _CLOSED.search(title):      # PAI Partners deja las cerradas en el tablero
            continue                   # y las marca en el propio título

        o = _offer(company, f"{title} · {loc}" if loc else title, url, loc)
        if o["key"] in seenk:
            continue
        seenk.add(o["key"])
        offers.append(o)
    return offers


# Los programas de verano no salen de ningún ATS: son páginas de marketing que
# se abren y se cierran cada primavera. Lista manual, como las DIRECT_*, con las
# URL comprobadas a mano. El título dice qué es cada cosa: unas son un programa
# con nombre propio y otras el portal donde su convocatoria de verano aparece —
# no les invento nombre a las que su web no se lo pone.
SUMMER_PROGRAMS = [
    # --- Consultoría y auditoría ---
    ("KPMG", "KPMG Blue Summer Experience · verano (6-8 semanas, jun-jul) · 2º-3º de carrera · Madrid",
     "https://kpmg.com/es/es/carreras/estudiantes-recien-graduados/kpmg-blue-summer-experience.html"),
    ("Deloitte", "DRisk Experience · verano (6 semanas en julio) · penúltimo año STEM · Madrid / Barcelona",
     "https://www.deloitte.com/es/es/careers/explore-your-fit/students/drisk-experience.html"),
    ("Deloitte", "Becas de verano · convocatoria en su portal de estudiantes · España",
     "https://www.deloitte.com/es/es/careers/explore-your-fit/students/becas-estudiantes.html"),
    ("PwC", "Becas y prácticas de verano · convocatoria en su portal · España",
     "https://www.pwc.es/es/carrera-profesional/becas-y-practicas-profesionales.html"),
    ("EY", "Summer Internship Programme · verano · estudiantes de grado · Reino Unido",
     "https://www.ey.com/en_uk/careers/students/undergraduates/sip"),
    ("EY", "Becas de verano · buscador de su portal · España",
     "https://careers.ey.com/ey/search/?q=summer&locationsearch=Spain"),
    ("BCG", "Visiting Associate · prácticas de verano (11 semanas) · desde 5º semestre · Madrid / Barcelona",
     "https://careers.bcg.com/global/en/locations/spain"),
    ("BCG", "Summer internships · Early Careers · Europa",
     "https://careers.bcg.com/students"),
    ("McKinsey", "Prácticas de verano · buscador de su portal · Europa",
     "https://www.mckinsey.com/careers/search-jobs#/?query=summer"),
    ("Bain", "Associate Consultant Internship · verano (10 semanas) · grado y máster · Europa",
     "https://www.bain.com/careers/work-with-us/internships-programs/associate-consultant-internship/"),
    # --- Banca de inversión ---
    ("Goldman Sachs", "Summer Analyst Programme (EMEA) · verano · penúltimo año · Londres",
     "https://www.goldmansachs.com/careers/students/programs-and-internships/emea/summer-analyst-programme"),
    ("JP Morgan", "Summer programs · programas de verano para estudiantes · Europa",
     "https://careers.jpmorgan.com/global/en/students/programs"),
    ("Citi", "Summer Analyst · programas de verano para estudiantes · Europa",
     "https://jobs.citi.com/students-and-graduates"),
    ("Morgan Stanley", "Summer Analyst Programme · verano (10-13 semanas) · penúltimo año · Londres (EMEA)",
     "https://www.morganstanley.com/people-opportunities/students-graduates"),
    ("Deutsche Bank", "Internship Programme · verano (8-10 semanas) · penúltimo año · Londres / Europa",
     "https://careers.db.com/students-graduates/internship-programme/"),
    ("Bloomberg", "London Summer Internship · verano (10 semanas, jun-ago) · piden hablantes de español",
     "https://www.bloomberg.com/company/early-careers/student-programs/"),
    # --- Trading / quant ---
    ("Optiver", "Summer Internship · verano (10 semanas, jul-ago) · trading, research y tech · Ámsterdam",
     "https://www.optiver.com/join-us/students/"),
    ("IMC", "Summer Internship Programme · verano (10 semanas) · penúltimo año · Ámsterdam",
     "https://www.imc.com/eu/careers/students-graduates/internships"),
    ("Flow Traders", "Trading Intern · verano (8 semanas desde el 1 de julio, alojamiento incluido) · Ámsterdam",
     "https://www.flowtraders.com/careers/job-search/"),
    ("Jane Street", "Summer Internships · verano (10-12 semanas, may-sep) · cualquier curso · Londres",
     "https://www.janestreet.com/join-jane-street/internships/"),
    ("Citadel", "Summer Internship · verano (11 semanas) · quant, trading y software · Londres / Europa",
     "https://www.citadel.com/careers/open-opportunities/internships/"),
    ("Point72", "Point72 Academy Investment Analyst Summer Internship · verano (8 semanas, jun-ago) · Londres",
     "https://careers.point72.com/"),
    ("DRW", "Summer Internship · verano (10 semanas, piso amueblado incluido) · Londres",
     "https://www.drw.com/work-at-drw/interns"),
    ("Jump Trading", "Summer Internship · verano (10-12 semanas) · Londres / Ámsterdam",
     "https://www.jumptrading.com/careers/"),
    ("Marshall Wace", "Technology & Quant Research Internship · verano (jun-sep) · Londres",
     "https://www.mwam.com/join-us/internships/"),
    ("Schonfeld", "Summer Internship · verano (10 semanas, jun-ago) · ops, quant y software · Londres",
     "https://www.schonfeld.com/careers/students-and-early-career/"),
    ("Quadrature", "Summer Internship · verano (11 semanas) · quant dev y core tech · Londres",
     "https://www.quadrature.ai/careers/internships/"),
    # --- España: grandes empresas ---
    ("Santander", "Summer Internship · verano (8 semanas) · penúltimo curso o 1º de máster",
     "https://www.santander.com/en/careers/where-you-want-to-create-an-impact/santander-future-talent/summer-internship"),
    ("Santander", "Summer Internship Program HQ · verano (2 meses a jornada completa, 26 becas) · Madrid",
     "https://www.santander.com/en/careers/where-you-want-to-create-an-impact/santander-future-talent/summer-internship-program-hq"),
    ("Repsol", "Becas Talent Energy · 2 meses, se pueden concentrar en verano · España",
     "https://www.repsol.com/en/careers/internships-and-graduate-programs/index.cshtml"),
    ("UBS", "Summer Internship Program · verano (empieza en junio) · penúltimo año · Londres / EMEA",
     "https://www.ubs.com/global/en/careers/early-careers/summer-internship-program.html"),
    ("Bank of America", "Summer Analyst Programme (EMEA) · verano · penúltimo año · Londres",
     "https://careers.bankofamerica.com/en-us/students/programs"),
    ("HSBC", "Summer Internship · verano · penúltimo año · Londres / Europa",
     "https://www.hsbc.com/careers/students-and-graduates"),
    # --- Tecnología ---
    ("Google", "STEP Internship · verano (12 semanas) · 1º-2º de carrera · Madrid / Londres / Zúrich / Dublín",
     "https://www.google.com/about/careers/applications/students/"),
    ("Accenture", "Summer Internship · verano (11 semanas) · estudiantes de grado · España",
     "https://www.accenture.com/es-es/careers/life-at-accenture/internships-students"),
    ("Palantir", "Path Internship · verano (10-12 semanas) · 2º-3º de carrera sin experiencia previa · Londres",
     "https://www.palantir.com/careers/students/path/"),
    ("Revolut", "Rev-celerator Internship Programme · verano (8-10 semanas) · Reino Unido / Polonia / Portugal",
     "https://www.revolut.com/internship-programme/"),
    ("Databricks", "Software Engineering Intern · verano (12 semanas) · Ámsterdam / Berlín",
     "https://www.databricks.com/company/careers/university-recruiting"),
]
# Spring weeks / insight programmes: una semana en Semana Santa para 1º-2º de
# carrera, sin experiencia previa. NO son prácticas de verano —de hecho el filtro
# _summer_ok las tira, por eso van por su propia vía con season=False— pero son
# la puerta de entrada real: la mayoría de las ofertas de summer internship de la
# banca salen de quien hizo su spring week el año anterior. Tampoco entran en el
# email (ver main): el correo sigue siendo solo verano.
#
# La primera oleada de convocatorias abre en SEPTIEMBRE y la segunda en enero, y
# casi ninguna tiene fecha límite fija: cierran cuando se llenan las plazas.
#
# Misma regla que en SUMMER_PROGRAMS: no se inventan nombres de programa. Si la
# web oficial no publica una página propia, se enlaza el portal donde aparece la
# convocatoria y el título lo dice. Sin años en el texto (los leería _grad_ok).
INSIGHT_PROGRAMS = [
    ("JP Morgan", "Spring Insight Programme · 1 semana en Semana Santa · 1º de carrera · Londres / EMEA",
     "https://careers.jpmorgan.com/global/en/students/programs/spring-insights"),
    ("Deutsche Bank", "Spring into Banking · 1 semana en Semana Santa · 1º de carrera · Londres",
     "https://careers.db.com/students-graduates/insight-programmes/uk-and-ireland/spring-into-banking"),
    ("Deutsche Bank", "Insight Programmes · todas las regiones · primeros cursos",
     "https://careers.db.com/students-graduates/insight-programmes/"),
    ("Goldman Sachs", "Future Possibilities Insight Event · evento de introducción a la firma · EMEA",
     "https://www.goldmansachs.com/careers/students/programs-and-internships/emea/future-possibilities-insight-event"),
    ("Nomura", "Insight Programs · primeros cursos · Londres / EMEA",
     "https://www.nomura.com/careers/early-careers/insight-programs/"),
    ("HSBC", "Insight programmes · primeros cursos · Reino Unido / Europa",
     "https://www.hsbc.com/careers/students-and-graduates/insight-programmes"),
    ("Bank of America", "Spring Insight · 1 semana · 1º de carrera · convocatoria en su portal de programas · Londres / Chester",
     "https://careers.bankofamerica.com/en-us/students/programs"),
    ("Morgan Stanley", "Insight programmes · convocatoria en su portal de programas · Londres (EMEA)",
     "https://www.morganstanley.com/people-opportunities/students-graduates/programs"),
    ("Barclays", "Spring Insight / Discovery · 1 semana en Semana Santa · convocatoria en su portal de early careers · Londres",
     "https://search.jobs.barclays/early-careers"),
    ("Citi", "Insight programme · convocatoria en su portal de estudiantes · Londres / EMEA",
     "https://jobs.citi.com/students-and-graduates"),
    ("Jane Street", "Programas y eventos para primeros cursos · Londres",
     "https://www.janestreet.com/join-jane-street/programs-and-events/"),
]


def _programa_rows(cat, nombre, rid, programas):
    """Fila única con una lista curada a mano. No tocan la red: son páginas de
    marketing que se abren y se cierran cada primavera, así que van escritas
    aquí y no salen de ningún ATS."""
    return {"cat": cat, "name": nombre, "company": "", "url": "", "repo": rid,
            "offers": [_offer(c, r, u) for c, r, u in programas],
            "ok": True, "err": "", "excl": {}}


# ---------- configuración efectiva ----------
# Estos tres los usan los scrapers ya escritos (que los leen como globales, no
# por parámetro) y se rehacen en cada `configurar()`. Los valores de aquí son
# los de arranque: sirven para `--check` y para importar el módulo sin config.
_LOC = _compilar_loc(["es", "pt", "uk", "ie", "de", "fr", "it", "pl",
                      "benelux", "alpes", "nordicos", "europa", "remoto"])
_TOP = _compilar_top(PRESTIGE)
_SF_PAISES = _SF_EU

# SuccessFactors filtra por NOMBRE DE PAÍS, no por ciudad ni por regex: hay que
# consultarlo país a país. De ahí este puente entre las regiones del config y lo
# que ese portal entiende. Las regiones sin país propio (remoto, europa) no
# aportan ninguno: no hay nada que preguntarle a un buscador por país.
_PAISES_REGION = {
    "es": ["Spain"], "pt": ["Portugal"], "uk": ["United Kingdom"],
    "ie": ["Ireland"], "de": ["Germany"], "fr": ["France"], "it": ["Italy"],
    "pl": ["Poland"], "benelux": ["Netherlands", "Belgium", "Luxembourg"],
    "alpes": ["Switzerland", "Austria"],
    "nordicos": ["Denmark", "Sweden", "Norway", "Finland"],
    "us": ["United States"],
    "latam": ["Mexico", "Brazil", "Colombia", "Chile", "Argentina"],
}


def configurar(cfg):
    """Aplica el config: compila las regexes que usan los scrapers y devuelve
    los umbrales de los filtros. Devolver los filtros en vez de dejarlos en
    globales es lo que hace que `_filtrar` se pueda probar sin tocar nada."""
    global _LOC, _TOP, _SF_PAISES
    regiones = cfg.get("regiones") or list(REGIONES)
    _LOC = _compilar_loc(regiones)
    top = cfg.get("empresas_top") or PRESTIGE
    _TOP = _compilar_top(list(top) + list(cfg.get("empresas_top_extra") or []))
    paises = [p for r in regiones for p in _PAISES_REGION.get(r, [])]
    _SF_PAISES = tuple(dict.fromkeys(paises)) or _SF_EU
    return {"grad_year": int(cfg.get("grad_year", 2030)),
            "solo_verano": bool(cfg.get("solo_verano", True)),
            "solo_grado": bool(cfg.get("solo_grado", True)),
            "necesito_visado_us": bool(cfg.get("necesito_visado_us", True))}


def _es_top(company):
    return bool(company) and bool(_TOP.search(company))


def _filtrar(offers, f, verano=True):
    """Aplica los filtros y devuelve (las que pasan, cuántas cayó cada uno).

    `verano=False` salta el filtro de temporada: lo usan las spring weeks, que
    por definición no son de verano y aun así interesan."""
    kept, excl = [], {"graduación": 0, "temporada": 0, "perfil": 0}
    for o in offers:
        if not _grad_ok(o.get("role", ""), f["grad_year"]):
            excl["graduación"] += 1                # exige graduarse antes
        elif verano and f["solo_verano"] and not _summer_ok(o.get("role", "")):
            excl["temporada"] += 1                 # no es de verano
        elif not _realistic(o, f["solo_grado"], f["necesito_visado_us"]):
            excl["perfil"] += 1                    # PhD/máster/ciudadanía US
        else:
            kept.append(o)
    return kept, {k: v for k, v in excl.items() if v}


# ---------- la pasada ----------

def _fuentes_directas(specs, cat, prefix, f):
    """Una fila por empresa. Que una fuente falle no tumba la pasada: se anota
    el error en la fila y se sigue, porque un ATS caído es lo normal, no la
    excepción — y perder las 80 fuentes que sí contestaron por culpa de una es
    lo que no puede pasar."""
    rows = []
    for d in specs:
        rid = f"{prefix}:{d['company']}"
        row = {"cat": cat, "name": d["name"], "company": d["company"],
               "url": d.get("url") or _direct_url(d), "repo": rid,
               "offers": [], "ok": None, "err": "", "excl": {}}
        if d.get("link_only"):              # marca sin listado público: solo el enlace
            row["ok"] = True
        elif not _queda_tiempo():
            row["err"] = "sin comprobar: se agotó el techo de reloj"
        else:
            try:
                row["offers"], row["excl"] = _filtrar(fetch_eu(d), f)
                row["ok"] = True
            except Exception as e:                              # noqa: BLE001
                row["err"] = f"{type(e).__name__}: {e}"[:70]
        rows.append(row)
        PROGRESO["hechas"] += 1
    return rows


def _fuentes_github(f, cfg):
    """Tableros comunitarios: se lee el README en crudo y se parsea la tabla."""
    solo_top = bool(cfg.get("github_solo_top", True))
    rows = []
    for t in TABLEROS:
        owner, repo, branch, path = _parse_url(t["url"])
        rid = f"{owner}/{repo}"
        row = {"cat": t["cat"], "name": t["name"], "company": "", "url": t["url"],
               "repo": rid, "offers": [], "ok": None, "err": "", "excl": {}}
        if not _queda_tiempo():
            row["err"] = "sin comprobar: se agotó el techo de reloj"
            rows.append(row)
            continue
        try:
            meta = _api(f"https://api.github.com/repos/{rid}", cfg)
            branch = branch or meta.get("default_branch") or "HEAD"
        except Exception as e:                                  # noqa: BLE001
            row["err"] = f"api: {e}"[:70]                       # sin token, 403 es normal
            branch = branch or "HEAD"
        try:
            offers = parse_offers(_raw(owner, repo, branch, path))
            offers, row["excl"] = _filtrar(offers, f)
            if solo_top:
                n = len(offers)
                offers = [o for o in offers if _es_top(o["company"])]
                row["excl"]["fuera de tus empresas top"] = n - len(offers)
            row["offers"] = offers
            row["ok"] = True
            row["err"] = ""
        except Exception as e:                                  # noqa: BLE001
            row["err"] = f"{type(e).__name__}: {e}"[:70]
        rows.append(row)
        PROGRESO["hechas"] += 1
    return rows


FUENTES = ("programas", "spring", "es", "eu", "banca", "consultoria", "github")


def pasada(cfg, quiero=None):
    """Mira todas las fuentes pedidas y devuelve sus filas.

    `quiero` acota a un subconjunto de FUENTES (lo usa `--solo` para probar una
    sola sin esperar la pasada entera)."""
    global _FIN
    f = configurar(cfg)
    pide = [x for x in (quiero or cfg.get("fuentes") or FUENTES) if x in FUENTES]
    _FIN = time.time() + int(cfg.get("max_seconds", MAX_SECONDS))
    rows = []
    plan = [("es", DIRECT_ES, ES_CAT, "es"), ("eu", DIRECT_EU, EU_CAT, "eu"),
            ("banca", DIRECT_BANK, DIRECT_CAT, "bank"),
            ("consultoria", DIRECT_CONSULT, CONSULT_CAT, "consult")]
    PROGRESO.update(hechas=0, total=sum(len(specs) for clave, specs, _, _ in plan if clave in pide)
                    + (len(TABLEROS) if "github" in pide else 0))
    # Primero lo que no toca la red: si el techo de reloj salta a mitad, al menos
    # el catálogo no se queda del todo vacío.
    if "programas" in pide:
        r = _programa_rows(SUMMER_CAT, "Programas de verano · España y Europa",
                           "summer:programas", SUMMER_PROGRAMS)
        r["offers"], r["excl"] = _filtrar(r["offers"], f)
        rows.append(r)
    if "spring" in pide:
        r = _programa_rows(INSIGHT_CAT, "Spring weeks e insight programmes · Europa",
                           "insight:programas", INSIGHT_PROGRAMS)
        # verano=False: el filtro de temporada las tiraría todas, y son justo la
        # puerta de entrada a las prácticas de verano del año siguiente.
        r["offers"], r["excl"] = _filtrar(r["offers"], f, verano=False)
        rows.append(r)
    for clave, specs, cat, prefix in plan:
        if clave in pide:
            print(f"· {clave}: {len(specs)} fuentes…", flush=True)
            rows += _fuentes_directas(specs, cat, prefix, f)
    if "github" in pide:
        print(f"· github: {len(TABLEROS)} tableros…", flush=True)
        rows += _fuentes_github(f, cfg)
    _FIN = None
    return rows


def guardar(rows):
    """Escribe el catálogo. Única puerta: `db.upsert()`, que es idempotente.

    Cada empresa vigilada deja además una fila-marcador con el enlace a su web,
    para que exista en la app aunque hoy tenga la convocatoria cerrada o no
    exponga listado público."""
    conn = db.connect()
    nuevas = sum(db.upsert(conn, r["offers"], source=r["repo"], category=r["cat"],
                           board=r["name"]) for r in rows if r["offers"])
    for r in rows:
        if r.get("company"):
            db.upsert(conn, [{"key": f"link:{r['company']}", "company": r["company"],
                              "role": "Prácticas en su web", "url": r.get("url") or ""}],
                      source=r["repo"], category=r["cat"], board=r["name"], kind=db.LINK)
    total = db.stats(conn)["n"]
    conn.close()
    return nuevas, total


def informe(rows):
    ok = [r for r in rows if r["ok"]]
    fallos = [r for r in rows if r["err"]]
    n = sum(len(r["offers"]) for r in rows)
    print(f"\n{n} ofertas de {len(ok)}/{len(rows)} fuentes que respondieron.")
    for r in sorted(rows, key=lambda r: -len(r["offers"])):
        if r["offers"]:
            extra = "  (" + ", ".join(f"−{v} {k}" for k, v in r["excl"].items()) + ")" if r["excl"] else ""
            print(f"  {len(r['offers']):4d}  {r['name']}{extra}")
    if fallos:
        print(f"\n{len(fallos)} fuentes sin contestar (normal: un ATS caído, un WAF, un 429):")
        for r in fallos:
            print(f"        {r['name']}: {r['err']}")
    # Que fallen TODAS y todas por el certificado no es "un ATS caído": es este
    # Python, que no encuentra las raíces CA del sistema. Pasa recién instalado
    # el Python de python.org en macOS. Sin este aviso son 89 líneas de error y
    # ninguna que diga qué hacer.
    if not ok and any("CERTIFICATE_VERIFY" in (r["err"] or "") for r in fallos):
        print("\n→ Ninguna fuente ha contestado y todas se quejan del certificado:"
              "\n  este Python no confía en nadie todavía. Arréglalo con UNA de las dos:"
              "\n    open \"/Applications/Python 3.x/Install Certificates.command\""
              "\n    python3 -m pip install --user certifi")


# ---------- tests ----------

def _check():
    """Tests propios, sin red. `python3 ingesta.py --check`."""
    cfg = cargar_config()
    f = configurar(cfg)

    # El config de ejemplo tiene que traer TODO lo que la ingesta lee: es el
    # valor por defecto de quien acaba de clonar, no un adorno.
    ej = json.loads(EJEMPLO.read_text())
    for k in ("grad_year", "solo_verano", "solo_grado", "necesito_visado_us",
              "regiones", "fuentes", "github_solo_top", "max_seconds"):
        assert k in ej, f"config.example.json sin la clave {k}"
    assert set(ej["fuentes"]) <= set(FUENTES), "fuentes desconocidas en el ejemplo"
    assert set(ej["regiones"]) <= set(REGIONES), "regiones desconocidas en el ejemplo"

    # Año de graduación: solo tira lo que EXIGE graduarse antes.
    assert _grad_ok("Summer Analyst 2027", 2030)            # temporada != graduación
    assert not _grad_ok("Intern (Class of 2026)", 2030)
    assert _grad_ok("Intern (Class of 2026 or later)", 2030)
    assert _grad_ok("Intern (Class of 2026)", 2025)         # a otro le vale

    # Temporada y perfil.
    assert _summer_ok("Summer Internship") and not _summer_ok("Spring Intern")
    assert _summer_ok("Software Engineer Intern")           # sin señal, se conserva
    assert not _realistic({"role": "PhD Research Intern"})
    assert _realistic({"role": "PhD Research Intern"}, solo_grado=False)
    assert not _realistic({"role": "Intern", "uscit": True})
    assert _realistic({"role": "Intern", "uscit": True}, necesito_visado_us=False)

    # Regiones: la regex se compone con lo que se pida, y una errata no puede
    # vaciar el catálogo en silencio (casa con todo, y avisa).
    assert _compilar_loc(["es"]).search("Madrid, Spain")
    assert not _compilar_loc(["es"]).search("Warsaw, Poland")
    assert _compilar_loc(["pl"]).search("Warsaw, Poland")
    assert _compilar_loc(["noexiste"]).search("cualquier cosa")

    # Empresas top: el alias largo gana al corto (si no, "Citigroup" -> "citi").
    top = _compilar_top(["citi", "citigroup"])
    assert top.search("Citigroup Inc").group(1).lower() == "citigroup"
    assert not _compilar_top(["citi"]).search("Citizens Bank")
    assert not _compilar_top([]).search("Goldman Sachs")     # lista vacía: nada destaca

    # Parseo de un README de tablero.
    md = ("| Company | Role | Location |\n|---|---|---|\n"
          "| **Citadel** | [Software Intern](https://x.com/a) | London |\n"
          "| ↳ | Quant Intern 🛂 | New York |\n")
    offers = parse_offers(md)
    assert len(offers) == 2, offers
    assert offers[0]["company"] == "Citadel" and offers[0]["url"] == "https://x.com/a"
    assert offers[1]["company"] == "Citadel", "la fila ↳ hereda la empresa"
    assert offers[1]["nospon"] and not offers[0]["nospon"]
    assert offers[0]["key"] != offers[1]["key"]

    # La ubicación entra en la key: sin ella, "Software Intern" de Londres y el
    # de Nueva York llegan a la base indistinguibles y la dedup los fusiona.
    a = parse_offers("| X | Intern | London |")[0]
    b = parse_offers("| X | Intern | Berlin |")[0]
    assert a["key"] != b["key"]

    # Filtros de punta a punta.
    kept, excl = _filtrar([{"role": "Summer Intern"}, {"role": "Spring Intern"},
                           {"role": "Intern (Class of 2024)"}, {"role": "PhD Intern"}],
                          {"grad_year": 2030, "solo_verano": True,
                           "solo_grado": True, "necesito_visado_us": True})
    assert len(kept) == 1 and excl == {"graduación": 1, "temporada": 1, "perfil": 1}, (kept, excl)

    # Cada fuente declarada tiene rótulo y un ATS que sabemos leer.
    claves = {"gh", "lever", "ashby", "sr", "workable", "recruitee", "teamtailor",
              "sf", "avature", "wd", "amazon", "jpm", "link_only"}
    for lista in (DIRECT_ES, DIRECT_EU, DIRECT_BANK, DIRECT_CONSULT):
        for d in lista:
            assert d.get("company") and d.get("name"), d
            assert claves & set(d), f"{d['company']}: sin ATS conocido"
            assert d.get("url") or _direct_url(d), d          # siempre hay enlace

    # Las categorías las pinta la app leyéndolas de la base: si aquí se cambia
    # una y allí no, la pestaña de spring weeks se queda vacía sin dar error.
    app = (HERE / "app.py").read_text()
    for cat in (INSIGHT_CAT, US_CAT):
        assert cat in app, f"app.py ya no conoce la categoría {cat!r}"

    # El techo de reloj corta por donde pasa toda la red, no por scraper.
    global _FIN
    _FIN = time.time() - 1
    try:
        _exigir_tiempo()
        raise AssertionError("el techo de reloj no cortó")
    except SinTiempo:
        pass
    _FIN = None
    assert _queda_tiempo()

    n = len(DIRECT_ES) + len(DIRECT_EU) + len(DIRECT_BANK) + len(DIRECT_CONSULT)
    print(f"ingesta.py: todo bien ({n} fuentes directas, {len(TABLEROS)} tableros).")


def main(argv):
    if "--check" in argv:
        return _check()
    cfg = cargar_config()
    if "--fuentes" in argv:
        configurar(cfg)
        for clave, specs in (("es", DIRECT_ES), ("eu", DIRECT_EU),
                             ("banca", DIRECT_BANK), ("consultoria", DIRECT_CONSULT)):
            print(f"{clave:12} {len(specs):3d} fuentes")
            for d in specs:
                print(f"             · {d['name']}")
        print(f"{'github':12} {len(TABLEROS):3d} tableros")
        print(f"{'programas':12} {len(SUMMER_PROGRAMS):3d} programas de verano")
        print(f"{'spring':12} {len(INSIGHT_PROGRAMS):3d} spring weeks")
        return
    solo = None
    if "--solo" in argv:
        solo = argv[argv.index("--solo") + 1].split(",")
    t0 = time.time()
    rows = pasada(cfg, solo)
    informe(rows)
    if "--dry-run" in argv:
        print("\n--dry-run: no se ha escrito nada en jobs.db.")
        return
    try:
        nuevas, total = guardar(rows)
        print(f"\njobs.db: +{nuevas} nuevas, {total} en el catálogo"
              f" ({time.time() - t0:.0f}s).")
    except Exception as e:                                  # noqa: BLE001
        print(f"\njobs.db: NO se pudo guardar ({e}).")
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]) or 0)
