"""
股票模拟交易引擎

功能:
  - 模拟 A 股交易（T+1、涨跌停、最小交易单位）
  - 计算交易费用（佣金、印花税、过户费、滑点）
  - 持仓管理（买入冻结、卖出解冻、盈亏计算）
  - 交易明细打印

规则说明:
  - T+1: 今日买入的股票，明日才能卖出；每次买入刷新冻结日期
  - 同日限制: 同一只股票同一天只能执行买入或卖出之一
  - 涨跌停: 涨停无法买入，跌停无法卖出（基于昨收价判断）
  - 最小单位: 买入数量必须是 LOT_SIZE(100) 的整数倍
"""

import config as cfg


# ======================== 数据结构 ========================

class Position:
    """股票持仓"""

    def __init__(self, ts_code: str, volume: int, avg_cost: float, last_buy_date: str):
        self.ts_code = ts_code            # 股票代码
        self.volume = volume              # 持有股数
        self.avg_cost = avg_cost          # 平均成本价
        self.last_buy_date = last_buy_date  # 最近买入日期（T+1 判断用）

    def market_value(self, price: float) -> float:
        """按指定价格计算持仓市值"""
        return self.volume * price

    def update_buy(self, volume: int, actual_price: float, date: str):
        """买入后更新持仓（刷新冻结日期、重算均价）
        
        均价 = (原持仓成本 + 本次成交金额) / 总股数
        注：avg_cost 仅反映股票实际成交价，不含交易费用
        """
        total_cost = self.avg_cost * self.volume + actual_price * volume
        self.volume += volume
        self.avg_cost = total_cost / self.volume if self.volume > 0 else 0
        self.last_buy_date = date  # 刷新冻结日期

    def __repr__(self):
        return (f"Position({self.ts_code}, vol={self.volume}, "
                f"avg_cost={self.avg_cost:.4f}, last_buy={self.last_buy_date})")


class TradeRecord:
    """交易记录"""

    def __init__(self, trade_date: str, ts_code: str, direction: str,
                 volume: int, price: float, actual_price: float,
                 amount: float, fees: dict, profit: float = 0.0):
        self.trade_date = trade_date
        self.ts_code = ts_code
        self.direction = direction        # 'BUY' / 'SELL'
        self.volume = volume
        self.price = price                # 原始价格
        self.actual_price = actual_price  # 实际成交价（含滑点）
        self.amount = amount              # 成交金额
        self.fees = fees                  # 费用明细
        self.profit = profit              # 卖出盈亏

    @property
    def total_cost(self) -> float:
        """总费用（佣金+印花税+过户费）"""
        return sum(self.fees.values())

    def __repr__(self):
        arrow = "→" if self.direction == "BUY" else "←"
        return (f"[{self.trade_date}] {arrow} {self.ts_code} "
                f"{self.volume}股 @{self.actual_price:.4f} "
                f"费用={self.total_cost:.2f}")


# ======================== 模拟引擎 ========================

