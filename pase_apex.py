#!/usr/bin/env python3
"""
Pase de una aplicación APEX de TEST a PRD usando ORDS REST Enabled SQL (HTTPS).

Requisitos:  pip install requests

Variables de entorno (una vez por máquina, no en el código):
  TEST_URL  = https://<host-test>/ords/<alias_schema>
  TEST_USER / TEST_PASS   usuario y clave del schema en TEST
  PRD_URL   = https://<host-prd>/ords/<alias_schema>
  PRD_USER  / PRD_PASS    usuario y clave del schema en PRD
  Git (opcional): un repositorio por app en repos_git.json, junto a este script:
                {"101": "C:/repos/app-101", "100": "C:/repos/app-100"}
                (también se acepta APEX_GIT_REPO_<ID> o APEX_GIT_REPO para un único repo).
                Si la app tiene repositorio, cada pase a PRD
                se guarda en <repo>/apex/f<ID>.sql con un commit

Uso:
  python pase_apex.py --app 100 --workspace MI_WS --schema MI_SCHEMA
  python pase_apex.py --app 100 --workspace MI_WS --schema MI_SCHEMA --sup-obj --yes
  Prueba inversa, como copia con otro ID para no pisar la app de TEST:
  python pase_apex.py --desde PRD --hacia TEST --app 100 --app-destino 9100 --workspace MI_WS --schema MI_SCHEMA
  Prueba solo en TEST (copia la app dentro del mismo ambiente):
  python pase_apex.py --desde TEST --hacia TEST --app 100 --app-destino 9100 --workspace MI_WS --schema MI_SCHEMA
  Con descripción para el commit y push al remoto:
  python pase_apex.py --app 100 --workspace MI_WS --schema MI_SCHEMA --mensaje "Nuevo reporte" --push
  Ejecutar antes scripts de base de datos en el destino (en el orden indicado):
  python pase_apex.py --app 100 --paginas 10 --sql 001_tablas.sql --sql 002_paquete.sql --workspace MI_WS --schema MI_SCHEMA
  Antes de instalar, detecta objetos de BD que usa el código y faltan en el destino, y pregunta
  si crearlos (requiere sincronizar_objetos.py en la misma carpeta; --sin-objetos lo omite).
  Misma aplicación con distinto ID en cada ambiente (reemplaza la 101 de PRD con la 166 de TEST,
  conservando el alias de la 101 y las suscripciones):
  python pase_apex.py --app 166 --app-destino 101 --reemplazar --workspace MI_WS --schema MI_SCHEMA
  Solo algunas páginas (reemplaza esas páginas en la app existente con el mismo ID):
  python pase_apex.py --app 100 --paginas 10,20 --workspace MI_WS --schema MI_SCHEMA
"""
import argparse
import datetime
import json
import os
import pathlib
import re
import subprocess
import time
import sys

import requests

try:
    import sincronizar_objetos as so  # debe estar en la misma carpeta
except ImportError:
    so = None

TIEMPO_RESPALDO = 180  # segundos; si el respaldo tarda más, se pregunta si continuar sin él
TIEMPO_EXPORT = 600    # segundos máximos esperando que el job de exportación termine
TIEMPO_INSTALAR = 300  # segundos; si no responde, se verifica en el destino si la instalación se aplicó

EXPORT_SQL = """
declare
  l_files apex_t_export_files;
begin
  if :comps is null then
    l_files := apex_export.get_application(p_application_id        => :app_id,
                                           p_with_no_subscriptions => :sin_subs = 'Y');
  else
    l_files := apex_export.get_application(p_application_id        => :app_id,
                                           p_components            => apex_string.split(:comps, ','),
                                           p_with_no_subscriptions => :sin_subs = 'Y');
  end if;
  :contents := l_files(1).contents;
end;"""

INSTALL_SQL = """
begin
  -- fija la identidad de workspace en la sesión (necesario al conectarse como ADMIN)
  apex_util.set_workspace(p_workspace => :workspace);
  apex_application_install.set_workspace(:workspace);
  apex_application_install.set_application_id(:app_dest);
  apex_application_install.set_schema(:schema);
  if :offset = 'Y' then
    -- IDs internos nuevos para no chocar con otra app de la misma instancia
    apex_application_install.generate_offset;
  end if;
  if :alias is not null then
    apex_application_install.set_application_alias(:alias);
  end if;
  apex_application_install.set_auto_install_sup_obj(p_auto_install_sup_obj => :sup_obj = 'Y');
  apex_application_install.install(
    p_source => apex_t_export_files(
                  apex_t_export_file(name => 'f' || :app_dest || '.sql', contents => :contents)),
    p_overwrite_existing => true);
end;"""


def run(env, sql, binds, timeout=900):
    url = os.environ[f"{env}_URL"].rstrip("/") + "/_/sql"
    auth = (os.environ[f"{env}_USER"], os.environ[f"{env}_PASS"])
    r = requests.post(url, auth=auth, json={"statementText": sql, "binds": binds}, timeout=timeout)
    r.raise_for_status()
    item = r.json()["items"][0]
    if "errorCode" in item:
        detalle = item.get("errorDetails") or item.get("errorMessage") or json.dumps(item)[:2000]
        raise RuntimeError(f"[{env}] error {item['errorCode']}: {detalle}")
    return {b["name"]: b.get("result") for b in item.get("binds", [])}


def run_script(env, texto):
    """Ejecuta un script SQL (varias sentencias; bloques PL/SQL terminados en /)."""
    url = os.environ[f"{env}_URL"].rstrip("/") + "/_/sql"
    auth = (os.environ[f"{env}_USER"], os.environ[f"{env}_PASS"])
    r = requests.post(url, auth=auth, data=texto.encode("utf-8"), timeout=900,
                      headers={"Content-Type": "application/sql"})
    r.raise_for_status()
    items = r.json()["items"]
    errores = [i for i in items if "errorCode" in i]
    for i in errores:
        sentencia = " ".join((i.get("statementText") or "").split())[:120]
        print(f"      ERROR en: {sentencia}\n      {(i.get('errorDetails') or '').splitlines()[0]}")
    return len(items), len(errores)


def apps_en(env, ids):
    """{id: alias} de las aplicaciones indicadas que existen en el ambiente."""
    sql = ("select application_id, alias from apex_applications where application_id in "
           "(select to_number(column_value) from table(apex_string.split(:ids, ',')))")
    url = os.environ[f"{env}_URL"].rstrip("/") + "/_/sql"
    auth = (os.environ[f"{env}_USER"], os.environ[f"{env}_PASS"])
    r = requests.post(url, auth=auth, timeout=120, json={"statementText": sql, "binds": [
        {"name": "ids", "data_type": "VARCHAR", "value": ",".join(str(i) for i in ids)}]})
    r.raise_for_status()
    item = r.json()["items"][0]
    if "errorCode" in item:
        raise RuntimeError(f"[{env}] {item.get('errorDetails')}")
    return {int(f["application_id"]): f["alias"] for f in item["resultSet"]["items"]}


