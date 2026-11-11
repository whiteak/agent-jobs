import heapq


def _check(grid):
    if not grid or not all(isinstance(r, str) for r in grid):
        raise ValueError("empty")
    w = len(grid[0])
    if w == 0 or any(len(r) != w for r in grid):
        raise ValueError("not rectangular")
    s = e = None; ns = ne = 0
    for i, row in enumerate(grid):
        for j, ch in enumerate(row):
            if ch == "S":
                s = (i, j); ns += 1
            elif ch == "E":
                e = (i, j); ne += 1
            elif not (ch in "#.abcdABCD" or ch in "123456789"):
                raise ValueError(f"bad char {ch!r}")
    if ns != 1 or ne != 1:
        raise ValueError("need one S and one E")
    return s, e


def cheapest_route(grid, max_keys=0):
    s, e = _check(grid)
    h, w = len(grid), len(grid[0])
    start = (s[0], s[1], 0)
    dist = {start: 0}; prev = {}
    pq = [(0, start)]
    while pq:
        d, st = heapq.heappop(pq)
        if d > dist[st]:
            continue
        r, c, keys = st
        if (r, c) == e:
            path = [(r, c)]
            while st in prev:
                st = prev[st]; path.append((st[0], st[1]))
            return d, path[::-1]
        for dr, dc in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nr, nc = r + dr, c + dc
            if not (0 <= nr < h and 0 <= nc < w):
                continue
            ch = grid[nr][nc]
            if ch == "#":
                continue
            nk = keys
            if ch in "ABCD" and not keys >> (ord(ch) - 65) & 1:
                continue
            if ch in "abcd":
                bit = 1 << (ord(ch) - 97)
                if not keys & bit and bin(keys).count("1") < max_keys:
                    nk = keys | bit
            nd = d + (int(ch) if ch.isdigit() else 1)
            ns = (nr, nc, nk)
            if nd < dist.get(ns, 1 << 60):
                dist[ns] = nd; prev[ns] = st
                heapq.heappush(pq, (nd, ns))
    return None


def best_cost(grid, max_keys):
    r = cheapest_route(grid, max_keys)
    return None if r is None else r[0]
