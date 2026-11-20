# restep (Python)

Cliente genérico para APIs [Restep 1](https://restep.wubytes.com/es/1/), guiado por el
documento de descubrimiento (§4.2). Python 3.10 o posterior, solo biblioteca estándar
(urllib). La versión asíncrona usa [httpx](https://www.python-httpx.org/) si está instalado;
no es una dependencia.

No genera código: lee el descubrimiento al conectar y con él sabe qué colecciones hay, qué
métodos admiten, por qué campos se filtra y ordena, con qué operadores y de qué tipo. Lo que el
descubrimiento dice que el servidor rechazaría se rechaza antes de enviarlo, con la misma
excepción que daría el servidor.

## Uso

```python
import restep

api = restep.connect("http://localhost:8080/", token=None, language="es")
api.profiles            # frozenset({'core', 'query', 'writes'})
api.collections         # {'books': <restep.Collection books /books>, ...}

books = api["books"]
page = books.list(filter={"price": {"le": "20"}, "category": ["novela-negra", "poesia"]},
                  sort="-rating", fields=["title", "price"], limit=10, count=True)
page.items, page.total, page.total.estimated
page = page.next_page()            # None en la última; sigue next tal cual (§5.2)

for book in books.iter(filter={"year": {"ge": 2023}}, limit=50):   # todas las páginas
    ...
books.count(filter={"rating": {"null": True}})                      # Total(3, estimated=False)

book = books.get("7")              # lleva su ETag de elemento en book["_etag"] (§7.2)
new = books.create({"title": "...", "price": "9.90", "category": "poesia"})
new = books.update(new["id"], {"price": "10.50"}, if_match=new)     # If-Match con su _etag
books.delete(new["id"], if_match=new)

api["orders"].action("2001", "cancel")                 # POST /orders/2001:cancel (§10.1)
api["orders"].action(None, "import", {"rows": [...]})  # acción de colección
api["reviews"].at(id="7").list()                       # subcolección /books/{id}/reviews
api.status()                                           # True/False según el status declarado (§4.4)
```

### `restep.connect(url, token=None, language=None, *, ...)`

| Opción | Qué hace |
|---|---|
| `token` | `Authorization: Bearer`, solo hacia el origen de la API (§4.3) |
| `language` | `Accept-Language` para los textos de la API: errores y descripciones (§5.6) |
| `headers` | Cabeceras adicionales en cada petición |
| `validate=True` | Comprueba parámetros, métodos y acciones contra el descubrimiento antes de enviar |
| `retries=0` | Reintentos ante `429` y `503` (abajo) |
| `max_retry_wait=60` | Si `Retry-After` pide esperar más, no se reintenta |
| `timeout=30` | Segundos por petición |

Si la raíz no es un documento de descubrimiento, busca en sus enlaces `rel="service-desc"`,
primero los de tipo JSON (§4.2). Si no encuentra ninguno, supone solo `core`: `api.discovery`
es `None` y `api.collection("things")` (o `api.collection("x", path="/ruta")`) da una colección
sin declaraciones.

### Filtros

`filter` es un diccionario campo → condición (§6.1):

| Python | Petición |
|---|---|
| `{"category": "poesia"}` | `filter[category]=poesia` |
| `{"price": {"le": "20", "ge": 10}}` | `filter[price][le]=20&filter[price][ge]=10` |
| `{"id": ["7", "12"]}` o `{"id": {"in": ["7", "12"]}}` | `filter[id][in]=7,12` |
| `{"rating": {"null": True}}` | `filter[rating][null]=true` |
| `{"name.es-mx": "Separador"}` | `filter[name.es-mx]=Separador` |

Los valores se convierten según el tipo declarado: `Decimal`, `int` y `float` para `decimal`,
`number` e `integer`; `date`, `datetime` con zona horaria y `timedelta` para `date`,
`datetime` y `duration`; `bool` solo para `boolean`. Una cadena también vale si tiene la forma
del tipo. Un `None` es un error (para «sin valor», `{"null": True}`).

`sort`, `fields` e `include` aceptan una cadena con comas o una lista.

### Validación local

Con `validate=True` (por defecto), antes de enviar:

- `UnsupportedParameter`: un perfil que el servidor no anuncia (`query`, `relations`), un campo
  u operador de filtro, un campo de orden o una relación no declarados, una ruta de `include`
  más honda que `include_depth`, `count` en una colección que no lo declara (§5.3).
- `InvalidParameter`: un valor que no encaja con el tipo, un `enum` fuera de `values`, una
  lista `in` vacía o con comas, un `limit` fuera de `1..max_limit`, un campo de `fields` que no
  está en el esquema `item` (si viene incrustado), una ruta que entra en un mapa de idioma
  (`name.es`), una ruta de relación que no está en `include`.
- `MethodNotAllowed` (405): un método que `methods` o `item_methods` no declaran (§4.3).
- `NotFound` (404): una acción que la colección no declara, o sin la extensión `actions`.
- `Invalid` (422): un `create` que trae `id` (§5.1).
- `ValueError`/`TypeError`: un id que no es de Restep (§3), una variable de ruta que falta, una
  acción de elemento sin id.

Estos errores llevan `local = True`, y `problem`, `parameter` y `detail` como los del servidor,
así que el mismo `except` sirve para los dos. Con `validate=False` todo va al servidor.

### Errores

Cada respuesta `4xx` o `5xx` se convierte en una excepción a partir de su problem+json (§5.4):

```python
try:
    books.list(filter={"nope": 1})
except restep.UnsupportedParameter as e:   # no reintentes: la API no ofrece eso
    e.status, e.code, e.parameter, e.detail, e.errors, e.reason, e.problem, e.headers
except restep.InvalidParameter as e:       # corrige el valor y reintenta
    ...
```

Jerarquía: `RestepError` → `ClientError` (4xx) y `ServerError` (5xx). Una clase por `code` de
la tabla de §5.4: `BadRequest` (y sus hijas `InvalidParameter`, `UnsupportedParameter`,
`IdempotencyKeyRequired`), `Unauthenticated` (con `resource_metadata` del reto),
`Forbidden`, `NotFound`, `MethodNotAllowed` (con `allow`), `NotAcceptable`, `Conflict`,
`Gone` (`StateExpired`, `Retired`), `PreconditionFailed`, `ContentTooLarge`, `UriTooLong`,
`UnsupportedMediaType`, `Invalid`, `IdempotencyMismatch`, `NotExecuted`,
`PreconditionRequired`, `RateLimited`, `Internal`, `ServerNotImplemented`, `Unavailable`.
Un `code` desconocido se trata según su estado (§5.4): la clase del estado si solo hay una, si
no `ClientError` o `ServerError`. `e.retry_after` da los segundos de `Retry-After`.

Una respuesta con `Deprecation` (§5.5) emite un `restep.RestepDeprecationWarning`.

### Escrituras

- `create` envía una `Idempotency-Key` nueva (un UUID, como cadena de Structured Fields) si el
  servidor anuncia `writes` (§7.1). `idempotency_key="..."` usa la tuya; `None`, ninguna. Los
  reintentos de una misma llamada llevan la misma clave.
- Las acciones declaradas `"idempotent": false` llevan clave igual que `create`; las
  idempotentes, no. Una acción con `"method": "GET"` va por `GET` y no admite cuerpo.
- `update` usa `application/merge-patch+json` (§5.1). `if_match` acepta la ETag (con o sin
  comillas) o el elemento leído, del que toma `_etag`.
- `get` sin `fields` ni `include`, `create` y `update` copian la cabecera `ETag` en `_etag`
  cuando es la ETag de elemento (fuerte y sin codificación de contenido, §7.2), así que el
  elemento devuelto sirve tal cual como `if_match`.

### Reintentos

Con `retries=n`, un `429` se repite siempre (no se atendió) y un `503` solo si repetirlo no
puede duplicar el efecto: `GET`, `PATCH`, `DELETE`, un `POST` con `Idempotency-Key` o una acción
idempotente. Espera lo que diga `Retry-After` (segundos o fecha HTTP) o, sin él, 0,5 s, 1 s,
2 s... Por defecto no reintenta.

### OAuth (§10.5)

```python
try:
    api = restep.connect(url)
except restep.Unauthenticated as e:
    e.resource_metadata                     # URL de los metadatos, del reto del 401
meta = restep.oauth_metadata(url)           # metadatos RFC 9728, comprobando resource
restep.oauth_metadata_url("https://example.com/api/")
# 'https://example.com/.well-known/oauth-protected-resource/api/'
```

El cliente no obtiene tokens: eso es cosa de la biblioteca OAuth que uses. Con el token,
`restep.connect(url, token=...)`.

### Asíncrono

```python
import restep.aio

async with await restep.aio.connect(url, language="es") as api:
    page = await api["books"].list(limit=10)
    async for book in api["books"].iter():
        ...
    await api["books"].update("7", {"price": "19.00"}, if_match=book)
```

La misma API, con `await` y `async for`. Necesita httpx (`pip install httpx`, o el extra
`restep[async]`); sin él, `restep.aio.connect` da `ImportError` salvo que le pases `client=`,
cualquier objeto con la forma de `httpx.AsyncClient` (`async request(method, url, headers=,
content=)` que devuelve algo con `status_code`, `headers` y `content`).

## Pruebas

```bash
python3 tools/restep-py/test.py      # desde restep/
```

Arranca el servidor de referencia en el puerto 8890 y, con `RESTEP_TOKEN`, en el 8891, y dos
servidores de pega en Python en el 8892 y el 8893 para lo que el de referencia no tiene
(acciones, subcolecciones, fallos `429`/`503`, `Deprecation`, `service-desc` y un servidor
solo core). Las comprobaciones de validación local piden lo mismo con `validate=True` y con
`validate=False` y exigen la misma clase de error y el mismo `parameter` del cliente y del
servidor. La parte asíncrona se prueba con un cliente de pega con la forma de httpx; si httpx
está instalado, también con él.

## Limitaciones

- No valida los cuerpos contra los esquemas `create`/`update` (haría falta un validador de
  JSON Schema); de eso se encarga el servidor con su `422 invalid`. Tampoco descarga esquemas
  enlazados: la comprobación de `fields` solo usa un esquema `item` incrustado.
- No implementa `sync` (`:changes`, eventos), `batch`, `filter-expr` ni `query-method`, ni
  `Prefer: return=minimal`.
- La rama de httpx real no se ha probado aquí (httpx no está instalado en esta máquina); la
  lógica asíncrona sí, con el cliente de pega.
- Los tipos son `dict[str, Any]`: el cliente es dinámico. Para tipos por colección está
  `restep-ts` (TypeScript).

## Dudas sobre la spec

- **¿Un `503` o un `429` garantizan que no hubo efecto?** La tabla de §5.4 no lo dice. El
  cliente supone que un `429` no se atendió y solo repite un `503` cuando repetir es seguro de
  todos modos; convendría que la spec dijera si `rate_limited` y `unavailable` implican «sin
  efecto».
- **Acción no declarada.** §10.1 dice cómo se declaran las acciones pero no qué estado da
  `POST /coleccion/7:nombre` con un nombre no declarado (`404`, `405`, `400`?). El servidor de
  referencia da `404`; el cliente usa `NotFound` en local por coherencia.
- **`fields` con `id` o miembros `_`.** §6.3 dice que `id` y los `_*` van siempre, pero no si
  `fields=id` es válido o un `invalid_parameter`. El cliente lo deja pasar.
- **`include` anidado en una relación a varias colecciones** (`to` como lista): §8 no dice cómo
  se valida `include=a.b` cuando `a` puede apuntar a colecciones con relaciones distintas. El
  cliente lo rechaza en local.
- **Enum y descubrimiento desfasado.** §3 pide tolerar valores de enumeración nuevos; un filtro
  `enum` con un valor fuera de `values` se rechaza en local porque el descubrimiento se lee al
  conectar, pero un cliente que guardara el descubrimiento mucho tiempo podría rechazar un
  valor recién añadido. Quizá merezca una nota sobre cuánto puede cachearse el descubrimiento.
- **`PATCH` y reintentos.** Un merge patch es idempotente en efecto, pero la spec no lo afirma
  ni dice si un `PATCH` repetido tras un `503` es seguro sin `If-Match`. El cliente lo trata
  como repetible.

## Código y avisos

Código y avisos: <https://github.com/restep-spec/restep-py>. Ese repositorio se genera desde la web de
Restep, donde la herramienta se desarrolla junto a la [especificación](https://restep.wubytes.com/1/) y el servidor de
referencia: `vendor/` y `schema/` son copias de la web para que las pruebas funcionen solas.

Licencia MIT.
