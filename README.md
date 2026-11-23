# restep (Python)

Cliente genérico para APIs [Restep 1](https://restep.wubytes.com/es/1/) (borrador 6), guiado
por el documento de descubrimiento (§4.2). Python 3.10 o posterior, solo biblioteca estándar
(urllib). La versión asíncrona usa [httpx](https://www.python-httpx.org/) si está instalado;
no es una dependencia.

```bash
pip install restep-py            # se importa como `restep`
pip install "restep-py[async]"   # con httpx
```

No genera código: lee el descubrimiento al conectar y con él sabe qué colecciones hay, qué
métodos admiten, por qué campos se filtra y ordena, con qué operadores y de qué tipo. Lo que el
descubrimiento dice que el servidor rechazaría se rechaza antes de enviarlo, con la misma
excepción que daría el servidor.

## Uso

```python
import restep

api = restep.connect("http://localhost:8080/", token=None, language="es")
api.profiles            # frozenset({'core', 'query', 'writes', 'cache'})
api.version             # '1.0-draft.6'
api.pre_release         # 'draft.6': un borrador, sin las garantías de una versión publicada (§1.1)
api.collections         # {'books': <restep.Collection books /books>, ...}
api.singletons          # {'shop': <restep.Singleton shop /shop>} (§5.9)
api.deprecations        # avisos de retirada, con announced y sunset (§5.5)

books = api["books"]
page = books.list(filter={"price": {"le": "20"}, "category": ["novela-negra", "poesia"]},
                  sort="-rating", fields=["title", "price"], limit=10, count=True)
page.items, page.total, page.total.estimated
page = page.next_page()            # None en la última; sigue next tal cual (§5.2)

for book in books.iter(filter={"year": {"ge": 2023}}, limit=50):   # todas las páginas
    ...
books.count(filter={"rating": {"null": True}})                      # Total(3, estimated=False)
books.list(named={"featured": True, "search": "niebla"})           # filtros con nombre (§6.1), sin operadores

book = books.get("7")              # lleva su ETag de elemento en book["_etag"] (§7.2)
new = books.create({"title": "...", "price": "9.90", "category": "poesia"})
new = books.update(new["id"], {"price": "10.50"}, if_match=new)     # If-Match con su _etag
books.delete(new["id"], if_match=new)

api["orders"].action("2001", "cancel")                 # POST /orders/2001:cancel (§11.1)
api["orders"].action(None, "import", {"rows": [...]})  # acción de colección
api["reviews"].at(book_id="7").list()                  # subcolección /books/{book_id}/reviews
api["reviews"].at(book_id="7").ref("3")                # {"id": "3", "href": "/books/7/reviews/3"} (§5.1)
api["categories"].create({"id": "papeleria", ...})     # id elegido por el cliente (id_from, §5.1)
api["categories"].collation("name")                    # "server" o "codepoint" (§6.1)
api["products"].inherited(item, "tax_rate")            # "own", {"href": …}, "default", "hidden" (§11.6)
shop = api.singletons["shop"].get()                    # singleton (§5.9), con _etag
api.singletons["shop"].update({"phone": "…"}, if_match=shop)
api.status()                                           # True/False según el status declarado (§4.4)
```

### `restep.connect(url, token=None, language=None, *, ...)`

| Opción | Qué hace |
|---|---|
| `token` | `Authorization: Bearer`, solo hacia el origen de la API (§4.3) |
| `language` | `Accept-Language` para los textos de la API: errores y descripciones (§5.6) |
| `headers` | Cabeceras adicionales en cada petición |
| `validate=True` | Comprueba parámetros, métodos y acciones contra el descubrimiento antes de enviar |
| `retries=0` | Reintentos ante `429`, `503` y `409 request_in_progress` (abajo) |
| `max_retry_wait=60` | Si `Retry-After` pide esperar más, no se reintenta |
| `timeout=30` | Segundos por petición |

Si la raíz no es un documento de descubrimiento, busca en sus enlaces `rel="service-desc"`,
primero los de tipo JSON (§4.2). Si no encuentra ninguno, supone solo `core`: `api.discovery`
es `None` y `api.collection("things")` (o `api.collection("x", path="/ruta")`) da una colección
sin declaraciones.

Si el documento declara en `restep` algo que no sigue la gramática de §1.1 o una versión mayor
que no es 1 (`"2.0"`, `"1.00"`), `connect` da `restep.UnsupportedVersion`: esa API no es Restep 1,
y tampoco se supone solo `core` (§1.1). `restep.parse_version("1.0-draft.6")` da `(1, 0, "draft.6")`.

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
- `Invalid` (422): un `create` que trae `id` donde lo asigna el servidor (sin `id_from` o con
  `"server"`) o donde `id_from` tiene un valor desconocido; o que no lo trae con `"client"` (§5.1).
- `BadRequest` (400): una `idempotency_key` explícita sin credenciales cuando el servidor declara
  `"anonymous": false` (§7.1).
- Lo que el descubrimiento declara con un valor que el cliente no conoce se trata como no
  declarado (§4.2): un filtro o un filtro con nombre con un `type` desconocido
  (`UnsupportedParameter`), un `count` desconocido, una acción con `on` o `method` desconocidos
  (`NotFound`, también con `validate=False`) y un aviso de retirada con un `kind` desconocido (no
  sale en `api.deprecations`).
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
`RequestInProgress` (el otro `409`: la misma petición con la misma `Idempotency-Key` sigue en
curso, §7.1; no es hija de `Conflict`, porque se puede repetir), `Gone` (`StateExpired`, `Retired`), `PreconditionFailed`, `ContentTooLarge`, `UriTooLong`,
`UnsupportedMediaType`, `Invalid`, `IdempotencyMismatch`, `NotExecuted`,
`PreconditionRequired`, `RateLimited`, `Internal`, `ServerNotImplemented`, `Unavailable`.
Un `code` desconocido se trata según su estado (§5.4): la clase del estado si solo hay una, si
no `ClientError` o `ServerError`. `e.retry_after` da los segundos de `Retry-After`.

Una respuesta con `Deprecation` (§5.5) emite un `restep.RestepDeprecationWarning`.

### Escrituras

- `create` envía una `Idempotency-Key` nueva (un UUID, como cadena de Structured Fields) si el
  servidor anuncia `writes` (§7.1). `idempotency_key="..."` usa la tuya; `None`, ninguna. Los
  reintentos de una misma llamada llevan la misma clave. Si el servidor declara
  `"idempotency": {"anonymous": false}` y la conexión no lleva credenciales (`token`, o
  `Authorization` o `Cookie` en `headers`), la clave automática no se pone; una acción
  idempotente no la lleva nunca.
- Todo cuerpo va con `Content-Type: application/json`, salvo el de `update`, que es un merge
  patch (§3).
- Las acciones declaradas `"idempotent": false` llevan clave igual que `create`; las
  idempotentes, no. Una acción con `"method": "GET"` va por `GET` y no admite cuerpo.
- `update` usa `application/merge-patch+json` (§5.1). `if_match` acepta la ETag (con o sin
  comillas) o el elemento leído, del que toma `_etag`.
- `get` sin `fields` ni `include`, `create` y `update` copian la cabecera `ETag` en `_etag`
  cuando es la ETag de elemento (fuerte y sin codificación de contenido, §7.2), así que el
  elemento devuelto sirve tal cual como `if_match`. Los elementos de un listado traen su propio
  `_etag` cuando el servidor lo pone (§7.2), y sirven igual sin leerlos antes.

### Reintentos

Con `retries=n`, un `429` se repite siempre (no se atendió) y un `503` solo si repetirlo no
puede duplicar el efecto: `GET`, `PATCH`, `DELETE`, un `POST` con `Idempotency-Key` o con un
`id` elegido por el cliente (§5.7), o una acción idempotente. Un `409 request_in_progress` también se repite, con la misma `Idempotency-Key`; un
`409 conflict`, nunca. Espera lo que diga `Retry-After` (segundos o fecha HTTP) o, sin él, 0,5 s,
1 s, 2 s... Por defecto no reintenta: sin `retries`, un `409 request_in_progress` llega como
`restep.RequestInProgress`, con `e.retry_after`.

### OAuth (§11.5)

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
`restep-py[async]`); sin él, `restep.aio.connect` da `ImportError` salvo que le pases `client=`,
cualquier objeto con la forma de `httpx.AsyncClient` (`async request(method, url, headers=,
content=)` que devuelve algo con `status_code`, `headers` y `content`).

## Pruebas

```bash
python3 tools/restep-py/test.py      # desde restep/
```

Arranca el servidor de referencia en el puerto 8890 y, con `RESTEP_TOKEN`, en el 8891, y dos
servidores de pega en Python en el 8892 y el 8893 para lo que el de referencia no tiene o no
provoca (acciones con `on` o `method` desconocidos, fallos `429`/`503`, `Deprecation`,
`service-desc` y un servidor solo core). Contra la referencia se prueban la subcolección
`reviews` con su padre (`404` si el libro no existe, borrada con él), `include` con profundidad 2,
la relación `highlight` con `href`, la polimórfica `showcase` con `type`, y las acciones
`low_stock` (`GET` de colección), `restock` (`POST` de elemento con `input`) y
`find_or_create` e `inherit_tax` (idempotentes). Las comprobaciones de validación local piden lo mismo con `validate=True` y con
`validate=False` y exigen la misma clase de error y el mismo `parameter` del cliente y del
servidor. La parte asíncrona se prueba con un cliente de pega con la forma de httpx; si httpx
está instalado, también con él.

## Borrador 6: lo que el cliente cumple (§12.2)

- **Versión** (§1.1): valida `restep` con su gramática y rechaza otra versión mayor; `api.version`
  y `api.pre_release` dicen si es un borrador o un candidato. El cliente no hace nada distinto
  con un borrador: solo lo expone, para que quien lo use no le suponga garantías.
- **Partes experimentales** (§1.2): `restep.IMPLEMENTED_VERSIONS` es solo `1.0-draft.6`: el texto de la `1.0` aún no existe.
  `api.usable("inherit")` es falso si el servidor declara otra versión, y entonces
  `Collection.inherited` da `restep.ExperimentalNotUsable`. `sync`, `batch`, `filter-expr` y
  `query-method` no los usa nunca (no los implementa).
- **Miembros y valores desconocidos** (§3, §4.2): se ignoran; los valores desconocidos de `type`,
  `on`, `method`, `kind`, `count`, `id_from` y `collations` siguen las reglas de arriba.
- **Tipos de medio y `+`** (§3, §5.2): `application/json` en todo cuerpo (merge patch en
  `PATCH`); la query se codifica con `quote`, así que un `+` literal sale `%2B` (desfases
  horarios, cursores); `next` se sigue tal cual.
- **Enlaces y credenciales** (§3, §4.3): no sigue enlaces con un esquema que no sea `https` ni el
  de la raíz; el token y las cabeceras de `headers` solo van al origen de la API; las
  redirecciones no se siguen.
- **`id_from`, subcolecciones y singletons** (§5.1, §4.2, §5.9): arriba. Un singleton con
  plantilla en `path` o con `parent` se ignora.
- **Errores** (§5.4): un `code` desconocido, por la clase de su estado; un `reason` desconocido no
  cambia nada: la excepción es la de su `code` y el `reason` queda en `e.reason`.
- **`inherit`** (§11.6): un `from` desconocido en `_inherited` es `"hidden"`.

## Limitaciones

- No valida los cuerpos contra los esquemas `create`/`update` (haría falta un validador de
  JSON Schema); de eso se encarga el servidor con su `422 invalid`. Tampoco descarga esquemas
  enlazados: la comprobación de `fields` solo usa un esquema `item` incrustado.
- No implementa `sync` (`:changes`, eventos), `batch`, `filter-expr` ni `query-method`, ni
  `Prefer: return=minimal`, ni las relaciones de un singleton más allá de leerlas.
- No vuelve a pedir el descubrimiento tras un `unsupported_parameter` o un `405` para algo que su
  copia declara (§4.2, un DEBERÍA), ni lo revalida con su `Cache-Control`: lo lee una vez al
  conectar. Tampoco hace nada con el perfil `cache` (§8) más allá de copiar la `ETag` en `_etag`.
- No comprueba que una colección declarada sea coherente (variables distintas, `parent` que
  existe): eso es cosa del servidor y de `restep-check`.
- Con `idempotency.anonymous: false`, solo sabe si lleva credenciales por `token` y por
  `Authorization` o `Cookie` en `headers`; un certificado de cliente no lo ve.
- La rama de httpx real no se ha probado aquí (httpx no está instalado en esta máquina); la
  lógica asíncrona sí, con el cliente de pega.
- Los tipos son `dict[str, Any]`: el cliente es dinámico. Para tipos por colección está
  `restep-ts` (TypeScript).

## Dudas sobre la spec

Las dudas del borrador 5 (si `429` y `503` garantizan que no hubo efecto, qué da una acción no
declarada, si `fields=id` vale, si un `PATCH` repetido es seguro, cuánto guardar el
descubrimiento) las resuelve el borrador 6 en §5.7, §11.1, §6.3 y §4.2. Quedan:

- **`include` anidado en una relación a varias colecciones** (`to` como lista): §9 no dice cómo
  se valida `include=a.b` cuando `a` puede apuntar a colecciones con relaciones distintas. El
  cliente lo rechaza en local.
- **Qué es «usar» `inherit`.** §1.2 dice que usar una parte experimental es mandar una petición
  que solo ella define o apoyarse en una de sus reglas para leer una respuesta. Escribir el campo
  propio es un `PATCH` corriente, así que el cliente solo lo restringe en `inherited()`, que lee
  `_inherited`. Leer el campo efectivo como un campo más tampoco sería usarlo.
- **`"1.0"` como versión implementada.** §1.2 pide implementar «el texto de la versión que
  declara»; el cliente da por hecho que la 1.0 publicada tendrá el texto del borrador 6 en sus
  partes experimentales, cosa que solo se sabrá al publicarla.

## Código y avisos

Código y avisos: <https://github.com/restep-spec/restep-py>. Ese repositorio se genera desde la web de
Restep, donde la herramienta se desarrolla junto a la [especificación](https://restep.wubytes.com/1/) y el servidor de
referencia: `vendor/` y `schema/` son copias de la web para que las pruebas funcionen solas.

Licencia MIT.
