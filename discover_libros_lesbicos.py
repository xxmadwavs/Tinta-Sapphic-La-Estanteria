#!/usr/bin/env python3
"""
discover_libros_lesbicos.py
-----------------------------
Recorre Open Library (por subject: lesbian_fiction, lesbian_romance, lesbian,
lgbtq_romance) y Google Books (por queries subject:"Lesbian Fiction" /
"Lesbian Romance" / etc) buscando libros sapphic/lesbicos, fusiona los
resultados de ambas fuentes en una sola ficha por libro, descarta
automaticamente lo que ya esta en tu books_data.json (comparando titulo +
autores), y deja el resto en un JSON de "candidatos nuevos" listo para
revisar con revisar_altas_nuevas_libros.html.

No inventa nada ni decide por ti: genres, subgenres, identidades,
characters, dinamicas, tropes, content_warnings, edad, pais_cultura,
formato, spicy, quedan vacios (se rellenan en la pagina de revision). Solo
rellena automaticamente los campos "seguros" (titulo, autores, anio,
portada, sinopsis en el idioma de origen, paginas, isbn si esta disponible,
enlaces de la fuente) y SIEMPRE los deja marcados como candidatos, nunca
como altas definitivas.

Uso:
    python3 discover_libros_lesbicos.py books_data.json \
        [--max-pages-openlibrary 20] [--max-pages-googlebooks 20]

Genera:
    discoveries_libros_lesbicos.json  -> candidatos nuevos, para revisar_altas_nuevas_libros.html
    discover_log.txt                  -> resumen de lo recorrido y descartado

No requiere librerias externas (solo stdlib). No requiere API key en
ninguna de las 2 fuentes.
"""

import argparse
import json
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from difflib import SequenceMatcher

OPENLIBRARY_SUBJECTS_BASE = "https://openlibrary.org/subjects"
GOOGLE_BOOKS_BASE = "https://www.googleapis.com/books/v1/volumes"

USER_AGENT = "sapphic-books-discover-script/1.0"

OL_DELAY = 0.6
GB_DELAY = 1.0

MAX_RETRIES_429 = 3
BACKOFF_SECONDS = [4, 10, 20]

DUP_THRESHOLD = 0.85      # umbral para decidir "ya lo tienes en catalogo"
CLUSTER_THRESHOLD = 0.85  # umbral para fusionar el mismo libro entre fuentes

# Subjects de Open Library a recorrer (slug -> etiqueta legible que se guarda
# como genero_fuente para la pagina de revision).
OL_SUBJECTS = [
    ("lesbian_fiction", "Lesbian Fiction"),
    ("lesbian_romance", "Lesbian Romance"),
    ("lesbian", "Lesbian"),
    ("lgbtq_romance", "LGBTQ Romance"),
]
OL_PAGE_SIZE = 50

# Queries de busqueda de Google Books (todas dentro del genero, no por titulo).
GB_QUERIES = [
    'subject:"Lesbian Fiction"',
    'subject:"Lesbian Romance"',
    'subject:"Fiction / Lesbian"',
    'subject:"Sapphic Romance"',
]
GB_PAGE_SIZE = 40  # maximo permitido por la API

STATS = {
    "openlibrary": {"vistos": 0, "errores": {}},
    "googlebooks": {"vistos": 0, "errores": {}},
}


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------

def log(msg, logfile):
    print(msg)
    logfile.write(msg + "\n")


def similar(a, b):
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def normalize(s):
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return s.strip()


def bucket_key(norm):
    if not norm:
        return ""
    return norm.split(" ", 1)[0][:4]


def gen_temp_id():
    import random
    import string
    base = format(int(time.time() * 1000), "x")
    rand = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    return f"cand_{base}{rand}"


def http_get_json(url, headers=None, source=None):
    req = urllib.request.Request(url, headers=headers or {"User-Agent": USER_AGENT})
    attempt = 0
    while True:
        try:
            with urllib.request.urlopen(req, timeout=25) as resp:
                return json.loads(resp.read().decode("utf-8")), None
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < MAX_RETRIES_429:
                time.sleep(BACKOFF_SECONDS[attempt])
                attempt += 1
                continue
            reason = f"http_{e.code}"
            if source:
                STATS[source]["errores"][reason] = STATS[source]["errores"].get(reason, 0) + 1
            return None, reason
        except Exception as e:
            reason = f"conn_error:{type(e).__name__}"
            if source:
                STATS[source]["errores"][reason] = STATS[source]["errores"].get(reason, 0) + 1
            return None, reason


