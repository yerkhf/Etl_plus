
import json
import re
import sqlite3
import tempfile
import unicodedata
from difflib import get_close_matches
from datetime import datetime, date
from urllib.parse import quote

import pandas as pd
import pydeck as pdk
import requests
import streamlit as st


# ============================================================
# CONFIGURACIÓN GENERAL
# ============================================================

st.set_page_config(
    page_title="ETL ",
    layout="wide"
)

API_CHILE_ABIERTO = "https://chileabierto.cl/api/v1"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
WIKIDATA_ENTITY = "https://www.wikidata.org/wiki/Special:EntityData/{qid}.json"

HEADERS = {
    "User-Agent": "ETL (streamlit app)"
}


# ============================================================
# FUNCIONES GENERALES
# ============================================================

def quitar_tildes(texto):
    """Elimina tildes para comparar nombres de manera flexible."""
    texto = str(texto)
    texto = unicodedata.normalize("NFD", texto)
    texto = "".join(c for c in texto if unicodedata.category(c) != "Mn")
    return texto


def normalizar_espacios(texto):
    """Quita espacios innecesarios."""
    return re.sub(r"\s+", " ", str(texto).strip())


def normalizar_texto_base(texto):
    """Normalización base: limpia caracteres raros, espacios y tildes para comparación."""
    texto = normalizar_espacios(texto)
    texto = texto.replace("Û", "ó").replace("ﬂ", "ss")
    return texto


def aplicar_formato(texto, formato):
    """Aplica el formato elegido por el usuario."""
    texto = normalizar_texto_base(texto)

    if formato == "MAYÚSCULAS":
        return texto.upper()
    if formato == "minúsculas":
        return texto.lower()

    return texto.title()


def leer_archivo_lineas(archivo):
    """Lee archivos TXT con soporte para UTF-8 y Latin-1."""
    try:
        return archivo.read().decode("utf-8").splitlines()
    except UnicodeDecodeError:
        archivo.seek(0)
        return archivo.read().decode("latin-1").splitlines()


def dataframe_to_csv_bytes(df):
    """Convierte un DataFrame a CSV descargable."""
    return df.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")


def crear_sqlite_bytes(tablas):
    """
    Recibe un diccionario {nombre_tabla: dataframe}
    y devuelve una base SQLite en bytes para descarga.
    """
    with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as tmp:
        ruta_db = tmp.name

    conexion = sqlite3.connect(ruta_db)

    for nombre_tabla, df in tablas.items():
        df.to_sql(nombre_tabla, conexion, if_exists="replace", index=False)

    conexion.close()

    with open(ruta_db, "rb") as archivo_db:
        return archivo_db.read()



# COMUNAS


@st.cache_data(ttl=60 * 60 * 24)
def obtener_comunas_chile_abierto():
    """
    Obtiene comunas desde Chile Abierto.
    La API entrega nombre, región, población, coordenadas y fuente.
    """
    url = f"{API_CHILE_ABIERTO}/comunas"
    try:
        respuesta = requests.get(url, headers=HEADERS, timeout=20)
        respuesta.raise_for_status()
        data = respuesta.json()
    except requests.RequestException as error:
        st.error(f"No se pudo conectar a Chile Abierto: {error}")
        return [], "Chile Abierto", "https://chileabierto.cl/api"

    comunas = data.get("data", [])
    fuente = data.get("source", "Chile Abierto")
    fuente_url = data.get("source_url", "https://chileabierto.cl/api")

    return comunas, fuente, fuente_url


def extraer_comunas_desde_archivo(archivo):
    """
    Extrae comunas desde TXT o CSV.
    Si el archivo tiene una columna llamada comuna, la usa.
    Si no, toma la primera columna o cada línea del TXT.
    """
    nombre = archivo.name.lower()

    if nombre.endswith(".csv"):
        try:
            df = pd.read_csv(archivo, encoding="utf-8")
        except UnicodeDecodeError:
            archivo.seek(0)
            df = pd.read_csv(archivo, encoding="latin-1")

        columnas_normalizadas = {quitar_tildes(c.lower()): c for c in df.columns}

        if "comuna" in columnas_normalizadas:
            col = columnas_normalizadas["comuna"]
        elif columnas_con_comuna := [
            original
            for normalizada, original in columnas_normalizadas.items()
            if "comuna" in normalizada
        ]:
            col = columnas_con_comuna[0]
        else:
            col = df.columns[0]
            st.warning(
                f"No se encontró una columna llamada comuna. "
                f"Se usará la primera columna del archivo: {col}."
            )

        return df[col].dropna().astype(str).tolist()

    lineas = leer_archivo_lineas(archivo)
    comunas = []

    for linea in lineas:
        linea = normalizar_espacios(linea)
        if not linea:
            continue

        # Permite líneas simples o separadas por ; / ,
        if ";" in linea:
            comunas.append(linea.split(";")[0])
        elif "," in linea:
            comunas.append(linea.split(",")[0])
        else:
            comunas.append(linea)

    return comunas


