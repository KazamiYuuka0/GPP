"""simulate.py 功能验证测试"""
from simulate import StockSimulator

def test_basic_buy_sell():
    """测试正常买入卖出流程"""
    sim = StockSimulator()
    r1 = sim.buy("000001.SZ", price=15.0, volume=1000, trade_date="20240102", pre_close=14.5)
    assert r1 is not None, "买入失败"
    assert r1.direction == "BUY"
    assert r1.volume == 1000
    # 成交价 = 15.0 * 1.001 = 15.015
    assert r1.actual_price == 15.015, f"成交价错误: {r1.actual_price}"
    print("[PASS] 基本买入")

def test_t1_restriction():
    """测试 T+1 限制"""
    sim = StockSimulator()
    sim.buy("000001.SZ", price=15.0, volume=1000, trade_date="20240102", pre_close=14.5)
    r = sim.sell("000001.SZ", price=16.0, volume=1000, trade_date="20240102", pre_close=15.2)
    assert r is None, "T+1 限制失效"
    print("[PASS] T+1 同日卖出被拒")

def test_next_day_sell():
    """测试次日正常卖出"""
    sim = StockSimulator()
    sim.buy("000001.SZ", price=15.0, volume=1000, trade_date="20240102", pre_close=14.5)
    r = sim.sell("000001.SZ", price=16.0, volume=1000, trade_date="20240103", pre_close=15.2)
    assert r is not None, "次日卖出失败"
    assert r.direction == "SELL"
    # 卖出成交价 = 16.0 * 0.999 = 15.984
    assert r.actual_price == 15.984, f"卖出成交价错误: {r.actual_price}"
    print("[PASS] 次日正常卖出")

def test_limit_up():
    """测试涨停无法买入"""
    sim = StockSimulator()
    # pre_close=14.5, 涨停价 = round(14.5 * 1.1, 2) = 15.95
    r = sim.buy("000001.SZ", price=16.0, volume=100, trade_date="20240104", pre_close=14.5)
    assert r is None, "涨停应无法买入"
    print("[PASS] 涨停无法买入")

def test_same_day_restriction():
    """测试同日买卖限制"""
    sim = StockSimulator()
    sim.buy("600000.SH", price=10.0, volume=1000, trade_date="20240105", pre_close=9.5)
    r = sim.sell("600000.SH", price=10.5, volume=1000, trade_date="20240105", pre_close=10.0)
    assert r is None, "同日买卖限制失效"
    print("[PASS] 同日买卖限制")

def test_cash_conservation():
    """测试资金守恒"""
    sim = StockSimulator()
    sim.buy("000001.SZ", price=15.0, volume=1000, trade_date="20240102", pre_close=14.5)
    sim.sell("000001.SZ", price=16.0, volume=1000, trade_date="20240103", pre_close=15.2)
    summary = sim.get_summary(current_prices={"000001.SZ": 16.0})

    # 手动计算预期现金
    buy_amount = 15.015 * 1000  # 15015.00
    buy_commission = max(buy_amount * 0.0003, 5)  # 5.00 (最低佣金)
    buy_transfer = buy_amount * 0.00001  # 0.15
    buy_total = buy_amount + buy_commission + buy_transfer

    sell_amount = 15.984 * 1000  # 15984.00
    sell_commission = max(sell_amount * 0.0003, 5)  # 5.00 (最低佣金)
    sell_stamp = sell_amount * 0.0005  # 7.99
    sell_transfer = sell_amount * 0.00001  # 0.16
    sell_net = sell_amount - sell_commission - sell_stamp - sell_transfer

    expected_cash = 1000000 - buy_total + sell_net
    diff = abs(expected_cash - summary["cash"])
    assert diff < 0.01, f"资金不一致: 预期 {expected_cash:.2f}, 实际 {summary['cash']:.2f}, 差 {diff:.4f}"
    print(f"[PASS] 资金守恒 (预期 {expected_cash:.2f}, 实际 {summary['cash']:.2f})")

def test_lot_size():
    """测试最小交易单位"""
    sim = StockSimulator()
    # 买入 50 股，应被拒绝（不足 1 手）
    r = sim.buy("000001.SZ", price=15.0, volume=50, trade_date="20240102", pre_close=14.5)
    assert r is None, "不足 1 手应被拒绝"
    # 买入 150 股，应调整为 100 股
    r = sim.buy("000001.SZ", price=15.0, volume=150, trade_date="20240102", pre_close=14.5)
    assert r is not None and r.volume == 100, f"应调整为100股, 实际 {r.volume if r else 'None'}"
    print("[PASS] 最小交易单位 100 股")

def test_limit_down_sell():
    """测试跌停无法卖出"""
    sim = StockSimulator()
    sim.buy("000001.SZ", price=10.0, volume=1000, trade_date="20240102", pre_close=9.5)
    # pre_close=10.0, 跌停价 = round(10.0 * 0.9, 2) = 9.0
    r = sim.sell("000001.SZ", price=9.0, volume=1000, trade_date="20240103", pre_close=10.0)
    assert r is None, "跌停应无法卖出"
    print("[PASS] 跌停无法卖出")

def test_star_board_limit():
    """测试创业板/科创板 20% 涨跌停"""
    sim = StockSimulator()
    # 创业板 300001, 涨停价 = round(10.0 * 1.2, 2) = 12.0
    r = sim.buy("300001.SZ", price=12.0, volume=100, trade_date="20240102", pre_close=10.0)
    assert r is None, "创业板涨停应无法买入"
    # 11.9 未涨停，可以买入
    r = sim.buy("300001.SZ", price=11.9, volume=100, trade_date="20240103", pre_close=10.0)
    assert r is not None, "创业板 11.9 未涨停，应可买入"
    print("[PASS] 创业板 20% 涨跌停")

def test_position_update():
    """测试持仓均价更新"""
    sim = StockSimulator()
    sim.buy("000001.SZ", price=10.0, volume=1000, trade_date="20240102", pre_close=9.5)
    # pre_close=10.5, 涨停价=11.55, 买入价11.0 未涨停
    sim.buy("000001.SZ", price=11.0, volume=1000, trade_date="20240103", pre_close=10.5)
    pos = sim.positions["000001.SZ"]
    assert pos.volume == 2000, f"持仓量错误: {pos.volume}"
    # 均价 = (10.01*1000 + 11.011*1000) / 2000
    expected_avg = (10.01 * 1000 + 11.011 * 1000) / 2000
    assert abs(pos.avg_cost - expected_avg) < 0.01, f"均价错误: {pos.avg_cost} vs {expected_avg}"
    print(f"[PASS] 持仓均价更新 (均价 {pos.avg_cost:.4f})")

if __name__ == "__main__":
    test_basic_buy_sell()
    test_t1_restriction()
    test_next_day_sell()
    test_limit_up()
    test_same_day_restriction()
    test_cash_conservation()
    test_lot_size()
    test_limit_down_sell()
    test_star_board_limit()
    test_position_update()
    print("\n全部测试通过!")