# ---------------------------------------------------------------------------
# Indice del catalogo existente (para descartar duplicados)
# ---------------------------------------------------------------------------

def build_catalog_index(catalog):
    """bucket -> lista de (titulo_normalizado, titulo_original, entry)"""
    index = {}
    for entry in catalog:
        titles = [entry.get("title")]
        saga = entry.get("sagaName")
        if saga:
            titles.append(saga)
        for t in titles:
            if not t:
                continue
            norm = normalize(t)
            if not norm:
                continue
            index.setdefault(bucket_key(norm), []).append((norm, t, entry))
    return index


def is_in_catalog(candidate_titles, catalog_index):
    """Devuelve (True, titulo_catalogo, score) si el candidato ya esta en el catalogo."""
    best_title, best_score = None, 0
    for ct in candidate_titles:
        if not ct:
            continue
        norm_c = normalize(ct)
        if not norm_c:
            continue
        bucket = catalog_index.get(bucket_key(norm_c), [])
        for norm_e, orig_e, _entry in bucket:
            if norm_c == norm_e:
                return True, orig_e, 1.0
            score = similar(norm_c, norm_e)
            if score > best_score:
                best_score, best_title = score, orig_e
    if best_score >= DUP_THRESHOLD:
        return True, best_title, best_score
    return False, best_title, best_score


# ---------------------------------------------------------------------------
# Clustering entre fuentes (mismo libro visto en Open Library / Google Books)
# ---------------------------------------------------------------------------

class Cluster:
    __slots__ = ("titles_norm", "openlibrary", "googlebooks")

    def __init__(self):
        self.titles_norm = set()
        self.openlibrary = None
        self.googlebooks = None


def merge_into_clusters(clusters, cluster_index, source_name, raw, titles):
    """Busca un cluster existente por similitud de titulo; si no hay, crea uno."""
    norm_titles = [normalize(t) for t in titles if t]
    norm_titles = [t for t in norm_titles if t]
    if not norm_titles:
        return

    found = None
    for nt in norm_titles:
        bucket = cluster_index.get(bucket_key(nt), [])
        for cluster in bucket:
            if any(nt == existing or similar(nt, existing) >= CLUSTER_THRESHOLD for existing in cluster.titles_norm):
                found = cluster
                break
        if found:
            break

    if found is None:
        found = Cluster()
        clusters.append(found)

    found.titles_norm.update(norm_titles)
    setattr(found, source_name, raw)

    for nt in norm_titles:
        cluster_index.setdefault(bucket_key(nt), [])
        if found not in cluster_index[bucket_key(nt)]:
            cluster_index[bucket_key(nt)].append(found)


# ---------------------------------------------------------------------------
# Open Library: pagina por subject (lesbian_fiction, lesbian_romance, ...)
# ---------------------------------------------------------------------------

def fetch_openlibrary_subject(slug, label, max_pages, logfile):
    results = []
    offset = 0
    for page in range(max_pages):
        q = urllib.parse.urlencode({"limit": OL_PAGE_SIZE, "offset": offset, "details": "false"})
        url = f"{OPENLIBRARY_SUBJECTS_BASE}/{slug}.json?{q}"
        data, err = http_get_json(url, source="openlibrary")
        time.sleep(OL_DELAY)
        if err or not data:
            log(f"[OpenLibrary:{slug}] pagina offset={offset}: error {err}", logfile)
            break
        works = data.get("works", []) or []
        if page == 0 and not works and not (data.get("work_count") or 0):
            log(f"[OpenLibrary] subject '{slug}' no encontrado o vacio, se omite.", logfile)
            break
        STATS["openlibrary"]["vistos"] += len(works)
        for w in works:
            title = w.get("title")
            if not title:
                continue
            autores = [a.get("name") for a in (w.get("authors") or []) if a.get("name")]
            cover_id = w.get("cover_id")
            imagen = f"https://covers.openlibrary.org/b/id/{cover_id}-L.jpg" if cover_id else None
            key = w.get("key")
            results.append({
                "titulo_principal": title,
                "titulos": [title],
                "autores": autores,
                "anio": w.get("first_publish_year"),
                "imagen": imagen,
                "desc": None,
                "paginas": None,
                "isbn": None,
                "idioma": None,
                "generos": [label],
                "url": f"https://openlibrary.org{key}" if key else None,
                "id": key,
            })
        log(f"[OpenLibrary:{slug}] offset {offset} ok ({len(works)} obras, total fuente: {data.get('work_count')})", logfile)
        offset += OL_PAGE_SIZE
        if offset >= (data.get("work_count") or 0) or not works:
            break
    return results


