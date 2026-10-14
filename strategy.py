"""
冲量交易策略

执行方式:
  - 信号在日 T 收盘时计算，但在日 T+1 开盘价成交（消除前瞻偏差）
  - 冲量计算支持两种模式（由 MOMENTUM_USE_CLOSE_TO_CLOSE 控制）:
    - False: 冲量 = 今日收盘 - 今日开盘
    - True:  冲量 = 今日收盘 - 昨日收盘
  - 卖出条件（高止盈）: 利润率 > 高阈值 且 冲量 > 卖出冲量阈值 → 卖出（涨势中获利了结）
  - 卖出条件（低止盈）: 利润率 > 低阈值 且 冲量 < 卖出冲量阈值 → 卖出（跌势中保底获利）
  - 买入条件: 冲量 > 买入冲量阈值，持股种类不超过上限，随机选择符合条件的股票
  - 买卖价格: 次日开盘价（simulate 引擎扣除滑点和手续费）
  - 同日同股买卖不可能碰撞（卖出要求已持有且冲量<0，买入要求未持有且冲量>0）
"""

import os
import pandas as pd
import numpy as np
import config as cfg
from simulate import StockSimulator, Position


class SlopeMomentumStrategy:
    """
    冲量策略

    数据结构说明:
      - self.positions: list[Position]  每次买入创建独立 Position，互不干扰
      - self._pos_index: {ts_code: list[Position]}  按股票代码快速索引
      - 同一股票多次买入 → 多个独立 Position，各自独立判断卖出条件
    """

    def __init__(self, **kwargs):
        # 策略参数
        self.num_stock_types = kwargs.get('num_stock_types', cfg.NUM_STOCK_TYPES)
        self.sell_threshold_low = kwargs.get('sell_threshold_low', cfg.SELL_PROFIT_THRESHOLD_LOW)
        self.sell_threshold_high = kwargs.get('sell_threshold_high', cfg.SELL_PROFIT_THRESHOLD_HIGH)
        self.buy_momentum_threshold = kwargs.get('buy_momentum_threshold', cfg.BUY_MOMENTUM_THRESHOLD)
        self.sell_momentum_threshold = kwargs.get('sell_momentum_threshold', cfg.SELL_MOMENTUM_THRESHOLD)

        # 成交方式：True=当日收盘价，False=次日开盘价（默认）
        self.use_close_price = kwargs.get('use_close_price', False)

        # 冲量计算模式：True=今日收盘-昨日收盘，False=今日收盘-今日开盘
        self.momentum_use_close_to_close = kwargs.get(
            'momentum_use_close_to_close', cfg.MOMENTUM_USE_CLOSE_TO_CLOSE)

        # 数据
        self.daily_data = {}
        self.all_dates = []
        self.prices_by_date = {}
        self.all_codes = []          # 全部可选股票代码列表

        # 交易引擎（仅用于现金管理和汇总统计）
        self.simulator = None

        # 待执行订单（收盘生成，次日开盘执行）
        self.pending_sell = []       # [{'ts_code': str, 'pos_id': int, 'volume': int}, ...]
        self.pending_buy = []        # [{'ts_code': str}, ...]

        # 持仓管理（策略层独立管理，不依赖 simulator.positions）
        self.positions = []          # list[Position]，每次买入创建新条目
        self._pos_index = {}         # {ts_code: list[Position]}

        # 买卖互斥：刚卖出的不买回，刚买入的不卖出
        self._just_sold_codes = set()   # 本轮执行中卖出的股票
        self._just_bought_codes = set() # 本轮执行中买入的股票

        # 统计
        self.daily_total_value = []
        self.trade_count = 0

        # 随机种子（可由外部覆盖，默认取 config）
        self.random_seed = kwargs.get('random_seed', cfg.INITIAL_RANDOM_SEED)

        # 独立随机源（固定种子，保证可复现性）
        self._rng = np.random.RandomState(self.random_seed)

    # ======================== 数据加载 ========================

    def load_data(self):
        """从 CSV 加载沪深300行情数据"""
        data_dir = os.path.join(os.path.dirname(__file__), cfg.DATA_DIR)
        constituents_path = os.path.join(data_dir, "hs300_constituents.csv")
        daily_path = os.path.join(data_dir, "hs300_daily_hfq.csv")

        constituents_df = pd.read_csv(constituents_path)
        self.all_codes = constituents_df["ts_code"].tolist()

        daily_df = pd.read_csv(daily_path)
        daily_df["trade_date"] = daily_df["trade_date"].astype(str)
        if cfg.ADJ_NORM:
            daily_df = SlopeMomentumStrategy._apply_adj_norm(daily_df)

        self.daily_data = {
            code: group.reset_index(drop=True)
            for code, group in daily_df.groupby("ts_code")
        }

        self.all_dates = sorted(daily_df["trade_date"].unique().tolist())

        ts_set = set(self.all_codes)
        self.prices_by_date = {}
        for date, grp in daily_df[daily_df["ts_code"].isin(ts_set)].groupby("trade_date"):
            self.prices_by_date[date] = {
                row["ts_code"]: row.to_dict()
                for _, row in grp.iterrows()
            }

        print(f"数据加载完成: {len(self.all_codes)} 只股票, "
              f"{len(self.all_dates)} 个交易日 "
              f"({self.all_dates[0]} ~ {self.all_dates[-1]})")

    @staticmethod
    def load_data_static():
        """静态加载数据并返回 dict（不打印），供 batch 主进程一次性加载后共享给 worker"""
        data_dir = os.path.join(os.path.dirname(__file__), cfg.DATA_DIR)
        constituents_path = os.path.join(data_dir, "hs300_constituents.csv")
        daily_path = os.path.join(data_dir, "hs300_daily_hfq.csv")

        constituents_df = pd.read_csv(constituents_path)
        all_codes = constituents_df["ts_code"].tolist()

        daily_df = pd.read_csv(daily_path)
        daily_df["trade_date"] = daily_df["trade_date"].astype(str)
        if cfg.ADJ_NORM:
            daily_df = SlopeMomentumStrategy._apply_adj_norm(daily_df)

        daily_data = {
            code: group.reset_index(drop=True)
            for code, group in daily_df.groupby("ts_code")
        }

        all_dates = sorted(daily_df["trade_date"].unique().tolist())

        ts_set = set(all_codes)
        prices_by_date = {}
        for date, grp in daily_df[daily_df["ts_code"].isin(ts_set)].groupby("trade_date"):
            prices_by_date[date] = {
                row["ts_code"]: row.to_dict()
                for _, row in grp.iterrows()
            }

        return {
            'daily_data': daily_data,
            'all_dates': all_dates,
            'prices_by_date': prices_by_date,
            'all_codes': all_codes,
        }

    # ======================== 复权处理 ========================

    @staticmethod
    def _apply_adj_norm(daily_df):
        """
        把后复权价(adj_*)归一化回该股首日的原始价量级，覆盖 open/high/low/close/pre_close。

        为什么这么做:
          - 直接用 adj_* 列: 它是"原始价 × 累计复权因子"，量级会到几百上千元，
            10 万本金按 100 股整手基本买不起 -> 回测失真
          - 直接用原始列: 除权日价格跳空，持仓盈亏与斜率(冲量)信号都会被算错
          - 归一化后: 收益率是真实值(含分红再投资)，价格量级仍接近真实价格，
            100 股整手约束与按资金分配的预算逻辑都不受影响

        注意: 分红按"再投资"计入价格序列，不是现金入账。
        """
        cols = ["open", "high", "low", "close", "pre_close"]
        if not all(f"adj_{c}" in daily_df.columns for c in cols):
            return daily_df                      # 数据里没有复权列，保持原样

        df = daily_df.sort_values(["ts_code", "trade_date"])
        first = df.groupby("ts_code")[["close", "adj_close"]].first()
        scale = first["close"] / first["adj_close"]
        scale = scale.replace([np.inf, -np.inf], np.nan).fillna(1.0)
        df = df.join(scale.rename("_scale"), on="ts_code")
        for c in cols:
            df[c] = (df[f"adj_{c}"] * df["_scale"]).round(4)
        return df.drop(columns=["_scale"]).reset_index(drop=True)

    # ======================== 持仓管理工具 ========================

    def _rebuild_pos_index(self):
        """重建 ts_code → [Position] 索引"""
        self._pos_index = {}
        for pos in self.positions:
            self._pos_index.setdefault(pos.ts_code, []).append(pos)

    def _get_holding_codes(self):
        """当前持有的不同股票代码集合"""
        return set(self._pos_index.keys())

    def _calc_sell_fees(self, amount):
        """计算卖出费用（佣金 + 印花税 + 过户费）"""
        commission = max(amount * cfg.COMMISSION_RATE, cfg.COMMISSION_MIN)
        stamp_tax = amount * cfg.STAMP_TAX_RATE
        transfer_fee = amount * cfg.TRANSFER_FEE_RATE
        return commission, stamp_tax, transfer_fee

    def _calc_buy_fees(self, amount):
        """计算买入费用（佣金 + 过户费）"""
        commission = max(amount * cfg.COMMISSION_RATE, cfg.COMMISSION_MIN)
        transfer_fee = amount * cfg.TRANSFER_FEE_RATE
        return commission, transfer_fee

    def _portfolio_value(self, current_prices):
        """计算当前总资产（现金 + 持仓市值）"""
        market_value = sum(
            pos.volume * current_prices.get(pos.ts_code, pos.avg_cost)
            for pos in self.positions
        )
        return self.simulator.cash + market_value

    # ======================== 回测主循环 ========================

    def run(self, initial_capital: float = None, verbose: bool = False,
             use_close_price: bool = None, print_monthly: bool = True) -> dict:
        """
        运行回测

        流程:
          Day 0: 随机选 a 只股票，等金额以 100 手为单位买满
          Day 1+: 以今日开盘价执行昨日订单 → 以今日收盘价生成新订单
        """
        if not self.prices_by_date:
            self.load_data()

        self.simulator = StockSimulator(initial_capital=initial_capital, verbose=verbose)
        # 清空 simulator 的 positions（策略层自行管理）
        self.simulator.positions = {}

        self.positions = []
        self._pos_index = {}
        self.pending_sell = []
        self.pending_buy = []
        self._just_sold_codes = set()
        self._just_bought_codes = set()
        self.daily_total_value = []
        self.trade_count = 0
        self._rng = np.random.RandomState(self.random_seed)

        if verbose:
            print(f"\n开始回测 | 初始资金: {self.simulator.initial_capital:,.0f} 元")
            print(f"策略参数: 持股种类={self.num_stock_types}, "
                  f"低止盈={self.sell_threshold_low * 100:.1f}%, "
                  f"高止盈={self.sell_threshold_high * 100:.1f}%")
        if use_close_price is not None:
            self.use_close_price = use_close_price

        if verbose:
            price_mode = "当日收盘价" if self.use_close_price else "次日开盘价（消除前瞻偏差）"
            print(f"成交方式: {price_mode}")
            momentum_mode = "今日收盘-昨日收盘" if self.momentum_use_close_to_close else "今日收盘-今日开盘"
            print(f"冲量模式: {momentum_mode}")
            print("-" * 60)

        for date_idx, date in enumerate(self.all_dates):
            today_prices = self.prices_by_date.get(date, {})

            if date_idx == 0:
                # 首日：初始建仓
                self._initial_buy(date, today_prices, verbose)
            else:
                # 执行昨日产生的待执行订单
                self._execute_pending(date, today_prices, verbose)

            # 以今日收盘价生成明日订单
            self._generate_signals(date, date_idx, today_prices, verbose)

            # 记录每日总资产（以收盘价计）
            current_prices = {
                code: data["close"]
                for code, data in today_prices.items()
            }
            total_val = self._portfolio_value(current_prices)
            self.daily_total_value.append((date, total_val))

        # 回测结束，输出摘要
        final_prices = {
            code: data["close"]
            for code, data in self.prices_by_date.get(self.all_dates[-1], {}).items()
        }
        result = self._get_summary(final_prices)
        if verbose:
            self._print_result(result)
        if print_monthly:
            self.print_monthly_summary()
        return result

    # ======================== 初始建仓 ========================

    def _initial_buy(self, date, today_prices, verbose):
        """
        随机选取 a 只股票，金额均等，以 100 手为单位买入
        盈余金额留在现金账户
        """
        available_codes = [c for c in self.all_codes if c in today_prices]
        selected = self._rng.choice(
            available_codes,
            size=min(self.num_stock_types, len(available_codes)),
            replace=False
        ).tolist()

        budget_per_stock = self.simulator.cash / len(selected)

        if verbose:
            print(f"\n[初始建仓] 选取 {len(selected)} 只股票, "
                  f"每只预算 {budget_per_stock:,.0f} 元")

        for ts_code in selected:
            price_key = "close" if self.use_close_price else "open"
            open_price = today_prices[ts_code][price_key]
            actual_price = round(open_price * (1 + cfg.SLIPPAGE), 4)
            max_volume = int(budget_per_stock / actual_price)
            lots = max_volume // cfg.LOT_SIZE
            volume = lots * cfg.LOT_SIZE

            if volume <= 0:
                continue

            amount = round(actual_price * volume, 2)
            commission = max(amount * cfg.COMMISSION_RATE, cfg.COMMISSION_MIN)
            transfer_fee = amount * cfg.TRANSFER_FEE_RATE
            total_cost = amount + commission + transfer_fee

            if total_cost > self.simulator.cash:
                # 缩减一手再试
                lots -= 1
                volume = lots * cfg.LOT_SIZE
                if volume <= 0:
                    continue
                amount = round(actual_price * volume, 2)
                commission = max(amount * cfg.COMMISSION_RATE, cfg.COMMISSION_MIN)
                transfer_fee = amount * cfg.TRANSFER_FEE_RATE
                total_cost = amount + commission + transfer_fee
                if total_cost > self.simulator.cash:
                    continue

            self.simulator.cash -= total_cost

            # avg_cost 包含买入费用，反映真实持仓成本
            real_cost_per_share = round(total_cost / volume, 4)
            pos = Position(ts_code, volume, real_cost_per_share, date)
            self.positions.append(pos)

            self.trade_count += 1
            if verbose:
                print(f"  买入 {ts_code} | {lots}手 ({volume}股) "
                      f"@ {actual_price:.4f} | 花费 {total_cost:,.2f}")

        self._rebuild_pos_index()

        if verbose:
            print(f"  初始建仓完成: {len(self._pos_index)} 只股票, "
                  f"剩余现金 {self.simulator.cash:,.2f}")

    # ======================== 信号计算 ========================

    def _get_prev_close(self, ts_code, date_idx):
        """获取昨日收盘价，若无前一日数据则返回 None"""
        if date_idx <= 0:
            return None
        prev_date = self.all_dates[date_idx - 1]
        prev_prices = self.prices_by_date.get(prev_date, {})
        if ts_code in prev_prices:
            return prev_prices[ts_code]["close"]
        return None

    def _calc_momentum(self, ts_code, close, open_p, date_idx):
        """根据配置模式计算冲量"""
        if self.momentum_use_close_to_close:
            prev_close = self._get_prev_close(ts_code, date_idx)
            if prev_close is not None:
                return close - prev_close
            # 首日无前日数据，回退到 open_to_close
            return close - open_p
        return close - open_p

    def _generate_signals(self, date, date_idx, today_prices, verbose):
        """
        以今日收盘价计算买卖信号

        冲量计算:
          - open_to_close 模式: 冲量 = 收盘 - 开盘
          - close_to_close 模式: 冲量 = 收盘 - 昨日收盘
        卖出: 高止盈(利润率 > 高阈值 且 冲量 > 卖出冲量阈值) 或 低止盈(利润率 > 低阈值 且 冲量 < 卖出冲量阈值)
        买入: 冲量 > 买入冲量阈值，持股种类未达上限，随机选择
        """
        self.pending_sell = []
        self.pending_buy = []

        if not today_prices:
            return

        # ---- 卖出信号（按每笔持仓独立判断）----
        for pos in list(self.positions):
            ts_code = pos.ts_code
            if ts_code not in today_prices:
                continue
            if pos.volume <= 0:
                continue

            row = today_prices[ts_code]
            close = row["close"]
            open_p = row["open"]
            momentum = self._calc_momentum(ts_code, close, open_p, date_idx)

            # 用该笔持仓自己的买入价计算利润
            sell_price = round(close * (1 - cfg.SLIPPAGE), 4)
            sell_amount = round(sell_price * pos.volume, 2)
            commission, stamp_tax, transfer_fee = self._calc_sell_fees(sell_amount)
            total_fees = commission + stamp_tax + transfer_fee

            cost_basis = pos.avg_cost * pos.volume
            profit_pct = ((sell_amount - total_fees) - cost_basis) / cost_basis

            if profit_pct > self.sell_threshold_high and momentum > self.sell_momentum_threshold:
                # 高止盈：利润 > 高阈值 且 冲量 > 阈值 → 卖出（涨势中获利了结）
                if ts_code not in self._just_bought_codes:
                    self.pending_sell.append({
                        'ts_code': ts_code,
                        'pos_id': id(pos),
                        'volume': pos.volume
                    })
            elif profit_pct > self.sell_threshold_low and momentum < self.sell_momentum_threshold:
                # 低止盈：利润 > 低阈值 且 冲量 < 阈值 → 卖出
                if ts_code not in self._just_bought_codes:
                    self.pending_sell.append({
                        'ts_code': ts_code,
                        'pos_id': id(pos),
                        'volume': pos.volume
                    })

        # ---- 买入信号 ----
        holding_codes = self._get_holding_codes()
        available_slots = self.num_stock_types - len(holding_codes)

        if available_slots > 0:
            # 冲量 > 0 的股票
            candidates = []
            for ts_code, row in today_prices.items():
                momentum = self._calc_momentum(ts_code, row["close"], row["open"], date_idx)
                if momentum > self.buy_momentum_threshold and ts_code not in holding_codes \
                        and ts_code not in self._just_sold_codes:
                    candidates.append(ts_code)

            if candidates:
                n_buy = min(available_slots, len(candidates))
                chosen = self._rng.choice(candidates, size=n_buy, replace=False).tolist()
                for ts_code in chosen:
                    self.pending_buy.append({'ts_code': ts_code})

        if verbose and (self.pending_sell or self.pending_buy):
            print(f"\n  [信号 {date}] "
                  f"卖出: {[s['ts_code'] for s in self.pending_sell]} | "
                  f"买入: {[b['ts_code'] for b in self.pending_buy]}")

    # ======================== 执行订单 ========================

    def _execute_pending(self, date, today_prices, verbose):
        """
        以今日开盘价执行昨日收盘产生的订单

        流程:
          1. 执行卖出（释放资金）
          2. 等金额分配剩余现金，执行买入
        """
        if not self.pending_sell and not self.pending_buy:
            # 无订单时也要清空互斥集合，防止过期数据残留
            self._just_sold_codes = set()
            self._just_bought_codes = set()
            return

        # ---- Step 1: 执行卖出（按独立 Position）----
        actual_sold_codes = set()
        for order in self.pending_sell:
            ts_code = order['ts_code']
            if ts_code not in today_prices:
                continue

            # 找到对应的 Position
            target_pos = None
            for pos in self.positions:
                if id(pos) == order['pos_id']:
                    target_pos = pos
                    break
            if target_pos is None:
                continue

            price_key = "close" if self.use_close_price else "open"
            open_price = today_prices[ts_code][price_key]
            actual_price = round(open_price * (1 - cfg.SLIPPAGE), 4)
            volume = target_pos.volume

            sell_amount = round(actual_price * volume, 2)
            commission, stamp_tax, transfer_fee = self._calc_sell_fees(sell_amount)
            total_fees = commission + stamp_tax + transfer_fee

            # 用该笔持仓自己的买入价计算盈亏
            profit = round((actual_price - target_pos.avg_cost) * volume - total_fees, 2)

            # 更新现金
            self.simulator.cash += sell_amount - total_fees

            # 直接移除该 Position
            self.positions.remove(target_pos)
            self._rebuild_pos_index()
            self.trade_count += 1
            actual_sold_codes.add(ts_code)

            if self.simulator.verbose:
                print(f"  [卖出] {ts_code} | {volume}股 "
                      f"@ {actual_price:.4f} (成本={target_pos.avg_cost:.4f}) | "
                      f"费用={total_fees:.2f} | 盈亏={profit:+.2f}")

        # 只记录实际成交的卖出
        self._just_sold_codes = actual_sold_codes

        # ---- Step 2: 执行买入 ----
        if not self.pending_buy:
            self._just_bought_codes = set()
            self.pending_sell = []
            self.pending_buy = []
            return

        available_cash = self.simulator.cash
        if available_cash <= 0:
            self._just_bought_codes = set()
            self.pending_sell = []
            self.pending_buy = []
            return

        budget_per_stock = available_cash / len(self.pending_buy)

        # 只记录实际成交的买入
        actual_bought_codes = set()

        for order in self.pending_buy:
            ts_code = order['ts_code']
            if ts_code not in today_prices:
                continue

            price_key = "close" if self.use_close_price else "open"
            open_price = today_prices[ts_code][price_key]
            actual_price = round(open_price * (1 + cfg.SLIPPAGE), 4)

            # 以预算计算最大可买手数
            max_volume = int(min(budget_per_stock, self.simulator.cash) / actual_price)
            lots = max_volume // cfg.LOT_SIZE
            volume = lots * cfg.LOT_SIZE

            if volume <= 0:
                continue

            buy_amount = round(actual_price * volume, 2)
            commission, transfer_fee = self._calc_buy_fees(buy_amount)
            total_cost = buy_amount + commission + transfer_fee

            if total_cost > self.simulator.cash:
                lots -= 1
                volume = lots * cfg.LOT_SIZE
                if volume <= 0:
                    continue
                buy_amount = round(actual_price * volume, 2)
                commission, transfer_fee = self._calc_buy_fees(buy_amount)
                total_cost = buy_amount + commission + transfer_fee
                if total_cost > self.simulator.cash:
                    continue

            self.simulator.cash -= total_cost

            # avg_cost 包含买入费用，反映真实持仓成本
            real_cost_per_share = round(total_cost / volume, 4)
            pos = Position(ts_code, volume, real_cost_per_share, date)
            self.positions.append(pos)
            self._rebuild_pos_index()
            self.trade_count += 1
            actual_bought_codes.add(ts_code)

            if self.simulator.verbose:
                print(f"  [买入] {ts_code} | {lots}手 ({volume}股) "
                      f"@ {actual_price:.4f} | 花费 {total_cost:,.2f}")

        # 只记录实际成交的买入，用于下一轮信号生成时的互斥
        self._just_bought_codes = actual_bought_codes

        self.pending_sell = []
        self.pending_buy = []

    # ======================== 结果输出 ========================

    def _get_summary(self, current_prices):
        """生成回测摘要"""
        total_value = self._portfolio_value(current_prices)
        market_value = sum(
            pos.volume * current_prices.get(pos.ts_code, pos.avg_cost)
            for pos in self.positions
        )
        total_return = (total_value / self.simulator.initial_capital - 1) * 100

        return {
            'initial_capital': self.simulator.initial_capital,
            'cash': round(self.simulator.cash, 2),
            'market_value': round(market_value, 2),
            'total_value': round(total_value, 2),
            'total_return_pct': round(total_return, 2),
            'num_trades': self.trade_count,
            'num_holdings': len(self._pos_index),
        }

    def _print_result(self, result: dict):
        """打印回测结果摘要"""
        print("\n" + "=" * 60)
        print("  回测结果")
        print("=" * 60)
        print(f"  初始资金:     {result['initial_capital']:>14,.2f} 元")
        print(f"  最终总资产:   {result['total_value']:>14,.2f} 元")
        print(f"  现金余额:     {result['cash']:>14,.2f} 元")
        print(f"  持仓市值:     {result['market_value']:>14,.2f} 元")
        print(f"  总收益率:     {result['total_return_pct']:>+13.2f} %")
        print(f"  交易次数:     {result['num_trades']:>14} 次")
        print(f"  期末持仓:     {result['num_holdings']:>14} 只")

        # 计算最大回撤
        if self.daily_total_value:
            values = [v for _, v in self.daily_total_value]
            peak = values[0]
            max_drawdown = 0.0
            for v in values:
                if v > peak:
                    peak = v
                dd = (peak - v) / peak
                if dd > max_drawdown:
                    max_drawdown = dd
            print(f"  最大回撤:     {max_drawdown * 100:>13.2f} %")

            # 年化收益
            n_days = len(self.daily_total_value)
            if n_days > 1:
                total_return = result['total_value'] / result['initial_capital'] - 1
                annual_return = (1 + total_return) ** (250 / n_days) - 1
                print(f"  年化收益率:   {annual_return * 100:>+13.2f} %")

        print("=" * 60)

    def get_monthly_summary(self):
        """
        按月汇总每日总资产

        返回 list[dict]，每项包含:
          - month: 'YYYY-MM'
          - last_value: 该月最后一个交易日的总资产
          - first_value: 该月第一个交易日的总资产
          - return_pct: 该月收益率 (%)
        """
        if not self.daily_total_value:
            return []

        from collections import OrderedDict
        monthly = OrderedDict()
        for date_str, value in self.daily_total_value:
            # 兼容 YYYY-MM-DD 和 YYYYMMDD 两种格式
            if '-' in date_str:
                ym = date_str[:7]  # 'YYYY-MM'
            else:
                ym = date_str[:6]  # 'YYYYMM' -> 转为 'YYYY-MM'
                ym = f"{ym[:4]}-{ym[4:6]}"
            monthly.setdefault(ym, []).append(value)

        summary = []
        for ym, values in monthly.items():
            first_val = values[0]
            last_val = values[-1]
            ret_pct = (last_val / first_val - 1) * 100 if first_val > 0 else 0.0
            summary.append({
                'month': ym,
                'first_value': round(first_val, 2),
                'last_value': round(last_val, 2),
                'return_pct': round(ret_pct, 2),
            })
        return summary

    def print_monthly_summary(self):
        """打印月度资产汇总"""
        summary = self.get_monthly_summary()
        if not summary:
            return

        print("\n" + "-" * 50)
        print("  月度资产汇总")
        print("-" * 50)
        print(f"  {'月份':<10} {'月初资产':>14} {'月末资产':>14} {'月收益率':>10}")
        print("-" * 50)
        for row in summary:
            print(f"  {row['month']:<10} {row['first_value']:>14,.2f} {row['last_value']:>14,.2f} "
                  f"{row['return_pct']:>+9.2f}%")
        print("-" * 50)


# ======================== 入口 ========================

if __name__ == "__main__":
    strategy = SlopeMomentumStrategy()
    result = strategy.run(verbose=False)
