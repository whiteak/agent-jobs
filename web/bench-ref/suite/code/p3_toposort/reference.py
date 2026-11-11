import heapq


def plan(tasks):
    deps = {t: list(dict.fromkeys(d)) for t, d in tasks.items()}
    for d in list(deps.values()):
        for x in d:
            deps.setdefault(x, [])
    indeg = {t: len(d) for t, d in deps.items()}
    users = {t: [] for t in deps}
    for t, d in deps.items():
        for x in d:
            users[x].append(t)
    heap = [t for t, n in indeg.items() if n == 0]
    heapq.heapify(heap)
    order = []
    while heap:
        t = heapq.heappop(heap); order.append(t)
        for u in users[t]:
            indeg[u] -= 1
            if indeg[u] == 0:
                heapq.heappush(heap, u)
    if len(order) == len(deps):
        return ("order", order)
    # find a cycle among remaining nodes: walk dependencies until a repeat
    remaining = {t for t, n in indeg.items() if n > 0}
    start = min(remaining)
    seen = {}
    path = []
    t = start
    while t not in seen:
        seen[t] = len(path); path.append(t)
        t = next(x for x in deps[t] if x in remaining)
    return ("cycle", path[seen[t]:])