def preparar_comunas_entrada(lista_comunas, formato):
    """Normaliza y elimina duplicados desde el listado ingresado o cargado."""
    registros = []
    vistos = set()
    duplicados = 0

    for comuna_original in lista_comunas:
        comuna_limpia = aplicar_formato(comuna_original, formato)
        clave = quitar_tildes(comuna_limpia).lower()

        if not clave:
            continue

        if clave in vistos:
            duplicados += 1
            continue

        vistos.add(clave)
        registros.append({
            "comuna_original": comuna_original,
            "comuna_normalizada": comuna_limpia,
            "clave_busqueda": clave
        })

    return registros, duplicados


def buscar_comuna_en_fuente(clave_busqueda, comunas_api):
    """
    Busca una comuna en la fuente oficial/pública.
    Primero intenta coincidencia exacta sin tildes.
    Luego entrega sugerencias por subcadena y por similitud para errores tipográficos.
    """
    exactas = []
    sugerencias = []
    comunas_por_clave = {}

    for item in comunas_api:
        nombre_api = item.get("name", "")
        clave_api = quitar_tildes(nombre_api).lower()
        comunas_por_clave[clave_api] = item

        if clave_api == clave_busqueda:
            exactas.append(item)

        if clave_busqueda in clave_api or clave_api in clave_busqueda:
            sugerencias.append(item)

    if exactas:
        return exactas[0], exactas

    if not sugerencias:
        claves_similares = get_close_matches(
            clave_busqueda,
            comunas_por_clave.keys(),
            n=5,
            cutoff=0.72
        )
        sugerencias = [comunas_por_clave[clave] for clave in claves_similares]

    if len(sugerencias) == 1:
        return sugerencias[0], sugerencias

    return None, sugerencias


def procesar_comunas(lista_comunas, formato):
    """Consolida comunas con región y población usando Chile Abierto."""
    comunas_api, fuente, fuente_url = obtener_comunas_chile_abierto()
    entradas, duplicados = preparar_comunas_entrada(lista_comunas, formato)

    consolidados = []
    no_encontrados = []
    errores = []

    for entrada in entradas:
        try:
            encontrado, sugerencias = buscar_comuna_en_fuente(
                entrada["clave_busqueda"],
                comunas_api
            )

            if encontrado:
                consolidados.append({
                    "codigo_comuna": encontrado.get("code"),
                    "nombre_comuna": encontrado.get("name"),
                    "region": encontrado.get("region_name"),
                    "habitantes": encontrado.get("population"),
                    "latitud": encontrado.get("lat"),
                    "longitud": encontrado.get("lng"),
                    "fuente": fuente,
                    "fuente_url": fuente_url,
                    "fecha_captura": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                })
            else:
                no_encontrados.append({
                    "comuna_ingresada": entrada["comuna_original"],
                    "comuna_normalizada": entrada["comuna_normalizada"],
                    "opciones": ", ".join([s.get("name", "") for s in sugerencias]) if sugerencias else "Sin sugerencias"
                })

        except Exception as error:
            errores.append({
                "comuna": entrada["comuna_original"],
                "error": str(error)
            })

    df_consolidados = pd.DataFrame(
        consolidados,
        columns=[
            "codigo_comuna",
            "nombre_comuna",
            "region",
            "habitantes",
            "latitud",
            "longitud",
            "fuente",
            "fuente_url",
            "fecha_captura"
        ]
    )

    if not df_consolidados.empty:
        df_consolidados = df_consolidados.drop_duplicates(
            subset=["codigo_comuna"],
            keep="last"
        ).reset_index(drop=True)

    df_no_encontrados = pd.DataFrame(
        no_encontrados,
        columns=["comuna_ingresada", "comuna_normalizada", "opciones"]
    )
    df_errores = pd.DataFrame(
        errores,
        columns=["comuna", "error"]
    )

    auditoria = pd.DataFrame([{
        "fecha_hora_ejecucion": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "registros_leidos": len(lista_comunas),
        "comunas_procesadas": len(entradas),
        "duplicados_eliminados": duplicados,
        "registros_consolidados_correctamente": len(df_consolidados),
        "registros_no_encontrados": len(df_no_encontrados),
        "errores": len(df_errores)
    }])

    return df_consolidados, df_no_encontrados, df_errores, auditoria


