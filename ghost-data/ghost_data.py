import json
import os
import re
import sys
import time
from datetime import datetime, timezone as utc_timezone
from pathlib import Path
from urllib.parse import urlencode, urlparse, parse_qs, unquote

import phonenumbers
import requests
from phonenumbers import carrier, geocoder, timezone
from phonenumbers import PhoneNumberType
from bs4 import BeautifulSoup


ARCHIVO_REPORTE = Path("reporte_telefonico.json")
MAX_RESULTADOS = 10
PAUSA_ENTRE_BUSQUEDAS = 5  # segundos, para evitar bloqueos del buscador

# Indicadores en títulos/fragmentos que sugieren que el número es de un negocio.
INDICADORES_EMPRESA = [
    "srl", "s.r.l", "s.a.", "eirl", "sas", "llc", "inc", "ltd", "corp",
    "empresa", "tienda", "restaurante", "clínica", "clinica", "farmacia",
    "hotel", "taller", "consultorio", "servicios", "ventas", "distribuidora",
    "horario", "dirección", "direccion", "sucursal", "whatsapp business",
]
DOMINIOS_DIRECTORIO = [
    "paginasamarillas", "yelp.", "google.com/maps", "tripadvisor",
    "infoisinfo", "cybo.com", "guiatelefonica", "dnb.com", "opencorporates",
    "yellowpages.com", "bbb.org", "manta.com", "superpages.com",
    "mapquest.com", "chamberofcommerce.com",
]
DOMINIOS_PERSONALES = [
    "facebook.com", "instagram.com", "linkedin.com/in/", "tiktok.com",
    "x.com", "twitter.com",
]

# Sitios donde se buscan menciones con "site:", además de la búsqueda general.
# Marketplace va incluido en facebook.com; twitter.com cubre posts antiguos de X.
SITIOS_GENERALES = [
    "facebook.com", "instagram.com", "linkedin.com",
    "tiktok.com", "x.com", "twitter.com",
]
SITIOS_POR_REGION = {
    "DO": ["paginasamarillas.com.do", "corotos.com.do", "cybo.com"],
    "ES": ["paginasamarillas.es", "milanuncios.com"],
    "MX": ["seccionamarilla.com.mx"],
    "US": [
        "yelp.com", "yellowpages.com", "bbb.org", "manta.com",
        "superpages.com", "mapquest.com", "chamberofcommerce.com",
        "craigslist.org",
    ],
}
# Reportes de usuarios sobre llamadas de spam o fraude (varios países).
SITIOS_REPORTES_SPAM = [
    "tellows.com", "spamcalls.net", "shouldianswer.com", "800notes.com",
    "whocallsme.com", "callfilter.app", "nomorobo.com",
]
# Google ignora las palabras a partir de la 32: los site: se reparten en grupos.
MAX_SITIOS_POR_CONSULTA = 8
MAX_PAGINAS_A_VISITAR = 8

SERPAPI_URL = "https://serpapi.com/search.json"
# Peticiones enviadas en esta ejecución (SerpAPI no cobra todas: caché, errores...).
uso_serpapi = {"enviadas": 0}
# Requieren sesión o bloquean scraping: no se visitan, solo se usa el fragmento.
DOMINIOS_NO_VISITABLES = DOMINIOS_PERSONALES

TIPOS_LINEA = {
    PhoneNumberType.FIXED_LINE: "Línea fija",
    PhoneNumberType.MOBILE: "Móvil",
    PhoneNumberType.FIXED_LINE_OR_MOBILE: "Fija o móvil",
    PhoneNumberType.TOLL_FREE: "Línea gratuita",
    PhoneNumberType.PREMIUM_RATE: "Tarifa premium",
    PhoneNumberType.SHARED_COST: "Coste compartido",
    PhoneNumberType.VOIP: "VoIP",
    PhoneNumberType.PERSONAL_NUMBER: "Número personal",
    PhoneNumberType.PAGER: "Buscapersonas",
    PhoneNumberType.UAN: "Número de acceso universal",
    PhoneNumberType.VOICEMAIL: "Buzón de voz",
    PhoneNumberType.UNKNOWN: "Desconocido",
}


