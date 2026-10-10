Build a live scoreboard API for sports games.

- `POST /games/{game_id}/score` with a JSON body `{"home": 2, "away": 1}`
  records the game's current score (both are non-negative integers, otherwise
  `422`) and answers `200` with `{"game_id": "...", "home": 2, "away": 1}`.
- `GET /games/{game_id}` answers the latest score in the same shape, or `404`
  if no score was ever posted for that game.
- `GET /games/{game_id}/live` streams the game's score updates as Server-Sent
  Events. Each event's `data` is the score as JSON, in the same shape. A client
  connected to the stream receives every score posted after it connected,
  within a second, in order. Many clients may watch the same game.

It runs as a single process, so keeping the scores in memory is fine.
