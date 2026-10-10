"""The /notes routes, their models, and an in-memory store.

The store keeps notes in this process's memory, so they are gone on restart.
Replace it with a database when that matters; the routes stay the same.
"""

from itertools import count
from threading import Lock

from oxbrook import HTTPError, Reply, Request, Router
from pydantic import BaseModel, Field


class NoteIn(BaseModel):
    """What a client sends to create or replace a note."""

    title: str = Field(min_length=1, max_length=200)
    body: str = ""


class Note(NoteIn):
    """A stored note."""

    id: int


class NoteStore:
    """Notes in memory, shared by every worker loop.

    Handlers on different loops run on different threads at once, so every
    access holds the lock.
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self._ids = count(1)
        self._notes: dict[int, Note] = {}

    def all(self) -> list[Note]:
        with self._lock:
            return list(self._notes.values())

    def get(self, note_id: int) -> Note | None:
        with self._lock:
            return self._notes.get(note_id)

    def add(self, note: NoteIn) -> Note:
        with self._lock:
            stored = Note(id=next(self._ids), **note.model_dump())
            self._notes[stored.id] = stored
            return stored

    def replace(self, note_id: int, note: NoteIn) -> Note | None:
        with self._lock:
            if note_id not in self._notes:
                return None
            stored = Note(id=note_id, **note.model_dump())
            self._notes[note_id] = stored
            return stored

    def remove(self, note_id: int) -> bool:
        with self._lock:
            return self._notes.pop(note_id, None) is not None


router = Router(prefix="/notes")


# `tool=True` also offers a route to AI agents over MCP, at /mcp. Only the
# read-only routes are: an agent that can list notes cannot delete them.
@router.get("", tool=True)
async def list_notes(request: Request):
    """List every note."""
    return request.state.notes.all()


@router.post("")
async def create_note(request: Request, note: NoteIn):
    """Create a note and return it with its id."""
    return Reply(request.state.notes.add(note), status=201)


@router.get("/{note_id}", tool=True)
async def get_note(request: Request, note_id: int):
    """Read one note by its id."""
    note = request.state.notes.get(note_id)
    if note is None:
        raise HTTPError(404, f"There is no note {note_id}.")
    return note


@router.put("/{note_id}")
async def replace_note(request: Request, note_id: int, note: NoteIn):
    """Replace a note's title and body."""
    stored = request.state.notes.replace(note_id, note)
    if stored is None:
        raise HTTPError(404, f"There is no note {note_id}.")
    return stored


@router.delete("/{note_id}")
async def delete_note(request: Request, note_id: int):
    """Delete a note. Answers 204 with no body."""
    if not request.state.notes.remove(note_id):
        raise HTTPError(404, f"There is no note {note_id}.")