def variantes_numero(numero):
    """Genera los formatos en que el número suele aparecer escrito en la web."""
    nacional = str(numero.national_number)
    variantes = {
        phonenumbers.format_number(numero, phonenumbers.PhoneNumberFormat.E164),
        phonenumbers.format_number(
            numero, phonenumbers.PhoneNumberFormat.INTERNATIONAL
        ),
        phonenumbers.format_number(
            numero, phonenumbers.PhoneNumberFormat.NATIONAL
        ),
    }
    # Formatos NANP habituales: 829-890-5818 y 829.890.5818
    if numero.country_code == 1 and len(nacional) == 10:
        variantes.add(f"{nacional[:3]}-{nacional[3:6]}-{nacional[6:]}")
        variantes.add(f"{nacional[:3]}.{nacional[3:6]}.{nacional[6:]}")
    variantes.add(nacional)
    return sorted(variantes)


class BusquedaBloqueada(Exception):
    pass


def buscar_menciones_brave(consulta, clave):
    """Búsqueda vía Brave Search API (sin captchas). Requiere BRAVE_API_KEY."""
    try:
        r = requests.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": consulta, "count": MAX_RESULTADOS},
            headers={"X-Subscription-Token": clave, "Accept": "application/json"},
            timeout=12,
        )
        r.raise_for_status()
        return [
            {"titulo": x.get("title", ""), "url": x.get("url", ""),
             "fragmento": BeautifulSoup(x.get("description", ""),
                                        "html.parser").get_text()}
            for x in r.json().get("web", {}).get("results", [])
        ]
    except (requests.RequestException, ValueError) as error:
        print(f"[!] Brave Search: {error}")
        return []


def buscar_menciones(consulta):
    """Busca menciones públicas y devuelve sus títulos, enlaces y fragmentos."""
    url_base = "https://html.duckduckgo.com/html/"

    cabeceras = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
        )
    }

    resultados = []
    vistos = set()

    try:
        respuesta = requests.get(
            url_base,
            params={"q": consulta},
            headers=cabeceras,
            timeout=12
        )
        respuesta.raise_for_status()

        # DuckDuckGo devuelve 202 o una página "anomaly" cuando bloquea.
        if respuesta.status_code == 202 or "anomaly" in respuesta.text.lower():
            print(f"[!] El buscador bloqueó la consulta {consulta}.")
            raise BusquedaBloqueada()

        soup = BeautifulSoup(respuesta.text, "html.parser")

        for bloque in soup.select(".result"):
            enlace = bloque.select_one("a.result__a")
            fragmento = bloque.select_one(".result__snippet")

            if not enlace:
                continue

            titulo = enlace.get_text(" ", strip=True)
            url = enlace.get("href", "").strip()
            texto = (
                fragmento.get_text(" ", strip=True)
                if fragmento else ""
            )

            # Resolver enlaces de redireccionamiento del buscador.
            if url.startswith("//"):
                url = "https:" + url

            if url.startswith("/"):
                url = "https://duckduckgo.com" + url

            if "duckduckgo.com/l/" in url:
                destino = parse_qs(urlparse(url).query).get("uddg")
                if destino:
                    url = unquote(destino[0])

            # Solo aceptar destinos HTTP/HTTPS.
            if urlparse(url).scheme not in ("http", "https"):
                continue

            if url in vistos:
                continue

            vistos.add(url)
            resultados.append({
                "titulo": titulo,
                "url": url,
                "fragmento": texto
            })

            if len(resultados) >= MAX_RESULTADOS:
                break

    except BusquedaBloqueada:
        raise
    except requests.Timeout:
        print("[!] Tiempo de espera agotado en el buscador.")
    except requests.RequestException as error:
        print(f"[!] Error HTTP: {error}")
    except Exception as error:
        print(f"[!] Error al analizar resultados: {error}")

    return resultados


