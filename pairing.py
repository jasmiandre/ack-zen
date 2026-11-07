"""Deterministic, staff-editable event pairing suggestions. Pure functions, no I/O.

Cost per pair (lower = better): repeat 1000 >> same gender 100 >> level distance*10 (same level 0, adjacent 10).
Unknown/unrecognised level = treated as 20 (never invented as a member trait). Odd counts: one person rests per round,
chosen by fewest rests so far (rotating). Imbalanced genders: leftovers are paired same-gender at the lowest cost.
ponytail: greedy + 2-opt, not optimal matching; fine for <=MAX_PEOPLE, swap for Hungarian/blossom if events grow.
"""
LEVELS = ("Pemula", "Menengah", "Mahir")
MAX_ROUNDS, MAX_PEOPLE = 6, 60


def cost(a, b, seen):
    """a,b = (id, gender, level)."""
    c = 1000 if frozenset((a[0], b[0])) in seen else 0
    c += 0 if a[1] != b[1] else 100
    if a[2] in LEVELS and b[2] in LEVELS:
        c += abs(LEVELS.index(a[2]) - LEVELS.index(b[2])) * 10
    else:
        c += 20
    return c


def _round(people, seen):
    pairs = []
    pool = sorted(people)
    edges = sorted((cost(a, b, seen), a[0], b[0], a, b) for i, a in enumerate(pool) for b in pool[i + 1:])
    used = set()
    for _, _, _, a, b in edges:
        if a[0] not in used and b[0] not in used:
            used |= {a[0], b[0]}
            pairs.append((a, b))
    improved = True
    while improved:   # 2-opt: swap partners while total cost drops (strict decrease => terminates)
        improved = False
        for i in range(len(pairs)):
            for j in range(i + 1, len(pairs)):
                (a, b), (c, d) = pairs[i], pairs[j]
                cur = cost(a, b, seen) + cost(c, d, seen)
                for x, y in (((a, c), (b, d)), ((a, d), (b, c))):
                    if cost(*x, seen) + cost(*y, seen) < cur:
                        pairs[i], pairs[j] = x, y
                        improved = True
                        break
    return sorted(((min(a[0], b[0]), max(a[0], b[0])) for a, b in pairs))


def suggest(people, rounds, history=()):
    """people: [(id, gender, level)]; history: iterable of (id,id) already met. Returns [[(id, id|None), ...] per round]."""
    if not 1 <= rounds <= MAX_ROUNDS or len(people) > MAX_PEOPLE:
        raise ValueError("rounds/people out of bounds")
    people = sorted(people)
    if len({p[0] for p in people}) != len(people):
        raise ValueError("duplicate person")
    seen = {frozenset(p) for p in history}
    rests = {p[0]: 0 for p in people}
    out = []
    for _ in range(rounds):
        play, rest = list(people), None
        if len(play) % 2:
            rest = min(play, key=lambda p: (rests[p[0]], p[0]))
            play.remove(rest)
            rests[rest[0]] += 1
        prs = _round(play, seen) if len(play) > 1 else []
        seen |= {frozenset(p) for p in prs}
        out.append(prs + ([(rest[0], None)] if rest else []))
    return out


def _selfcheck():
    M, F = "Laki-laki", "Perempuan"
    P = [(1, M, "Pemula"), (2, M, "Mahir"), (3, F, "Pemula"), (4, F, "Mahir")]
    r = suggest(P, 1)[0]   # opposite gender + same level first
    assert r == [(1, 3), (2, 4)], r
    rs = suggest(P, 3)   # 3 rounds of 4 people: all 6 pairs distinct, no repeats
    flat = [frozenset(p) for rd in rs for p in rd]
    assert len(set(flat)) == 6, rs
    r = suggest([(1, M, "Pemula"), (2, F, "Menengah"), (3, F, "Mahir"), (4, M, "Mahir")], 1)[0]   # adjacent level beats 2 apart
    assert r == [(1, 2), (3, 4)] or r == [(1, 2), (3, 4)], r
    odd = suggest([(i, M if i % 2 else F, "Pemula") for i in range(1, 6)], 5)   # rest rotates, each rests once
    assert sorted(p[0] for rd in odd for p in rd if p[1] is None) == [1, 2, 3, 4, 5], odd
    assert all(len({x for p in rd for x in p if x}) == 5 for rd in odd)
    imb = suggest([(1, M, "Pemula"), (2, M, "Pemula"), (3, M, "Mahir"), (4, F, "Pemula")], 1)[0]
    assert (1, 4) in imb and (2, 3) in imb, imb
    assert suggest([], 2) == [[], []] and suggest([(1, M, None)], 1) == [[(1, None)]]
    try:
        suggest(P, 7)
        raise SystemExit("bound")
    except ValueError:
        pass


if __name__ == "__main__":
    _selfcheck()
    print("pairing ok")
