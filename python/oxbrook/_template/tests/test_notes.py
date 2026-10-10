def test_a_note_round_trip(client):
    created = client.post("/notes", json={"title": "Groceries", "body": "milk"})
    assert created.status_code == 201
    note = created.json()
    assert note["title"] == "Groceries"

    assert client.get(f"/notes/{note['id']}").json() == note
    assert note in client.get("/notes").json()

    replaced = client.put(f"/notes/{note['id']}", json={"title": "Shopping"})
    assert replaced.json() == {"id": note["id"], "title": "Shopping", "body": ""}

    assert client.delete(f"/notes/{note['id']}").status_code == 204
    assert client.get(f"/notes/{note['id']}").status_code == 404


def test_a_bad_note_is_refused(client):
    assert client.post("/notes", json={"title": ""}).status_code == 422
    assert client.post("/notes", json={"body": "no title"}).status_code == 422
    assert client.get("/notes/not-a-number").status_code == 422


def test_a_missing_note_is_404(client):
    assert client.get("/notes/999999").status_code == 404
    assert client.put("/notes/999999", json={"title": "x"}).status_code == 404
    assert client.delete("/notes/999999").status_code == 404


def test_the_server_is_healthy(client):
    assert client.get("/livez").status_code == 200
    assert client.get("/readyz").status_code == 200
