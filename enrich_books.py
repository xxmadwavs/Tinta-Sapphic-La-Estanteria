#!/usr/bin/env python3
"""
enrich_books.py
----------------
Rellena campos vacios de books_data.json usando Google Books y Open Library
(las dos unicas fuentes gratuitas con API publica real que existen para
libros), y genera enlaces de BUSQUEDA en Amazon.es y Casa del Libro para
que sea facil encontrar donde comprar cada titulo. Tambien genera un
archivo aparte con sugerencias de sinopsis en espanol y generos en ingles
para revisar a mano.

IMPORTANTE - por que no se usa Goodreads ni romance.io directamente:
Goodreads cerro su API publica para claves nuevas en 2020 y romance.io
nunca ha tenido una API publica. Extraer datos de esas paginas requeriria
"scrapear" el HTML, lo cual en un runner de GitHub Actions (IP compartida)
suele toparse con bloqueos anti-bot (Cloudflare, PerimeterX) y ademas es
fragil (se rompe cada vez que cambian el diseno de la web) y legalmente
gris (va contra sus terminos de uso). Por eso este script usa Google Books
y Open Library, que son gratuitas, oficiales y estables, y genera enlaces
de BUSQUEDA (no de producto exacto) para Amazon y Casa del Libro, que es
lo mas fiable que se puede automatizar sin arriesgarse a poner un enlace
roto o incorrecto.

Uso:
    python3 enrich_books.py books_data.json

Genera:
    books_data.actualizado.json   -> listo para "Importar JSON" en el admin
    sugerencias_revisar.json      -> sinopsis en espanol y generos en ingles
                                      encontrados, para revisar a mano
    enrich_log.txt                -> log de que se encontro/no se encontro
                                      por titulo, incluyendo el motivo del
                                      fallo

No requiere librerias externas (solo stdlib: urllib, json, time, difflib).
"""

import json
import sys
import time
import urllib.request
import urllib.parse
import urllib.error
from difflib import SequenceMatcher

GOOGLE_BOOKS_BASE = "https://www.googleapis.com/books/v1/volumes"
OPENLIBRARY_BASE = "https://openlibrary.org/search.json"

USER_AGENT = "tinta-sapphic-enrich-script/1.0"

# Google Books es bastante mas permisivo que Goodreads/romance.io, pero
# igualmente conviene ir despacio para no saturar una IP compartida de
# GitHub Actions.
GBOOKS_DELAY = 1.0
OPENLIB_DELAY = 1.0

MAX_RETRIES_429 = 2
BACKOFF_SECONDS = [4, 10]

MATCH_THRESHOLD = 0.5

STATS = {
    "google_books": {"ok": 0, "sin_match": 0, "error": {}},
    "open_library": {"ok": 0, "sin_match": 0, "error": {}},
}


def log(msg, logfile):
    print(msg)
    logfile.write(msg + "\n")


def similar(a, b):
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def _record_error(source, reason):
    STATS[source]["error"][reason] = STATS[source]["error"].get(reason, 0) + 1


def http_get_json(url, source=None):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    attempt = 0
    while True:
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode("utf-8")), None
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < MAX_RETRIES_429:
                time.sleep(BACKOFF_SECONDS[attempt])
                attempt += 1
                continue
            reason = f"http_{e.code}"
            if source:
                _record_error(source, reason)
            return None, reason
        except Exception as e:
            reason = f"conn_error:{type(e).__name__}"
            if source:
                _record_error(source, reason)
            return None, reason


# ---------------------------------------------------------------------------
# Google Books
# ---------------------------------------------------------------------------

def _gbooks_query(title, author, lang_restrict=None):
    q_parts = [f'intitle:"{title}"']
    if author:
        q_parts.append(f'inauthor:"{author}"')
    q = " ".join(q_parts)
    params = {"q": q, "maxResults": 5}
    if lang_restrict:
        params["langRestrict"] = lang_restrict
    url = f"{GOOGLE_BOOKS_BASE}?{urllib.parse.urlencode(params)}"
    data, err = http_get_json(url, source="google_books")
    time.sleep(GBOOKS_DELAY)
    return data, err