def vista_comunas():
    st.header("I. Normalización y consolidación de comunas")
    st.write(
        "Carga un archivo con comunas. "
        "La app normaliza nombres, elimina duplicados, consulta Chile Abierto y genera auditoría."
    )

    formato = "Título"

    archivo = st.file_uploader(
        "Cargar archivo de comunas TXT o CSV",
        type=["txt", "TXT", "csv", "CSV"],
        key="archivo_comunas"
    )

    lista_comunas = []

    if archivo is not None:
        lista_comunas.extend(extraer_comunas_desde_archivo(archivo))

    if st.button("Procesar comunas"):
        if not lista_comunas:
            st.warning("Debes cargar un archivo con al menos una comuna.")
            return

        try:
            df_consolidados, df_no_encontrados, df_errores, auditoria = procesar_comunas(
                lista_comunas,
                formato
            )

            st.success("Proceso de comunas finalizado.")

            st.subheader("Comunas consolidadas")
            st.dataframe(df_consolidados, use_container_width=True)

            st.subheader("Auditoría del proceso")
            st.dataframe(auditoria, use_container_width=True)

            if not df_no_encontrados.empty:
                st.subheader("No encontradas / revisar opciones")
                st.dataframe(df_no_encontrados, use_container_width=True)

            if not df_errores.empty:
                st.subheader("Errores")
                st.dataframe(df_errores, use_container_width=True)

            db_bytes = crear_sqlite_bytes({
                "comunas_consolidadas": df_consolidados,
                "auditoria_comunas": auditoria,
                "comunas_no_encontradas": df_no_encontrados,
                "errores_comunas": df_errores
            })

            c1, c2, c3 = st.columns(3)
            with c1:
                st.download_button(
                    "Descargar comunas consolidadas CSV",
                    dataframe_to_csv_bytes(df_consolidados),
                    "comunas_consolidadas.csv",
                    "text/csv"
                )
            with c2:
                st.download_button(
                    "Descargar auditoría CSV",
                    dataframe_to_csv_bytes(auditoria),
                    "auditoria_comunas.csv",
                    "text/csv"
                )
            with c3:
                st.download_button(
                    "Descargar base SQLite",
                    db_bytes,
                    "comunas_final.db",
                    "application/octet-stream"
                )

        except Exception as error:
            st.error(f"No se pudo completar el proceso: {error}")


# FAMOSOS 


def limpiar_nombre_famoso(nombre):
    """Elimina numeración inicial del dataset de famosos."""
    return re.sub(r"^\d+\.\s*", "", str(nombre)).strip()


