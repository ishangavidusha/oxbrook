#!/usr/bin/env python3
"""Configuration: `Settings` read from the environment, and the `oxbrook run`
options, an env file and `oxbrook settings` around it.

The class is tested in process. The command is tested as a user runs it, in
subprocesses with a real environment, because what matters there is which
value wins when a flag, a variable, `PORT` and a file all say something.

Held to account above all: no error message, listing or log line ever
repeats a value from the environment. Every check of that uses a sentinel
that would be found if it leaked.
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from decimal import Decimal
from pathlib import Path

from oxbrook import Settings, SettingsError
from oxbrook._cli import UsageError, read_env_file
from pydantic import BaseModel, Field, SecretStr

failures: list[str] = []
BIN = Path(sys.executable).parent
EXE = ".exe" if sys.platform == "win32" else ""
SENTINEL = "s3ntinel-value-7741"


def check(cond: bool, msg: str) -> None:
    if not cond:
        failures.append(msg)


def raises(exc: type, fn) -> BaseException | None:
    try:
        fn()
    except exc as caught:
        return caught
    return None


class Environment:
    """Variables set for one block and removed after, whatever happens."""

    def __init__(self, **values: str) -> None:
        self.values = values
        self.saved: dict[str, str | None] = {}

    def __enter__(self) -> None:
        for name, value in self.values.items():
            self.saved[name] = os.environ.get(name)
            os.environ[name] = value

    def __exit__(self, *exc) -> None:
        for name, value in self.saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


class Database(BaseModel):
    host: str
    port: int = 5432


class Config(Settings, prefix="T_"):
    database_url: SecretStr
    port: int = 8000
    debug: bool = False
    ratio: float = 0.5
    price: Decimal = Decimal("1.0")
    origins: list[str] = Field(default_factory=list)
    ids: set[int] = Field(default_factory=set)
    limits: dict[str, int] = Field(default_factory=dict)
    database: Database | None = None
    data_dir: Path = Path("/tmp")
    nickname: str | None = None
    token: str = Field(default="none", alias="GITHUB_TOKEN_T")


def values_are_typed() -> None:
    with Environment(T_DATABASE_URL=SENTINEL, T_PORT="9000", T_DEBUG="yes", T_RATIO="0.25",
                     T_PRICE="2.50", T_ORIGINS="https://a.example, https://b.example",
                     T_IDS="[1, 2, 2]", T_LIMITS='{"read": 10}',
                     T_DATABASE='{"host": "db", "port": "6543"}', T_DATA_DIR="/srv/data",
                     T_NICKNAME="ox", GITHUB_TOKEN_T="gh"):
        config = Config()
    check(config.port == 9000 and config.debug is True and config.ratio == 0.25,
          f"scalars read as {config.port!r}, {config.debug!r}, {config.ratio!r}")
    check(config.price == Decimal("2.50"), f"Decimal read as {config.price!r}")
    check(config.origins == ["https://a.example", "https://b.example"],
          f"a comma-separated list read as {config.origins!r}")
    check(config.ids == {1, 2}, f"a JSON list read as {config.ids!r}")
    check(config.limits == {"read": 10}, f"a JSON dict read as {config.limits!r}")
    check(config.database == Database(host="db", port=6543),
          f"a nested model read as {config.database!r}")
    check(config.data_dir == Path("/srv/data"), f"a Path read as {config.data_dir!r}")
    check(config.nickname == "ox" and config.token == "gh",
          f"optional and aliased fields read as {config.nickname!r}, {config.token!r}")
    check(config.database_url.get_secret_value() == SENTINEL, "the secret was not read")
    check(SENTINEL not in repr(config) and SENTINEL not in str(config),
          "a SecretStr's value appeared in repr")

    with Environment(T_DATABASE_URL="x"):
        defaults = Config()
    check(defaults.port == 8000 and defaults.origins == [] and defaults.database is None,
          f"defaults were not used: {defaults!r}")


def problems_are_listed_together() -> None:
    with Environment(T_PORT=SENTINEL, T_DEBUG=SENTINEL, T_LIMITS=SENTINEL,
                     T_DATABASE='{"host": "db", "port": "' + SENTINEL + '"}'):
        error = raises(SettingsError, Config)
    check(error is not None, "invalid settings were accepted")
    if error is None:
        return
    check(set(error.problems) == {"T_DATABASE_URL", "T_PORT", "T_DEBUG", "T_LIMITS",
                                  "T_DATABASE"},
          f"the problems listed were {sorted(error.problems)}")
    check(error.problems.get("T_DATABASE_URL") == "required, and not set",
          f"a missing variable was described as {error.problems.get('T_DATABASE_URL')!r}")
    check(error.problems.get("T_LIMITS") == "not valid JSON",
          f"bad JSON was described as {error.problems.get('T_LIMITS')!r}")
    check(str(error.problems.get("T_DATABASE", "")).startswith("port: "),
          f"a nested field's problem did not name it: {error.problems.get('T_DATABASE')!r}")
    check(SENTINEL not in str(error) and SENTINEL not in repr(error.problems),
          f"a value from the environment appeared in the error:\n{error}")
    check(isinstance(error, ValueError), "SettingsError is not a ValueError")
    check(error.settings is Config, "the error did not carry its class")


def where_values_come_from() -> None:
    class Child(Config):
        extra: int = 1

    class Plain(Settings):
        api_key: str

    secrets = Path(tempfile.mkdtemp(prefix="oxbrook-secrets-"))
    (secrets / "s_password").write_text("from-file\n")
    (secrets / "S_TOKEN").write_text("file-token")

    class Secret(Settings, prefix="S_", secrets_dir=secrets):
        password: SecretStr
        token: str = "default"

    with Environment(T_DATABASE_URL="x", T_EXTRA="5"):
        child = Child()
    check(child.extra == 5, "a subclass did not inherit its parent's prefix")
    with Environment(API_KEY="k"):
        check(Plain().api_key == "k", "no prefix did not read the bare name")
    with Environment(T_DATABASE_URL="x", T_PORT="9000"):
        check(Config(port=1).port == 1, "a constructor argument did not win over the variable")
    secret = Secret()
    check(secret.password.get_secret_value() == "from-file",
          "a secrets file (lowercase name, trailing newline) was not read as written")
    check(secret.token == "file-token", "a secrets file under the variable's name was not read")
    with Environment(S_TOKEN="from-env"):
        check(Secret().token == "from-env", "the environment did not win over a secrets file")
    check(raises(TypeError, lambda: Config(nope=1)) is not None,
          "an unknown constructor argument was accepted")


def frozen_once_read() -> None:
    with Environment(T_DATABASE_URL="x"):
        config = Config()
    check(raises(Exception, lambda: setattr(config, "port", 1)) is not None,
          "a setting could be changed after it was read")
    check(raises(TypeError, lambda: type("Bad", (Settings,), {}, prefix=3)) is not None,
          "a non-string prefix was accepted")


def env_files_are_read() -> None:
    path = Path(tempfile.mktemp(suffix=".env"))
    path.write_text(
        "# a comment\n"
        "\n"
        "export A=1\n"
        "B = two words  # trailing comment\n"
        "C='single # quoted \\n'\n"
        'D="double\\nline \\"q\\" back\\\\slash"\n'
        "E=\n"
        "F=a#b\n"
    )
    values = read_env_file(path)
    expected = {"A": "1", "B": "two words", "C": "single # quoted \\n",
                "D": 'double\nline "q" back\\slash', "E": "", "F": "a#b"}
    check(values == expected, f"the env file read as {values!r}")
    for text, says in (("NOT A LINE\n", "line 1"), (f"1BAD={SENTINEL}\n", "line 1"),
                       (f"OK=1\nX='{SENTINEL}\n", "line 2")):
        path.write_text(text)
        error = raises(UsageError, lambda: read_env_file(path))
        check(error is not None and says in str(error) and SENTINEL not in str(error),
              f"a malformed env file ({text!r}) gave {error!r}")
    error = raises(UsageError, lambda: read_env_file(path.with_suffix(".missing")))
    check(error is not None and "cannot read" in str(error), f"a missing file gave {error!r}")


# ---------------------------------------------------------------------------
# the command
# ---------------------------------------------------------------------------
APP = '''
from oxbrook import App, Request, Settings
from pydantic import SecretStr

class Config(Settings, prefix="APP_"):
    database_url: SecretStr
    greeting: str = "hello"
    origins: list[str] = []

config = Config()
app = App(openapi_url=None, docs_url=None, mcp_url=None)

@app.get("/")
async def root(_: Request):
    return {"greeting": config.greeting, "origins": config.origins}
'''


def project() -> Path:
    directory = Path(tempfile.mkdtemp(prefix="oxbrook-settings-"))
    (directory / "main.py").write_text(APP)
    return directory


def clean_env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("OXBROOK_", "APP_")) and k != "PORT"}
    return {**env, **extra}


def oxb(directory: Path, *args: str, env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run([str(BIN / f"oxb{EXE}"), *args], cwd=directory, env=env,
                          capture_output=True, text=True, timeout=60)


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def get(port: int) -> dict | None:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1) as response:
            return json.loads(response.read())
    except OSError:
        return None


def wait_for(predicate, timeout: float):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = predicate()
        if found:
            return found
        time.sleep(0.1)
    return None


def serving(directory: Path, args: list[str], env: dict[str, str], port: int) -> dict | None:
    """Start `oxb run`, return what `/` answered on `port`, and stop it."""
    server = subprocess.Popen([str(BIN / f"oxb{EXE}"), "run", "main:app", *args], cwd=directory,
                              env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True)
    try:
        return wait_for(lambda: get(port), 20)
    finally:
        server.terminate()
        try:
            server.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            server.kill()
            server.communicate()


def options_come_from_the_environment(directory: Path) -> None:
    base = clean_env(APP_DATABASE_URL=SENTINEL)
    port, other, third = free_port(), free_port(), free_port()
    got = serving(directory, [], {**base, "OXBROOK_PORT": str(port)}, port)
    check(got == {"greeting": "hello", "origins": []}, f"OXBROOK_PORT was not served: {got}")
    got = serving(directory, [], {**base, "PORT": str(other)}, other)
    check(got is not None, "PORT was not served")
    got = serving(directory, [], {**base, "PORT": str(other), "OXBROOK_PORT": str(third)}, third)
    check(got is not None, "OXBROOK_PORT did not win over PORT")
    got = serving(directory, ["--port", str(port)], {**base, "OXBROOK_PORT": str(other)}, port)
    check(got is not None, "--port did not win over OXBROOK_PORT")

    for name, value, says in (("OXBROOK_PORT", SENTINEL, "OXBROOK_PORT must be a whole number"),
                              ("OXBROOK_ACCESS_LOG", SENTINEL, "must be true or false"),
                              ("OXBROOK_LOG_LEVEL", "loud", "log level must be one of")):
        result = oxb(directory, "run", "main:app", env={**base, name: value})
        check(result.returncode == 2 and says in result.stderr,
              f"{name}={value!r} gave exit {result.returncode}: {result.stderr.strip()}")
        check(SENTINEL not in result.stderr + result.stdout, f"{name}'s value was printed")
        check("Traceback" not in result.stderr, f"{name} produced a traceback")

    port = free_port()
    server = subprocess.Popen([str(BIN / f"oxb{EXE}"), "run", "main:app"], cwd=directory,
                              env={**base, "OXBROOK_PORT": str(port),
                                   "OXBROOK_ACCESS_LOG": "true"},
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        check(wait_for(lambda: get(port), 20) is not None, "the access-log server did not start")
    finally:
        server.terminate()
        output, _ = server.communicate(timeout=20)
    check("GET / 200" in output, f"OXBROOK_ACCESS_LOG=true did not log:\n{output}")


def missing_settings_stop_the_start(directory: Path) -> None:
    result = oxb(directory, "run", "main:app", env=clean_env())
    check(result.returncode == 2, f"missing settings exited {result.returncode}")
    check("APP_DATABASE_URL: required, and not set" in result.stderr,
          f"the error did not name the variable:\n{result.stderr}")
    check("Traceback" not in result.stderr, "missing settings produced a traceback")


def an_env_file_fills_the_gaps(directory: Path) -> None:
    port, other = free_port(), free_port()
    (directory / ".env").write_text(
        f'APP_DATABASE_URL="{SENTINEL}"\nAPP_GREETING=from the file\nOXBROOK_PORT={port}\n')
    got = serving(directory, ["--env-file", ".env"], clean_env(APP_ORIGINS="a,b"), port)
    check(got == {"greeting": "from the file", "origins": ["a", "b"]},
          f"the env file and environment together served {got}")
    got = serving(directory, ["--env-file", ".env"],
                  clean_env(APP_GREETING="from the environment", OXBROOK_PORT=str(other)), other)
    check(got is not None and got["greeting"] == "from the environment",
          f"the env file overrode the environment: {got}")
    result = oxb(directory, "run", "main:app", "--env-file", "missing.env", env=clean_env())
    check(result.returncode == 2 and "cannot read the env file" in result.stderr,
          f"a missing env file gave exit {result.returncode}: {result.stderr.strip()}")


def a_reload_rereads_the_env_file(directory: Path) -> None:
    port = free_port()
    env_file = directory / ".env"
    env_file.write_text(f"APP_DATABASE_URL=x\nAPP_GREETING=first\nOXBROOK_PORT={port}\n")
    server = subprocess.Popen(
        [str(BIN / f"oxb{EXE}"), "run", "main:app", "--env-file", ".env", "--reload"],
        cwd=directory, env=clean_env(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True)
    try:
        first = wait_for(lambda: get(port), 20)
        check(first is not None and first["greeting"] == "first",
              f"the reloading server served {first}")
        time.sleep(0.5)
        env_file.write_text(f"APP_DATABASE_URL=x\nAPP_GREETING=second\nOXBROOK_PORT={port}\n")
        second = wait_for(lambda: (g := get(port)) and g["greeting"] == "second" and g, 20)
        check(second is not None,
              "an edit to the env file was not served after the reload it caused")
    finally:
        server.terminate()
        try:
            server.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            server.kill()
            server.communicate()


def the_settings_command(directory: Path) -> None:
    result = oxb(directory, "settings", "main:app", env=clean_env(APP_ORIGINS=SENTINEL))
    check(result.returncode == 1, f"missing settings listed with exit {result.returncode}")
    out = result.stdout
    check("main.Config" in out and "APP_DATABASE_URL" in out and "required, and not set" in out,
          f"the listing did not show the missing variable:\n{out}")
    check('default "hello"' in out, f"a default was not shown:\n{out}")
    check(SENTINEL not in out + result.stderr, f"a value was printed:\n{out}")

    result = oxb(directory, "settings", "main:app", "--json",
                 env=clean_env(APP_DATABASE_URL=SENTINEL, APP_GREETING=SENTINEL))
    check(result.returncode == 0, f"satisfied settings exited {result.returncode}")
    check(SENTINEL not in result.stdout, "--json printed a value")
    rows = {row["variable"]: row for row in json.loads(result.stdout)[0]["variables"]}
    check(rows["APP_DATABASE_URL"]["secret"] is True
          and rows["APP_DATABASE_URL"]["source"] == "environment"
          and rows["APP_DATABASE_URL"]["problem"] is None,
          f"the secret's row was {rows['APP_DATABASE_URL']}")
    check(rows["APP_ORIGINS"]["source"] == "default" and rows["APP_ORIGINS"]["default"] == [],
          f"a default's row was {rows['APP_ORIGINS']}")

    (directory / ".env").write_text("APP_DATABASE_URL=x\n")
    result = oxb(directory, "settings", "main:app", "--env-file", ".env", env=clean_env())
    check(result.returncode == 0, f"settings with --env-file exited {result.returncode}")


def main() -> None:
    for step in (values_are_typed, problems_are_listed_together, where_values_come_from,
                 frozen_once_read, env_files_are_read):
        try:
            step()
            print(f"  {step.__name__}: ok", flush=True)
        except Exception as exc:
            failures.append(f"{step.__name__} raised {type(exc).__name__}: {exc}")
            print(f"  {step.__name__}: ERROR", flush=True)
    directory = project()
    for step in (options_come_from_the_environment, missing_settings_stop_the_start,
                 an_env_file_fills_the_gaps, a_reload_rereads_the_env_file,
                 the_settings_command):
        try:
            step(directory)
            print(f"  {step.__name__}: ok", flush=True)
        except Exception as exc:
            failures.append(f"{step.__name__} raised {type(exc).__name__}: {exc}")
            print(f"  {step.__name__}: ERROR", flush=True)

    if failures:
        print("\nfailures:")
        for f in failures:
            print(f"  - {f}")
    print("\nRESULT:", "FAIL" if failures else "PASS")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