def formato_local(numero):
    """El formato local más habitual (829-890-5818 en NANP)."""
    nacional = str(numero.national_number)
    if numero.country_code == 1 and len(nacional) == 10:
        return f"{nacional[:3]}-{nacional[3:6]}-{nacional[6:]}"
    return phonenumbers.format_number(
        numero, phonenumbers.PhoneNumberFormat.NATIONAL
    )


def consulta_whatsapp(numero):
    """Enlaces de WhatsApp publicados en webs y anuncios."""
    e164 = phonenumbers.format_number(numero, phonenumbers.PhoneNumberFormat.E164)
    return f'"wa.me/{e164.lstrip("+")}"'


def consultas_para(numero):
    """Devuelve (consulta, etiqueta): todas las variantes y búsquedas por sitio."""
    consultas = [(f'"{v}"', v) for v in variantes_numero(numero)]

    # Para las búsquedas por sitio basta el formato local más habitual.
    local = formato_local(numero)
    region = phonenumbers.region_code_for_number(numero)
    for sitio in (SITIOS_GENERALES + SITIOS_POR_REGION.get(region, [])
                  + SITIOS_REPORTES_SPAM):
        consultas.append((f'"{local}" site:{sitio}', f"site:{sitio}"))

    consultas.append((consulta_whatsapp(numero), "wa.me"))
    return consultas


def consultas_serpapi(numero):
    """Agrupa las consultas con OR: Google entiende bien varios formatos a la vez."""
    variantes = " OR ".join(f'"{v}"' for v in variantes_numero(numero))
    region = phonenumbers.region_code_for_number(numero)
    local = formato_local(numero)
    consultas = [(variantes, "google:variantes")]
    for etiqueta, sitios in (
        ("google:redes", SITIOS_GENERALES),
        ("google:directorios", SITIOS_POR_REGION.get(region, [])),
        ("google:reportes_spam", SITIOS_REPORTES_SPAM),
    ):
        for i in range(0, len(sitios), MAX_SITIOS_POR_CONSULTA):
            grupo = " OR ".join(
                f"site:{s}" for s in sitios[i:i + MAX_SITIOS_POR_CONSULTA]
            )
            consultas.append((f'"{local}" ({grupo})', etiqueta))
    consultas.append((consulta_whatsapp(numero), "google:wa.me"))
    return consultas


def busquedas_por_numero(entrada):
    """Búsquedas de SerpAPI que gasta un número (dependen de las fuentes del país)."""
    try:
        numero = phonenumbers.parse(entrada, None)
    except phonenumbers.NumberParseException:
        return None
    return len(consultas_serpapi(numero)) + 1  # + la ficha de negocio


def _llamar_serpapi(parametros, clave):
    """Hace la petición a SerpAPI.

    Devuelve el JSON, {} si Google no tiene resultados o None si la búsqueda falló.
    """
    for intento in range(2):
        uso_serpapi["enviadas"] += 1
        try:
            r = requests.get(
                SERPAPI_URL, params={**parametros, "api_key": clave}, timeout=60
            )
            datos = r.json()
            break
        except requests.Timeout:
            # El reintento suele salir de la caché de SerpAPI, que no se cobra.
            print("[!] SerpAPI no respondió a tiempo; "
                  + ("reintentando..." if intento == 0 else "se omite la consulta."))
        except (requests.RequestException, ValueError) as error:
            print(f"[!] SerpAPI: {error}")
            return None
    else:
        return None
    # SerpAPI indica errores (sin créditos, clave mala...) en el campo "error".
    if datos.get("error"):
        if "hasn't returned any results" in datos["error"]:
            return {}
        print(f"[!] SerpAPI: {datos['error']}")
        return None
    return datos


