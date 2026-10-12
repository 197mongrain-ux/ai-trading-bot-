from hl_bot.research.setup_search import FEATURES, CoinCtx, Setup, _scales, distance, feature_stats, nearest

DAY, H1 = 86400, 3600


def _bars():
    bars = []
    t0 = 1_780_000_000 // DAY * DAY
    px = 100.0
    for i in range(24 * 40):
        o = px
        px = 100 + (i % 24) * 0.1 - (0.5 if (i // 24) % 2 else 0)
        bars.append({"t": float(t0 + i * H1), "o": o, "h": max(o, px) + 0.2, "l": min(o, px) - 0.2, "c": px, "v": 10.0})
    return bars


def test_ctx_builds_features_without_lookahead():
    ctx = CoinCtx("BTC", _bars())
    s = ctx.setup_at(len(ctx.h1) - 1, require_retest=False)
    assert s is not None and set(FEATURES) <= set(s.f)


def _s(t, **f):
    base = {k: 0.0 for k in FEATURES}
    base.update(f)
    return Setup("BTC", "short", t, 100, 99, 100, 95, base, win=int(f.get("adx_1h", 0) > 1))


def test_nearest_ranks_closest_first():
    lib = [_s(1, adx_1h=0.0), _s(2, adx_1h=5.0), _s(3, adx_1h=10.0)]
    nn, _ = nearest({k: 0.0 for k in FEATURES} | {"adx_1h": 9.0}, lib, k=2)
    assert [s.t for _, s in nn] == [3, 2]
    sc = _scales([s.f for s in lib])
    assert distance(lib[0].f, lib[0].f, sc) == 0


def test_feature_stats_flags_separating_feature():
    lib = [_s(i, adx_1h=float(i)) for i in range(6)]
    top = feature_stats(lib)[0]
    assert top["feature"] == "adx_1h" and top["auc"] == 1.0
