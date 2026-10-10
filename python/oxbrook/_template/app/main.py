"""The application: settings, what lives as long as the server, and the routes."""

from contextlib import asynccontextmanager

from oxbrook import App, Health

from . import notes
from .settings import settings


@asynccontextmanager
async def lifespan(_app: App):
    # Runs once per process. What it yields is `request.state` in every
    # handler. A connection pool or async client does not go here: it belongs
    # to one event loop, and the server runs several. Make those in a
    # `worker_lifespan`, which runs once per loop (see AGENTS.md).
    yield {"notes": notes.NoteStore()}


app = App(
    title="{{title}}",
    version="0.1.0",
    debug=settings.debug,
    lifespan=lifespan,
    health=Health(),
)
app.include(notes.router)