def detectar_fecha_famoso(fecha_texto):
    """
    Detecta fechas normales, fechas aproximadas y fechas a.C.
    Retorna un diccionario con datos normalizados.
    """
    original = str(fecha_texto).strip()
    texto = original.lower().replace("/", "-").replace(".", "")

    # Caso a.C. con posible mes y día: 100 a.C./07/12
    if "ac" in texto or "a c" in texto:
        year_match = re.search(r"(\d{1,4})", texto)
        nums = re.findall(r"\d+", texto)

        if not year_match:
            return None

        year_bce = int(year_match.group(1))
        month = None
        day = None

        if len(nums) >= 3:
            month = int(nums[1])
            day = int(nums[2])

        edad = date.today().year + year_bce - 1

        return {
            "fecha_nacimiento": None,
            "fecha_formato_chile": f"{day:02d}-{month:02d}-{year_bce} a.C." if day and month else f"Año {year_bce} a.C.",
            "anio": -year_bce,
            "mes": month,
            "dia": day,
            "edad": edad,
            "cumple_hoy": "SI" if day == date.today().day and month == date.today().month else "NO",
            "tipo_fecha": "a.C."
        }

    # Caso aproximado: alrededor de 1028
    if "alrededor" in texto:
        nums = re.findall(r"\d+", texto)
        if not nums:
            return None

        year = int(nums[0])
        edad = date.today().year - year

        return {
            "fecha_nacimiento": None,
            "fecha_formato_chile": f"Año {year} aprox.",
            "anio": year,
            "mes": None,
            "dia": None,
            "edad": edad,
            "cumple_hoy": "NO",
            "tipo_fecha": "aproximada"
        }

    formatos = ["%Y-%m-%d", "%d-%m-%Y"]

    for formato in formatos:
        try:
            fecha = datetime.strptime(texto, formato).date()
            hoy = date.today()
            edad = hoy.year - fecha.year
            if (hoy.month, hoy.day) < (fecha.month, fecha.day):
                edad -= 1

            return {
                "fecha_nacimiento": str(fecha),
                "fecha_formato_chile": fecha.strftime("%d-%m-%Y"),
                "anio": fecha.year,
                "mes": fecha.month,
                "dia": fecha.day,
                "edad": edad,
                "cumple_hoy": "SI" if hoy.day == fecha.day and hoy.month == fecha.month else "NO",
                "tipo_fecha": "exacta"
            }
        except ValueError:
            continue

    return None


def procesar_famosos(archivo):
    """Procesa el archivo de famosos y calcula edad/cumpleaños."""
    lineas = leer_archivo_lineas(archivo)
    registros = []
    descartados = []

    for linea in lineas:
        linea = linea.strip()
        if not linea:
            continue

        partes = linea.split(" - ", 1)
        if len(partes) != 2:
            descartados.append({
                "linea": linea,
                "motivo": "No contiene separador válido ' - '"
            })
            continue

        nombre = limpiar_nombre_famoso(partes[0])
        fecha_data = detectar_fecha_famoso(partes[1])

        if fecha_data is None:
            descartados.append({
                "linea": linea,
                "motivo": "Fecha no convertible"
            })
            continue

        registros.append({
            "nombre": nombre,
            **fecha_data
        })

    df = pd.DataFrame(registros)

    if not df.empty:
        df = df.drop_duplicates(
            subset=["nombre", "anio", "mes", "dia", "tipo_fecha"]
        ).sort_values("nombre").reset_index(drop=True)

    return df, pd.DataFrame(descartados)


@st.cache_data(ttl=60 * 60 * 24)
def buscar_imagen_wikidata(nombre):
    """
    Busca una persona en Wikidata y obtiene su imagen principal P18.
    Guarda URL de fuente, imagen y fecha de captura.
    """
    params = {
        "action": "wbsearchentities",
        "search": nombre,
        "language": "en",
        "format": "json",
        "limit": 5
    }

    r = requests.get(WIKIDATA_API, params=params, headers=HEADERS, timeout=20)
    r.raise_for_status()
    resultados = r.json().get("search", [])

    if not resultados:
        return {
            "nombre_buscado": nombre,
            "estado": "No encontrado",
            "qid": None,
            "imagen_url": None,
            "fuente": "Wikidata",
            "fuente_url": None,
            "fecha_captura": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }

    qid = resultados[0].get("id")
    label = resultados[0].get("label", nombre)

    r_entidad = requests.get(
        WIKIDATA_ENTITY.format(qid=qid),
        headers=HEADERS,
        timeout=20
    )
    r_entidad.raise_for_status()
    entidad = r_entidad.json()["entities"][qid]

    claims = entidad.get("claims", {})
    imagen_url = None

    if "P18" in claims:
        filename = claims["P18"][0]["mainsnak"]["datavalue"]["value"]
        imagen_url = "https://commons.wikimedia.org/wiki/Special:FilePath/" + quote(filename)

    return {
        "nombre_buscado": nombre,
        "nombre_wikidata": label,
        "estado": "Imagen encontrada" if imagen_url else "Persona encontrada sin imagen",
        "qid": qid,
        "imagen_url": imagen_url,
        "fuente": "Wikidata / Wikimedia Commons",
        "fuente_url": f"https://www.wikidata.org/wiki/{qid}",
        "fecha_captura": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "api_respuesta_resumida": json.dumps(resultados[0], ensure_ascii=False)
    }