def buscar_menciones_serpapi(consulta, clave, region):
    """Búsqueda en Google vía SerpAPI. Requiere SERPAPI_KEY."""
    datos = _llamar_serpapi(
        {"engine": "google", "q": consulta, "hl": "es",
         "gl": region.lower(), "num": MAX_RESULTADOS},
        clave,
    ) or {}
    return [
        {"titulo": x.get("title", ""), "url": x.get("link", ""),
         "fragmento": x.get("snippet", "")}
        for x in datos.get("organic_results", [])
        if x.get("link", "").startswith(("http://", "https://"))
    ]


def buscar_ficha_google(numero):
    """Ficha de negocio que Google asocia al número (la de Google Maps).

    El motor google_maps de SerpAPI no encuentra negocios por teléfono; una
    búsqueda normal del número sin comillas sí devuelve su ficha
    (knowledge_graph) o un bloque de sitios locales (local_results).
    """
    clave = os.getenv("SERPAPI_KEY")
    if not clave:
        return []
    region = phonenumbers.region_code_for_number(numero)
    datos = _llamar_serpapi(
        {"engine": "google", "q": formato_local(numero), "hl": "es",
         "gl": region.lower()},
        clave,
    )
    if datos is None:
        print("[!] Ficha de Google: la búsqueda falló, no se pudo comprobar.")
        return []

    lugares = datos.get("local_results") or []
    if isinstance(lugares, dict):
        lugares = lugares.get("places", [])
    if datos.get("knowledge_graph"):
        lugares = [datos["knowledge_graph"], *lugares]

    # Con hl=es algunos campos llegan traducidos ("teléfono", "dirección").
    campo = lambda l, *claves: next((l[c] for c in claves if l.get(c)), None)
    patron = patron_numero(numero)
    fichas = []
    for l in lugares:
        telefono = campo(l, "phone", "teléfono")
        # Google puede mostrar negocios parecidos: solo vale el mismo teléfono.
        if not (telefono and patron.search(telefono)):
            continue
        fichas.append({
            "nombre": l.get("title"), "tipo": l.get("type"),
            "direccion": campo(l, "address", "dirección"), "telefono": telefono,
            "web": campo(l, "website", "sitio_web", "sitio web"),
            "valoracion": l.get("rating"), "resenas": l.get("review_count"),
            "place_id": l.get("place_id"),
        })
    if not fichas:
        print("[i] Ficha de Google: no hay un negocio con este número.")
    return fichas


def estado_cuenta_serpapi():
    """Búsquedas que quedan este mes. Consultar la cuenta no gasta búsquedas."""
    clave = os.getenv("SERPAPI_KEY")
    if not clave:
        return None
    try:
        r = requests.get(
            "https://serpapi.com/account.json", params={"api_key": clave},
            timeout=15,
        )
        datos = r.json()
    except (requests.RequestException, ValueError):
        return None
    if datos.get("error"):
        print(f"[!] SerpAPI: {datos['error']}")
        return None
    restantes = datos.get("total_searches_left", datos.get("plan_searches_left"))
    if restantes is None:
        return None
    return {
        "busquedas_por_mes": datos.get("searches_per_month"),
        "usadas_este_mes": datos.get("this_month_usage"),
        "busquedas_restantes": restantes,
    }


def mostrar_cuota(cuota, por_numero):
    if not cuota:
        return
    linea = f"[i] SerpAPI: {cuota['busquedas_restantes']} búsquedas restantes este mes"
    if por_numero:
        linea += (f" → quedan ~{cuota['busquedas_restantes'] // por_numero} números "
                  f"de este país ({por_numero} búsquedas por número)")
    print(linea + ".")


