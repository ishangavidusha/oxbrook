# Configuration

A service runs with different settings in development, staging and
production: another database, another secret, debug on or off. Those settings
belong in the environment, not in the code, so the same build runs everywhere
and a secret never sits in a repository.

```python
from oxbrook import App, CORS, Settings
from pydantic import SecretStr

class Config(Settings, prefix="APP_"):
    database_url: SecretStr
    session_secret: SecretStr
    allowed_origins: list[str] = []
    debug: bool = False

config = Config()

app = App(debug=config.debug, cors=CORS(allow_origins=config.allowed_origins))
```

`Config()` reads `APP_DATABASE_URL`, `APP_SESSION_SECRET`,
`APP_ALLOWED_ORIGINS` and `APP_DEBUG`. Created at import, as here, a missing
or invalid value stops the app before it serves anything, with every problem
listed at once:

```text
oxbrook: error: Config cannot be read from the environment:
  APP_DATABASE_URL: required, and not set
  APP_DEBUG: Input should be a valid boolean, unable to interpret input
```

The message names variables, never values. A value in the wrong variable may
be a secret, and startup errors end up in logs.

## Settings

`Settings` is a pydantic model, so fields, types, defaults and validators are
pydantic's. Each field is read from the environment variable of the same name
in capitals, after `prefix`:

- Scalars convert as a request body would: `"8000"` to an `int`, `"true"`,
  `"1"`, `"yes"` or `"on"` to `True`.
- A `list`, `tuple` or `set` is plain comma-separated text, `a,b,c`, or JSON
  if it starts with `[`.
- A `dict` or a nested model is JSON.
- `Field(alias="GITHUB_TOKEN")` names the variable exactly, with no prefix.

A value passed to the constructor wins over the environment, which is how a
test sets one:

```python
config = Config(database_url="postgresql://localhost/test")
```

Settings are frozen once read: they are shared by every worker loop and
thread, and a setting changed while the server runs would be one no one could
see. A subclass keeps its parent's prefix unless it states its own.

**Type secrets as `SecretStr`.** Its value is masked in `repr`, in logs and in
tracebacks, and read with `.get_secret_value()` where it is needed.

### Secrets files

```python
class Config(Settings, prefix="APP_", secrets_dir="/run/secrets"):
    database_url: SecretStr
```

Docker and Kubernetes can mount each secret as a file rather than an
environment variable, which keeps it out of the process listing and out of
anything that dumps the environment. With `secrets_dir`, a variable that is
not set is read from the file of the same name, or its lowercase, in that
directory. A trailing newline is dropped. An environment variable still wins.

## Server options

Every `oxbrook run` option can be set in the environment as `OXBROOK_` and its
name:

```bash
OXBROOK_HOST=0.0.0.0 OXBROOK_PORT=8080 OXBROOK_ACCESS_LOG=true oxbrook run main:app
```

A flag on the command line wins over its variable. `PORT` is read after
`OXBROOK_PORT`, because hosting platforms set it and expect the server to
follow. A value that cannot be read stops the command with the variable's
name.

`app.run()` reads none of these: options passed in Python are the ones it
uses.

## Development: an env file

```bash
oxbrook run main:app --reload --env-file .env
```

```bash
# .env, not committed
APP_DATABASE_URL=postgresql://localhost/dev
APP_SESSION_SECRET="dev only, not a secret"
APP_DEBUG=true
```

`--env-file` puts the file's variables into the environment before the app is
imported. A variable already set wins over the file, so a value exported in the
shell overrides it for one run. With `--reload`, editing the file restarts the
server, which reads it again.

The format is `NAME=value` per line. `#` starts a comment, `export ` before a
name is allowed, a value in single quotes is taken as written, and in double
quotes `\n`, `\t`, `\"` and `\\` are escapes. Nothing is interpolated.

Keep `.env` out of version control and out of images; in production, the
platform sets the environment.

## Checking what is needed

```bash
oxbrook settings main:app
```

```text
main.Config
  VARIABLE            TYPE               STATUS
  APP_DATABASE_URL    SecretStr, secret  required, and not set
  APP_SESSION_SECRET  SecretStr, secret  set
  APP_ALLOWED_ORIGINS list[str]          default []
  APP_DEBUG           bool               set
```

Lists every variable the app's `Settings` classes read, with its type and
whether the environment satisfies it now, and exits with status 1 if anything
is missing or invalid. It works even when creating the settings fails, which
is when it is wanted. Values are never shown, set or not. `--json` gives the
same as data, and `--env-file` checks against a file.

Run in a deploy pipeline against the target environment, it fails the deploy
before the old version is replaced, rather than after.
