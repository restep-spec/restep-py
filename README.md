# restep (Python)

Generic client for [Restep 1](https://w3id.org/restep/1/) APIs (draft 7), driven by the
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
api.version             # '1.0-draft.7'
api.pre_release         # 'draft.7': a draft, without the guarantees of a published version (§1.1)
api.default_language    # 'es-es': the language of a response when none in Accept-Language matches (§5.6)
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

# Draft 7
books.list(named={"active": False})                    # turns off a default filter (§6.1): filter[active]=false
for book in books.copy():                              # a full copy: every default filter off, no other filter (§5.2)
    ...
books.action("7", "compare", params={"with": "12"})    # GET /books/7:compare?with=12, declared params (§11.1)
result = books.action("7", "reserve", full=True)       # ActionResult: status, body, location, item, stale (§11.1)
api["orders"].new()                                    # GET /orders:new: starting values and effective schema
api["files"].upload(data, "image/jpeg", filename="cover.jpg")   # the bytes of a file (§5.1)
api["drafts"].update("d1", {...}, if_none_match="*")   # upsert: create only if the id is free (§5.1)
api["orders"].effects("cancel")                        # frozenset of destroys/external/charges (§11.1)
api["events"].instant(event, "starts_at")              # a local date-time as an instant, with its zone (§3)
api.singletons["settings"].at(reader_id="7").get()     # a singleton under an item (§5.9)
api.singletons["me"].action("verify_email")            # POST /me:verify_email
```

### `restep.connect(url, token=None, language=None, *, ...)`

| Option | What it does |
|---|---|
| `token` | `Authorization: Bearer`, only to the API's origin (§4.3) |
| `language` | `Accept-Language` for the API's texts: errors and descriptions (§5.6) |
| `headers` | Extra headers on every request |
| `validate=True` | Checks parameters, methods and actions against the discovery document before sending |
| `retries=0` | Retries on `408`, `421`, `425`, `429`, `502`, `503`, `504` and `409 request_in_progress` (below) |
| `max_retry_wait=60` | If `Retry-After` asks to wait longer, it does not retry |
| `timeout=30` | Seconds per request |

A response is a discovery document only if it is a `2xx` whose body is a JSON object with a string
`restep` (§4.2). If the root gives a `2xx` without one, or a `404`, it looks through its
`rel="service-desc"` links, JSON ones first, and takes the first target that is a discovery
document of Restep 1. If it finds none, it assumes `core` only: `api.discovery` is `None` and
`api.collection("things")` (or `api.collection("x", path="/path")`) gives a collection with no
declarations. Any other error on the root (a `401`, a `503`) says nothing about the profiles:
`connect` raises it.

If the document declares in `restep` something that does not follow the grammar of §1.1, or a
major version other than 1 (`"2.0"`, `"1.00"`), `connect` raises `restep.UnsupportedVersion`: that
API is not Restep 1, and `core` only is not assumed either (§1.1).
`restep.parse_version("1.0-draft.7")` gives `(1, 0, "draft.7")`.

### Filters

`filter` is a dictionary of field → condition (§6.1):

| Python | Request |
|---|---|
| `{"category": "poesia"}` | `filter[category]=poesia` |
| `{"price": {"le": "20", "ge": 10}}` | `filter[price][le]=20&filter[price][ge]=10` |
| `{"id": ["7", "12"]}` or `{"id": {"in": ["7", "12"]}}` | `filter[id][in]=7,12` |
| `{"category": {"nin": ["poesia"]}}` | `filter[category][nin]=poesia` |
| `{"title": {"prefix": "La "}}`, `{"title": {"contains": "niebla"}}` | `filter[title][prefix]=La ` |
| `{"rating": {"null": True}}` | `filter[rating][null]=true` |
| `{"name.es-mx": "Separador"}` | `filter[name.es-mx]=Separador` |
| `{"reviews.rating": 5}` (declared with `"quantifier": "some"`) | `filter[reviews.rating]=5` |
| `{"reviews": {"null": True}}` (`null` only, with `"quantifier": "some"` or without it) | `filter[reviews][null]=true` |

Values are converted according to the declared type: `Decimal`, `int` and `float` for `decimal`,
`number` and `integer`; `date`, time-zone-aware `datetime` and `timedelta` for `date`,
`datetime` and `duration`; a naive `datetime` for `localdatetime`; `bool` only for `boolean`. A
string also works if it has the shape of the type: for `integer` and `number` (and a `GET`
action's params of those types, each element of a list too), the number grammar of RFC 8259 with
exponent, so `"2e1"` is `20` and `"+20"`, `".5"`, `"020"` and `"0x14"` are a local
`InvalidParameter` (§6.1, §11.1). An operand of an `integer` field is valid when its exact value
has no fractional part (§6.1): `20.0`, `Decimal("20.0")`, `"20.0"` and `"2e1"` are the same
operand as `20`, and `20.5` is a local `InvalidParameter`. A local date-time never has a leap
second (`:60`, §3). `None` is an error (for "no value", use
`{"null": True}`). A duration follows the grammar of §3: `PT1H30M`, `PT0.250S` (a fraction only in
the seconds, up to nine digits), never years or months in a filter; `restep.is_duration`,
`restep.duration_seconds` and `restep.iso_duration` (from a `timedelta`) help with it.

The operators the client uses are the declared ones it knows: an unknown one is ignored on its own
(§4.2), a filter with an unknown `quantifier` is not used, and on a path declared with
`quantifier` neither `ne` nor `nin` exists (§6.1). `null` under a `quantifier` tests the whole
group, never each element: alone, `True` selects an empty, absent or `null` group; with `in` or
another filter on the same group, `True` means that no visible element matches and `False` that
some does. `prefix` and `contains` exist only on strings,
and their operand cannot be empty.

Named filters (§6.1) go in `named`: `True` for a switch, a value for one with `type`. A switch
declared `"default": true` applies to every list request; `False` turns it off
(`filter[active]=false`), which on any other switch sends nothing. `books.default_filters` and
`books.required_filters` list them; a list request without a required filter fails locally, and
`copy()` (the request for a full copy, §5.2) does not exist on such a collection.

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
- `UnsupportedMediaType` (415) and `ContentTooLarge` (413): an upload of a media type `upload` does
  not declare, or larger than `max_size` (§5.1).
- For a `GET` action (§11.1): `UnsupportedParameter` for a parameter it does not declare (one
  without `params` takes none), `InvalidParameter` for a required one missing, a value of the wrong type, an empty list or a list
  value with a comma.
- `ValueError`/`TypeError`: an id that is not a Restep id (§3), a missing path variable, an item
  action without an id, a body that is not I-JSON (an unpaired surrogate, a noncharacter, a number
  that is not finite, §3).

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
`Forbidden`, `NotFound`, `MethodNotAllowed` (with `allow`), `NotAcceptable`, `RequestTimeout`,
`Conflict`, `RequestInProgress` (the other `409`: the same request with the same `Idempotency-Key`
is still in progress, §7.1; not a child of `Conflict`, because it can be repeated), `Gone`
(`StateExpired`, `Retired`), `PreconditionFailed`, `ContentTooLarge`, `UriTooLong`,
`UnsupportedMediaType`, `MisdirectedRequest`, `Invalid`, `IdempotencyMismatch`, `NotExecuted`,
`TooEarly`, `PreconditionRequired`, `RateLimited`, `Internal`, `ServerNotImplemented`,
`BadGateway`, `Unavailable`, `GatewayTimeout`. The table is closed (§5.4): `restep.CODES` maps
each code to its status, as `problem.json` does, and any other status has the code `http_` and its
digits (`restep.code_for_status(431)` is `"http_431"`). A `code` this client does not know is
handled by its status: the status's class if there is only one, otherwise `ClientError` or
`ServerError`. `e.retry_after` gives the seconds from `Retry-After`, and `e.accept_post` the media
types of `Accept-Post`, which a `415` to a `POST` should carry (§5.1); a local `415` from `upload`
gives the declared ones.

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
  An action with `"method": "GET"` goes over `GET`, takes no body and takes its declared `params`
  (§11.1); a list parameter goes as values separated by commas.
- `action(..., full=True)` gives an `ActionResult`. When the response carried the item's
  representation with `Content-Location`, `item` is that new copy, with `_etag`; otherwise, after an
  unsafe action on an item or a singleton that succeeded, `stale` is true: a copy you kept must be
  read again before you use it (§11.1). A replay (§7.1) whose body is not the item's current
  representation carries no `Content-Location`, so `item` is `None` and `stale` is true: its body
  is never taken as the new copy. A safe action's `output` is not a representation, so it
  never updates a copy.
- A collection action named `changes` is reserved for `sync`, and one named `new` is only the safe
  `GET` collection action: declared otherwise, they are not used (§11.1).
- `upload(data, media_type, filename=...)` sends the bytes of a file to a collection that declares
  `upload`, with their own `Content-Type` and, with a name, `Content-Disposition` (RFC 6266); the
  new item carries `_content` (§5.1). `schemas.create` announces creation from JSON
  (`creates_from_json`): in a collection with `upload` and without `create`, `create()` is a local
  `UnsupportedMediaType` (it takes only files). With both, a JSON `create()` is always an item
  creation, and `upload()` sends a JSON file (`application/json`) as the `file` part of
  `multipart/form-data` (RFC 7578), where the collection declares `"multipart": true`; without
  it, a local `UnsupportedMediaType`. Other files go as their bytes: only `application/json`
  itself selects creation, so a `+json` type (`application/ld+json`) is a file like any other, and
  in a collection that only takes uploads a JSON file that `media_types` admits, also through
  `application/*` or `*/*`, goes as its bytes. A file of type `multipart/form-data` goes only as
  the `file` part, never to a collection without `"multipart": true` (a local
  `UnsupportedMediaType`, also with `validate=False`). The `file` part always carries a
  `filename`: the given one, or `file`.
- `create()`, `upload()` and an `update()` that creates the item return a `restep.Created` (a
  `dict`) after a `201`, whose `location` is its `Location` as an absolute URL. After a replayed
  `201` without body, which carries no `ETag` (§7.3), it is empty and has no `_etag`: read the
  item at `location` to write with `if_match`.
- Two `null` filters on the same to-many relationship or list field, its own or that of a path
  through it (`reviews` and `reviews.id`), are a local `InvalidParameter` (§6.1); `null_group()`
  finds the group from `relations` (`"many": true`), the array fields of `schemas.item` and, for a
  dotless filter, its `quantifier`. An infinite or `NaN` operand is a local `InvalidParameter`;
  a huge one in text, such as `"1e400"`, goes as written.
- In a collection with `upsert`, `update` on a free `id` creates the item (`201`), and
  `if_none_match="*"` makes it succeed only then (§5.1).
- `effects(operation, item=...)` gives the effects of an action or an unsafe method that a person
  may want to approve first (§11.1, §4.3): with nothing declared, none for a safe action and all
  three for an unsafe operation; all three when a value is unknown, also on an action declared
  safe; `destroys` for an undeclared `DELETE`; none for `GET`. A singleton's `effects("PATCH")`
  is what its `method_effects` declares, and all three without it (§5.9).
- `update` uses `application/merge-patch+json` (§5.1). `if_match` accepts the ETag (with or
  without quotes) or the item you read, from which it takes `_etag`.
- `get` without `fields` or `include`, `create` and `update` copy the `ETag` header into `_etag`
  when it is the item ETag (strong and without content coding, §7.2), so the returned item works
  as is as `if_match`. Items in a listing carry their own `_etag` when the server sets it (§7.2),
  and work the same way without reading them first.

### Retries

With `retries=n` (§5.7):

- A `429` or `503` with a `problem+json` body comes from the API, which never gives it to a request
  that had an effect, so it is repeated whatever the method. Without that body it may come from an
  intermediary, and it is repeated only if repeating cannot duplicate the effect: `GET`, `PATCH`,
  `DELETE`, a `POST` with an `Idempotency-Key` or with a client-chosen `id`, or an idempotent action.
  In a collection that declares `reusable_ids`, a `PATCH` or `DELETE` counts only with `If-Match`.
- A `502` or `504` says nothing about the effect: it is repeated only in those same cases.
- A `408` (not received in time) and a `425` (this client never sends TLS early data) are repeated
  as is. A `421` is repeated over another connection, which only the synchronous client guarantees
  (urllib opens one per request); `restep.aio` does not repeat it.
- A `409 request_in_progress` is repeated with the same `Idempotency-Key`; a `409 conflict`, never.
- A `POST` that is safe to repeat only because of its key is never repeated once
  `idempotency.ttl` (24 hours if not declared) has passed since its first attempt: the caller then
  has to find out whether the first attempt took effect, and for a creation without a
  client-chosen `id` the outcome stays uncertain unless it can establish it another way, such as a
  filter on a field the API keeps unique or the `created` list of `:changes` (§5.7).

It waits for whatever `Retry-After` says (seconds or an HTTP date) or, without it, 0.5 s, 1 s,
2 s... By default it does not retry: without `retries`, a `409 request_in_progress` arrives as
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
python3 tools/restep-py/test.py      # from restep/; or pytest tools/restep-py/test.py
RESTEP_SERVER=/path/to/restep-server.mjs python3 tools/restep-py/test.py   # another copy of the reference
```

It starts the reference server and, with `RESTEP_TOKEN`, a second one, plus two fake Python
servers, each on a free random port, for what the reference server lacks or cannot trigger
(actions with an unknown `on` or `method`, retried failures, `Deprecation`, `service-desc`, a
core-only server and everything draft 7 adds: default and required filters, `nin`, `prefix`,
`contains` and `quantifier`, localized fields, local times, effects, uploads, `upsert`,
`reusable_ids`, action `params` and results, singletons under an item). Against the reference server it tests the `reviews` subcollection with its
parent (`404` if the book does not exist, deleted along with it), `include` with depth 2, the
`highlight` relation with `href`, the polymorphic `showcase` with `type`, and the actions
`low_stock` (collection `GET`), `restock` (item `POST` with `input`) and `find_or_create` and
`inherit_tax` (idempotent). The local validation checks send the same request with
`validate=True` and with `validate=False` and require the same error class and the same
`parameter` from client and server. The async part is tested with a fake httpx-shaped client;
if httpx is installed, with httpx too.

## Draft 7: what the client complies with (§12.2)

- **Version** (§1.1): validates `restep` against its grammar and rejects any other major version;
  `api.version` and `api.pre_release` say whether it is a draft or a candidate. The client does
  nothing differently with a draft: it only exposes it, so whoever uses it does not assume
  guarantees.
- **Experimental parts** (§1.2): `restep.IMPLEMENTED_VERSIONS` is only `1.0-draft.7`: the text of
  `1.0` does not exist yet. `api.usable("inherit")` is false if the server declares another
  version, and then `Collection.inherited` raises `restep.ExperimentalNotUsable`. It never uses
  `sync`, `batch`, `filter-expr` or `query-method` (it does not implement them).
- **Discovery** (§4.2): a document only from a `2xx` JSON object with a string `restep`; `core`
  only after a `2xx` without one or a `404`, never after another error; of several `service-desc`
  links, the first target that is a discovery document of Restep 1.
- **Unknown members and values** (§3, §4.2): ignored; unknown values of `type`, `on`, `method`,
  `kind`, `count`, `id_from` and `collations` follow the rules above; an unknown operator is
  ignored on its own, a filter with an unknown `quantifier` is not used, and an unknown value of
  `effects` counts as all three effects, also on an action declared safe.
- **What it sends is I-JSON** (§3): no unpaired surrogates, noncharacters or non-finite numbers.
- **Full copies** (§5.2): `copy()` turns off every default filter and carries no other filter.
- **Media types and `+`** (§3, §5.2): `application/json` on every body (merge patch on `PATCH`);
  the query is encoded with `quote`, so a literal `+` goes out as `%2B` (time-zone offsets,
  cursors); `next` is followed as is.
- **Links and credentials** (§3, §4.3): it does not follow links with a scheme other than `https`
  or the root's; the token and the `headers` only go to the API's origin; redirects are not
  followed.
- **`id_from`, subcollections and singletons** (§5.1, §4.2, §5.9): above. A singleton whose
  `path` is a template without `parent`, or whose `parent` names no collection or singleton, is
  ignored; one with a valid `parent` is used with `at(...)`.
- **Errors** (§5.4): an unknown `code` goes by its status's class; an unknown `reason` changes
  nothing: the exception is the one for its `code` and the `reason` stays in `e.reason`.
- **Retries** (§5.7): a key is not reused automatically after `idempotency.ttl` (above).
- **Actions** (§11.1): after an unsafe action on an item, `ActionResult.stale` says whether a stored
  copy must be revalidated; with `Content-Location`, `ActionResult.item` is the new copy.
- **`inherit`** (§11.6): an unknown `from` in `_inherited` is `"hidden"`, and a field whose `inherit`
  declaration carries a member the client does not know (other than an `x-` one) is not inherited:
  `inherited()` raises `KeyError` for it. With `"combine": "fields"`, `inherited()` gives
  `{"fields": {member: origin}}`.

## Limitations

- It does not validate bodies against the `create`/`update` schemas (that would need a JSON
  Schema validator); the server handles that with its `422 invalid`. Nor does it download linked
  schemas: the `fields` check only uses an embedded `item` schema.
- It does not implement `sync` (`:changes`, events), `batch`, `filter-expr` or `query-method`, nor
  `Prefer: return=minimal`, nor a singleton's relations beyond reading them, nor
  `inherit_items` (§11.6), nor the `multipart/form-data` form of an upload for files other than
  JSON or `multipart/form-data`.
- It keeps no copies of items, so revalidating one after an action is left to the caller, with
  `ActionResult.stale`.
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
long to keep the discovery document) were settled by draft 6 in §5.7, §11.1, §6.3 and §4.2.
Still open:

- **Nested `include` on a relation to several collections** (`to` as a list): §9 does not say how
  `include=a.b` is validated when `a` can point to collections with different relations. The
  client rejects it locally.
- **What "using" `inherit` means.** §1.2 says using an experimental part means sending a request
  only that part defines, or relying on one of its rules to read a response. Writing the own
  field is an ordinary `PATCH`, so the client only restricts it in `inherited()`, which reads
  `_inherited`. Reading the effective field like any other field would not be using it either.
- **`"1.0"` as an implemented version.** §1.2 asks for implementing "the text of the version it
  declares"; the client assumes the published 1.0 will have draft 7's text in its experimental
  parts, which will only be known once it is published.

## Code and issues

Code and issues: <https://github.com/restep-spec/restep-py>. That repository is generated from the Restep
site, where the tool is developed alongside the [specification](https://w3id.org/restep/1/) and the reference
server: `vendor/` and `schema/` are copies from the site so the tests run on their own.

MIT licensed.