def vista_famosos():
    st.header("II. Famosos, edad e imagen desde API")
    st.write(
        "Carga el archivo de famosos. La app calcula edad, normaliza fechas y permite ver una imagen "
        "del famoso seleccionado usando Wikidata/Wikimedia Commons."
    )

    archivo = st.file_uploader(
        "Cargar archivo TXT de famosos",
        type=["txt", "TXT"],
        key="archivo_famosos"
    )

    if archivo is not None:
        if st.button("Procesar famosos"):
            df, df_descartados = procesar_famosos(archivo)
            st.session_state["df_famosos"] = df
            st.session_state["df_famosos_descartados"] = df_descartados
            st.session_state["imagenes_famosos_cache"] = {}
            st.session_state.pop("imagen_famoso", None)

    df = st.session_state.get("df_famosos")

    if isinstance(df, pd.DataFrame) and not df.empty:
        st.success(f"Famosos procesados correctamente: {len(df)}")
        st.dataframe(df, use_container_width=True)

        seleccionado = st.selectbox(
            "Selecciona un famoso para ver imagen",
            df["nombre"].tolist()
        )

        fila = df[df["nombre"] == seleccionado].iloc[0]
        imagenes_cache = st.session_state.setdefault("imagenes_famosos_cache", {})

        col1, col2 = st.columns([1, 2])

        with col1:
            st.metric("Edad calculada", int(fila["edad"]))
            st.write(f"Fecha normalizada: **{fila['fecha_formato_chile']}**")
            st.write(f"Cumple hoy: **{fila['cumple_hoy']}**")

        with col2:
            if seleccionado not in imagenes_cache:
                with st.spinner("Consultando API de Wikidata..."):
                    imagenes_cache[seleccionado] = buscar_imagen_wikidata(seleccionado)
                    st.session_state["imagenes_famosos_cache"] = imagenes_cache

            imagen_data = imagenes_cache.get(seleccionado)

            if imagen_data and imagen_data.get("nombre_buscado") == seleccionado:
                st.write(f"Estado: **{imagen_data.get('estado')}**")
                st.write(f"Fuente: **{imagen_data.get('fuente')}**")
                st.write(f"Capturado: **{imagen_data.get('fecha_captura')}**")

                if imagen_data.get("fuente_url"):
                    st.link_button("Abrir fuente", imagen_data["fuente_url"])

                if imagen_data.get("imagen_url"):
                    st.image(
                        imagen_data["imagen_url"],
                        caption=f"Imagen de {seleccionado}",
                        width=320
                    )

        if imagenes_cache:
            df_cache = pd.DataFrame(imagenes_cache.values())
            db_bytes = crear_sqlite_bytes({
                "famosos_normalizados": df,
                "famosos_imagenes_cache": df_cache
            })

            st.download_button(
                "Descargar base SQLite con caché de imagen",
                db_bytes,
                "famosos_imagenes.db",
                "application/octet-stream"
            )

        st.download_button(
            "Descargar famosos normalizados CSV",
            dataframe_to_csv_bytes(df),
            "famosos_normalizados.csv",
            "text/csv"
        )

        if not st.session_state.get("df_famosos_descartados", pd.DataFrame()).empty:
            st.subheader("Registros descartados")
            st.dataframe(st.session_state["df_famosos_descartados"], use_container_width=True)

    elif isinstance(df, pd.DataFrame) and df.empty:
        st.warning("No se encontraron famosos válidos en el archivo procesado.")



# LUGARES HISTÓRICOS


def leer_dataset_lugares(archivo):
    """Lee dataset de lugares separados por punto y coma."""
    try:
        return pd.read_csv(archivo, sep=";", encoding="utf-8")
    except UnicodeDecodeError:
        archivo.seek(0)
        return pd.read_csv(archivo, sep=";", encoding="latin-1")


def separar_georeferencia(georef):
    """Separa georeferencia en latitud y longitud."""
    try:
        partes = str(georef).split(",")
        if len(partes) != 2:
            return None, None

        latitud = float(partes[0].strip())
        longitud = float(partes[1].strip())
        return latitud, longitud
    except Exception:
        return None, None


