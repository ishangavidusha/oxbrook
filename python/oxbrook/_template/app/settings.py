"""Configuration, read from the environment once, at import.

Each field is the variable of the same name in capitals after the prefix:
`debug` is `APP_DEBUG`. A missing or invalid value stops the app at startup
with every problem listed; `oxbrook settings app.main:app` shows them without
starting it.
"""

from oxbrook import Settings


class AppSettings(Settings, prefix="APP_"):
    # Exception text in 500 responses. Development only.
    debug: bool = False


settings = AppSettings()