COMPS_SQL = """
select type_name, id, name from apex_appl_export_comps
 where application_id = :app_id and type_name <> 'PAGE'"""


def componentes_faltantes(a, dest, texto):
    """Componentes compartidos que el export usa y no existen en la app de destino."""
    def leer(env, app):
        return so.consulta(env, COMPS_SQL, [{"name": "app_id", "data_type": "NUMBER", "value": app}])
    destino = leer(a.hacia, dest)
    en_destino = {(f["type_name"], str(f["id"])) for f in destino}
    id_por_nombre = {(f["type_name"], (f.get("name") or "").upper()): str(f["id"]) for f in destino}
    t = texto.upper()
    # se recorren el export una sola vez: números (IDs) y textos entre comillas
    numeros = set(re.findall(r"\d+", texto))
    citados = set(re.findall(r"'([^']*)'", t))
    usados = []
    for f in leer(a.desde, a.app):
        clave = (f["type_name"], str(f["id"]))
        if clave in en_destino:
            continue
        nombre = (f.get("name") or "").upper()
        # las referencias van por ID interno; las LOV con nombre también pueden ir por nombre
        if clave[1] in numeros or (nombre and nombre in citados):
            f["id_destino"] = id_por_nombre.get((f["type_name"], nombre))
            f["mismo_nombre"] = f["id_destino"] is not None
            usados.append(f)
    return usados


def mapear_ids(texto, mapeo):
    """Reemplaza referencias wwv_flow_imp.id(<id origen>) por el ID del componente equivalente en el destino."""
    return re.sub(r"wwv_flow_imp\.id\((\d+)\)",
                  lambda m: f"wwv_flow_imp.id({mapeo.get(m.group(1), m.group(1))})", texto)


ENTRADAS_SQL = "select * from apex_application_list_entries where application_id = :app_id"


def pagina_de(target):
    m = re.match(r"f\?p=[^:]*:(\d+)(:|$)", target or "")
    return int(m.group(1)) if m else None


def _plsql(v):
    return "null" if v in (None, "") else "'" + str(v).replace("'", "''") + "'"


AUTH_SQL = ("select authorization_scheme_id, authorization_scheme_name from apex_application_authorization "
            "where application_id = :app_id")


def mapear_autorizacion(a, dest, e):
    """Devuelve el valor de p_security_scheme para el destino, o None si no se puede resolver."""
    crudo = str(e.get("authorization_scheme_id") or "").strip()
    nombre = (e.get("authorization_scheme") or "").strip()
    if not crudo and not nombre:
        return ""  # sin autorización
    neg = crudo.startswith("!")
    num = crudo.lstrip("!")
    if crudo and not num.isdigit():
        return _plsql(crudo)  # valores especiales, p. ej. MUST_NOT_BE_PUBLIC_USER
    def leer(env, app):
        return so.consulta(env, AUTH_SQL, [{"name": "app_id", "data_type": "NUMBER", "value": app}])
    org = {str(x["authorization_scheme_id"]): x["authorization_scheme_name"] for x in leer(a.desde, a.app)}
    dst = {x["authorization_scheme_name"].upper(): str(x["authorization_scheme_id"]) for x in leer(a.hacia, dest)}
    nombre_org = org.get(num) or nombre
    id_dst = dst.get((nombre_org or "").upper())
    if not id_dst:
        return None
    return ("'!'||" if neg else "") + f"wwv_flow_imp.id({id_dst})"


def revisar_menu(a, dest, paginas_pasadas):
    """Entradas de menú de las páginas pasadas que faltan en el destino.
    Devuelve bloques create_list_item para agregar al export (solo los que el usuario acepta).
    No reemplaza la lista: agrega la entrada a la lista que ya existe en el destino."""
    def leer(env, app):
        return so.consulta(env, ENTRADAS_SQL, [{"name": "app_id", "data_type": "NUMBER", "value": app}])
    try:
        origen, destino = leer(a.desde, a.app), leer(a.hacia, dest)
    except (RuntimeError, requests.RequestException, KeyError) as e:
        print(f"    Aviso: no pude revisar las entradas de menú ({e}).")
        return []
    por_id_org = {e["list_entry_id"]: e for e in origen}
    bloques = []
    for e in origen:
        p = pagina_de(e.get("entry_target"))
        if p not in paginas_pasadas:
            continue
        lista = e["list_name"]
        lista_dst = [x for x in destino if x["list_name"] == lista]
        if any(pagina_de(x.get("entry_target")) == p for x in lista_dst):
            continue  # ya existe
        print(f"    Diferencia: la entrada de menú '{e['entry_text']}' (lista '{lista}') no existe en {a.hacia}.")

        motivo = None
        if not lista_dst:
            motivo = f"la lista '{lista}' no existe o está vacía en {a.hacia}"
        elif e.get("build_option"):
            motivo = f"tiene build option ({e.get('build_option')})"
        elif e.get("condition_type_code") or e.get("condition_type"):
            motivo = f"tiene condición de visualización ({e.get('condition_type')})"
        seguridad = ""
        if not motivo:
            try:
                seguridad = mapear_autorizacion(a, dest, e)
            except (RuntimeError, requests.RequestException, KeyError) as err:
                seguridad = None
                print(f"      Aviso: no pude revisar la autorización ({err}).")
            if seguridad is None:
                motivo = (f"usa la autorización '{e.get('authorization_scheme')}', "
                          f"que no encuentro en {a.hacia} con el mismo nombre")
        padre_dst = None
        padre_org = por_id_org.get(e.get("list_entry_parent_id"))
        if not motivo and padre_org:
            cand = [x for x in lista_dst if x["entry_text"] == padre_org["entry_text"]]
            if len(cand) != 1:
                motivo = f"no encuentro en {a.hacia} la entrada padre '{padre_org['entry_text']}'"
            else:
                padre_dst = cand[0]["list_entry_id"]
        if motivo:
            print(f"      No se crea automáticamente: {motivo}. Créala a mano en {a.hacia}:")
            print(f"      Texto '{e['entry_text']}', página {p}, ícono {e.get('entry_image') or '-'}, "
                  f"secuencia {e.get('display_sequence')}"
                  + (f", padre '{padre_org['entry_text']}'" if padre_org else ""))
            continue

        r = "s" if a.yes else input(f"    ¿Crearla en la lista '{lista}' de {a.hacia} junto con la página? (s/n): ").strip().lower()
        if r != "s":
            print("      Omitida.")
            continue
        params = [
            f" p_id=>wwv_flow_imp.id({e['list_entry_id']})",
            f",p_list_id=>wwv_flow_imp.id({lista_dst[0]['list_id']})",
            f",p_list_item_display_sequence=>{e.get('display_sequence') or 10}",
            f",p_list_item_link_text=>{_plsql(e.get('entry_text'))}",
            *([f",p_static_id=>{_plsql(e['static_id'])}"] if e.get("static_id") else []),
            f",p_list_item_link_target=>{_plsql(e.get('entry_target'))}",
        ]
        if e.get("entry_image"):
            params.append(f",p_list_item_icon=>{_plsql(e['entry_image'])}")
        if padre_dst:
            # el parámetro que usan los exports de APEX es p_parent_list_item_id (p_list_item_parent_id se ignora)
            params.append(f",p_parent_list_item_id=>wwv_flow_imp.id({padre_dst})")
        if seguridad:
            params.append(f",p_security_scheme=>{seguridad}")
            print(f"      Con la autorización '{e.get('authorization_scheme')}' de {a.hacia}.")
        tipo_actual = e.get("current_for_pages_type_code")
        if tipo_actual:
            params.append(f",p_list_item_current_type=>{_plsql(tipo_actual)}")
            if e.get("current_for_pages_expression"):
                params.append(f",p_list_item_current_for_pages=>{_plsql(e['current_for_pages_expression'])}")
        bloques.append(f"prompt --application/shared_components/navigation/lists/entrada_{p}\n"
                       "begin\nwwv_flow_imp_shared.create_list_item(\n" + "\n".join(params) + "\n);\nend;\n/\n")
        print(f"      Se agregará a la lista '{lista}' de {a.hacia}.")
    return bloques


