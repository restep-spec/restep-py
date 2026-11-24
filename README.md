# restep (Python)

Generic client for [Restep 1](https://restep.wubytes.com/1/) APIs (draft 6), driven by the
discovery document (§4.2). Python 3.10 or later, standard library only (urllib). The async
version uses [httpx](https://www.python-httpx.org/) if it is installed; it is not a dependency.

```bash
pip install restep-py            # imported as `restep`
pip install "restep-py[async]"   # with httpx
```

It does not generate code: it reads the discovery document on connect, and from it knows which
collections exist, which methods they accept, which fields can be filtered and sorted, with which
operators and of which type. Whatever the discovery document says the server would reject is
rejected before it is sent, with the same exception the server would give.

## Usage

```python
import restep

api = restep.connect("http://localhost:8080/", token=None, language="es")
api.profiles            # frozenset({'core', 'query', 'writes', 'cache'})
api.version             # '1.0-draft.6'
api.pre_release         # 'draft.6': a draft, without the guarantees of a published version (§1.1)
api.collections         # {'books': <restep.Collection books /books>, ...}
api.singletons          # {'shop': <restep.Singleton shop /shop>} (§5.9)
api.deprecations        # deprecation notices, with announced and sunset (§5.5)

books = api["books"]
page = books.list(filter={"price": {"le": "20"}, "category": ["novela-negra", "poesia"]},
                  sort="-rating", fields=["title", "price"], limit=10, count=True)
page.items, page.total, page.total.estimated
page = page.next_page()            # None on the last one; follows next as is (§5.2)

for book in books.iter(filter={"year": {"ge": 2023}}, limit=50):   # every page
    ...
books.count(filter={"rating": {"null": True}})                      # Total(3, estimated=False)
books.list(named={"featured": True, "search": "niebla"})           # named filters (§6.1), no operators

book = books.get("7")              # carries its item ETag in book["_etag"] (§7.2)
new = books.create({"title": "...", "price": "9.90", "category": "poesia"})
new = books.update(new["id"], {"price": "10.50"}, if_match=new)     # If-Match with its _etag
books.delete(new["id"], if_match=new)

api["orders"].action("2001", "cancel")                 # POST /orders/2001:cancel (§11.1)
api["orders"].action(None, "import", {"rows": [...]})  # collection action
api["reviews"].at(book_id="7").list()                  # subcollection /books/{book_id}/reviews
api["reviews"].at(book_id="7").ref("3")                # {"id": "3", "href": "/books/7/reviews/3"} (§5.1)
api["categories"].create({"id": "papeleria", ...})     # client-chosen id (id_from, §5.1)
api["categories"].collation("name")                    # "server" or "codepoint" (§6.1)
api["products"].inherited(item, "tax_rate")            # "own", {"href": …}, "default", "hidden" (§11.6)
shop = api.singletons["shop"].get()                    # singleton (§5.9), with _etag
api.singletons["shop"].update({"phone": "…"}, if_match=shop)
api.status()                                           # True/False from the declared status (§4.4)
```

### `restep.connect(url, token=None, language=None, *, ...)`

| Option | What it does |
|---|---|
| `token` | `Authorization: Bearer`, only to the API's origin (§4.3) |
| `language` | `Accept-Language` for the API's texts: errors and descriptions (§5.6) |
| `headers` | Extra headers on every request |
| `validate=True` | Checks parameters, methods and actions against the discovery document before sending |
| `retries=0` | Retries on `429`, `503` and `409 request_in_progress` (below) |
| `max_retry_wait=60` | If `Retry-After` asks to wait longer, it does not retry |
| `timeout=30` | Seconds per request |

If the root is not a discovery document, it looks through its `rel="service-desc"` links, JSON
ones first (§4.2). If it finds none, it assumes `core` only: `api.discovery` is `None` and
`api.collection("things")` (or `api.collection("x", path="/path")`) gives a collection with no
declarations.

If the document declares in `restep` something that does not follow the grammar of §1.1, or a
major version other than 1 (`"2.0"`, `"1.00"`), `connect` raises `restep.UnsupportedVersion`: that
API is not Restep 1, and `core` only is not assumed either (§1.1).
`restep.parse_version("1.0-draft.6")` gives `(1, 0, "draft.6")`.

### Filters

`filter` is a dictionary of field → condition (§6.1):

| Python | Request |
|---|---|
| `{"category": "poesia"}` | `filter[category]=poesia` |
| `{"price": {"le": "20", "ge": 10}}` | `filter[price][le]=20&filter[price][ge]=10` |
| `{"id": ["7", "12"]}` or `{"id": {"in": ["7", "12"]}}` | `filter[id][in]=7,12` |
| `{"rating": {"null": True}}` | `filter[rating][null]=true` |
| `{"name.es-mx": "Separador"}` | `filter[name.es-mx]=Separador` |

Values are converted according to the declared type: `Decimal`, `int` and `float` for `decimal`,
`number` and `integer`; `date`, time-zone-aware `datetime` and `timedelta` for `date`,
`datetime` and `duration`; `bool` only for `boolean`. A string also works if it has the shape of
the type. `None` is an error (for "no value", use `{"null": True}`).

`sort`, `fields` and `include` accept a comma-separated string or a list.

### Local validation

With `validate=True` (the default), before sending:

- `UnsupportedParameter`: a profile the server does not announce (`query`, `relations`); an
  undeclared filter field or operator, sort field or relation; an `include` path deeper than
  `include_depth`; `count` on a collection that does not declare it (§5.3).
- `InvalidParameter`: a value that does not fit the type, an `enum` outside `values`, an empty
  `in` list or one with commas, a `limit` outside `1..max_limit`, a `fields` field that is not in
  the `item` schema (if embedded), a path that goes into a language map (`name.es`), a relation
  path that is not in `include`.
- `MethodNotAllowed` (405): a method that `methods` or `item_methods` do not declare (§4.3).
- `NotFound` (404): an action the collection does not declare, or without the `actions`
  extension.
- `Invalid` (422): a `create` that carries `id` where the server assigns it (no `id_from`, or
  `"server"`) or where `id_from` has an unknown value; or one that lacks it with `"client"` (§5.1).
- `BadRequest` (400): an explicit `idempotency_key` without credentials when the server declares
  `"anonymous": false` (§7.1).
- Anything the discovery document declares with a value the client does not know is treated as
  undeclared (§4.2): a filter or named filter with an unknown `type` (`UnsupportedParameter`), an
  unknown `count`, an action with an unknown `on` or `method` (`NotFound`, also with
  `validate=False`), and a deprecation notice with an unknown `kind` (left out of
  `api.deprecations`).
- `ValueError`/`TypeError`: an id that is not a Restep id (§3), a missing path variable, an item
  action without an id.

These errors carry `local = True`, plus `problem`, `parameter` and `detail` like the server's, so
the same `except` handles both. With `validate=False` everything goes to the server.

### Errors

Every `4xx` or `5xx` response becomes an exception built from its problem+json (§5.4):

```python
try:
    books.list(filter={"nope": 1})
except restep.UnsupportedParameter as e:   # don't retry: the API does not offer that
    e.status, e.code, e.parameter, e.detail, e.errors, e.reason, e.problem, e.headers
except restep.InvalidParameter as e:       # fix the value and retry
    ...
```

Hierarchy: `RestepError` → `ClientError` (4xx) and `ServerError` (5xx). One class per `code` in
the table in §5.4: `BadRequest` (and its children `InvalidParameter`, `UnsupportedParameter`,
`IdempotencyKeyRequired`), `Unauthenticated` (with `resource_metadata` from the challenge),
`Forbidden`, `NotFound`, `MethodNotAllowed` (with `allow`), `NotAcceptable`, `Conflict`,
`RequestInProgress` (the other `409`: the same request with the same `Idempotency-Key` is still in
progress, §7.1; not a child of `Conflict`, because it can be repeated), `Gone` (`StateExpired`,
`Retired`), `PreconditionFailed`, `ContentTooLarge`, `UriTooLong`,
`UnsupportedMediaType`, `Invalid`, `IdempotencyMismatch`, `NotExecuted`,
`PreconditionRequired`, `RateLimited`, `Internal`, `ServerNotImplemented`, `Unavailable`.
An unknown `code` is handled by its status (§5.4): the status's class if there is only one,
otherwise `ClientError` or `ServerError`. `e.retry_after` gives the seconds from `Retry-After`.

A response with `Deprecation` (§5.5) emits a `restep.RestepDeprecationWarning`.

### Writes

- `create` sends a fresh `Idempotency-Key` (a UUID, as a Structured Fields string) if the server
  announces `writes` (§7.1). `idempotency_key="..."` uses yours; `None`, none at all. Retries of
  the same call carry the same key. If the server declares
  `"idempotency": {"anonymous": false}` and the connection has no credentials (`token`, or
  `Authorization` or `Cookie` in `headers`), the automatic key is not set; an idempotent action
  never carries one.
- Every body goes with `Content-Type: application/json`, except `update`'s, which is a merge
  patch (§3).
- Actions declared `"idempotent": false` carry a key just like `create`; idempotent ones do not.
  An action with `"method": "GET"` goes over `GET` and takes no body.
- `update` uses `application/merge-patch+json` (§5.1). `if_match` accepts the ETag (with or
  without quotes) or the item you read, from which it takes `_etag`.
- `get` without `fields` or `include`, `create` and `update` copy the `ETag` header into `_etag`
  when it is the item ETag (strong and without content coding, §7.2), so the returned item works
  as is as `if_match`. Items in a listing carry their own `_etag` when the server sets it (§7.2),
  and work the same way without reading them first.

### Retries

With `retries=n`, a `429` is always repeated (it was not processed) and a `503` only if repeating
it cannot duplicate the effect: `GET`, `PATCH`, `DELETE`, a `POST` with an `Idempotency-Key` or
with a client-chosen `id` (§5.7), or an idempotent action. A `409 request_in_progress` is also
repeated, with the same `Idempotency-Key`; a `409 conflict`, never. It waits for whatever
`Retry-After` says (seconds or an HTTP date) or, without it, 0.5 s, 1 s, 2 s... By default it does
not retry: without `retries`, a `409 request_in_progress` arrives as
`restep.RequestInProgress`, with `e.retry_after`.

### OAuth (§11.5)

```python
try:
    api = restep.connect(url)
except restep.Unauthenticated as e:
    e.resource_metadata                     # metadata URL, from the 401 challenge
meta = restep.oauth_metadata(url)           # RFC 9728 metadata, checking resource
restep.oauth_metadata_url("https://example.com/api/")
# 'https://example.com/.well-known/oauth-protected-resource/api/'
```

The client does not obtain tokens: that is the job of whatever OAuth library you use. Once you
have the token, `restep.connect(url, token=...)`.

### Async

```python
import restep.aio

async with await restep.aio.connect(url, language="es") as api:
    page = await api["books"].list(limit=10)
    async for book in api["books"].iter():
        ...
    await api["books"].update("7", {"price": "19.00"}, if_match=book)
```

The same API, with `await` and `async for`. It needs httpx (`pip install httpx`, or the
`restep-py[async]` extra); without it, `restep.aio.connect` raises `ImportError` unless you pass
`client=`, any object shaped like `httpx.AsyncClient` (`async request(method, url, headers=,
content=)` returning something with `status_code`, `headers` and `content`).

## Tests

```bash
python3 tools/restep-py/test.py      # from restep/
```

It starts the reference server on port 8890 and, with `RESTEP_TOKEN`, on 8891, plus two fake
Python servers on 8892 and 8893 for what the reference server lacks or cannot trigger (actions
with an unknown `on` or `method`, `429`/`503` failures, `Deprecation`, `service-desc` and a
core-only server). Against the reference server it tests the `reviews` subcollection with its
parent (`404` if the book does not exist, deleted along with it), `include` with depth 2, the
`highlight` relation with `href`, the polymorphic `showcase` with `type`, and the actions
`low_stock` (collection `GET`), `restock` (item `POST` with `input`) and `find_or_create` and
`inherit_tax` (idempotent). The local validation checks send the same request with
`validate=True` and with `validate=False` and require the same error class and the same
`parameter` from client and server. The async part is tested with a fake httpx-shaped client;
if httpx is installed, with httpx too.

## Draft 6: what the client complies with (§12.2)

- **Version** (§1.1): validates `restep` against its grammar and rejects any other major version;
  `api.version` and `api.pre_release` say whether it is a draft or a candidate. The client does
  nothing differently with a draft: it only exposes it, so whoever uses it does not assume
  guarantees.
- **Experimental parts** (§1.2): `restep.IMPLEMENTED_VERSIONS` is only `1.0-draft.6`: the text of
  `1.0` does not exist yet. `api.usable("inherit")` is false if the server declares another
  version, and then `Collection.inherited` raises `restep.ExperimentalNotUsable`. It never uses
  `sync`, `batch`, `filter-expr` or `query-method` (it does not implement them).
- **Unknown members and values** (§3, §4.2): ignored; unknown values of `type`, `on`, `method`,
  `kind`, `count`, `id_from` and `collations` follow the rules above.
- **Media types and `+`** (§3, §5.2): `application/json` on every body (merge patch on `PATCH`);
  the query is encoded with `quote`, so a literal `+` goes out as `%2B` (time-zone offsets,
  cursors); `next` is followed as is.
- **Links and credentials** (§3, §4.3): it does not follow links with a scheme other than `https`
  or the root's; the token and the `headers` only go to the API's origin; redirects are not
  followed.
- **`id_from`, subcollections and singletons** (§5.1, §4.2, §5.9): above. A singleton with a
  template in `path` or with `parent` is ignored.
- **Errors** (§5.4): an unknown `code` goes by its status's class; an unknown `reason` changes
  nothing: the exception is the one for its `code` and the `reason` stays in `e.reason`.
- **`inherit`** (§11.6): an unknown `from` in `_inherited` is `"hidden"`.

## Limitations

- It does not validate bodies against the `create`/`update` schemas (that would need a JSON
  Schema validator); the server handles that with its `422 invalid`. Nor does it download linked
  schemas: the `fields` check only uses an embedded `item` schema.
- It does not implement `sync` (`:changes`, events), `batch`, `filter-expr` or `query-method`, nor
  `Prefer: return=minimal`, nor a singleton's relations beyond reading them.
- It does not fetch the discovery document again after an `unsupported_parameter` or a `405` for
  something its copy declares (§4.2, a SHOULD), nor revalidate it with its `Cache-Control`: it
  reads it once on connect. Nor does it do anything with the `cache` profile (§8) beyond copying
  the `ETag` into `_etag`.
- It does not check that a declared collection is consistent (distinct variables, a `parent` that
  exists): that is up to the server and `restep-check`.
- With `idempotency.anonymous: false`, it only knows it has credentials through `token` and
  `Authorization` or `Cookie` in `headers`; it cannot see a client certificate.
- The real httpx branch has not been tested here (httpx is not installed on this machine); the
  async logic has, with the fake client.
- Types are `dict[str, Any]`: the client is dynamic. For per-collection types there is
  `restep-ts` (TypeScript).

## Questions about the spec

The questions from draft 5 (whether `429` and `503` guarantee there was no effect, what an
undeclared action returns, whether `fields=id` is valid, whether a repeated `PATCH` is safe, how
long to keep the discovery document) are settled by draft 6 in §5.7, §11.1, §6.3 and §4.2.
Still open:

- **Nested `include` on a relation to several collections** (`to` as a list): §9 does not say how
  `include=a.b` is validated when `a` can point to collections with different relations. The
  client rejects it locally.
- **What "using" `inherit` means.** §1.2 says using an experimental part means sending a request
  only that part defines, or relying on one of its rules to read a response. Writing the own
  field is an ordinary `PATCH`, so the client only restricts it in `inherited()`, which reads
  `_inherited`. Reading the effective field like any other field would not be using it either.
- **`"1.0"` as an implemented version.** §1.2 asks for implementing "the text of the version it
  declares"; the client assumes the published 1.0 will have draft 6's text in its experimental
  parts, which will only be known once it is published.

## Code and issues

Code and issues: <https://github.com/restep-spec/restep-py>. That repository is generated from the Restep
site, where the tool is developed alongside the [specification](https://restep.wubytes.com/1/) and the reference
server: `vendor/` and `schema/` are copies from the site so the tests run on their own.

MIT licensed.
