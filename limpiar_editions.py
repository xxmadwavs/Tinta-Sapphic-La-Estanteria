#!/usr/bin/env python3
"""
limpiar_editions.py
--------------------
Arregla en un books_data.actualizado.json ya generado (con la version
anterior, con el bug) los enlaces de busqueda que anadio enrich_books.py:

  1. Quita el "lang": "ES" de los enlaces marcados como "(buscar)" -- ese
     campo indicaba (mal) el idioma de la edicion del libro, cuando en
     realidad solo era el idioma de la web de la tienda. Se sustituye por
     "busqueda": true.
  2. Elimina los enlaces de "Casa del Libro (buscar)", "Fnac (buscar)" y
     "Todostuslibros (buscar)" en los libros que NO tienen isbn (esas
     tiendas casi nunca indexan self-published sin ISBN, asi que el
     enlace no llevaba al libro). El de "Amazon (buscar)" se mantiene
     siempre.

No toca nada mas (year/pages/isbn/synopsis/etc. se quedan igual).

Uso:
    python3 limpiar_editions.py books_data.actualizado.json
Genera:
    books_data.actualizado.limpio.json
"""

import json
import sys
from pathlib import Path

SOLO_CON_ISBN = {"Casa del Libro (buscar)", "Fnac (buscar)", "Todostuslibros (buscar)"}


def main():
    if len(sys.argv) < 2:
        print("Uso: python3 limpiar_editions.py books_data.actualizado.json")
        sys.exit(1)

    path = Path(sys.argv[1])
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    quitados = 0
    lang_arreglado = 0

    for entry in data:
        eds = entry.get("editions") or []
        tiene_isbn = bool(entry.get("isbn"))
        nuevos = []
        for ed in eds:
            if not isinstance(ed, dict):
                nuevos.append(ed)
                continue
            es_busqueda = "(buscar)" in (ed.get("store") or "")
            if es_busqueda and ed.get("store") in SOLO_CON_ISBN and not tiene_isbn:
                quitados += 1
                continue
            if es_busqueda and ed.get("lang") == "ES":
                del ed["lang"]
                ed["busqueda"] = True
                lang_arreglado += 1
            nuevos.append(ed)
        entry["editions"] = nuevos

    out_path = path.with_name(path.stem + ".limpio.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"Enlaces con 'lang: ES' corregidos: {lang_arreglado}")
    print(f"Enlaces de Casa del Libro/Fnac/Todostuslibros quitados (sin ISBN): {quitados}")
    print(f"Listo: {out_path}")


if __name__ == "__main__":
    main()
