"""
参数网格搜索批处理脚本

遍历 NUM_STOCK_TYPES × SELL_THRESHOLD_LOW × SELL_THRESHOLD_HIGH × RANDOM_SEED_LIST 的所有组合，
运行回测并汇总结果，输出最优参数。
"""

import os
import time
import pandas as pd
from concurrent.futures import ProcessPoolExecutor, as_completed
from strategy import SlopeMomentumStrategy

# ======================== 并行配置 ========================
# 工作进程数，None 表示自动检测（取逻辑处理器数的一半，接近物理核心数）
NUM_WORKERS = 8  # 例如设为 4 表示固定 4 个进程

# ======================== 搜索空间（在此修改参数范围）========================
#
# 持股种类上限 NUM_STOCK_TYPES
#   起始值: 5
#   终止值: 50（含）
#   步  长: 5
#   生成值: 5, 10, 15, 20, 25, 30, 35, 40, 45, 50
#
NUM_STOCK_TYPES_START = 40
NUM_STOCK_TYPES_STOP  = 150   # 含
NUM_STOCK_TYPES_STEP  = 10

#
# 低止盈阈值 SELL_PROFIT_THRESHOLD_LOW
#   起始值: 0.000  (0%)
#   终止值: 0.050  (5%，含）
#   步  长: 0.005  (0.5%)
#
SELL_THRESHOLD_LOW_START = 0.025
SELL_THRESHOLD_LOW_STOP  = 0.025  # 含
SELL_THRESHOLD_LOW_STEP  = 0.005

#
# 高止盈阈值 SELL_PROFIT_THRESHOLD_HIGH
#   起始值: 0.050  (5%)
#   终止值: 0.150  (15%，含）
#   步  长: 0.010  (1%)
#
SELL_THRESHOLD_HIGH_START = 0.35
SELL_THRESHOLD_HIGH_STOP  = 0.35  # 含
SELL_THRESHOLD_HIGH_STEP  = 0.05

#
# 随机种子列表 RANDOM_SEED_LIST
#   每个种子独立跑一遍，用于验证参数稳健性
#   设为 [None] 表示仅使用 config 中的默认种子
#
RANDOM_SEED_LIST = [
    466, 392, 861, 622, 593, 612, 644, 467, 544, 814, 480, 909, 885, 175, 380, 859, 146, 404, 657, 704, 713, 8, 38, 42,
    888, 92, 632, 220, 459, 358, 118, 406, 416, 426, 232, 831, 230, 17, 471, 402, 695, 251, 904, 676, 671, 998, 745,
    621, 900, 878,
]

# ======================== 由上方参数自动生成 ========================
NUM_STOCK_TYPES_RANGE = list(
    range(NUM_STOCK_TYPES_START, NUM_STOCK_TYPES_STOP + 1, NUM_STOCK_TYPES_STEP)
)
SELL_THRESHOLD_LOW_RANGE = [
    round(SELL_THRESHOLD_LOW_START + i * SELL_THRESHOLD_LOW_STEP, 4)
    for i in range(int(round((SELL_THRESHOLD_LOW_STOP - SELL_THRESHOLD_LOW_START) / SELL_THRESHOLD_LOW_STEP)) + 1)
]
SELL_THRESHOLD_HIGH_RANGE = [
    round(SELL_THRESHOLD_HIGH_START + i * SELL_THRESHOLD_HIGH_STEP, 4)
    for i in range(int(round((SELL_THRESHOLD_HIGH_STOP - SELL_THRESHOLD_HIGH_START) / SELL_THRESHOLD_HIGH_STEP)) + 1)
]

# ======================== Worker 预加载（每个进程只读一次 CSV）========================

_worker_data = None   # 每个 worker 进程的全局数据缓存


def _worker_init():
    """Worker 进程启动时调用，预加载 CSV 数据（每个 worker 只读一次磁盘）"""
    global _worker_data
    _worker_data = SlopeMomentumStrategy.load_data_static()


# ======================== 子进程执行函数 ========================

def _run_single(args):
    """单次回测（在子进程中执行，复用 worker 预加载的数据，避免重复读 CSV）"""
    num_stocks, threshold_low, threshold_high, initial_capital, seed = args
    strategy = SlopeMomentumStrategy(
        num_stock_types=num_stocks,
        sell_threshold_low=threshold_low,
        sell_threshold_high=threshold_high,
        random_seed=seed,
        momentum_use_close_to_close=True,
    )
    # 复用 worker 预加载的数据（零 I/O 开销）
    strategy.daily_data = _worker_data['daily_data']
    strategy.all_dates = _worker_data['all_dates']
    strategy.prices_by_date = _worker_data['prices_by_date']
    strategy.all_codes = _worker_data['all_codes']

    result = strategy.run(initial_capital=initial_capital, verbose=False,
                           use_close_price=True, print_monthly=False)
    return {
        'num_stock_types': num_stocks,
        'sell_threshold_low': threshold_low,
        'sell_threshold_high': threshold_high,
        'random_seed': seed,
        'total_return_pct': result['total_return_pct'],
        'total_value': result['total_value'],
        'cash': result['cash'],
        'market_value': result['market_value'],
        'num_trades': result['num_trades'],
        'num_holdings': result['num_holdings'],
        'monthly_summary': strategy.get_monthly_summary(),
    }


# ======================== 主流程 ========================