BC_SQL = "select * from apex_application_bc_entries where application_id = :app_id"


def revisar_breadcrumb(a, dest, paginas_pasadas):
    """Entradas de breadcrumb de las páginas pasadas que faltan en el destino.
    Devuelve bloques create_menu_option para agregar al export (solo los que el usuario acepta)."""
    def leer(env, sql, app):
        return so.consulta(env, sql, [{"name": "app_id", "data_type": "NUMBER", "value": app}])
    try:
        org, dst = leer(a.desde, BC_SQL, a.app), leer(a.hacia, BC_SQL, dest)
        # breadcrumb equivalente en el destino, por nombre
        nom_org = {str(f["id"]): f["name"] for f in leer(a.desde, COMPS_SQL, a.app) if f["type_name"] == "BREADCRUMB"}
        id_dst = {f["name"].upper(): str(f["id"]) for f in leer(a.hacia, COMPS_SQL, dest) if f["type_name"] == "BREADCRUMB"}
    except (RuntimeError, requests.RequestException, KeyError) as e:
        print(f"    Aviso: no pude revisar el breadcrumb ({e}).")
        return []
    por_id_org = {e["breadcrumb_entry_id"]: e for e in org}
    ids_dst = {e["breadcrumb_entry_id"] for e in dst}
    bloques = []
    for e in org:
        p = e.get("defined_for_page")
        if p not in paginas_pasadas:
            continue
        nombre_bc = nom_org.get(str(e["breadcrumb_id"]), "")
        bc_dst = id_dst.get(nombre_bc.upper())
        en_bc_dst = [x for x in dst if str(x["breadcrumb_id"]) == bc_dst]
        if bc_dst and any(x.get("defined_for_page") == p for x in en_bc_dst):
            continue  # ya existe
        print(f"    Diferencia: la entrada de breadcrumb '{e['entry_label']}' (página {p}) no existe en {a.hacia}.")

        motivo, padre_dst = None, None
        padre_org = por_id_org.get(e.get("parent_breadcrumb_id"))
        if not bc_dst:
            motivo = f"no encuentro en {a.hacia} el breadcrumb '{nombre_bc}'"
        elif e.get("build_option"):
            motivo = f"tiene build option ({e.get('build_option')})"
        elif e.get("condition_type_code") or e.get("condition_type"):
            motivo = f"tiene condición de visualización ({e.get('condition_type')})"
        elif e["breadcrumb_entry_id"] in ids_dst:
            motivo = f"su ID interno ya está usado en {a.hacia}"
        elif padre_org:
            # el padre se busca por página y, si no tiene, por etiqueta
            cand = [x for x in en_bc_dst if padre_org.get("defined_for_page") is not None
                    and x.get("defined_for_page") == padre_org.get("defined_for_page")]
            if not cand:
                cand = [x for x in en_bc_dst if x["entry_label"] == padre_org["entry_label"]]
            if len(cand) != 1:
                motivo = f"no encuentro en {a.hacia} la entrada padre '{padre_org['entry_label']}'"
            else:
                padre_dst = cand[0]["breadcrumb_entry_id"]
        seguridad = ""
        if not motivo:
            try:
                seguridad = mapear_autorizacion(a, dest, e)
            except (RuntimeError, requests.RequestException, KeyError):
                seguridad = None
            if seguridad is None:
                motivo = f"usa la autorización '{e.get('authorization_scheme')}', que no encuentro en {a.hacia}"
        if motivo:
            print(f"      No se crea automáticamente: {motivo}. Créala a mano en {a.hacia} "
                  f"(Shared Components > Breadcrumbs > '{nombre_bc}'):")
            print(f"      Página {p}, etiqueta '{e['entry_label']}'"
                  + (f", padre '{padre_org['entry_label']}'" if padre_org else ", sin padre"))
            continue

        r = "s" if a.yes else input(f"    ¿Crearla en el breadcrumb '{nombre_bc}' de {a.hacia} junto con la página? (s/n): ").strip().lower()
        if r != "s":
            print("      Omitida.")
            continue
        params = [f" p_id=>wwv_flow_imp.id({e['breadcrumb_entry_id']})"]
        if padre_dst:
            params.append(f",p_parent_id=>wwv_flow_imp.id({padre_dst})")
        params.append(f",p_menu_id=>wwv_flow_imp.id({bc_dst})")
        params.append(f",p_short_name=>{_plsql(e.get('entry_label'))}")
        if e.get("static_id") and not any(x.get("static_id") == e["static_id"] for x in en_bc_dst):
            params.append(f",p_static_id=>{_plsql(e['static_id'])}")
        if e.get("entry_long_label"):
            params.append(f",p_long_name=>{_plsql(e['entry_long_label'])}")
        if e.get("url"):
            params.append(f",p_link=>{_plsql(e['url'])}")
        params.append(f",p_page_id=>{int(p)}")
        if e.get("current_for_pages"):
            params.append(f",p_also_current_for_pages=>{_plsql(e['current_for_pages'])}")
        if seguridad:
            params.append(f",p_security_scheme=>{seguridad}")
        bloques.append(f"prompt --application/shared_components/navigation/breadcrumbs/entrada_{p}\n"
                       "begin\nwwv_flow_imp_shared.create_menu_option(\n" + "\n".join(params) + "\n);\nend;\n/\n")
        print(f"      Se agregará al breadcrumb '{nombre_bc}' de {a.hacia}"
              + (f", bajo '{padre_org['entry_label']}'." if padre_org else "."))
    return bloques