def query_google_books(title, authors):
    author = authors[0] if authors else None

    # Primero intentamos solo en espanol (para tener sinopsis en espanol
    # si existe una edicion traducida). Si no hay nada, probamos sin
    # restriccion de idioma para poder rellenar autor/anio/isbn/paginas
    # aunque la sinopsis salga en ingles (esa se descarta mas abajo, no
    # se traduce automaticamente).
    candidates = []
    for lang in ("es", None):
        data, err = _gbooks_query(title, author, lang)
        if err:
            continue
        items = (data or {}).get("items") or []
        for it in items:
            vi = it.get("volumeInfo", {}) or {}
            score = similar(title, vi.get("title", ""))
            candidates.append((score, vi))

    if not candidates:
        STATS["google_books"]["sin_match"] += 1
        return None, "sin_resultados"

    candidates.sort(key=lambda c: c[0], reverse=True)
    best_score, best = candidates[0]
    if best_score < MATCH_THRESHOLD:
        STATS["google_books"]["sin_match"] += 1
        return None, "sin_match_confiable"

    STATS["google_books"]["ok"] += 1

    isbn13, isbn10 = None, None
    for ident in best.get("industryIdentifiers", []) or []:
        if ident.get("type") == "ISBN_13":
            isbn13 = ident.get("identifier")
        elif ident.get("type") == "ISBN_10":
            isbn10 = ident.get("identifier")

    anio = None
    pub = best.get("publishedDate")
    if pub:
        try:
            anio = int(pub[:4])
        except ValueError:
            anio = None

    # Sinopsis: solo se usa si la edicion encontrada esta en espanol.
    # Si esta en ingles, se descarta a proposito (mejor no tener sinopsis
    # sugerida que sugerir una en el idioma equivocado sin traducir).
    synopsis_es = None
    if (best.get("language") or "").lower() == "es" and best.get("description"):
        synopsis_es = best["description"]

    return {
        "match_score": best_score,
        "autores": best.get("authors") or [],
        "anio": anio,
        "paginas": best.get("pageCount"),
        "isbn": isbn13 or isbn10,
        "categorias_en": best.get("categories") or [],
        "synopsis_es": synopsis_es,
        "idioma_encontrado": best.get("language"),
    }, None


# ---------------------------------------------------------------------------
# Open Library (fallback para autor/anio/isbn/paginas)
# ---------------------------------------------------------------------------

def query_open_library(title, authors):
    author = authors[0] if authors else None
    q = title if not author else f"{title} {author}"
    params = {"q": q, "limit": 5}
    url = f"{OPENLIBRARY_BASE}?{urllib.parse.urlencode(params)}"
    data, err = http_get_json(url, source="open_library")
    time.sleep(OPENLIB_DELAY)
    if err:
        return None, err
    docs = (data or {}).get("docs") or []
    if not docs:
        STATS["open_library"]["sin_match"] += 1
        return None, "sin_resultados"

    best_doc, best_score = None, 0
    for d in docs:
        score = similar(title, d.get("title", ""))
        if score > best_score:
            best_score, best_doc = score, d

    if not best_doc or best_score < MATCH_THRESHOLD:
        STATS["open_library"]["sin_match"] += 1
        return None, "sin_match_confiable"

    STATS["open_library"]["ok"] += 1
    isbns = best_doc.get("isbn") or []
    return {
        "match_score": best_score,
        "autores": best_doc.get("author_name") or [],
        "anio": best_doc.get("first_publish_year"),
        "paginas": best_doc.get("number_of_pages_median"),
        "isbn": isbns[0] if isbns else None,
    }, None


# ---------------------------------------------------------------------------
# Enlaces de compra (busqueda, no producto exacto garantizado)
# ---------------------------------------------------------------------------

def gen_link_id():
    import random
    import string
    base = format(int(time.time() * 1000), "x")
    rand = "".join(random.choices(string.ascii_lowercase + string.digits, k=5))
    return f"bk_{base}{rand}"


def build_amazon_search_url(isbn, title, author):
    query = isbn if isbn else f"{title} {author or ''}".strip()
    return f"https://www.amazon.es/s?k={urllib.parse.quote(query)}"


def build_casadellibro_search_url(isbn, title, author):
    query = isbn if isbn else f"{title} {author or ''}".strip()
    params = {"busqueda": query, "nivel": 5, "auto": 0, "maxresultados": 5}
    return f"https://www.casadellibro.com/busqueda-generica?{urllib.parse.urlencode(params)}"


# ---------------------------------------------------------------------------
# Fusion de resultados y aplicacion a la entrada
# ---------------------------------------------------------------------------

def is_empty(v):
    return v is None or v == "" or v == [] or v is False


def first_truthy(*vals):
    for v in vals:
        if v:
            return v
    return None