def run_grid_search(initial_capital=None, verbose=False):
    """
    网格搜索所有参数组合（多进程并行）

    返回 DataFrame，按总收益率降序排列
    """
    # 构建任务列表
    tasks = []
    for num_stocks in NUM_STOCK_TYPES_RANGE:
        for threshold_low in SELL_THRESHOLD_LOW_RANGE:
            for threshold_high in SELL_THRESHOLD_HIGH_RANGE:
                if threshold_high <= threshold_low:
                    continue  # 高止盈必须大于低止盈
                for seed in RANDOM_SEED_LIST:
                    tasks.append((num_stocks, threshold_low, threshold_high, initial_capital, seed))

    total = len(tasks)
    max_workers = NUM_WORKERS if NUM_WORKERS else max(1, os.cpu_count() // 2)

    print(f"参数网格搜索 | 共 {total} 种组合 | 进程数={max_workers}")
    print(f"  NUM_STOCK_TYPES:        {NUM_STOCK_TYPES_RANGE}")
    print(f"  SELL_THRESHOLD_LOW:     {SELL_THRESHOLD_LOW_RANGE}")
    print(f"  SELL_THRESHOLD_HIGH:    {SELL_THRESHOLD_HIGH_RANGE}")
    print(f"  RANDOM_SEED_LIST:       {RANDOM_SEED_LIST}")
    print("-" * 70)

    t0 = time.time()
    results = []
    done = 0

    with ProcessPoolExecutor(max_workers=max_workers,
                              initializer=_worker_init) as executor:
        futures = {executor.submit(_run_single, task): task for task in tasks}
        for future in as_completed(futures):
            row = future.result()
            results.append(row)
            done += 1

            if verbose:
                elapsed = time.time() - t0
                avg_per_run = elapsed / done
                eta = avg_per_run * (total - done)
                print(f"  [{done:3d}/{total}] "
                      f"return={row['total_return_pct']:+7.2f}%  "
                      f"stocks={row['num_stock_types']:3d}  low={row['sell_threshold_low']:.4f}  high={row['sell_threshold_high']:.4f}  "
                      f"seed={row['random_seed']}  "
                      f"ETA={eta:.0f}s")

    elapsed_total = time.time() - t0
    print("-" * 70)
    print(f"搜索完成 | 耗时 {elapsed_total:.1f}s")

    df = pd.DataFrame(results)
    df = df.sort_values('total_return_pct', ascending=False).reset_index(drop=True)

    # 打印 Top 10（按总收益率降序，只展示三列）
    top_cols = ['total_return_pct', 'num_stock_types', 'sell_threshold_low', 'sell_threshold_high', 'random_seed']
    print("\n===== Top 50 参数组合（按总收益率降序）=====")
    print(df[top_cols].head(50).to_string(index=False))

    # 最优参数
    best = df.iloc[0]
    print(f"\n最优参数:")
    print(f"  NUM_STOCK_TYPES        = {int(best['num_stock_types'])}")
    print(f"  SELL_THRESHOLD_LOW     = {best['sell_threshold_low']:.4f}")
    print(f"  SELL_THRESHOLD_HIGH    = {best['sell_threshold_high']:.4f}")
    print(f"  总收益率               = {best['total_return_pct']:+.2f}%")
    print(f"  随机种子               = {int(best['random_seed'])}")
    print(f"  交易次数               = {int(best['num_trades'])}")

    # 打印所有参数组合的月度明细（每组展示所有 seed）
    _print_all_monthly(df)

    return df


def _print_all_monthly(df):
    """打印所有参数组合的月度资产汇总（每组展示所有 seed）"""
    param_cols = ['num_stock_types', 'sell_threshold_low', 'sell_threshold_high']

    # 按参数组合分组，按平均收益率降序排列
    group_avg = df.groupby(param_cols)['total_return_pct'].mean().reset_index()
    group_avg = group_avg.sort_values('total_return_pct', ascending=False)

    for _, params in group_avg.iterrows():
        ns = int(params['num_stock_types'])
        tl = params['sell_threshold_low']
        th = params['sell_threshold_high']

        # 筛选该参数组合的所有 seed
        mask = (df['num_stock_types'] == ns) & \
               (df['sell_threshold_low'] == tl) & \
               (df['sell_threshold_high'] == th)
        group = df[mask].sort_values('total_return_pct', ascending=False)

        if group.empty:
            continue

        avg_ret = group['total_return_pct'].mean()
        print(f"\n{'=' * 70}")
        print(f"  月度明细 | stocks={ns}  low={tl:.4f}  high={th:.4f}  "
              f"({len(group)} seeds, avg={avg_ret:+.2f}%)")
        print(f"{'=' * 70}")

        # 取第一个种子的月度结构作为表头
        first_summary = group.iloc[0]['monthly_summary']
        if not first_summary:
            continue
        months = [r['month'] for r in first_summary]

        # 表头: seed + 每个月份 + 总收益
        header = f"  {'seed':>6}"
        for m in months:
            header += f" {m:>12}"
        header += f" {'总收益':>10}"
        print(header)
        print("-" * len(header))

        for _, row in group.iterrows():
            seed = int(row['random_seed'])
            summary = row['monthly_summary']
            line = f"  {seed:>6}"
            for m_row in summary:
                line += f" {m_row['last_value']:>12,.0f}"
            line += f" {row['total_return_pct']:>+9.2f}%"
            print(line)
        print("-" * len(header))


if __name__ == "__main__":
    df = run_grid_search()
    # 可选：导出到 CSV
    # df.to_csv("grid_search_results.csv", index=False)