def agregar_bloques(texto, bloques):
    marca = "prompt --application/end_environment"
    i = texto.rfind(marca)
    if i < 0:
        raise RuntimeError("no encuentro el cierre del export para agregar la entrada de menú")
    return texto[:i] + "".join(bloques) + texto[i:]


DEPS_SQL = """
select referenced_type as object_type, referenced_name as object_name from dba_dependencies
 where owner = :owner and referenced_owner = :owner
   and name in (select column_value from table(apex_string.split(:lista, ',')))
union
select type, name from dba_dependencies
 where owner = :owner and referenced_owner = :owner and type = 'TRIGGER'
   and referenced_name in (select column_value from table(apex_string.split(:lista, ',')))"""


def objetos_faltantes(a, texto):
    """Objetos del schema que el código exportado usa (directa o indirectamente) y faltan en el destino."""
    owner = a.schema.upper()
    b = [{"name": "owner", "data_type": "VARCHAR", "value": owner}]
    org = {(f["object_type"], f["object_name"]) for f in so.consulta(a.desde, so.OBJETOS_SQL, b)}
    dst = {(f["object_type"], f["object_name"]) for f in so.consulta(a.hacia, so.OBJETOS_SQL, b)}
    faltan = org - dst
    if not faltan:
        return []
    t = texto.upper()
    # objetos que el código nombra, existan o no en el destino
    # se extraen una sola vez todas las palabras del export (mismos límites que un identificador Oracle)
    palabras = set(re.findall(r"[A-Z0-9_$#]+", t))
    referenciados = {o for o in org if o[1] in palabras}
    usados = referenciados & faltan
    nuevos = referenciados  # primera vuelta: también los existentes, para hallar triggers faltantes
    while nuevos:  # agrega lo que esos objetos necesitan (p. ej. la tabla de una vista) y sus triggers
        lista = ",".join(n for _, n in nuevos)
        deps = so.consulta(a.desde, DEPS_SQL, b + [{"name": "lista", "data_type": "VARCHAR", "value": lista}])
        nuevos = ({(d["object_type"], d["object_name"]) for d in deps} & faltan) - usados
        usados |= nuevos
    return sorted(usados, key=lambda o: (so.ORDEN.index(o[0]), o[1]))


def preparar_objetos(a, texto, carpeta):
    faltan = objetos_faltantes(a, texto)
    if not faltan:
        print("    Todos los objetos de base de datos que usa existen en el destino.")
        return None
    elegidos = []
    for tipo, nombre in faltan:
        print(f"    Diferencia: {tipo} {nombre} existe en {a.desde} pero no en {a.hacia}.")
        r = "s" if a.yes else input(f"    ¿Crearlo en {a.hacia} antes de instalar? (s/n): ").strip().lower()
        if r == "s":
            elegidos.append((tipo, nombre))
        else:
            print("      Omitido: si la página lo usa, fallará al ejecutarse en el destino.")
    if not elegidos:
        return None
    partes, fks = [], []
    for tipo, nombre in elegidos:
        d, fk = so.ddl(a.desde, a.schema.upper(), tipo, nombre)
        partes.append(f"-- {tipo} {nombre}\n{d}\n")
        if fk:
            fks.append(f"-- FKs de {nombre}\n{fk}\n")
    # el código de triggers, vistas y paquetes suele nombrar tablas sin schema
    script = f"alter session set current_schema = {a.schema.upper()};\n\n" + "\n".join(partes + fks)
    (carpeta / "objetos_bd.sql").write_text(script, encoding="utf-8")
    print(f"    DDL de {len(elegidos)} objetos guardado en {carpeta / 'objetos_bd.sql'}")
    return script


PAGES_SQL = """
select page_id, page_name from apex_application_pages
 where application_id = :app_id
   and page_id in (select to_number(column_value) from table(apex_string.split(:pags, ',')))"""


def paginas(env, app_id, lista):
    url = os.environ[f"{env}_URL"].rstrip("/") + "/_/sql"
    auth = (os.environ[f"{env}_USER"], os.environ[f"{env}_PASS"])
    r = requests.post(url, auth=auth, timeout=120, json={"statementText": PAGES_SQL, "binds": [
        {"name": "app_id", "data_type": "NUMBER", "value": app_id},
        {"name": "pags", "data_type": "VARCHAR", "value": lista}]})
    r.raise_for_status()
    item = r.json()["items"][0]
    if "errorCode" in item:
        raise RuntimeError(f"[{env}] error {item['errorCode']}: {item.get('errorDetails')}")
    return {int(f["page_id"]): f["page_name"] for f in item["resultSet"]["items"]}


def ids_alineados(a):
    """True si la app de origen y la de destino comparten IDs internos (mismo linaje de export/import)."""
    sql = "select template_id from apex_application_templates where application_id = :app_id"
    def ids(env):
        url = os.environ[f"{env}_URL"].rstrip("/") + "/_/sql"
        auth = (os.environ[f"{env}_USER"], os.environ[f"{env}_PASS"])
        r = requests.post(url, auth=auth, timeout=120, json={"statementText": sql, "limit": 10000, "binds": [
            {"name": "app_id", "data_type": "NUMBER", "value": a.app}]})
        r.raise_for_status()
        item = r.json()["items"][0]
        if "errorCode" in item:
            raise RuntimeError(f"[{env}] {item.get('errorDetails')}")
        return {f["template_id"] for f in item["resultSet"]["items"]}
    origen, destino = ids(a.desde), ids(a.hacia)
    if not origen:
        return True
    return len(origen & destino) / len(origen) >= 0.8