def buscar_todas_las_variantes(numero):
    """Lanza todas las consultas y fusiona resultados sin duplicados.

    Prioridad: SerpAPI (SERPAPI_KEY) > Brave (BRAVE_API_KEY) > DuckDuckGo.
    """
    clave_serpapi = os.getenv("SERPAPI_KEY")
    clave_brave = os.getenv("BRAVE_API_KEY")
    region = phonenumbers.region_code_for_number(numero)
    consultas = (consultas_serpapi(numero) if clave_serpapi
                 else consultas_para(numero))
    resultados = {}
    for i, (consulta, etiqueta) in enumerate(consultas):
        if clave_serpapi:
            encontrados = buscar_menciones_serpapi(consulta, clave_serpapi, region)
        elif clave_brave:
            encontrados = buscar_menciones_brave(consulta, clave_brave)
        else:
            if i:
                time.sleep(PAUSA_ENTRE_BUSQUEDAS)
            try:
                encontrados = buscar_menciones(consulta)
            except BusquedaBloqueada:
                print("[!] Se omiten las demás consultas. Define SERPAPI_KEY "
                      "o BRAVE_API_KEY para evitar bloqueos.")
                break
        for r in encontrados:
            r_existente = resultados.setdefault(r["url"], {**r, "variantes": []})
            r_existente["variantes"].append(etiqueta)
    return list(resultados.values())


def patron_numero(numero):
    """Regex que encuentra el número con cualquier separador (espacios, guiones...)."""
    digitos = str(numero.national_number)
    return re.compile(
        r"(?<!\d)(?:\+?" + str(numero.country_code) + r"[\s\-.()]*)?"
        + r"[\s\-.()]*".join(digitos) + r"(?!\d)"
    )


def coincide_dominio(url, dominios):
    """Compara por dominio real: evita que "yandex.com" cuente como "x.com"."""
    partes = urlparse(url.lower())
    host = partes.netloc.split(":")[0].removeprefix("www.")
    ruta_url = partes.path.lstrip("/")
    for dominio in dominios:
        nombre, _, ruta = dominio.partition("/")
        if ((host == nombre or host.endswith("." + nombre))
                and ruta_url.startswith(ruta)):
            return True
    return False


def numero_en_fragmento(mencion, patron):
    return bool(patron.search(f"{mencion['titulo']} {mencion['fragmento']}"))


def separar_menciones(menciones, numero):
    """Separa las menciones que contienen el número de las que no.

    Google devuelve coincidencias parciales (mismo prefijo, palabras sueltas):
    solo cuentan si el número aparece en el fragmento o en la página visitada.
    """
    patron = patron_numero(numero)
    confirmadas, descartadas = [], []
    for m in menciones:
        m["numero_en_fragmento"] = numero_en_fragmento(m, patron)
        en_pagina = m.get("pagina", {}).get("numero_confirmado", False)
        (confirmadas if m["numero_en_fragmento"] or en_pagina
         else descartadas).append(m)
    return confirmadas, descartadas


def _datos_jsonld(soup):
    """Extrae nombre, dirección y teléfono de bloques schema.org (JSON-LD)."""
    fichas = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            datos = json.loads(script.string or "")
        except ValueError:
            continue
        pendientes = datos if isinstance(datos, list) else [datos]
        while pendientes:
            nodo = pendientes.pop()
            if not isinstance(nodo, dict):
                continue
            pendientes.extend(nodo.get("@graph", []))
            tipo = nodo.get("@type", "")
            tipo = " ".join(tipo) if isinstance(tipo, list) else str(tipo)
            if not nodo.get("name") or not (nodo.get("telephone") or nodo.get("address")):
                continue
            direccion = nodo.get("address")
            if isinstance(direccion, dict):
                direccion = ", ".join(
                    str(direccion[k]) for k in
                    ("streetAddress", "addressLocality", "addressRegion",
                     "addressCountry")
                    if direccion.get(k)
                )
            fichas.append({
                "tipo_schema": tipo,
                "nombre": nodo.get("name"),
                "direccion": direccion,
                "telefono": nodo.get("telephone"),
            })
    return fichas