def fetch_openlibrary_gl(max_pages, logfile):
    all_results = []
    for slug, label in OL_SUBJECTS:
        all_results.extend(fetch_openlibrary_subject(slug, label, max_pages, logfile))
    return all_results


# ---------------------------------------------------------------------------
# Google Books: pagina por query de subject
# ---------------------------------------------------------------------------

def fetch_googlebooks_query(query, max_pages, logfile):
    results = []
    for page in range(max_pages):
        start_index = page * GB_PAGE_SIZE
        q = urllib.parse.urlencode({
            "q": query, "startIndex": start_index, "maxResults": GB_PAGE_SIZE, "printType": "books",
        })
        url = f"{GOOGLE_BOOKS_BASE}?{q}"
        data, err = http_get_json(url, source="googlebooks")
        time.sleep(GB_DELAY)
        if err or not data:
            log(f"[GoogleBooks:{query}] pagina startIndex={start_index}: error {err}", logfile)
            break
        items = data.get("items", []) or []
        if page == 0 and not items:
            log(f"[GoogleBooks] query {query} no devolvio resultados, se omite.", logfile)
            break
        STATS["googlebooks"]["vistos"] += len(items)
        for it in items:
            info = it.get("volumeInfo", {}) or {}
            title = info.get("title")
            if not title:
                continue
            subtitle = info.get("subtitle")
            titulo_completo = f"{title}: {subtitle}" if subtitle else title
            anio = None
            pub_date = info.get("publishedDate")
            if pub_date and len(pub_date) >= 4 and pub_date[:4].isdigit():
                anio = int(pub_date[:4])
            isbn = None
            for ident in info.get("industryIdentifiers", []) or []:
                if ident.get("type") in ("ISBN_13", "ISBN_10"):
                    isbn = ident.get("identifier")
                    if ident.get("type") == "ISBN_13":
                        break
            imagen = ((info.get("imageLinks") or {}).get("thumbnail") or "").replace("http://", "https://")
            results.append({
                "titulo_principal": title,
                "titulos": [title, titulo_completo] if subtitle else [title],
                "autores": info.get("authors") or [],
                "anio": anio,
                "imagen": imagen or None,
                "desc": info.get("description"),
                "paginas": info.get("pageCount"),
                "isbn": isbn,
                "idioma": info.get("language"),
                "generos": info.get("categories") or [],
                "maturity": info.get("maturityRating"),
                "url": info.get("infoLink") or info.get("previewLink"),
                "id": it.get("id"),
            })
        log(f"[GoogleBooks:{query}] startIndex {start_index} ok ({len(items)} obras, total fuente: {data.get('totalItems')})", logfile)
        if start_index + GB_PAGE_SIZE >= (data.get("totalItems") or 0) or not items:
            break
    return results


def fetch_googlebooks_gl(max_pages, logfile):
    all_results = []
    for query in GB_QUERIES:
        all_results.extend(fetch_googlebooks_query(query, max_pages, logfile))
    return all_results


# ---------------------------------------------------------------------------
# Fusion final por cluster -> ficha candidata
# ---------------------------------------------------------------------------

def first_truthy(*vals):
    for v in vals:
        if v:
            return v
    return None