def separar_direccion(direccion):
    """Divide la dirección completa en calle, número, ciudad/provincia y país."""
    partes = [p.strip() for p in str(direccion).split(",") if p.strip()]
    primera_parte = partes[0] if partes else ""
    pais = partes[-1] if len(partes) > 1 else ""

    numero_calle = ""
    nombre_calle = primera_parte

    match = re.match(r"^(\d+)\s+(.*)", primera_parte)
    if match:
        numero_calle = match.group(1)
        nombre_calle = match.group(2)

    if len(partes) > 2:
        ciudad_estado_provincia = ", ".join(partes[1:-1])
    elif len(partes) == 2:
        ciudad_estado_provincia = partes[0]
    else:
        ciudad_estado_provincia = ""

    return nombre_calle, numero_calle, ciudad_estado_provincia, pais


def procesar_lugares(archivo):
    """Normaliza lugares, direcciones y georeferencias."""
    df_original = leer_dataset_lugares(archivo)
    df_original.columns = [normalizar_texto_base(c) for c in df_original.columns]

    if len(df_original.columns) < 3:
        raise ValueError("El archivo debe tener al menos 3 columnas: lugar, dirección y georeferencia.")

    col_lugar = df_original.columns[0]
    col_direccion = df_original.columns[1]
    col_georef = df_original.columns[2]

    registros = []
    descartados = []

    for _, fila in df_original.iterrows():
        nombre_lugar = normalizar_texto_base(fila[col_lugar])
        direccion_completa = normalizar_texto_base(fila[col_direccion])
        georef = normalizar_texto_base(fila[col_georef])

        latitud, longitud = separar_georeferencia(georef)

        if not nombre_lugar or latitud is None or longitud is None:
            descartados.append({
                "nombre_lugar": nombre_lugar,
                "direccion_completa": direccion_completa,
                "georeferencia": georef,
                "motivo": "Lugar vacío o georeferencia inválida"
            })
            continue

        nombre_calle, numero_calle, ciudad_estado_provincia, pais = separar_direccion(direccion_completa)

        registros.append({
            "nombre_lugar": nombre_lugar,
            "direccion_completa": direccion_completa,
            "nombre_calle": nombre_calle,
            "numero_calle": numero_calle,
            "ciudad_estado_provincia": ciudad_estado_provincia,
            "pais": pais,
            "latitud": latitud,
            "longitud": longitud
        })

    df_limpio = pd.DataFrame(registros)

    if not df_limpio.empty:
        df_limpio = df_limpio.drop_duplicates(
            subset=["nombre_lugar", "latitud", "longitud"]
        ).sort_values("nombre_lugar").reset_index(drop=True)

    return df_limpio, pd.DataFrame(descartados)


def crear_tablas_lugares(df_limpio):
    """Crea las tres tablas normalizadas solicitadas."""
    lugares = []
    direcciones = []
    georeferencias = []

    for id_lugar, fila in enumerate(df_limpio.itertuples(index=False), start=1):
        lugares.append({
            "id_lugar": id_lugar,
            "nombre_lugar": fila.nombre_lugar
        })

        direcciones.append({
            "id_direccion": id_lugar,
            "id_lugar": id_lugar,
            "nombre_calle": fila.nombre_calle,
            "numero_calle": fila.numero_calle,
            "ciudad_estado_provincia": fila.ciudad_estado_provincia,
            "pais": fila.pais
        })

        georeferencias.append({
            "id_georeferencia": id_lugar,
            "id_lugar": id_lugar,
            "latitud": fila.latitud,
            "longitud": fila.longitud
        })

    return pd.DataFrame(lugares), pd.DataFrame(direcciones), pd.DataFrame(georeferencias)