def enrich_entry(entry, logfile):
    titulo = entry["title"]
    authors = entry.get("authors") or []
    changes = {}
    suggestions = {}

    gb, gb_err = query_google_books(titulo, authors)
    ol, ol_err = query_open_library(titulo, authors)

    if not gb and not ol:
        log(f"[SIN DATOS] {titulo}  (google_books={gb_err}, open_library={ol_err})", logfile)
    else:
        log(f"[OK] {titulo}  (google_books={'si' if gb else gb_err}, open_library={'si' if ol else ol_err})", logfile)

    # --- year ---
    if is_empty(entry.get("year")):
        anio = first_truthy((gb or {}).get("anio"), (ol or {}).get("anio"))
        if anio:
            changes["year"] = anio

    # --- pages ---
    if is_empty(entry.get("pages")):
        paginas = first_truthy((gb or {}).get("paginas"), (ol or {}).get("paginas"))
        if paginas:
            changes["pages"] = paginas

    # --- isbn ---
    isbn_encontrado = first_truthy((gb or {}).get("isbn"), (ol or {}).get("isbn"))
    if is_empty(entry.get("isbn")) and isbn_encontrado:
        changes["isbn"] = isbn_encontrado

    # --- enlaces de compra: se anaden SIEMPRE que no exista ya un enlace
    #     de esa tienda (no se duplican en reejecuciones), fusionando con
    #     lo que ya tenga la entrada ---
    existentes = entry.get("editions") or []
    tiendas_existentes = {e.get("store") for e in existentes if isinstance(e, dict)}
    autor0 = authors[0] if authors else None
    isbn_para_link = isbn_encontrado or entry.get("isbn")

    nuevos_enlaces = []
    if "Amazon" not in tiendas_existentes:
        nuevos_enlaces.append({
            "lang": "ES",
            "store": "Amazon (buscar)",
            "url": build_amazon_search_url(isbn_para_link, titulo, autor0),
        })
    if "Casa del Libro" not in tiendas_existentes and "Casa del Libro (buscar)" not in tiendas_existentes:
        nuevos_enlaces.append({
            "lang": "ES",
            "store": "Casa del Libro (buscar)",
            "url": build_casadellibro_search_url(isbn_para_link, titulo, autor0),
        })

    if nuevos_enlaces:
        for n in nuevos_enlaces:
            n["id"] = gen_link_id()
        changes["editions"] = existentes + nuevos_enlaces

    # --- SUGERENCIAS (nunca se escriben directo, siempre a revisar) ---
    # Sinopsis en espanol encontrada en Google Books (solo si el idioma de
    # la edicion encontrada es 'es'; si solo hay en ingles, se descarta
    # a proposito en vez de traducirla automaticamente).
    if (gb or {}).get("synopsis_es"):
        suggestions["synopsis_es_encontrada"] = gb["synopsis_es"]
        suggestions["synopsis_actual"] = entry.get("synopsis")

    categorias_en = (gb or {}).get("categorias_en") or []
    if categorias_en:
        suggestions["categorias_fuente_en"] = categorias_en

    if suggestions:
        suggestions["titulo"] = titulo
        suggestions["id"] = entry["id"]

    return changes, suggestions


def main():
    if len(sys.argv) < 2:
        print("Uso: python3 enrich_books.py books_data.json")
        sys.exit(1)

    path = sys.argv[1]
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    logfile = open("enrich_log.txt", "w", encoding="utf-8")
    all_suggestions = []
    updated = 0

    for i, entry in enumerate(data):
        changes, suggestions = enrich_entry(entry, logfile)
        if changes:
            entry.update(changes)
            updated += 1
        if suggestions:
            all_suggestions.append(suggestions)
        if (i + 1) % 20 == 0:
            print(f"... {i + 1}/{len(data)} procesados")

    with open("books_data.actualizado.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    with open("sugerencias_revisar.json", "w", encoding="utf-8") as f:
        json.dump(all_suggestions, f, ensure_ascii=False, indent=2)

    resumen = ["", "===== RESUMEN POR FUENTE ====="]
    for src, s in STATS.items():
        resumen.append(f"{src}: {s['ok']} encontrados, {s['sin_match']} sin coincidencia, errores={s['error']}")
    resumen.append("===============================")
    for line in resumen:
        log(line, logfile)

    logfile.close()
    print(f"\nListo. {updated} entradas con campos/enlaces anadidos.")
    print(f"{len(all_suggestions)} entradas con sugerencias para revisar en sugerencias_revisar.json")
    print("Revisa el RESUMEN POR FUENTE al final de enrich_log.txt si algo sigue sin funcionar:")
    print("  - muchos 'http_429' = Google Books/Open Library estan limitando al runner")
    print("  - muchos 'conn_error' = problema de red/DNS en el runner")
    print("  - muchos 'sin_resultados'/'sin_match_confiable' = titulos que esas fuentes no tienen (normal en autopublicados)")


if __name__ == "__main__":
    main()
