# graphbound

[![CI](https://github.com/wrinklybeef/graphbound/actions/workflows/ci.yml/badge.svg)](https://github.com/wrinklybeef/graphbound/actions/workflows/ci.yml)

A small synchronous Microsoft Graph client built on `requests`:

- **Bounded retries.** Reads retry on throttling and transient failures inside an
  attempt limit, a cooperative deadline, and a cap on requested retry sleep.
  Budgets nest, so one budget can cover a whole multi-request operation.
- **No replayed writes.** A `POST`, `PATCH` or `DELETE` is never sent again after
  a timeout or a 5xx, because the first attempt may have been applied.
- **Host pinning.** The bearer token goes to one host. Absolute URLs,
  `@odata.nextLink` values and redirects that point anywhere else are refused
  before anything is sent.
- **Safe identifiers.** Object IDs are validated before they are put in a path,
  which closes off a way to turn "remove from group" into "delete user".

It does not acquire tokens. Bring your own, from MSAL or anywhere else.

## Install

```
pip install git+https://github.com/wrinklybeef/graphbound
```

Python 3.11 or later. The only dependency is `requests`.

## Quick start

This example lists user display names. Install MSAL for token acquisition:

```sh
pip install msal
```

Use an Entra app registration with the Microsoft Graph **application** permission
`User.Read.All` and tenant administrator consent. This is the least privileged
application permission for [listing users](https://learn.microsoft.com/en-us/graph/api/user-list?view=graph-rest-1.0#permissions).
Set these variables through your shell or secret manager:

| Variable | Value |
| --- | --- |
| `GRAPH_TENANT_ID` | Directory tenant ID for the tenant to read |
| `GRAPH_CLIENT_ID` | Application/client ID of that app registration |
| `GRAPH_CLIENT_SECRET` | A current client secret **value**, not its identifier |

The example uses the public Microsoft cloud. National clouds need the matching
authority, scope and Graph base URL. Keep credentials out of source control and logs.

```python
import os
from contextlib import closing

import msal
from graphbound import BudgetHttpSession, GraphClient, ReadRetryPolicy, read_budget

tenant_id = os.environ["GRAPH_TENANT_ID"]
client_id = os.environ["GRAPH_CLIENT_ID"]
credential = os.environ["GRAPH_CLIENT_SECRET"]

with read_budget(seconds=180) as budget, closing(BudgetHttpSession(budget)) as http:
    app = msal.ConfidentialClientApplication(
        client_id,
        authority=f"https://login.microsoftonline.com/{tenant_id}",
        client_credential=credential,
        http_client=http,
    )
    result = app.acquire_token_for_client(["https://graph.microsoft.com/.default"])
    token = result.get("access_token")
    if not token:
        raise SystemExit("Token acquisition failed; check credentials, tenant and admin consent.")

    with GraphClient(token, read_policy=ReadRetryPolicy()) as graph:
        for user in graph.paginate("users", params={"$select": "id,displayName"}):
            print(user["displayName"])
```

For a 403, check application permissions and consent. See [Errors](#errors) for
structured diagnostics.

`get`, `post`, `patch` and `delete` return the decoded JSON object, or `{}` for an
empty response. `paginate` yields items and `paginate_pages` yields pages; both
follow `@odata.nextLink`, rejecting malformed pages/links and repeated request URLs.
Only an absent continuation ends the collection. If iteration fails, accumulated
results are incomplete. Cycle detection stores one URL per page; use a shared
budget to bound a collection with many distinct links.

## Retries and budgets

| Request | Retried after | Limits |
| --- | --- | --- |
| `GET`, client has a `read_policy` | 429, 500, 502, 503, 504, timeouts, connection errors | 4 attempts, 90 s cooperative elapsed budget, no single wait over 30 s |
| Any other request | 429, once | waits for `Retry-After`, up to 30 s |
| A write after a timeout or 5xx | never | |

The limits in the first row are the `ReadRetryPolicy` defaults and can be changed.
A 429 is replayed even for writes because it means Graph did not process the
request. Pass `retry_throttling=False` to disable that default retry; an explicit
`read_policy` still controls GET retries. The error's `retry_after` attribute holds
the header Graph sent.

`Retry-After` is honored as seconds or as an HTTP date. If the server asks for a
wait longer than the policy allows, the client stops instead of retrying early,
since a retry inside the cooldown extends the throttling.

A `read_budget` shares a cooperative deadline across Graph calls inside it:

```python
from graphbound import GraphRetryExhausted, read_budget

try:
    with read_budget(seconds=600, max_sleep=180):  # the whole collection run
        users = list(graph.paginate("users"))
        with read_budget(seconds=60):  # one step inside it
            skus = graph.get("subscribedSkus")
except GraphRetryExhausted:
    ...  # out of time, attempts, or sleep allowance
```

Request timeouts shrink to the time left; retry sleeps charge every enclosing
budget, including on the default 429 path. Child budgets respect parent deadlines,
and late responses are discarded. Checks are cooperative:
[Requests timeouts](https://requests.readthedocs.io/en/latest/user/quickstart/#timeouts)
limit socket inactivity, not total download time. Blocking calls, slow responses,
callbacks and caller work can exceed the deadline before another check runs.
The library cannot cancel them or undo a write that finished late. Timeouts must
be finite and positive.

Budgets use a `ContextVar` in the current thread/task; a new thread needs an
explicitly copied context. `BudgetHttpSession` applies the same checks to MSAL,
as shown in the [quick start](#quick-start).

## Staying on the Graph host

Relative paths resolve against `base_url`, which defaults to
`https://graph.microsoft.com/v1.0/`. An absolute URL is accepted only on that same
host, which is how you reach `/beta` and how `nextLink` values are followed.

```python
graph.get("https://graph.microsoft.com/beta/users")  # fine
graph.get("https://example.com/users")  # UnsafeGraphUrl, nothing sent
```

Also refused: `http://`, credentials or a non-default port in the URL, fragments,
dot segments (literal or percent-encoded), backslashes and control characters.
Redirects are not followed; one raises `GraphError` with code
`UNEXPECTED_REDIRECT`.

For a national cloud, set the base URL and the client pins to that host instead:

```python
GraphClient(token, base_url="https://graph.microsoft.us/v1.0/")
```

## Identifiers and `/$ref`

Graph removes a group member with `DELETE /groups/{id}/members/{id}/$ref`. Without
the `/$ref` suffix, the same request addresses the directory object itself. If
the member ID comes from user input and ends in `#`, everything after it becomes
a URL fragment and is never sent:

```python
member_id = "11111111-1111-4111-8111-111111111111#"
f"groups/{group_id}/members/{member_id}/$ref"
# on the wire: DELETE /v1.0/groups/{group_id}/members/11111111-1111-4111-8111-111111111111
```

`graph_object_segment` accepts a canonical UUID and nothing else, so validate
every ID before it goes into a path:

```python
from graphbound import graph_object_segment

group = graph_object_segment(group_id)
member = graph_object_segment(member_id)  # raises InvalidGraphObjectId for the ID above
graph.delete(f"groups/{group}/members/{member}/$ref")
```

The client refuses fragments on its own as a second line of defence.

## Batches

`read_batch_once` posts up to 20 `GET` subrequests to `$batch` exactly once and
refuses batches that contain writes. It does not retry, because each subresponse
carries its own status and `Retry-After`; resend only the ones that failed.

```python
result = graph.read_batch_once(
    [{"id": "1", "method": "GET", "url": "/users/11111111-1111-4111-8111-111111111111"}]
)
```

## Observability

`on_failure` and `on_retry` receive small frozen dataclasses with the method,
attempt number, status, Graph error code and delay. They never contain a URL, a
request or response body, or the provider's message, so they are safe to log.

```python
GraphClient(token, read_policy=ReadRetryPolicy(), on_failure=log.warning, on_retry=log.info)
```

## Errors

| Exception | Meaning |
| --- | --- |
| `GraphError` | Graph answered with an error, or the response was unusable. `details` has the status, code, message and request ID. |
| `GraphRetryExhausted` | A `GraphError` with code `GRAPH_RETRY_EXHAUSTED`: an attempt limit, deadline or sleep cap was reached. |
| `UnsafeGraphUrl` | A `ValueError`. The request was refused before sending. |
| `InvalidGraphObjectId` | A `ValueError`. The value is not a canonical object ID. |
| `requests.Timeout`, `requests.ConnectionError` | Raised unchanged for requests that are not retried. |

`GraphRetryExhausted.reason` is `attempts`, `elapsed`, `sleep`, or `delay` (a server
cooldown exceeding the single-wait limit). `attempts` records actual sends when
raised by `GraphClient`. The error retains the last response status, request IDs
and `retry_after`, when available; status 503 is the fallback when the last attempt
had no response. A late successful response can therefore carry status 200 in an
exhaustion error. No response body or provider message is added to that error.

## What it is not

This is not a replacement for Microsoft's
[`msgraph-sdk`](https://github.com/microsoftgraph/msgraph-sdk-python). There are
no generated models, no async API and no token handling, a 401 is not refreshed
and retried, and endpoints that answer with a redirect to a download are not
supported. A client holds one `requests.Session`, so use one client per thread.

## Development

```
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\Activate.ps1
pip install -e ".[dev]"

ruff check . && ruff format --check .
mypy
pytest
```

The tests never touch the network. URL handling is tested through a real
`requests.Session` with a capturing adapter, so the assertions are about what
would have gone on the wire.

## Background

The client, retry budgets and identifier checks were extracted from the Graph
layer of [Joint Ops](https://jointops.io), a multi-tenant Microsoft 365
administration platform for managed service providers. Host pinning and redirect
refusal were added for this library.

## License

MIT. See [LICENSE](LICENSE).