def verificar_paginas(a, lista):
    origen = paginas(a.desde, a.app, lista)
    destino = paginas(a.hacia, a.app, lista)
    problemas = False
    for n in (int(x) for x in lista.split(",")):
        if n not in origen:
            print(f"    Página {n}: NO existe en {a.desde}")
            problemas = True
        elif n not in destino:
            print(f"    Página {n} '{origen[n]}': nueva en {a.hacia} (no aparecerá en el menú si no pasas la navegación)")
        elif origen[n] != destino[n]:
            print(f"    Página {n}: en {a.desde} es '{origen[n]}' pero en {a.hacia} es '{destino[n]}'")
            problemas = True
        else:
            print(f"    Página {n} '{origen[n]}': se reemplaza")
    if problemas and not a.forzar:
        sys.exit("Detenido: revisa las páginas marcadas. Si estás seguro, repite con --forzar.")
    return sorted(destino)  # páginas que ya existen en el destino (las que se reemplazan)


EXP_TABLA_SQL = """
begin
  execute immediate 'create table pase_apex_exports (id number generated always as identity primary key,
    creado timestamp default systimestamp, estado varchar2(10), contents clob, error varchar2(4000))';
exception when others then
  if sqlcode != -955 then raise; end if;  -- ya existe
end;"""

EXP_LANZAR_SQL = """
declare
  l_id number;
begin
  insert into pase_apex_exports (estado) values ('CORRIENDO') returning id into l_id;
  commit;
  dbms_scheduler.create_job(
    job_name   => 'PASE_APEX_EXP_' || l_id,
    job_type   => 'PLSQL_BLOCK',
    job_action => replace(:accion, '#ID#', l_id),
    enabled    => true,
    auto_drop  => true);
  :id := l_id;
end;"""

EXP_ACCION = """
declare
  l_files apex_t_export_files;
  l_err   varchar2(4000);
begin
  l_files := apex_export.get_application(p_application_id => #APP#, #COMPS#
                                         p_with_no_subscriptions => #SINSUBS#);
  update pase_apex_exports set estado = 'OK', contents = l_files(1).contents where id = #ID#;
  commit;
exception when others then
  -- sqlerrm no se puede usar dentro de una sentencia SQL: se guarda antes en una variable
  l_err := substr(sqlerrm || ' ' || dbms_utility.format_error_backtrace, 1, 4000);
  rollback;
  update pase_apex_exports set estado = 'ERROR', error = l_err where id = #ID#;
  commit;
end;"""

EXP_ESTADO_SQL = """
begin
  select estado, error into :estado, :error from pase_apex_exports where id = :id;
end;"""

EXP_TRAER_SQL = """
begin
  select contents into :contents from pase_apex_exports where id = :id;
  delete from pase_apex_exports where id = :id;
  commit;
end;"""

INST_LANZAR_SQL = """
declare
  l_id number;
begin
  insert into pase_apex_exports (estado, contents) values ('CORRIENDO', :contents) returning id into l_id;
  commit;
  dbms_scheduler.create_job(
    job_name   => 'PASE_APEX_INS_' || l_id,
    job_type   => 'PLSQL_BLOCK',
    job_action => replace(:accion, '#ID#', l_id),
    enabled    => true,
    auto_drop  => true);
  :id := l_id;
end;"""

INST_ACCION = """
declare
  l_contents clob;
  l_err      varchar2(4000);
begin
  select contents into l_contents from pase_apex_exports where id = #ID#;
  apex_util.set_workspace(p_workspace => '#WS#');
  apex_application_install.set_workspace('#WS#');
  apex_application_install.set_application_id(#APP#);
  apex_application_install.set_schema('#SCHEMA#');
  #OFFSET#
  #ALIAS#
  apex_application_install.set_auto_install_sup_obj(p_auto_install_sup_obj => #SUP#);
  apex_application_install.install(
    p_source => apex_t_export_files(apex_t_export_file(name => 'f#APP#.sql', contents => l_contents)),
    p_overwrite_existing => true);
  update pase_apex_exports set estado = 'OK', contents = null where id = #ID#;
  commit;
exception when others then
  l_err := substr(sqlerrm || ' ' || dbms_utility.format_error_backtrace, 1, 4000);
  rollback;
  update pase_apex_exports set estado = 'ERROR', error = l_err where id = #ID#;
  commit;
end;"""

EXP_BORRAR_SQL = "begin delete from pase_apex_exports where id = :id; commit; end;"

_tabla_lista = set()


def _asegurar_tabla(env):
    if env not in _tabla_lista:
        run(env, EXP_TABLA_SQL, [], timeout=60)
        _tabla_lista.add(env)


def esperar_job(env, exp_id, timeout, que):
    """Consulta cada 3 s hasta que el job termine. Lanza RuntimeError si falló o requests.Timeout si tarda."""
    inicio = time.monotonic()
    while True:
        time.sleep(3)
        fila = run(env, EXP_ESTADO_SQL, [
            {"name": "id", "data_type": "NUMBER", "value": exp_id},
            {"name": "estado", "data_type": "VARCHAR", "mode": "out"},
            {"name": "error", "data_type": "VARCHAR", "mode": "out"}], timeout=60)
        espera = int(time.monotonic() - inicio)
        if fila["estado"] in ("OK", "ERROR"):
            if espera > 3:
                print()
            if fila["estado"] == "ERROR":
                raise RuntimeError(f"[{env}] {que} falló: {fila.get('error')}")
            return
        print(f"\r      esperando a {env}... {espera} s", end="", flush=True)
        if espera > timeout:
            print()
            raise requests.Timeout(f"{que} en {env} sigue corriendo después de {timeout} s (registro {exp_id})")


def _txt(v):
    if not re.fullmatch(r"[A-Za-z0-9_$#.\-]+", str(v)):
        raise RuntimeError(f"valor no permitido: {v}")
    return str(v)


def instalar(a, dest, contenido, offset, alias):
    """Instala en un job de la base y espera a que termine, sin mantener una conexión larga."""
    _asegurar_tabla(a.hacia)
    accion = (INST_ACCION.replace("#WS#", _txt(a.workspace)).replace("#SCHEMA#", _txt(a.schema))
              .replace("#APP#", str(int(dest)))
              .replace("#OFFSET#", "apex_application_install.generate_offset;" if offset == "Y" else "")
              .replace("#ALIAS#", f"apex_application_install.set_application_alias('{_txt(alias)}');" if alias else "")
              .replace("#SUP#", "true" if a.sup_obj else "false"))
    out = run(a.hacia, INST_LANZAR_SQL, [
        {"name": "contents", "data_type": "CLOB", "value": contenido},
        {"name": "accion", "data_type": "VARCHAR", "value": accion},
        {"name": "id", "data_type": "NUMBER", "mode": "out"}], timeout=120)
    exp_id = int(out["id"])
    esperar_job(a.hacia, exp_id, TIEMPO_INSTALAR, "la instalación")
    run(a.hacia, EXP_BORRAR_SQL, [{"name": "id", "data_type": "NUMBER", "value": exp_id}], timeout=60)


