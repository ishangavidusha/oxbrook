from threading import Lock

from oxbrook import SSE, App, HTTPError, Request
from pydantic import BaseModel, Field

app = App()
scores: dict[str, dict] = {}
lock = Lock()


class Score(BaseModel):
    home: int = Field(ge=0)
    away: int = Field(ge=0)


@app.post("/games/{game_id}/score")
async def post_score(_: Request, game_id: str, score: Score):
    current = {"game_id": game_id, "home": score.home, "away": score.away}
    with lock:
        scores[game_id] = current
    await app.topic(f"game:{game_id}").emit(current)
    return current


@app.get("/games/{game_id}")
async def latest(_: Request, game_id: str):
    with lock:
        current = scores.get(game_id)
    if current is None:
        raise HTTPError(404)
    return current


@app.get("/games/{game_id}/live")
async def live(_: Request, game_id: str):
    return SSE(app.topic(f"game:{game_id}").subscribe())
