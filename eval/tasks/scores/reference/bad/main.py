import asyncio

from oxbrook import SSE, App, HTTPError, Request
from pydantic import BaseModel, Field

app = App()
scores: dict[str, dict] = {}
watchers: dict[str, list[asyncio.Queue]] = {}


class Score(BaseModel):
    home: int = Field(ge=0)
    away: int = Field(ge=0)


@app.post("/games/{game_id}/score")
async def post_score(_: Request, game_id: str, score: Score):
    current = {"game_id": game_id, "home": score.home, "away": score.away}
    scores[game_id] = current
    for queue in watchers.get(game_id, []):
        queue.put_nowait(current)
    return current


@app.get("/games/{game_id}")
async def latest(_: Request, game_id: str):
    if game_id not in scores:
        raise HTTPError(404)
    return scores[game_id]


@app.get("/games/{game_id}/live")
async def live(_: Request, game_id: str):
    queue: asyncio.Queue = asyncio.Queue()
    watchers.setdefault(game_id, []).append(queue)

    async def updates():
        while True:
            yield await queue.get()

    return SSE(updates())
