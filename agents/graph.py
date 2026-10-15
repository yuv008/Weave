"""Read-only graph access for agents.

Agents never write the company graph. ``ReadOnlyGraph`` exposes only
``ro_query``, and FalkorDB itself rejects writes sent through
``GRAPH.RO_QUERY``. A private graph opens only for its owner on a private
surface.
"""

from agents.gate.principal import Principal
from shared.graphdb import connect
from shared.schema import company_graph, private_graph


class ReadOnlyGraph:
    __slots__ = ("_graph", "name")

    def __init__(self, graph, name: str):
        self._graph = graph
        self.name = name

    def ro_query(self, query: str, params: dict | None = None):
        return self._graph.ro_query(query, params or {})


def open_company(tenant: str, db=None) -> ReadOnlyGraph:
    name = company_graph(tenant)
    return ReadOnlyGraph((db or connect()).select_graph(name), name)


def open_private(principal: Principal, db=None) -> ReadOnlyGraph:
    if not principal.reads_private or principal.user_id is None:
        raise PermissionError("the private graph is read only by its owner on a private surface")
    name = private_graph(principal.tenant, principal.user_id)
    return ReadOnlyGraph((db or connect()).select_graph(name), name)