def enriquecer_mencion(mencion, patron):
    """Visita la página: confirma el número y extrae datos estructurados."""
    url = mencion["url"]
    if coincide_dominio(url, DOMINIOS_NO_VISITABLES):
        mencion["pagina"] = {"visitada": False, "motivo": "requiere sesión"}
        return mencion

    try:
        r = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"},
            timeout=12,
        )
        r.raise_for_status()
    except requests.RequestException as error:
        mencion["pagina"] = {"visitada": False, "motivo": str(error)[:120]}
        return mencion

    soup = BeautifulSoup(r.text, "html.parser")
    fichas = _datos_jsonld(soup)
    for etiqueta in soup(["script", "style"]):
        etiqueta.decompose()
    texto = soup.get_text(" ", strip=True)

    meta = lambda prop: (soup.find("meta", property=prop) or {}).get("content")
    coincidencia = patron.search(texto)
    mencion["pagina"] = {
        "visitada": True,
        # Si el número no está en la página, el resultado es poco fiable.
        "numero_confirmado": bool(coincidencia),
        "contexto": (
            texto[max(0, coincidencia.start() - 150):coincidencia.end() + 150]
            if coincidencia else None
        ),
        "titulo_pagina": soup.title.get_text(strip=True) if soup.title else None,
        "og_site_name": meta("og:site_name"),
        "og_title": meta("og:title"),
        "fichas_schema": fichas,
    }
    return mencion


def enriquecer_menciones(menciones, numero):
    patron = patron_numero(numero)
    # Se visitan primero las que ya muestran el número en el fragmento.
    candidatas = sorted(menciones, key=lambda m: not numero_en_fragmento(m, patron))
    for mencion in candidatas[:MAX_PAGINAS_A_VISITAR]:
        enriquecer_mencion(mencion, patron)
    return menciones


def clasificar_titular(menciones, fichas_google=()):
    """Estima si el número pertenece a una empresa o a un particular.

    Es una heurística sobre texto público: devuelve la evidencia para que
    la conclusión se pueda revisar a mano.
    """
    puntos_empresa, puntos_persona, evidencia = 0, 0, []

    for m in menciones:
        texto = f"{m['titulo']} {m['fragmento']}".lower()
        url = m["url"].lower()

        claves = [k for k in INDICADORES_EMPRESA
                  if re.search(rf"(?<!\w){re.escape(k)}(?!\w)", texto)]
        if claves:
            puntos_empresa += len(claves)
            evidencia.append(f"Palabras de negocio {claves} en {m['url']}")

        if any(d in url for d in DOMINIOS_DIRECTORIO):
            puntos_empresa += 3
            evidencia.append(f"Aparece en directorio comercial: {m['url']}")

        pagina = m.get("pagina", {})
        for ficha in pagina.get("fichas_schema", []):
            if re.search(r"Organization|Business|Store|Restaurant|Corporation",
                         ficha["tipo_schema"]):
                puntos_empresa += 5
                evidencia.append(
                    f"Ficha schema.org {ficha['tipo_schema']}: "
                    f"{ficha['nombre']} ({ficha['direccion'] or 'sin dirección'})"
                )

        if coincide_dominio(url, DOMINIOS_PERSONALES):
            puntos_persona += 1
            evidencia.append(f"Aparece en red social: {m['url']}")

        # No indica empresa ni particular, pero conviene verlo.
        if coincide_dominio(url, SITIOS_REPORTES_SPAM):
            evidencia.append(f"Reportado en sitio de llamadas spam: {m['url']}")

    # Una ficha de Google con este mismo teléfono es la señal más fuerte.
    for ficha in fichas_google:
        puntos_empresa += 10
        evidencia.append(
            f"Ficha de Google: {ficha['nombre']} ({ficha['tipo'] or 'sin tipo'}) "
            f"— {ficha['direccion'] or 'sin dirección'}"
        )

    if puntos_empresa == puntos_persona == 0:
        conclusion = "Indeterminado"
    elif puntos_empresa >= puntos_persona * 2:
        conclusion = "Probablemente empresa"
    elif puntos_persona > puntos_empresa:
        conclusion = "Probablemente particular"
    else:
        conclusion = "Indeterminado"

    return {
        "conclusion": conclusion,
        "puntos_empresa": puntos_empresa,
        "puntos_persona": puntos_persona,
        "evidencia": evidencia,
    }