def build_candidate(cluster):
    o, g = cluster.openlibrary, cluster.googlebooks

    titulo = first_truthy((g or {}).get("titulo_principal"), (o or {}).get("titulo_principal"))
    titulos_todos = list(dict.fromkeys(
        ((o or {}).get("titulos") or []) + ((g or {}).get("titulos") or [])
    ))
    titulos_alt = [t for t in titulos_todos if t and t != titulo][:6]

    autores = first_truthy((g or {}).get("autores"), (o or {}).get("autores")) or []
    anio = first_truthy((g or {}).get("anio"), (o or {}).get("anio"))
    imagen = first_truthy((g or {}).get("imagen"), (o or {}).get("imagen"))
    desc = first_truthy((g or {}).get("desc"), (o or {}).get("desc"))
    paginas = first_truthy((g or {}).get("paginas"), (o or {}).get("paginas"))
    isbn = first_truthy((g or {}).get("isbn"), (o or {}).get("isbn"))
    idioma = first_truthy((g or {}).get("idioma"), (o or {}).get("idioma"))

    generos_fuente_en = list(dict.fromkeys(
        ((g or {}).get("generos") or []) + ((o or {}).get("generos") or [])
    ))

    maturity_sugerida = (g or {}).get("maturity") if (g or {}).get("maturity") not in (None, "NOT_MATURE") else None

    fuentes = {}
    if o:
        fuentes["openlibrary"] = {"id": o.get("id"), "url": o.get("url")}
    if g:
        fuentes["googlebooks"] = {"id": g.get("id"), "url": g.get("url")}

    editions = []
    if (g or {}).get("url"):
        editions.append({"lang": (idioma or "").upper() or None, "store": "Google Books", "url": g["url"]})
    if (o or {}).get("url"):
        editions.append({"lang": None, "store": "Open Library", "url": o["url"]})

    return {
        "id": gen_temp_id(),
        "title": titulo,
        "titulos_alt": titulos_alt,
        "authors": autores,
        "year": anio,
        "cover": imagen or "",
        "synopsis": desc or "",
        "desc_idioma_fuente": (idioma or "en/desconocido") + " (sin traducir, revisar antes de publicar)",
        "pages": paginas,
        "isbn": isbn,
        "ship": [],
        "genres": [],
        "subgenres": [],
        "identidades": [],
        "characters": [],
        "dinamicas": [],
        "tropes": [],
        "edad": None,
        "pais_cultura": [],
        "ambientacion": [],
        "content_warnings": [],
        "spicy": None,
        "formato": None,
        "adaptacion": None,
        "sagaName": None,
        "sagaNum": None,
        "source": None,
        "editions": editions,
        "saficlub": False,
        "safi_categories": [],
        "canal_rec": False,
        "canal_month": None,
        "my_rec": False,
        "my_rec_categories": [],
        "generos_fuente_en": generos_fuente_en,
        "maturity_sugerida": maturity_sugerida,
        "fuentes": fuentes,
    }


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("books_data_path")
    parser.add_argument("--max-pages-openlibrary", type=int, default=20)
    parser.add_argument("--max-pages-googlebooks", type=int, default=20)
    args = parser.parse_args()

    with open(args.books_data_path, encoding="utf-8") as f:
        catalog = json.load(f)

    logfile = open("discover_log.txt", "w", encoding="utf-8")
    log(f"Catalogo actual: {len(catalog)} entradas.", logfile)
    catalog_index = build_catalog_index(catalog)

    log("\n=== Recorriendo Open Library (subjects lesbian/sapphic) ===", logfile)
    ol_items = fetch_openlibrary_gl(args.max_pages_openlibrary, logfile)

    log("\n=== Recorriendo Google Books (subject:Lesbian ...) ===", logfile)
    gb_items = fetch_googlebooks_gl(args.max_pages_googlebooks, logfile)

    log(f"\nTotal obras vistas -> OpenLibrary: {len(ol_items)}, GoogleBooks: {len(gb_items)}", logfile)

    # --- Fusionar por titulo entre las 2 fuentes ---
    clusters = []
    cluster_index = {}
    for it in ol_items:
        merge_into_clusters(clusters, cluster_index, "openlibrary", it, it["titulos"])
    for it in gb_items:
        merge_into_clusters(clusters, cluster_index, "googlebooks", it, it["titulos"])

    log(f"\nObras unicas tras fusionar fuentes: {len(clusters)}", logfile)

    # --- Descartar lo que ya esta en el catalogo ---
    nuevos = []
    ya_en_catalogo = 0
    for cluster in clusters:
        candidate = build_candidate(cluster)
        titles_to_check = [candidate["title"]] + candidate["titulos_alt"]
        dup, matched_title, score = is_in_catalog(titles_to_check, catalog_index)
        if dup:
            ya_en_catalogo += 1
            continue
        nuevos.append(candidate)

    log(f"Ya estaban en tu catalogo (descartados): {ya_en_catalogo}", logfile)
    log(f"Candidatos NUEVOS para revisar: {len(nuevos)}", logfile)

    with open("discoveries_libros_lesbicos.json", "w", encoding="utf-8") as f:
        json.dump(nuevos, f, ensure_ascii=False, indent=2)

    resumen = ["", "===== RESUMEN POR FUENTE ====="]
    for src, s in STATS.items():
        resumen.append(f"{src}: {s['vistos']} obras vistas, errores={s['errores']}")
    resumen.append("===============================")
    for line in resumen:
        log(line, logfile)

    logfile.close()
    print(f"\nListo. {len(nuevos)} candidatos nuevos en discoveries_libros_lesbicos.json")
    print(f"({ya_en_catalogo} descartados por ya estar en tu catalogo)")
    print("Revisa discover_log.txt para ver el detalle por fuente.")


if __name__ == "__main__":
    main()