def export(env, app_id, comps=None, sin_subs=False, timeout=TIEMPO_EXPORT):
    """Exporta en un job de la base y trae el resultado al terminar, sin mantener una conexión larga."""
    if comps and not re.fullmatch(r"[A-Z_]+:\d+(,[A-Z_]+:\d+)*", comps):
        raise RuntimeError(f"lista de componentes inválida: {comps}")
    _asegurar_tabla(env)
    accion = (EXP_ACCION.replace("#APP#", str(int(app_id)))
              .replace("#COMPS#", f"p_components => apex_string.split('{comps}', ','),\n" if comps else "")
              .replace("#SINSUBS#", "true" if sin_subs else "false"))
    out = run(env, EXP_LANZAR_SQL, [
        {"name": "accion", "data_type": "VARCHAR", "value": accion},
        {"name": "id", "data_type": "NUMBER", "mode": "out"}], timeout=60)
    exp_id = int(out["id"])
    esperar_job(env, exp_id, timeout, "la exportación")
    out = run(env, EXP_TRAER_SQL, [
        {"name": "id", "data_type": "NUMBER", "value": exp_id},
        {"name": "contents", "data_type": "CLOB", "mode": "out"}], timeout=120)
    contents = out.get("contents")
    if not contents:
        raise RuntimeError(f"[{env}] el export de la app {app_id} vino vacío")
    return contents


def repo_para(app_id):
    """Carpeta del repositorio Git de la app: repos_git.json, luego APEX_GIT_REPO_<ID>, luego APEX_GIT_REPO."""
    archivo = pathlib.Path(__file__).resolve().parent / "repos_git.json"
    if archivo.is_file():
        try:
            repos = json.loads(archivo.read_text(encoding="utf-8"))
        except ValueError as e:
            print(f"    Aviso: repos_git.json no es un JSON válido ({e}); no se usa Git.")
            return None
        if str(app_id) in repos:
            return repos[str(app_id)]
    return os.environ.get(f"APEX_GIT_REPO_{app_id}") or os.environ.get("APEX_GIT_REPO")


def guardar_en_git(repo, app_id, contenido, mensaje, push):
    repo = pathlib.Path(repo)
    if not (repo / ".git").exists():
        raise RuntimeError(f"{repo} no es un repositorio Git (clónalo primero o revisa repos_git.json)")
    archivo = pathlib.Path("apex") / f"f{app_id}.sql"
    (repo / archivo).parent.mkdir(parents=True, exist_ok=True)
    (repo / archivo).write_text(contenido, encoding="utf-8", newline="\n")

    def git(*args):
        return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)

    git("add", str(archivo))
    if git("diff", "--cached", "--quiet").returncode == 0:
        print("    Git: sin cambios respecto al último commit.")
        return
    r = git("commit", "-m", mensaje)
    if r.returncode != 0:
        raise RuntimeError("git commit falló: " + (r.stderr or r.stdout))
    print(f"    Git: commit creado ({mensaje})")
    if push:
        r = git("push")
        if r.returncode != 0:
            raise RuntimeError("git push falló: " + (r.stderr or r.stdout))
        print("    Git: push realizado.")


def verificar_instalacion(a, dest, minutos):
    """True si la app (o las páginas pasadas) se modificaron en los últimos 'minutos' en el destino."""
    if a.paginas:
        sql = ("select case when max(last_updated_on) >= sysdate - :minutos / 1440 then 'S' else 'N' end as ok "
               "from apex_application_pages where application_id = :app_id "
               "and page_id in (select to_number(column_value) from table(apex_string.split(:pags, ',')))")
    else:
        sql = ("select case when max(last_updated_on) >= sysdate - :minutos / 1440 then 'S' else 'N' end as ok "
               "from apex_applications where application_id = :app_id")
    binds = [{"name": "minutos", "data_type": "NUMBER", "value": minutos},
             {"name": "app_id", "data_type": "NUMBER", "value": dest}]
    if a.paginas:
        binds.append({"name": "pags", "data_type": "VARCHAR", "value": a.paginas})
    try:
        filas = so.consulta(a.hacia, sql, binds) if so else []
        return bool(filas) and filas[0].get("ok") == "S"
    except (RuntimeError, requests.RequestException, KeyError):
        return False


