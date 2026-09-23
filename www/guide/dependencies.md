# Dependencies

A handler argument defaulted to `Depends(...)` is resolved before the handler
runs.

```python
from oxbrook import Depends

async def get_db(request):
    pool = request.state.pool           # this worker loop's pool
    db = await pool.acquire()
    try:
        yield db
    finally:
        await pool.release(db)          # runs after the handler, even on error

@app.get("/users")
async def list_users(_: Request, db = Depends(get_db)):
    return await db.fetch("select ...")
```

A dependency is any callable. It may take the request or take nothing, be sync
or async, be a plain function or a generator.

The pool comes from `request.state` rather than a module-level variable because
each worker loop needs its own: an asyncio pool cannot be shared between loops.
[Lifespan](lifespan.md) is where it gets created.

## Why this exists

Resolving a value could just as well be a function call at the top of the
handler. Releasing a resource could not. An async generator dependency gets its
teardown run after the handler returns, including when the handler raised, and
that guarantee is what a helper function called inside the handler cannot
provide.

## Teardown sees the outcome

A generator dependency is finished the way a `with` block is. If the handler
returned, the generator resumes after its `yield`. If the handler raised, the
same exception is raised at the `yield`. So a transaction is written the way it
reads:

```python
async def transaction(request):
    async with request.state.pool.acquire() as db:
        async with db.transaction():    # commit if the handler returned,
            yield db                    # roll back if it raised

@app.post("/users")
async def create_user(_: Request, user: NewUser, db = Depends(transaction)):
    return await db.fetchrow("insert into users ... returning *", ...)
```

An `HTTPError` raised by the handler is an exception like any other, so it rolls
back too. So does a request cancelled because its client left.

Teardown runs before the response is sent, and whatever it raises is the
request's outcome. A commit that fails is an error response, never the success
the handler returned. An exception handler registered for the driver's error
applies to it as it would to one the handler raised:

```python
@app.exception_handler(asyncpg.UniqueViolationError)
async def duplicate(request, exc):
    raise HTTPError(409, "already exists")
```

A dependency can also translate an error at its `yield` by raising a different
one, as leaving a `with` block would. One that catches the handler's exception
and raises nothing does not turn the request into a success: there is no
response to send in its place, so the original error stands.

Several dependencies are torn down in reverse order of setup, each seeing the
exception left by the one before it, and all of them run even if one raises.
A dependency must yield exactly once.

## Caching

Results are cached per request, so a dependency shared by three others runs
once.

```python
async def current_user(session = Depends(get_session)):
    return await lookup(session["user_id"])

async def permissions(user = Depends(current_user)):
    return user.permissions

@app.get("/admin")
async def admin(_: Request, user = Depends(current_user), perms = Depends(permissions)):
    # current_user ran once, not twice
    ...
```

## Sub-dependencies

A dependency can declare dependencies of its own, to any depth, and they
resolve the same way.

## What dependencies are not

Dependencies are not parameters. An argument defaulted to `Depends` is left out
of the OpenAPI document and out of an MCP tool's argument schema, because it is
not something a client supplies.