def vista_lugares():
    st.header("III. Lugares históricos en mapa mundial")
    st.write(
        "Carga el dataset de lugares. La app normaliza los datos, genera las tres tablas y muestra "
        "todos los puntos en un mapa mundial."
    )

    archivo = st.file_uploader(
        "Cargar archivo TXT o CSV de lugares",
        type=["txt", "TXT", "csv", "CSV"],
        key="archivo_lugares"
    )

    if archivo is not None:
        if st.button("Procesar lugares"):
            try:
                df_limpio, df_descartados = procesar_lugares(archivo)
                st.session_state["df_lugares_limpio"] = df_limpio
                st.session_state["df_lugares_descartados"] = df_descartados
            except ValueError as error:
                st.session_state.pop("df_lugares_limpio", None)
                st.session_state.pop("df_lugares_descartados", None)
                st.error(str(error))
            except Exception as error:
                st.session_state.pop("df_lugares_limpio", None)
                st.session_state.pop("df_lugares_descartados", None)
                st.error(f"No se pudo procesar el archivo de lugares: {error}")

    df_limpio = st.session_state.get("df_lugares_limpio")

    if isinstance(df_limpio, pd.DataFrame) and not df_limpio.empty:
        df_lugares, df_direcciones, df_georeferencias = crear_tablas_lugares(df_limpio)

        st.success(f"Lugares normalizados correctamente: {len(df_lugares)}")

        seleccionado = st.selectbox(
            "Selecciona un lugar para llegar a él",
            df_limpio["nombre_lugar"].tolist()
        )

        fila = df_limpio[df_limpio["nombre_lugar"] == seleccionado].iloc[0]

        st.subheader("Mapa mundial de lugares cargados")

        mapa_df = df_limpio.rename(columns={"latitud": "lat", "longitud": "lon"})
        mapa_seleccionado_df = pd.DataFrame([{
            "nombre_lugar": fila["nombre_lugar"],
            "direccion_completa": fila["direccion_completa"],
            "lat": fila["latitud"],
            "lon": fila["longitud"]
        }])

        capa_lugares = pdk.Layer(
            "ScatterplotLayer",
            data=mapa_df,
            get_position="[lon, lat]",
            get_radius=2500,
            get_fill_color=[50, 120, 190, 150],
            pickable=True,
            opacity=0.7
        )

        capa_seleccionado = pdk.Layer(
            "ScatterplotLayer",
            data=mapa_seleccionado_df,
            get_position="[lon, lat]",
            get_radius=9000,
            get_fill_color=[230, 70, 70, 220],
            pickable=True,
            opacity=0.7
        )

        vista = pdk.ViewState(
            latitude=float(fila["latitud"]),
            longitude=float(fila["longitud"]),
            zoom=12,
            pitch=0
        )

        tooltip = {
            "html": "<b>{nombre_lugar}</b><br>{direccion_completa}",
            "style": {"backgroundColor": "steelblue", "color": "white"}
        }

        st.pydeck_chart(
            pdk.Deck(
                layers=[capa_lugares, capa_seleccionado],
                initial_view_state=vista,
                tooltip=tooltip
            )
        )

        st.write(f"**Dirección:** {fila['direccion_completa']}")
        st.write(f"**Coordenadas:** {fila['latitud']}, {fila['longitud']}")

        url_maps = f"https://www.google.com/maps/search/?api=1&query={fila['latitud']},{fila['longitud']}"
        st.link_button("Abrir ubicación en Google Maps", url_maps)

        st.subheader("Tabla Lugares")
        st.dataframe(df_lugares, use_container_width=True)

        st.subheader("Tabla Direcciones")
        st.dataframe(df_direcciones, use_container_width=True)

        st.subheader("Tabla Georeferencias")
        st.dataframe(df_georeferencias, use_container_width=True)

        db_bytes = crear_sqlite_bytes({
            "lugares": df_lugares,
            "direcciones": df_direcciones,
            "georeferencias": df_georeferencias
        })

        c1, c2, c3, c4 = st.columns(4)
        with c1:
            st.download_button("Descargar lugares.csv", dataframe_to_csv_bytes(df_lugares), "lugares.csv", "text/csv")
        with c2:
            st.download_button("Descargar direcciones.csv", dataframe_to_csv_bytes(df_direcciones), "direcciones.csv", "text/csv")
        with c3:
            st.download_button("Descargar georeferencias.csv", dataframe_to_csv_bytes(df_georeferencias), "georeferencias.csv", "text/csv")
        with c4:
            st.download_button("Descargar base SQLite", db_bytes, "lugares_final.db", "application/octet-stream")

        if not st.session_state.get("df_lugares_descartados", pd.DataFrame()).empty:
            st.subheader("Registros descartados")
            st.dataframe(st.session_state["df_lugares_descartados"], use_container_width=True)


# ============================================================
# NAVEGACIÓN PRINCIPAL
# ============================================================

st.sidebar.title("Procesamiento de datos")
modulo = st.sidebar.radio(
    "Módulo",
    [
        "I. Comunas",
        "II. Famosos con imagen",
        "III. Lugares históricos"
    ]
)

st.sidebar.markdown("---")

st.title("Aplicación de procesamiento de datos")

if modulo == "I. Comunas":
    vista_comunas()
elif modulo == "II. Famosos con imagen":
    vista_famosos()
else:
    vista_lugares()
