"""Prueba en vivo qué slugs de Greenhouse / Lever / Ashby existen y cuántas
prácticas tienen en TUS regiones (las de `config.json`, vía `ingesta`).
Solo lectura de APIs públicas."""
import json
import sys
from concurrent.futures import ThreadPoolExecutor

import urllib.request

# Las mismas regexes que usa la ingesta, no una copia: si aquí se filtrara
# distinto, un tablero podría salir "con becas" y luego no aportar ninguna.
# `configurar()` recompila `_LOC` con las regiones del config del usuario, así
# que quien busque en Nueva York no descubre tableros por sus becas de Madrid.
import ingesta
from ingesta import _HDR, _CTX

ingesta.configurar(ingesta.cargar_config())

GH = [  # tech + fintech europeo con buena marca
    "spotify", "klarna", "n26", "wise", "monzo", "checkoutcom", "adyen", "glovo",
    "typeform", "factorialhr", "personio", "getyourguide", "deliveryhero", "deepl",
    "mollie", "miro", "bolt", "vinted", "deliveroo", "criteo", "contentsquare",
    "starlingbank", "zopa", "trainline", "depop", "gocardless", "tide", "curve",
    "onfido", "elevenlabs", "synthesia", "pigment", "swile", "ledger", "younited",
    "backmarket", "sorare", "alan", "qonto", "payfit", "dataiku", "datadog",
    "stripe", "databricks", "flowtraders", "gresearch", "marshallwace", "xtxmarkets",
    "balyasnyassetmanagement", "schonfeld", "millenniumpartners", "verition",
    "quantlab", "maven", "ebury", "seedtag", "jobandtalent", "cabify", "wallbox",
    "cover", "paack", "travelperk", "redpoints", "signaturit", "holded",
]
LEVER = [
    "klarna", "glovo", "typeform", "n26", "wise", "monzo", "vinted", "bolt",
    "personio", "getyourguide", "qonto", "alan", "swile", "ledger", "sorare",
    "backmarket", "voodoo", "believe", "younited", "ovhcloud", "criteo",
    "travelperk", "jobandtalent", "factorial", "mistral", "huggingface",
]
ASHBY = [
    "mistral", "elevenlabs", "synthesia", "traderepublic", "qonto", "alan",
    "pennylane", "pigment", "lovable", "n8n", "helsing", "quantco", "tacto",
    "parloa", "blackshark", "isar-aerospace", "the-exploration-company",
    "poolside", "photoroom", "dust", "linear", "ramp", "granola", "harvey",
    "legora", "juniper", "cera", "multiverse", "cleo", "zilch", "griffin",
]


def probe(ats, slug):
    try:
        if ats == "gh":
            u = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
        elif ats == "lever":
            u = f"https://api.lever.co/v0/postings/{slug}?mode=json"
        else:
            u = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
        req = urllib.request.Request(u, headers=_HDR)
        with urllib.request.urlopen(req, timeout=20, context=_CTX) as r:
            d = json.load(r)
    except Exception as e:                                   # noqa: BLE001
        return (ats, slug, None, 0, 0, str(e)[:28])

    if ats == "gh":
        items = [(j.get("title", ""), (j.get("location") or {}).get("name", ""))
                 for j in d.get("jobs", [])]
    elif ats == "lever":
        items = [(j.get("text", ""), (j.get("categories") or {}).get("location", ""))
                 for j in (d if isinstance(d, list) else [])]
    else:
        items = [(j.get("title", ""), j.get("location", ""))
                 for j in (d.get("jobs", []) if isinstance(d, dict) else [])]

    eu = [(t, l) for t, l in items if ingesta._LOC.search(l or "")]
    interns = [(t, l) for t, l in eu if ingesta._TITULO_BECA.search(t or "")]
    return (ats, slug, len(items), len(eu), len(interns),
            "; ".join(f"{t} · {l}" for t, l in interns[:2])[:90])


jobs = [("gh", s) for s in GH] + [("lever", s) for s in LEVER] + [("ashby", s) for s in ASHBY]
with ThreadPoolExecutor(max_workers=8) as ex:
    res = list(ex.map(lambda a: probe(*a), jobs))

live = [r for r in res if r[2]]
live.sort(key=lambda r: (-r[4], -r[3]))
print(f"{'ATS':<6} {'slug':<26} {'todo':>5} {'zona':>5} {'becas':>6}  ejemplo")
for ats, slug, tot, eu, it, ex_ in live:
    print(f"{ats:<6} {slug:<26} {tot:>5} {eu:>5} {it:>6}  {ex_}")
print(f"\nvivos: {len(live)}/{len(res)}  ·  con becas: {sum(1 for r in live if r[4])}")

# --save: deja los tableros CON becas en `descubiertos.json`, en la carpeta de datos,
# que es de donde ingesta.py los lee al arrancar para sumarlos a sus fuentes
# directas. Así la lista de empresas crece sin editar código: el cron semanal
# vuelve a probar los mismos slugs y recoge los que HOY dan 0 becas pero abren
# convocatoria más adelante (las de verano se abren cada primavera).
# Ojo: el pool de candidatos sigue siendo estas 3 listas de arriba. Crece con
# el calendario, no con empresas nuevas: para eso hay que añadir slugs a mano.
if "--save" in sys.argv:
    out = [{"company": slug.replace("-", " ").title(), ats: slug,
            "name": f"{slug.replace('-', ' ').title()} (descubierto por probe)"}
           for ats, slug, _tot, _eu, interns, _ex in live if interns]
    # Una pasada en la que NO ha contestado nadie (sin red, o este Python sin
    # certificados) no es "ya no hay tableros con becas": escribir [] encima
    # borraría en silencio los que había, y la ingesta de esta noche se quedaría
    # sin ellos. Ante la duda, se conserva lo anterior.
    if not live:
        print("→ no ha contestado ningún tablero: no toco los ficheros de antes.")
        raise SystemExit(1)
    blob = json.dumps(out, ensure_ascii=False, indent=1)
    ingesta.DESCUBIERTOS.write_text(blob)
    print(f"→ {len(out)} tableros con becas guardados en {ingesta.DESCUBIERTOS}")
