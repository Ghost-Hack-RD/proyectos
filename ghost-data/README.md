# Ghost Data

Herramienta OSINT en Python para analizar números de teléfono a partir de **información pública**: metadatos de numeración, menciones indexadas en Google y fichas de negocio.

## Qué hace

- **Metadatos:** país, región, tipo de línea y zona horaria (con [`phonenumbers`](https://github.com/daviddrysdale/python-phonenumbers)).
- **Menciones públicas:** busca el número en varios formatos en Google, redes sociales (Facebook, Instagram, LinkedIn, TikTok, X), enlaces `wa.me` y:
  - **Directorios por país:** RD (Páginas Amarillas, Corotos, Cybo), EE. UU. (Yelp, Yellow Pages, BBB, Manta, Superpages, MapQuest, Chamber of Commerce, Craigslist), España y México.
  - **Reportes de spam:** Tellows, SpamCalls, ShouldIAnswer, 800notes, WhoCallsMe, CallFilter, Nomorobo.
- **Verificación:** descarta los resultados en los que el número no aparece realmente y visita las páginas para confirmarlo.
- **Ficha de negocio:** detecta si Google asocia el número a un negocio (nombre, tipo, dirección, valoración).
- **Clasificación:** estima si el número es de una empresa o de un particular, mostrando la evidencia.
- **Reporte:** guarda todo en `reporte_telefonico.json`.

## Instalación

```bash
git clone https://github.com/Ghost-Hack-RD/proyectos.git
cd proyectos/ghost-data
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Claves de API (opcionales)

Las claves se leen de variables de entorno, nunca del código:

| Variable | Servicio | Para qué |
|---|---|---|
| `SERPAPI_KEY` | [SerpApi](https://serpapi.com) | Búsquedas en Google y ficha de negocio (recomendada; plan gratuito de 250 búsquedas/mes, unas 6 por número) |
| `BRAVE_API_KEY` | [Brave Search API](https://brave.com/search/api/) | Alternativa a SerpApi |
| `NUMVERIFY_API_KEY` | [Numverify](https://numverify.com) | Operador y tipo de línea |
| `TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN` | [Twilio Lookup](https://www.twilio.com/lookup) | Tipo de línea real y CNAM (EE. UU.) |

Sin claves, usa DuckDuckGo, que puede bloquear las consultas.

```bash
echo 'export SERPAPI_KEY="tu_clave"' >> ~/.zshrc   # o ~/.bashrc
source ~/.zshrc
```

## Uso

```bash
python ghost_data.py
```

Introduce el número en formato internacional, por ejemplo `+18095551234`.

## Limitaciones

- La región de numeración no confirma dónde vive el titular.
- El operador puede no reflejar la portabilidad.
- Solo encuentra el número si está **escrito** en contenido público indexado; no identifica cuentas privadas vinculadas a un número.
- Una mención web no verifica la identidad del titular.

## Uso responsable

Herramienta pensada para verificar números desconocidos, detectar spam o fraude y para investigación OSINT con fines legítimos. Respeta las leyes de protección de datos de tu país y los términos de servicio de cada plataforma. No la uses para acosar, vigilar o perfilar a personas.
