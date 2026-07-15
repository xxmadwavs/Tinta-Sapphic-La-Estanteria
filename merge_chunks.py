#!/usr/bin/env python3
"""
merge_chunks.py
----------------
Fusiona los resultados de todos los chunks generados por enrich_books.py
(ejecutados en paralelo, uno por job de GitHub Actions) sobre el
books_data.json original, y concatena las sugerencias y los logs.

Uso:
    python3 merge_chunks.py books_data.json /ruta/a/artifacts_descargados

Donde /ruta/a/artifacts_descargados es una carpeta que contiene, en
cualquier subcarpeta (busca de forma recursiva), archivos con estos
patrones:
    books_data.cambios.chunk*.json
    sugerencias_revisar.chunk*.json
    enrich_log.chunk*.txt

Genera en el directorio actual:
    books_data.actualizado.json
    sugerencias_revisar.json
    enrich_log.txt
"""

import json
import sys
from pathlib import Path


def main():
    if len(sys.argv) < 3:
        print("Uso: python3 merge_chunks.py books_data.json /ruta/a/artifacts_descargados")
        sys.exit(1)

    json_path = Path(sys.argv[1])
    artifacts_dir = Path(sys.argv[2])

    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)

    by_id = {entry["id"]: entry for entry in data}

    cambios_files = sorted(artifacts_dir.rglob("books_data.cambios.chunk*.json"))
    sugerencias_files = sorted(artifacts_dir.rglob("sugerencias_revisar.chunk*.json"))
    log_files = sorted(artifacts_dir.rglob("enrich_log.chunk*.txt"))

    if not cambios_files:
        print("AVISO: no se ha encontrado ningun archivo books_data.cambios.chunk*.json "
              f"dentro de {artifacts_dir}. Revisa que has descargado y descomprimido "
              "todos los artifacts de los jobs 'enrich' antes de ejecutar esto.")

    total_updated = 0
    for cf in cambios_files:
        with open(cf, encoding="utf-8") as f:
            cambios_chunk = json.load(f)
        for item in cambios_chunk:
            entry = by_id.get(item["id"])
            if entry is None:
                print(f"AVISO: id {item['id']} de {cf.name} no existe en {json_path.name}, se ignora.")
                continue
            entry.update(item["changes"])
            total_updated += 1

    all_suggestions = []
    for sf in sugerencias_files:
        with open(sf, encoding="utf-8") as f:
            all_suggestions.extend(json.load(f))

    with open("books_data.actualizado.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    with open("sugerencias_revisar.json", "w", encoding="utf-8") as f:
        json.dump(all_suggestions, f, ensure_ascii=False, indent=2)

    with open("enrich_log.txt", "w", encoding="utf-8") as out:
        for lf in log_files:
            out.write(f"\n===== {lf.name} =====\n")
            out.write(lf.read_text(encoding="utf-8"))

    print(f"Fusionados {len(cambios_files)} chunks de cambios ({total_updated} entradas actualizadas en total).")
    print(f"Fusionadas {len(sugerencias_files)} listas de sugerencias ({len(all_suggestions)} entradas en total).")
    print(f"Fusionados {len(log_files)} logs en enrich_log.txt.")
    print("Listo: books_data.actualizado.json, sugerencias_revisar.json, enrich_log.txt")


if __name__ == "__main__":
    main()
