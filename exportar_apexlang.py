#!/usr/bin/env python3
"""
Crea en el destino los objetos de base de datos que existen en el origen y faltan en el destino
(tablas con sus índices, comentarios y FKs, secuencias, vistas, tipos, funciones, procedimientos,
paquetes, triggers y sinónimos). Usa las mismas variables de entorno que pase_apex.py.

Por defecto SOLO genera el script y muestra el resumen; no cambia nada.
  python sincronizar_objetos.py --schema MI_SCHEMA
Para ejecutarlo en PRD:
  python sincronizar_objetos.py --schema MI_SCHEMA --aplicar
Limitar a ciertos objetos:
  python sincronizar_objetos.py --schema MI_SCHEMA --solo CLIENTES,SEQ_CLIENTES,PKG_CLIENTES

No modifica objetos que ya existen: las columnas faltantes y el código distinto solo se informan.
"""
import argparse
import datetime
import os
import pathlib
import sys

import requests

# orden de creación: lo que otros necesitan va primero
ORDEN = ["TYPE", "SEQUENCE", "TABLE", "VIEW", "FUNCTION", "PROCEDURE", "PACKAGE", "TRIGGER", "SYNONYM"]

OBJETOS_SQL = """
select object_type, object_name from dba_objects o
 where owner = :owner
   and object_type in ('TYPE','SEQUENCE','TABLE','VIEW','FUNCTION','PROCEDURE','PACKAGE','TRIGGER','SYNONYM')
   and generated = 'N'
   and object_name not like 'BIN$%' and object_name not like 'SYS\\_%' escape '\\'
   and object_name not like 'ISEQ$$%' and object_name not like 'DR$%'
   and object_name not like 'MLOG$%' and object_name not like 'RUPD$%'
   and not (object_type = 'TABLE' and exists
            (select 1 from dba_mviews m where m.owner = o.owner and m.mview_name = o.object_name))"""

COLUMNAS_SQL = """
select table_name, column_name from dba_tab_columns c
 where owner = :owner
   and exists (select 1 from dba_tables t where t.owner = c.owner and t.table_name = c.table_name)"""

CODIGO_SQL = """
select type, name, count(*) || '-' || sum(ora_hash(text)) as huella from dba_source
 where owner = :owner and type in ('FUNCTION','PROCEDURE','PACKAGE','PACKAGE BODY','TRIGGER','TYPE','TYPE BODY')
 group by type, name"""

DDL_SQL = """
declare
  l_ddl clob;
  l_fk  clob;
begin
  dbms_metadata.set_transform_param(dbms_metadata.session_transform, 'SEGMENT_ATTRIBUTES', false);
  dbms_metadata.set_transform_param(dbms_metadata.session_transform, 'SQLTERMINATOR', true);
  dbms_metadata.set_transform_param(dbms_metadata.session_transform, 'PRETTY', true);
  if :tipo = 'TABLE' then
    -- las FK van al final, cuando ya existen todas las tablas nuevas
    dbms_metadata.set_transform_param(dbms_metadata.session_transform, 'REF_CONSTRAINTS', false);
  end if;
  l_ddl := dbms_metadata.get_ddl(:tipo, :nombre, :owner);
  if :tipo = 'TABLE' then
    for i in (select index_name from dba_indexes
               where table_owner = :owner and table_name = :nombre
                 and generated = 'N' and index_type <> 'LOB'
                 and index_name not in (select index_name from dba_constraints
                                         where owner = :owner and table_name = :nombre
                                           and index_name is not null)) loop
      l_ddl := l_ddl || chr(10) || dbms_metadata.get_ddl('INDEX', i.index_name, :owner);
    end loop;
    begin
      l_ddl := l_ddl || chr(10) || dbms_metadata.get_dependent_ddl('COMMENT', :nombre, :owner);
    exception when others then null;  -- la tabla no tiene comentarios
    end;
    begin
      l_fk := dbms_metadata.get_dependent_ddl('REF_CONSTRAINT', :nombre, :owner);
    exception when others then null;  -- la tabla no tiene FKs
    end;
  end if;
  :ddl := l_ddl;
  :fk  := l_fk;
end;"""


def _post(env, **kw):
    url = os.environ[f"{env}_URL"].rstrip("/") + "/_/sql"
    auth = (os.environ[f"{env}_USER"], os.environ[f"{env}_PASS"])
    r = requests.post(url, auth=auth, timeout=900, **kw)
    r.raise_for_status()
    return r.json()["items"]


def consulta(env, sql, binds):
    filas, offset = [], 0
    while True:
        item = _post(env, json={"statementText": sql, "binds": binds, "offset": offset, "limit": 5000})[0]
        if "errorCode" in item:
            raise RuntimeError(f"[{env}] {item.get('errorDetails')}")
        rs = item["resultSet"]
        filas += rs["items"]
        if not rs.get("hasMore"):
            return filas
        offset += len(rs["items"])