def consultar_numverify(e164):
    """Operador y tipo de línea actualizados (opcional, NUMVERIFY_API_KEY)."""
    clave = os.getenv("NUMVERIFY_API_KEY")
    if not clave:
        return None
    try:
        r = requests.get(
            "http://apilayer.net/api/validate",
            params={"access_key": clave, "number": e164.lstrip("+")},
            timeout=12,
        )
        r.raise_for_status()
        datos = r.json()
        return {k: datos.get(k) for k in
                ("carrier", "line_type", "location", "country_name")}
    except (requests.RequestException, ValueError) as error:
        print(f"[!] Numverify: {error}")
        return None


def consultar_twilio(e164):
    """Tipo de línea real y CNAM (solo EE. UU.) vía Twilio Lookup v2.

    Opcional: requiere TWILIO_ACCOUNT_SID y TWILIO_AUTH_TOKEN.
    """
    sid, token = os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN")
    if not (sid and token):
        return None
    try:
        r = requests.get(
            f"https://lookups.twilio.com/v2/PhoneNumbers/{e164}",
            params={"Fields": "line_type_intelligence,caller_name"},
            auth=(sid, token),
            timeout=12,
        )
        r.raise_for_status()
        datos = r.json()
        return {
            "tipo_linea": datos.get("line_type_intelligence"),
            "nombre_cnam": datos.get("caller_name"),
        }
    except (requests.RequestException, ValueError) as error:
        print(f"[!] Twilio: {error}")
        return None


def analizar_numero(entrada):
    """Analiza metadatos técnicos y menciones web públicas."""
    try:
        numero = phonenumbers.parse(entrada.strip(), None)
    except phonenumbers.NumberParseException as error:
        raise ValueError(f"No se pudo interpretar el número: {error}")

    if not phonenumbers.is_valid_number(numero):
        raise ValueError("El número no es válido según la biblioteca.")

    e164 = phonenumbers.format_number(
        numero, phonenumbers.PhoneNumberFormat.E164
    )

    tipo = phonenumbers.number_type(numero)

    menciones, descartadas = separar_menciones(
        enriquecer_menciones(buscar_todas_las_variantes(numero), numero), numero
    )

    reporte = {
        "fecha_utc": datetime.now(utc_timezone.utc).isoformat(),
        "numero_e164": e164,
        "formato_internacional": phonenumbers.format_number(
            numero, phonenumbers.PhoneNumberFormat.INTERNATIONAL
        ),
        "region_numeracion": geocoder.description_for_number(
            numero, "es"
        ) or "No disponible",
        "codigo_region": phonenumbers.region_code_for_number(numero),
        "codigo_pais": numero.country_code,
        "posible_numero_valido": phonenumbers.is_possible_number(numero),
        "numero_valido": phonenumbers.is_valid_number(numero),
        "tipo_linea": TIPOS_LINEA.get(tipo, "Desconocido"),
        "operador_referencial": carrier.name_for_number(
            numero, "es"
        ) or "No disponible",
        "zonas_horarias": list(timezone.time_zones_for_number(numero)),
        "variantes_buscadas": variantes_numero(numero),
        "menciones_publicas": menciones,
        # Resultados del buscador que no contienen el número: no se clasifican.
        "menciones_descartadas": descartadas,
        "ficha_google": (fichas_google := buscar_ficha_google(numero)),
        "clasificacion_titular": clasificar_titular(menciones, fichas_google),
        "numverify": consultar_numverify(e164),
        "twilio_lookup": consultar_twilio(e164),
        "limitaciones": [
            "La región telefónica no confirma la residencia.",
            "El operador puede no reflejar la portabilidad.",
            "Las menciones web no verifican la identidad del titular.",
            "No se consultan bases privadas ni perfiles restringidos.",
            "No se obtienen fotografías privadas ni se infieren identidades."
        ]
    }

    return reporte


