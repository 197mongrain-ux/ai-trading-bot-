from hl_bot.research.pdl_retest import RetestParams, Trade, flow_ok, sanity


def _t(**kw):
    base = dict(coin="BTC", side="short", entry_t=0, entry=100.0, stop=101.0, target=97.0, size=10.0,
                risk_usd=10.0, equity=1000.0, level=100.2, flow="", macro="down")
    base.update(kw)
    return Trade(**base)


def test_sanity_passes_clean_short():
    assert sanity(_t(), RetestParams()) == []


def test_sanity_flags_risk_lev_and_order():
    p = RetestParams()
    assert sanity(_t(risk_usd=25.0), p)
    assert sanity(_t(size=250.0), p)
    assert sanity(_t(stop=99.0), p)


def test_flow_short_uses_tape_then_clv():
    bar = {"o": 100, "h": 101, "l": 99, "c": 99.2, "v": 1}
    assert flow_ok("short", bar, [], -5.0)[0]
    assert flow_ok("short", bar, [], None)[0]
    up = {"o": 99, "h": 101, "l": 98.9, "c": 100.9, "v": 1}
    assert not flow_ok("short", up, [], None)[0]
