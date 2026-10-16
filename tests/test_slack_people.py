from app.slack.people import enrich_slack_authors


class _Client:
    def __init__(self, profiles):
        self.profiles = profiles

    def user_profile(self, user_id: str) -> dict:
        return self.profiles[user_id]


def test_viewer_email_takes_the_authored_edges(graph, monkeypatch):
    monkeypatch.setenv("WEAVE_VIEWER_EMAIL", "viewer@example.com")
    monkeypatch.setenv("WEAVE_VIEWER_LEVEL", "1")
    monkeypatch.setenv("WEAVE_VIEWER_DEPT", "engineering")
    graph.query(
        """
        CREATE (p:Person {id: 't:slack:person:T:U1', tenant_id: 't', slack_id: 'U1'})
        CREATE (m:Message {id: 'm1', tenant_id: 't'})
        CREATE (p)-[:AUTHORED]->(m)
        CREATE (m)-[:AUTHORED_BY]->(p)
        CREATE (q:Person {id: 't:slack:person:T:U2', tenant_id: 't', slack_id: 'U2'})
        """
    )
    result = enrich_slack_authors(
        graph,
        _Client({"U1": {"name": "Viewer", "email": "viewer@example.com"}, "U2": {"name": "Ada", "email": ""}}),
        "t",
    )
    assert result["merged"] is True
    viewer = graph.query(
        "MATCH (p:Person {id: 't:person:viewer@example.com'}) RETURN p.slack_id, p.level, p.dept, p.name"
    ).result_set
    assert viewer == [["U1", 1, "engineering", "Viewer"]]
    assert graph.query("MATCH (p:Person {id: 't:slack:person:T:U1'}) RETURN count(p)").result_set[0][0] == 0
    authors = graph.query(
        "MATCH (m:Message {id: 'm1'})-[:AUTHORED_BY]->(p) RETURN p.id"
    ).result_set
    assert authors == [["t:person:viewer@example.com"]]
    other = graph.query("MATCH (p:Person {id: 't:slack:person:T:U2'}) RETURN p.name, p.level").result_set
    assert other == [["Ada", None]]
