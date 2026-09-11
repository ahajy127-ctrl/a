from risk_guard import live_entry_gate, require_verified_protection

def test_default_live_gate():
    r = live_entry_gate(mode='live', agent_ok=True, coin='BTC', whitelist={'BTC'},
                        leverage=3, max_leverage=5, margin=5, capital=100,
                        used_margin=0, max_total_risk=.10, position_count=0,
                        max_positions=3, backstop_enabled=True, require_backstop=True)
    assert r.allowed

def test_reject_unprotected():
    r = require_verified_protection(sl_installed=False, require_backstop=True)
    assert not r.allowed

def test_reject_risk_over_budget():
    r = live_entry_gate(mode='live', agent_ok=True, coin='BTC', whitelist={'BTC'},
                        leverage=3, max_leverage=5, margin=6, capital=100,
                        used_margin=5, max_total_risk=.10, position_count=0,
                        max_positions=3, backstop_enabled=True, require_backstop=True)
    assert not r.allowed

if __name__ == '__main__':
    test_default_live_gate(); test_reject_unprotected(); test_reject_risk_over_budget(); print('OK')