def ddl(env, owner, tipo, nombre):
    item = _post(env, json={"statementText": DDL_SQL, "binds": [
        {"name": "tipo", "data_type": "VARCHAR", "value": tipo},
        {"name": "nombre", "data_type": "VARCHAR", "value": nombre},
        {"name": "owner", "data_type": "VARCHAR", "value": owner},
        {"name": "ddl", "data_type": "CLOB", "mode": "out"},
        {"name": "fk", "data_type": "CLOB", "mode": "out"}]})[0]
    if "errorCode" in item:
        raise RuntimeError(f"[{env}] DDL de {tipo} {nombre}: {item.get('errorDetails')}")
    out = {b["name"]: b.get("result") for b in item.get("binds", [])}
    return (out.get("ddl") or "").strip(), (out.get("fk") or "").strip()


def ejecutar(env, script):
    items = _post(env, data=script.encode("utf-8"), headers={"Content-Type": "application/sql"})
    errores = 0
    for i in items:
        if "errorCode" not in i:
            continue
        sentencia = " ".join((i.get("statementText") or "").split())[:100]
        detalle = (i.get("errorDetails") or "").splitlines()[0]
        if i["errorCode"] == 24344:  # creado, pero con errores de compilación: se recompila al final
            print(f"    Aviso (compilación): {sentencia}")
        else:
            errores += 1
            print(f"    ERROR: {sentencia}\n           {detalle}")
    return len(items), errores


def main():
    p = argparse.ArgumentParser(description="Crea en el destino los objetos que faltan")
    p.add_argument("--schema", required=True, help="Schema a comparar (mismo nombre en ambos ambientes)")
    p.add_argument("--desde", choices=["TEST", "PRD"], default="TEST")
    p.add_argument("--hacia", choices=["TEST", "PRD"], default="PRD")
    p.add_argument("--solo", help="Limitar a estos objetos, separados por coma")
    p.add_argument("--aplicar", action="store_true", help="Ejecutar el script en el destino")
    a = p.parse_args()
    if a.desde == a.hacia:
        sys.exit("--desde y --hacia deben ser distintos")
    owner = a.schema.upper()
    b = [{"name": "owner", "data_type": "VARCHAR", "value": owner}]

    print(f"Leyendo objetos de {owner} en {a.desde} y {a.hacia}...")
    org = {(f["object_type"], f["object_name"]) for f in consulta(a.desde, OBJETOS_SQL, b)}
    dst = {(f["object_type"], f["object_name"]) for f in consulta(a.hacia, OBJETOS_SQL, b)}
    faltan = org - dst
    if a.solo:
        solo = {x.strip().upper() for x in a.solo.split(",")}
        faltan = {o for o in faltan if o[1] in solo}
    faltan = sorted(faltan, key=lambda o: (ORDEN.index(o[0]), o[1]))

    # informativo: diferencias en objetos que existen en ambos lados
    col_o = {(f["table_name"], f["column_name"]) for f in consulta(a.desde, COLUMNAS_SQL, b)}
    col_d = {(f["table_name"], f["column_name"]) for f in consulta(a.hacia, COLUMNAS_SQL, b)}
    tablas_d = {n for t, n in dst if t == "TABLE"}
    col_faltan = sorted((t, c) for t, c in col_o - col_d if t in tablas_d)
    cod_o = {(f["type"], f["name"]): f["huella"] for f in consulta(a.desde, CODIGO_SQL, b)}
    cod_d = {(f["type"], f["name"]): f["huella"] for f in consulta(a.hacia, CODIGO_SQL, b)}
    cod_dif = sorted(k for k in cod_o.keys() & cod_d.keys() if cod_o[k] != cod_d[k])

    if col_faltan:
        print(f"\nColumnas que existen en {a.desde} y faltan en {a.hacia} (no se crean, revisar a mano):")
        for t, c in col_faltan:
            print(f"  {t}.{c}")
    if cod_dif:
        print(f"\nCódigo distinto entre ambientes (no se toca; se pasa en su propio script):")
        for t, n in cod_dif:
            print(f"  {t} {n}")
    if not faltan:
        print(f"\nNo faltan objetos en {a.hacia}.")
        return

    print(f"\nObjetos que faltan en {a.hacia}:")
    partes, fks = [], []
    for tipo, nombre in faltan:
        print(f"  {tipo:<10} {nombre}")
        d, fk = ddl(a.desde, owner, tipo, nombre)
        partes.append(f"-- {tipo} {nombre}\n{d}\n")
        if fk:
            fks.append(f"-- FKs de {nombre}\n{fk}\n")
    # el código de triggers, vistas y paquetes suele nombrar tablas sin schema
    script = f"alter session set current_schema = {owner};\n\n" + "\n".join(partes + fks)

    carpeta = pathlib.Path("pases")
    carpeta.mkdir(exist_ok=True)
    archivo = carpeta / f"{datetime.datetime.now():%Y%m%d_%H%M%S}_objetos_{a.desde}_a_{a.hacia}.sql"
    archivo.write_text(script, encoding="utf-8")
    print(f"\nScript generado: {archivo}")

    if not a.aplicar:
        print("Revísalo. Para ejecutarlo, repite con --aplicar.")
        return
    if input(f"Escribe {a.hacia} para crear {len(faltan)} objetos en {a.hacia}: ") != a.hacia:
        sys.exit("Cancelado.")
    total, errores = ejecutar(a.hacia, script)
    print(f"\n{total} sentencias ejecutadas, {errores} con error.")
    if errores:
        sys.exit("Revisa los errores. Lo que se creó antes del error ya quedó aplicado.")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, requests.RequestException, KeyError) as e:
        sys.exit(f"ERROR: {e}")