def main():
    print("=" * 56)
    print("       OSINT TELEFÓNICO — GHOST DATA")
    print("=" * 56)

    entrada = input("Número internacional (ej. +1809...): ").strip()

    if not entrada:
        print("[!] Debes introducir un número.")
        sys.exit(1)

    try:
        cuota = estado_cuenta_serpapi()
        por_numero = busquedas_por_numero(entrada)
        mostrar_cuota(cuota, por_numero)
        if cuota and cuota["busquedas_restantes"] < (por_numero or 0):
            print("[!] No quedan búsquedas de SerpAPI este mes; "
                  "se usará Brave o DuckDuckGo.")
            os.environ.pop("SERPAPI_KEY")

        reporte = analizar_numero(entrada)

        print("\n--- METADATOS TELEFÓNICOS ---")
        print(f"Número: {reporte['numero_e164']}")
        print(f"Región: {reporte['region_numeracion']}")
        print(f"Código regional: {reporte['codigo_region']}")
        print(f"Tipo de línea: {reporte['tipo_linea']}")
        print(f"Operador referencial: {reporte['operador_referencial']}")
        print(f"Zonas horarias: {', '.join(reporte['zonas_horarias'])}")

        for fuente in ("numverify", "twilio_lookup"):
            if reporte[fuente]:
                print(f"{fuente}: {reporte[fuente]}")

        print("\n--- MENCIONES PÚBLICAS ---")
        resultados = reporte["menciones_publicas"]

        if resultados:
            for i, resultado in enumerate(resultados, 1):
                print(f"\n{i}. {resultado['titulo']}")
                print(f"   URL: {resultado['url']}")
                print(f"   Texto: {resultado['fragmento'] or 'Sin fragmento'}")
                print(f"   Encontrado con: {', '.join(resultado['variantes'])}")
                if resultado["numero_en_fragmento"]:
                    print("   Fragmento: contiene el número")
                pagina = resultado.get("pagina")
                if pagina and pagina["visitada"]:
                    estado = ("número confirmado" if pagina["numero_confirmado"]
                              else "número NO encontrado en la página")
                    print(f"   Página: {estado}")
                    for ficha in pagina["fichas_schema"]:
                        print(f"   Ficha: {ficha['nombre']} | "
                              f"{ficha['direccion'] or '-'} | {ficha['telefono'] or '-'}")
        else:
            print("No se encontraron menciones que contengan el número.")

        if reporte["menciones_descartadas"]:
            print(f"\n[i] {len(reporte['menciones_descartadas'])} resultados "
                  "descartados: el número no aparece en ellos "
                  "(detalle en el JSON, 'menciones_descartadas').")

        if reporte["ficha_google"]:
            print("\n--- FICHA DE NEGOCIO EN GOOGLE ---")
            for ficha in reporte["ficha_google"]:
                print(f"{ficha['nombre']} | {ficha['tipo'] or '-'} | "
                      f"{ficha['direccion'] or '-'} | {ficha['web'] or '-'}")

        clasif = reporte["clasificacion_titular"]
        print("\n--- ¿EMPRESA O PARTICULAR? ---")
        print(f"Conclusión: {clasif['conclusion']}")
        for linea in clasif["evidencia"]:
            print(f"   - {linea}")

        ARCHIVO_REPORTE.write_text(
            json.dumps(reporte, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )

        print(f"\n[+] Reporte guardado en: {ARCHIVO_REPORTE.resolve()}")
        if cuota:
            cuota_final = estado_cuenta_serpapi()
            mostrar_cuota(cuota_final, por_numero)
            if cuota_final:
                gastadas = (cuota["busquedas_restantes"]
                            - cuota_final["busquedas_restantes"])
                print(f"[i] SerpAPI: {uso_serpapi['enviadas']} búsquedas enviadas, "
                      f"{gastadas} descontadas de la cuota (las búsquedas en caché "
                      "o fallidas no siempre se cobran).")

    except (ValueError, OSError) as error:
        print(f"\n[!] {error}")
        sys.exit(1)


if __name__ == "__main__":
    main()
