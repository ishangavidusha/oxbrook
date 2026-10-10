Build a bookmarks API backed by PostgreSQL.

The database's URL is in the `DATABASE_URL` environment variable. Create the
table at startup if it does not exist. Use an async PostgreSQL driver
(`asyncpg` is installed).

- `POST /bookmarks` with a JSON body `{"url": "...", "title": "...", "tags": ["..."]}`
  creates a bookmark and answers `201` with it, including an integer `id` and
  a `created_at` timestamp in ISO 8601. `url` must start with `http://` or
  `https://`, otherwise the answer is `422`. `tags` is optional and defaults to
  an empty list.
- `GET /bookmarks` lists bookmarks, newest first. `GET /bookmarks?tag=python`
  lists only those with that tag.
- `GET /bookmarks/{id}` answers the bookmark, or `404`.
- `DELETE /bookmarks/{id}` answers `204`, or `404` if there is no such
  bookmark.

It will serve many clients at once in production.