def main():
    p = argparse.ArgumentParser(description="Pase de app APEX entre ambientes")
    p.add_argument("--desde", choices=["TEST", "PRD"], default="TEST", help="Ambiente origen")
    p.add_argument("--hacia", choices=["TEST", "PRD"], default="PRD", help="Ambiente destino")
    p.add_argument("--app", type=int, required=True, help="ID de la aplicación en el origen")
    p.add_argument("--app-destino", type=int, help="ID en el destino (por defecto el mismo)")
    p.add_argument("--reemplazar", action="store_true",
                   help="Con --app-destino: es la misma app con otro ID; reemplazarla conservando su alias")
    p.add_argument("--paginas", help="Solo estas páginas, separadas por coma (ej. 10,20)")
    p.add_argument("--workspace", required=True, help="Workspace en el destino")
    p.add_argument("--schema", required=True, help="Parsing schema en el destino")
    p.add_argument("--sup-obj", action="store_true", help="Instalar también los Supporting Objects")
    p.add_argument("--mensaje", help="Descripción del cambio para el commit en Git")
    p.add_argument("--push", action="store_true", help="Hacer git push después del commit")
    p.add_argument("--sql", action="append", default=[], metavar="ARCHIVO",
                   help="Script SQL a ejecutar en el destino antes de instalar (se puede repetir)")
    p.add_argument("--sin-objetos", action="store_true",
                   help="No revisar objetos de base de datos faltantes en el destino")
    p.add_argument("--sin-menu", action="store_true",
                   help="No revisar ni crear las entradas de menú y breadcrumb de las páginas")
    p.add_argument("--con-componentes", action="store_true",
                   help="Incluir en el pase los componentes compartidos faltantes (preguntando por cada uno)")
    p.add_argument("--sin-respaldo", action="store_true",
                   help="No exportar el respaldo del destino (p. ej. si ya creaste uno en Manage Backups de APEX)")
    p.add_argument("--forzar", action="store_true", help="Instalar aunque los nombres de página no coincidan")
    p.add_argument("--yes", action="store_true", help="No pedir confirmación (para pipelines)")
    a = p.parse_args()
    for f in a.sql:
        if not pathlib.Path(f).is_file():
            sys.exit(f"No encuentro el archivo SQL: {f}")
    dest = a.app_destino or a.app
    if a.desde == a.hacia and dest == a.app:
        sys.exit("En el mismo ambiente usa --app-destino con un ID distinto")
    # modo: normal (mismo ID), copia (otro ID, app nueva) o reemplazo (otro ID, misma app)
    offset, alias, sin_subs = "N", None, False
    if dest != a.app:
        existentes = apps_en(a.hacia, [a.app, dest])
        if a.reemplazar:
            if dest not in existentes:
                sys.exit(f"--reemplazar: la app {dest} no existe en {a.hacia}; no hay nada que reemplazar")
            alias = existentes[dest]
            if a.app in existentes:
                offset = "Y"
                print(f"    Aviso: {a.hacia} también tiene una app {a.app}; se generan IDs internos nuevos y "
                      f"los reportes guardados por los usuarios de la app {dest} no se conservarán.")
            print(f"    Modo reemplazo: la app {dest} de {a.hacia} quedará con el contenido de la {a.app} "
                  f"de {a.desde}, conservando su alias '{alias}'.")
        else:
            if dest in existentes:
                sys.exit(f"La app {dest} ya existe en {a.hacia}. Si es la misma aplicación con otro ID, "
                         "agrega --reemplazar; si quieres una copia nueva, usa un ID libre.")
            offset, alias, sin_subs = "Y", f"F{dest}", True
            print(f"    Modo copia: se crea la app {dest} en {a.hacia} como copia nueva de la {a.app}.")
    elif a.reemplazar:
        sys.exit("--reemplazar solo se usa junto con --app-destino distinto")

    comps = None
    existentes_dst = []
    if a.paginas:
        # un export de componentes solo calza en la misma app (mismo ID e IDs internos)
        if dest != a.app:
            sys.exit("--paginas no se puede combinar con un --app-destino distinto")
        a.paginas = ",".join(n.strip() for n in a.paginas.split(","))
        comps = ",".join("PAGE:" + n for n in a.paginas.split(","))
        print(f"0/3 Verificando páginas en {a.desde} y {a.hacia}...")
        try:
            alineados = ids_alineados(a)
        except (RuntimeError, requests.RequestException, KeyError) as e:
            print(f"    Aviso: no pude comparar los IDs internos ({e}).")
            alineados = a.forzar
        if not alineados and not a.forzar:
            sys.exit(f"Detenido: la app {a.app} de {a.desde} y la de {a.hacia} tienen IDs internos distintos "
                     "(no vienen del mismo export). Las páginas quedarían apuntando a componentes inexistentes. "
                     "Haz el pase de la app completa.")
        existentes_dst = verificar_paginas(a, a.paginas)

    carpeta = pathlib.Path("pases") / f"{datetime.datetime.now():%Y%m%d_%H%M%S}_f{a.app}"
    carpeta.mkdir(parents=True)

    que = f"páginas {a.paginas} de la app {a.app}" if comps else f"app {a.app}"
    print(f"1/3 Exportando {que} desde {a.desde}...")
    nuevo = export(a.desde, a.app, comps, sin_subs=sin_subs)

    if comps and so is not None and not a.sin_objetos:
        print(f"    Revisando componentes compartidos que usa y faltan en la app {dest} de {a.hacia}...")
        try:
            faltan_comps = componentes_faltantes(a, dest, nuevo)
        except (RuntimeError, requests.RequestException, KeyError) as e:
            print(f"    Aviso: no pude revisar componentes compartidos ({e}).")
            faltan_comps = []
        if not faltan_comps:
            print("    Todos los componentes compartidos que usa existen en el destino.")
        elif not a.con_componentes:
            # por defecto:
            #  - si el destino tiene un componente con el mismo nombre, la página se apunta a ese (se mapea el ID)
            #  - los breadcrumbs que no existen en el destino se incluyen
            #  - el resto (LOVs nuevas, etc.) solo se avisa
            mapeables = [f for f in faltan_comps if f["mismo_nombre"]]
            migas = [f for f in faltan_comps if f["type_name"] == "BREADCRUMB" and not f["mismo_nombre"]]
            otros = [f for f in faltan_comps if f not in mapeables and f not in migas]
            if otros:
                print(f"    Aviso: la página usa componentes compartidos que no existen en la app {dest} de {a.hacia}:")
                for f in otros:
                    print(f"      - {f['type_name']} '{f['name']}'")
                print(f"    Créalos en {a.hacia} antes de instalar; si falta alguno que la página necesita, "
                      "la instalación fallará y APEX deshará todo.")
                if not a.yes and input("    ¿Continuar de todas formas? (s/n): ").strip().lower() != "s":
                    sys.exit("Cancelado. No se modificó nada en el destino.")
            extra = [f"{f['type_name']}:{f['id']}" for f in migas]
            for f in migas:
                print(f"    Se incluye el BREADCRUMB '{f['name']}' en el pase.")
            if extra:
                comps += "," + ",".join(extra)
                print(f"    Exportando de nuevo con {len(extra)} componente(s)...")
                nuevo = export(a.desde, a.app, comps, sin_subs=sin_subs)
                # lo agregado (p. ej. una lista) puede usar otros componentes: se revisa sobre el export final
                incluidos = {x.split(":")[1] for x in extra}
                faltan_comps = componentes_faltantes(a, dest, nuevo)
                mapeables = [f for f in faltan_comps if f["mismo_nombre"]]
                nuevos_otros = [f for f in faltan_comps if not f["mismo_nombre"]
                                and str(f["id"]) not in incluidos and f not in otros
                                and (f["type_name"], f["name"]) not in {(o["type_name"], o["name"]) for o in otros}]
                for f in nuevos_otros:
                    print(f"    Aviso: lo agregado usa {f['type_name']} '{f['name']}', que no existe en {a.hacia}.")
            if mapeables:
                incluidos = {x.split(":")[1] for x in extra}
                usados_dst = [f for f in mapeables if str(f["id"]) not in incluidos]
                for f in mapeables:
                    if str(f["id"]) in incluidos:
                        print(f"    Se reemplaza el {f['type_name']} '{f['name']}' de {a.hacia} con el de {a.desde} "
                              f"(ID {f['id']} -> {f['id_destino']}).")
                    else:
                        print(f"    Se usará el {f['type_name']} '{f['name']}' que ya existe en {a.hacia} "
                              f"(ID {f['id']} -> {f['id_destino']}).")
                if usados_dst:
                    print(f"      La página tomará la definición que esos componentes tienen en {a.hacia}; "
                          f"si en {a.desde} los cambiaste, pásalos aparte.")
                nuevo = mapear_ids(nuevo, {str(f["id"]): f["id_destino"] for f in mapeables})
        else:
            extra = []
            for f in faltan_comps:
                print(f"    Diferencia: {f['type_name']} '{f['name']}' existe en {a.desde} pero no en {a.hacia}.")
                r = "s" if a.yes else input(f"    ¿Incluirlo en el pase? (s/n): ").strip().lower()
                if r == "s":
                    extra.append(f"{f['type_name']}:{f['id']}")
                else:
                    print("      Omitido: si la página lo usa, la instalación fallará.")
            if extra:
                comps += "," + ",".join(extra)
                print(f"    Exportando de nuevo con {len(extra)} componentes compartidos...")
                nuevo = export(a.desde, a.app, comps, sin_subs=sin_subs)

    if comps and so is not None and not a.sin_menu:
        paginas_set = {int(x) for x in a.paginas.split(",")}
        bloques = revisar_menu(a, dest, paginas_set) + revisar_breadcrumb(a, dest, paginas_set)
        if bloques:
            try:
                nuevo = agregar_bloques(nuevo, bloques)
            except RuntimeError as e:
                print(f"    Aviso: {e}; crea las entradas de menú y breadcrumb a mano.")

    (carpeta / f"f{a.app}_{a.desde}.sql").write_text(nuevo, encoding="utf-8")

    script_objetos = None
    if a.desde != a.hacia and not a.sin_objetos:
        if so is None:
            print("    Aviso: no encuentro sincronizar_objetos.py en esta carpeta; no reviso objetos de BD.")
        else:
            print(f"    Revisando objetos de base de datos que usa y faltan en {a.hacia}...")
            script_objetos = preparar_objetos(a, nuevo, carpeta)

    try:
        if a.sin_respaldo:
            print(f"2/3 Sin respaldo del script (--sin-respaldo). Usa Manage Backups de APEX si lo necesitas.")
        elif comps:
            # en un pase por páginas solo se respaldan las páginas que se van a reemplazar
            if not existentes_dst:
                print(f"2/3 Sin respaldo: todas las páginas son nuevas en {a.hacia}.")
            else:
                lista = ",".join(str(n) for n in existentes_dst)
                print(f"2/3 Respaldando las páginas {lista} actuales de {a.hacia}...")
                respaldo = export(a.hacia, dest, ",".join(f"PAGE:{n}" for n in existentes_dst),
                                  timeout=TIEMPO_RESPALDO)
                (carpeta / f"f{dest}_{a.hacia}_backup_paginas_{lista.replace(',', '_')}.sql").write_text(
                    respaldo, encoding="utf-8")
        else:
            print(f"2/3 Respaldando la app {dest} actual de {a.hacia}...")
            (carpeta / f"f{dest}_{a.hacia}_backup.sql").write_text(
                export(a.hacia, dest, timeout=TIEMPO_RESPALDO), encoding="utf-8")
    except requests.Timeout:
        print(f"    Aviso: el respaldo no terminó en {TIEMPO_RESPALDO} segundos ({a.hacia} puede estar con mucha carga).")
        if not a.yes and input("    ¿Continuar sin respaldo? (s/n): ").lower() != "s":
            sys.exit("Cancelado. No se modificó nada en el destino.")
    except (RuntimeError, requests.RequestException) as e:
        print(f"    Aviso: no se pudo respaldar {a.hacia} ({e}). ¿La app aún no existe ahí?")
        if not a.yes and input("    ¿Continuar sin respaldo? (s/n): ").lower() != "s":
            sys.exit("Cancelado.")

    if not a.yes and input(f"3/3 Escribe {a.hacia} para instalar {que} en {a.hacia} (como app {dest}): ") != a.hacia:
        sys.exit("Cancelado. Los archivos quedaron en " + str(carpeta))

    if script_objetos:
        print(f"    Creando objetos de base de datos en {a.hacia}...")
        total, errores = so.ejecutar(a.hacia, script_objetos)
        if errores:
            sys.exit(f"Detenido: {errores} de {total} sentencias fallaron. La app no se instaló.")
        print(f"      {total} sentencias OK")

    for f in a.sql:
        print(f"    Ejecutando {f} en {a.hacia}...")
        total, errores = run_script(a.hacia, f"alter session set current_schema = {a.schema.upper()};\n"
                                    + pathlib.Path(f).read_text(encoding="utf-8"))
        if errores:
            sys.exit(f"Detenido: {errores} de {total} sentencias de {f} fallaron. La app no se instaló.")
        print(f"      {total} sentencias OK")

    print(f"    Instalando en {a.hacia}...")
    inicio = datetime.datetime.now()
    try:
        instalar(a, dest, nuevo, offset, alias)
    except requests.Timeout:
        print(f"    La instalación sigue en curso en {a.hacia} después de {TIEMPO_INSTALAR // 60} minutos. "
              "Verificando si ya se aplicó...")
        minutos = int((datetime.datetime.now() - inicio).total_seconds() // 60) + 2
        if verificar_instalacion(a, dest, minutos):
            print("    La instalación SÍ se aplicó (fecha de modificación reciente en el destino).")
        else:
            sys.exit(f"Todavía no se aplicó. El job puede seguir corriendo en {a.hacia}: espera y revisa la app "
                     f"{dest} (o la tabla PASE_APEX_EXPORTS) antes de reintentar.")

    repo = repo_para(dest)
    if repo and a.hacia == "PRD" and (dest == a.app or a.reemplazar):
        print("    Guardando en Git la versión que quedó en PRD...")
        try:
            msg = f"Pase app {a.app} a PRD" + (f" (páginas {a.paginas})" if a.paginas else "")
            if a.mensaje:
                msg += f": {a.mensaje}"
            guardar_en_git(repo, dest, export("PRD", dest), msg, a.push)
        except (RuntimeError, requests.RequestException, OSError) as e:
            # el pase ya se hizo; un fallo de Git no debe parecer un fallo del pase
            print(f"    Aviso: el pase se completó, pero no se pudo guardar en Git: {e}")

    print(f"Listo. Export y respaldo guardados en {carpeta}")


if __name__ == "__main__":
    try:
        main()
    except requests.Timeout:
        sys.exit("ERROR: el ambiente no respondió a tiempo (la conexión pudo cortarse). "
                 "Si fue antes de la instalación no se modificó nada: vuelve a ejecutar el pase.")
    except (RuntimeError, requests.RequestException, KeyError) as e:
        sys.exit(f"ERROR: {e}")