class StockSimulator:
    """
    股票模拟交易引擎

    用法示例:
        sim = StockSimulator()
        sim.buy("000001.SZ", price=15.0, volume=1000, trade_date="20240102", pre_close=14.5)
        sim.sell("000001.SZ", price=16.0, volume=1000, trade_date="20240103", pre_close=15.2)
        print(sim.get_summary(current_prices={"000001.SZ": 16.0}))
    """

    def __init__(self, initial_capital: float = None, verbose: bool = False):
        self.initial_capital = initial_capital or cfg.INITIAL_CAPITAL
        self.cash = float(self.initial_capital)
        self.positions: dict[str, Position] = {}   # ts_code -> Position
        self.trade_log: list[TradeRecord] = []
        self.verbose = verbose  # 是否打印交易明细（由策略层控制）
        # 记录每日操作: {ts_code: 'buy'/'sell'}，用于同日限制
        self._daily_actions: dict[str, str] = {}
        self._current_date: str = ""  # 当前交易日，跨日时自动清空 _daily_actions

    # -------------------- 内部工具 --------------------

    def _check_new_day(self, trade_date: str):
        """跨日时清空当日操作记录"""
        if trade_date != self._current_date:
            self._current_date = trade_date
            self._daily_actions.clear()

    @staticmethod
    def _round_lot(volume: int) -> int:
        """向下取整到最小交易单位的整数倍"""
        return (volume // cfg.LOT_SIZE) * cfg.LOT_SIZE

    def _get_price_limit(self, ts_code: str) -> float:
        """根据股票代码判断涨跌停幅度"""
        # 创业板: 300xxx，科创板: 688xxx
        code = ts_code.split('.')[0]
        if code.startswith('300') or code.startswith('688'):
            return cfg.PRICE_LIMIT_STAR
        return cfg.PRICE_LIMIT

    def _is_limit_up(self, price: float, pre_close: float, ts_code: str) -> bool:
        """判断是否涨停（涨停 = 无法买入）"""
        if pre_close is None or pre_close <= 0:
            return False
        limit_pct = self._get_price_limit(ts_code)
        # 交易所方式：涨停价 = round(pre_close * (1 + limit_pct), 2)
        limit_price = round(pre_close * (1 + limit_pct), 2)
        return price >= limit_price

    def _is_limit_down(self, price: float, pre_close: float, ts_code: str) -> bool:
        """判断是否跌停（跌停 = 无法卖出）"""
        if pre_close is None or pre_close <= 0:
            return False
        limit_pct = self._get_price_limit(ts_code)
        # 交易所方式：跌停价 = round(pre_close * (1 - limit_pct), 2)
        limit_price = round(pre_close * (1 - limit_pct), 2)
        return price <= limit_price

    def _calc_commission(self, amount: float) -> float:
        """计算佣金"""
        return max(amount * cfg.COMMISSION_RATE, cfg.COMMISSION_MIN)

    def _calc_stamp_tax(self, amount: float) -> float:
        """计算印花税（仅卖出）"""
        return amount * cfg.STAMP_TAX_RATE

    def _calc_transfer_fee(self, amount: float) -> float:
        """计算过户费"""
        return amount * cfg.TRANSFER_FEE_RATE

    def _total_position_value(self, prices: dict = None) -> float:
        """计算当前总持仓市值"""
        total = 0.0
        for pos in self.positions.values():
            if prices and pos.ts_code in prices:
                total += pos.volume * prices[pos.ts_code]
            else:
                total += pos.volume * pos.avg_cost  # 无价格时用成本价估算
        return total

    def _print_trade(self, trade: TradeRecord, status: str = "成功"):
        """打印交易明细（仅 verbose=True 时输出）"""
        if not self.verbose:
            return
        arrow = "【买入】" if trade.direction == "BUY" else "【卖出】"
        print(f"\n{'='*55}")
        print(f"  {arrow}  {status}")
        print(f"  日期:     {trade.trade_date}")
        print(f"  股票:     {trade.ts_code}")
        print(f"  数量:     {trade.volume:,} 股")
        print(f"  委托价:   {trade.price:.4f}")
        print(f"  成交价:   {trade.actual_price:.4f}  (滑点 {cfg.SLIPPAGE*100:.1f}%)")
        print(f"  成交金额: {trade.amount:,.2f} 元")
        print(f"  ─────────────────────────")
        print(f"  佣金:     {trade.fees.get('commission', 0):,.2f} 元")
        if 'stamp_tax' in trade.fees:
            print(f"  印花税:   {trade.fees['stamp_tax']:,.2f} 元")
        print(f"  过户费:   {trade.fees.get('transfer_fee', 0):,.2f} 元")
        print(f"  费用合计: {trade.total_cost:,.2f} 元")
        if trade.direction == "SELL":
            pnl_str = f"{trade.profit:+,.2f}"
            print(f"  ─────────────────────────")
            print(f"  本次盈亏: {pnl_str} 元")
        print(f"  剩余现金: {self.cash:,.2f} 元")
        print(f"{'='*55}")

    # -------------------- 买入 --------------------

    def buy(self, ts_code: str, price: float, volume: int,
            trade_date: str, pre_close: float = None) -> TradeRecord | None:
        """
        买入股票

        Args:
            ts_code:    股票代码，如 "000001.SZ"
            price:      委托价格（收盘价或开盘价等）
            volume:     委托股数（会自动取整到100的整数倍）
            trade_date: 交易日期，格式 "YYYYMMDD"
            pre_close:  昨收价（用于涨跌停判断，None 则跳过该检查）

        Returns:
            TradeRecord 成功 / None 失败
        """
        self._check_new_day(trade_date)

        # ---- 规则检查 ----

        # 同日限制：今日已卖出过该股
        if self._daily_actions.get(ts_code) == 'sell':
            if self.verbose: print(f"  [拒绝] {ts_code} 今日已卖出，不可再买入（同日买卖限制）")
            return None

        # 涨停检查：涨停无法买入
        if self._is_limit_up(price, pre_close, ts_code):
            if self.verbose: print(f"  [拒绝] {ts_code} 涨停，无法买入")
            return None

        # 数量取整
        volume = self._round_lot(volume)
        if volume <= 0:
            if self.verbose: print(f"  [拒绝] 买入数量不足 1 手（{cfg.LOT_SIZE} 股）")
            return None

        # 滑点：买入价偏高
        actual_price = round(price * (1 + cfg.SLIPPAGE), 4)
        original_volume = volume  # 保存原始委托量，用于提示缩减

        # ---- 计算最大可买数量（取所有约束的最小值）----

        # 约束1：单股仓位上限
        total_value = self.cash + self._total_position_value()
        max_single = total_value * cfg.MAX_POSITION_RATIO
        current_value = 0.0
        if ts_code in self.positions:
            current_value = self.positions[ts_code].volume * actual_price
        max_buy_by_single = self._round_lot(int((max_single - current_value) / actual_price))
        if max_buy_by_single <= 0:
            if self.verbose: print(f"  [拒绝] {ts_code} 已达单股仓位上限 ({cfg.MAX_POSITION_RATIO*100:.0f}%)")
            return None

        # 约束2：总仓位上限
        remaining_total = total_value * cfg.MAX_TOTAL_POSITION - self._total_position_value()
        max_buy_by_total = self._round_lot(int(remaining_total / actual_price))
        if max_buy_by_total <= 0:
            if self.verbose: print(f"  [拒绝] 总仓位已达上限 ({cfg.MAX_TOTAL_POSITION*100:.0f}%)，无额度买入")
            return None

        # 约束3：现金可负担（考虑全部费用：佣金 + 过户费）
        # 总花费 = amount + commission + transfer_fee
        # 需考虑最低佣金：小额交易时 commission = COMMISSION_MIN 而非按比例
        # 情形A：按比例佣金 → amount * (1 + COMMISSION_RATE + TRANSFER_FEE_RATE) <= cash
        fee_ratio = 1 + cfg.COMMISSION_RATE + cfg.TRANSFER_FEE_RATE
        max_affordable_A = self.cash / fee_ratio
        # 情形B：最低佣金 → amount * (1 + TRANSFER_FEE_RATE) + COMMISSION_MIN <= cash
        max_affordable_B = (self.cash - cfg.COMMISSION_MIN) / (1 + cfg.TRANSFER_FEE_RATE) \
            if self.cash > cfg.COMMISSION_MIN else 0
        max_affordable_amount = min(max_affordable_A, max_affordable_B)
        max_buy_by_cash = self._round_lot(int(max_affordable_amount / actual_price))
        if max_buy_by_cash <= 0:
            if self.verbose: print(f"  [拒绝] 资金不足: 可用 {self.cash:,.2f} 元")
            return None

        # 取三个约束的最小值，向下取整到整手
        adjusted_volume = min(volume, max_buy_by_single, max_buy_by_total, max_buy_by_cash)
        adjusted_volume = self._round_lot(adjusted_volume)
        if adjusted_volume <= 0:
            if self.verbose: print(f"  [拒绝] 买入数量不足 1 手（{cfg.LOT_SIZE} 股）")
            return None
        if adjusted_volume < original_volume:
            reasons = []
            if adjusted_volume == max_buy_by_single: reasons.append("单股仓位上限")
            if adjusted_volume == max_buy_by_total: reasons.append("总仓位上限")
            if adjusted_volume == max_buy_by_cash: reasons.append("资金限制")
            if self.verbose: print(f"  [调整] {ts_code} 买入量 {original_volume} → {adjusted_volume}（{', '.join(reasons)}）")
        volume = adjusted_volume

        amount = round(actual_price * volume, 2)

        # 精确费用计算（基于最终成交量）
        commission = self._calc_commission(amount)
        transfer_fee = self._calc_transfer_fee(amount)
        total_fees = commission + transfer_fee
        total_cost = amount + total_fees

        # 最终资金校验（安全兜底）
        if total_cost > self.cash:
            if self.verbose: print(f"  [拒绝] 资金不足: 需要 {total_cost:,.2f}，可用 {self.cash:,.2f}")
            return None

        # ---- 执行买入 ----
        self.cash -= total_cost
        fees = {'commission': commission, 'transfer_fee': transfer_fee}

        if ts_code in self.positions:
            pos = self.positions[ts_code]
            pos.update_buy(volume, actual_price, trade_date)
        else:
            self.positions[ts_code] = Position(ts_code, volume, actual_price, trade_date)

        self._daily_actions[ts_code] = 'buy'

        trade = TradeRecord(
            trade_date=trade_date, ts_code=ts_code, direction='BUY',
            volume=volume, price=price, actual_price=actual_price,
            amount=amount, fees=fees
        )
        self.trade_log.append(trade)
        self._print_trade(trade)
        return trade

    # -------------------- 卖出 --------------------

    def sell(self, ts_code: str, price: float, volume: int,
             trade_date: str, pre_close: float = None) -> TradeRecord | None:
        """
        卖出股票

        Args:
            ts_code:    股票代码
            price:      委托价格
            volume:     委托股数
            trade_date: 交易日期
            pre_close:  昨收价（用于涨跌停判断）

        Returns:
            TradeRecord 成功 / None 失败
        """
        self._check_new_day(trade_date)

        # ---- 规则检查 ----

        # 持仓检查
        if ts_code not in self.positions:
            if self.verbose: print(f"  [拒绝] 不持有 {ts_code}，无法卖出")
            return None

        pos = self.positions[ts_code]

        # 同日限制：今日已买入过该股（在数量调整前检查，避免误导提示）
        if self._daily_actions.get(ts_code) == 'buy':
            if self.verbose: print(f"  [拒绝] {ts_code} 今日已买入，不可再卖出（同日买卖限制）")
            return None

        # T+1 检查：last_buy_date == trade_date 表示今日买入，冻结中
        if pos.last_buy_date == trade_date:
            if self.verbose: print(f"  [拒绝] {ts_code} 今日买入，T+1 冻结中，明日方可卖出")
            return None

        # 跌停检查：跌停无法卖出
        if self._is_limit_down(price, pre_close, ts_code):
            if self.verbose: print(f"  [拒绝] {ts_code} 跌停，无法卖出")
            return None

        # 数量取整（先取整再检查，避免零股问题）
        volume = self._round_lot(volume)
        if volume <= 0:
            if self.verbose: print(f"  [拒绝] 卖出数量不足 1 手（{cfg.LOT_SIZE} 股）")
            return None

        if volume > pos.volume:
            # 自动调整为可卖最大整手数量
            volume = self._round_lot(pos.volume)
            if volume <= 0:
                if self.verbose: print(f"  [拒绝] {ts_code} 持仓 {pos.volume} 股不足 1 手，无法卖出")
                return None
            if self.verbose: print(f"  [调整] {ts_code} 卖出量调整为 {volume}（持仓 {pos.volume} 股，取整手）")

        # 滑点：卖出价偏低
        actual_price = round(price * (1 - cfg.SLIPPAGE), 4)
        amount = round(actual_price * volume, 2)

        # 费用计算
        commission = self._calc_commission(amount)
        stamp_tax = self._calc_stamp_tax(amount)
        transfer_fee = self._calc_transfer_fee(amount)
        total_fees = commission + stamp_tax + transfer_fee

        # 计算盈亏
        profit = round((actual_price - pos.avg_cost) * volume - total_fees, 2)

        # ---- 执行卖出 ----
        self.cash += amount - total_fees

        pos.volume -= volume
        if pos.volume <= 0:
            del self.positions[ts_code]

        self._daily_actions[ts_code] = 'sell'

        fees = {'commission': commission, 'stamp_tax': stamp_tax, 'transfer_fee': transfer_fee}
        trade = TradeRecord(
            trade_date=trade_date, ts_code=ts_code, direction='SELL',
            volume=volume, price=price, actual_price=actual_price,
            amount=amount, fees=fees, profit=profit
        )
        self.trade_log.append(trade)
        self._print_trade(trade)
        return trade

    # -------------------- 查询 --------------------

    def portfolio_value(self, current_prices: dict) -> float:
        """
        计算总资产（现金 + 持仓市值）

        Args:
            current_prices: {ts_code: 当前价格}
        """
        market_value = sum(
            pos.volume * current_prices.get(pos.ts_code, pos.avg_cost)
            for pos in self.positions.values()
        )
        return self.cash + market_value

    def get_holdings(self, current_prices: dict = None) -> list[dict]:
        """获取当前持仓列表"""
        holdings = []
        for pos in self.positions.values():
            price = current_prices.get(pos.ts_code, pos.avg_cost) if current_prices else pos.avg_cost
            market_val = pos.volume * price
            pnl = (price - pos.avg_cost) * pos.volume
            pnl_pct = (price / pos.avg_cost - 1) * 100 if pos.avg_cost > 0 else 0
            holdings.append({
                'ts_code': pos.ts_code,
                'volume': pos.volume,
                'avg_cost': round(pos.avg_cost, 4),
                'current_price': round(price, 4),
                'market_value': round(market_val, 2),
                'pnl': round(pnl, 2),
                'pnl_pct': round(pnl_pct, 2),
                'last_buy_date': pos.last_buy_date,
            })
        return holdings

    def get_summary(self, current_prices: dict = None) -> dict:
        """
        获取账户摘要信息

        Returns:
            dict: 包含现金、持仓市值、总资产、收益率、持仓列表等
        """
        total_value = self.portfolio_value(current_prices or {})
        market_value = self._total_position_value(current_prices)
        total_return = (total_value / self.initial_capital - 1) * 100

        return {
            'initial_capital': self.initial_capital,
            'cash': round(self.cash, 2),
            'market_value': round(market_value, 2),
            'total_value': round(total_value, 2),
            'total_return_pct': round(total_return, 2),
            'num_trades': len(self.trade_log),
            'num_holdings': len(self.positions),
            'holdings': self.get_holdings(current_prices),
        }

    def print_summary(self, current_prices: dict = None):
        """打印账户摘要"""
        summary = self.get_summary(current_prices)
        print(f"\n{'='*55}")
        print(f"  账户摘要")
        print(f"{'='*55}")
        print(f"  初始资金:   {summary['initial_capital']:>14,.2f} 元")
        print(f"  现金余额:   {summary['cash']:>14,.2f} 元")
        print(f"  持仓市值:   {summary['market_value']:>14,.2f} 元")
        print(f"  总资产:     {summary['total_value']:>14,.2f} 元")
        print(f"  总收益率:   {summary['total_return_pct']:>+13.2f} %")
        print(f"  交易次数:   {summary['num_trades']:>14} 次")
        print(f"  持仓股票:   {summary['num_holdings']:>14} 只")

        if summary['holdings']:
            print(f"\n  {'股票':<12} {'数量':>8} {'成本':>10} {'现价':>10} {'盈亏':>12} {'盈亏%':>8}")
            print(f"  {'-'*62}")
            for h in summary['holdings']:
                print(f"  {h['ts_code']:<12} {h['volume']:>8,} "
                      f"{h['avg_cost']:>10.4f} {h['current_price']:>10.4f} "
                      f"{h['pnl']:>+12,.2f} {h['pnl_pct']:>+7.2f}%")
        print(f"{'='*55}")

    def print_trade_log(self):
        """打印全部交易记录"""
        if not self.trade_log:
            print("  暂无交易记录")
            return
        print(f"\n  交易记录（共 {len(self.trade_log)} 笔）")
        print(f"  {'日期':<10} {'方向':<6} {'股票':<12} {'数量':>8} {'成交价':>10} {'费用':>10} {'盈亏':>12}")
        print(f"  {'-'*70}")
        for t in self.trade_log:
            pnl_str = f"{t.profit:+,.2f}" if t.direction == 'SELL' else "-"
            print(f"  {t.trade_date:<10} {'买入' if t.direction=='BUY' else '卖出':<6} "
                  f"{t.ts_code:<12} {t.volume:>8,} {t.actual_price:>10.4f} "
                  f"{t.total_cost:>10.2f} {pnl_str:>12}")
