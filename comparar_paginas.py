#!/usr/bin/env python3
"""
Compara las páginas de una aplicación APEX entre TEST y PRD.
Usa las mismas variables de entorno que pase_apex.py (TEST_URL, TEST_USER, ...).

Uso:
  python comparar_paginas.py --app 100
  IDs distintos en cada ambiente:
  python comparar_paginas.py --app-test 158 --app-prd 100
Genera además comparacion_paginas_f<ID>.csv para abrir en Excel.
"""
import argparse
import csv
import os
import sys

import requests

SQL = """
select page_id, page_name, page_alias,
       to_char(last_updated_on, 'YYYY-MM-DD HH24:MI') as modificada,
       last_updated_by as por
  from apex_application_pages
 where application_id = :app_id
 order by page_id"""


def leer(env, app_id):
    url = os.environ[f"{env}_URL"].rstrip("/") + "/_/sql"
    auth = (os.environ[f"{env}_USER"], os.environ[f"{env}_PASS"])
    r = requests.post(url, auth=auth, timeout=120, json={
        "statementText": SQL,
        "binds": [{"name": "app_id", "data_type": "NUMBER", "value": app_id}]})
    r.raise_for_status()
    item = r.json()["items"][0]
    if "errorCode" in item:
        raise RuntimeError(f"[{env}] {item.get('errorDetails')}")
    return {int(f["page_id"]): f for f in item["resultSet"]["items"]}


def clave(f):
    # para reconocer la misma página con otro número: alias si tiene, si no el nombre
    return (f.get("page_alias") or f.get("page_name") or "").strip().upper()


def main():
    p = argparse.ArgumentParser(description="Compara páginas de una app entre TEST y PRD")
    p.add_argument("--app", type=int, help="Mismo ID en ambos ambientes")
    p.add_argument("--app-test", type=int, help="ID de la app en TEST")
    p.add_argument("--app-prd", type=int, help="ID de la app en PRD")
    a = p.parse_args()
    app_test, app_prd = a.app_test or a.app, a.app_prd or a.app
    if not app_test or not app_prd:
        sys.exit("Indica --app, o bien --app-test y --app-prd")

    test, prd = leer("TEST", app_test), leer("PRD", app_prd)
    if not test or not prd:
        sys.exit(f"No se encontraron páginas: TEST app {app_test} tiene {len(test)}, PRD app {app_prd} tiene {len(prd)}")
    print(f"Comparando TEST app {app_test} con PRD app {app_prd}\n")
    filas = []
    for n in sorted(set(test) | set(prd)):
        t, q = test.get(n), prd.get(n)
        if t and q:
            estado = "igual" if (t["page_name"] or "").strip() == (q["page_name"] or "").strip() else "NOMBRE DISTINTO"
        else:
            estado = "solo en TEST" if t else "solo en PRD"
        filas.append({
            "pagina": n, "estado": estado,
            "nombre_test": t["page_name"] if t else "", "nombre_prd": q["page_name"] if q else "",
            "modificada_test": t["modificada"] if t else "", "por_test": t["por"] if t else "",
            "modificada_prd": q["modificada"] if q else "", "por_prd": q["por"] if q else "",
        })

    print(f"{'Pág':>5}  {'Estado':<16} {'TEST':<35} PRD")
    for f in filas:
        if f["estado"] != "igual":
            print(f"{f['pagina']:>5}  {f['estado']:<16} {f['nombre_test'][:35]:<35} {f['nombre_prd']}")
    iguales = sum(f["estado"] == "igual" for f in filas)
    print(f"\n{iguales} páginas iguales, {len(filas) - iguales} con diferencias.")

    # misma página (por alias o nombre) con distinto número, solo entre las que no coinciden
    pendientes = {f["pagina"] for f in filas if f["estado"] != "igual"}
    por_clave_prd = {}
    for n, f in prd.items():
        if n in pendientes:
            por_clave_prd.setdefault(clave(f), []).append(n)
    candidatas = []
    for n, f in test.items():
        if n not in pendientes:
            continue
        for m in por_clave_prd.get(clave(f), []):
            if m != n and clave(f):
                candidatas.append((n, m, f["page_name"]))
    if candidatas:
        print("\nPosiblemente la misma página con otro número:")
        for n, m, nombre in candidatas:
            print(f"  TEST {n}  =  PRD {m}   ({nombre})")

    archivo = f"comparacion_paginas_test{app_test}_prd{app_prd}.csv"
    with open(archivo, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=list(filas[0].keys()) if filas else ["pagina"], delimiter=";")
        w.writeheader()
        w.writerows(filas)
    print(f"\nDetalle completo en {archivo}")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, requests.RequestException, KeyError) as e:
        sys.exit(f"ERROR: {e}")