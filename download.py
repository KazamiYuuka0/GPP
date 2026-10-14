"""
沪深300成分股历史行情下载脚本 (后复权)
使用 tushare pro 接口

功能:
  1. 获取指定日期范围内沪深300的所有成分股（含中途纳入和剔除）
  2. 下载每只成分股的日频行情数据
  3. 使用复权因子计算后复权价格: 后复权价 = 原始价 × 复权因子
  4. 输出为 CSV 文件

用法:
  python download.py --start 20200101 --end 20231231 --token YOUR_TOKEN
  python download.py --start 20200101 --end 20231231 --token YOUR_TOKEN --output data/result.csv

注意事项:
  - tushare 2000积分可调用 daily / adj_factor / index_weight 接口
  - 每只股票需 2 次 API 调用 (daily + adj_factor)，整体耗时约 5-10 分钟
  - 复权因子会缓存到 .data_cache/adj_cache/ 目录，中断重跑可自动跳过已下载部分
  - 若某只股票获取失败会自动跳过，不影响其余股票
"""

import tushare as ts
import pandas as pd
import os
import sys
import time
import argparse
from datetime import datetime


# ======================== 工具函数 ========================

def ensure_dir(filepath):
    """确保文件所在目录存在"""
    dirpath = os.path.dirname(filepath)
    if dirpath and not os.path.exists(dirpath):
        os.makedirs(dirpath, exist_ok=True)


# ======================== 成分股获取 ========================

def get_hs300_all_constituents(pro, start_date, end_date):
    """
    获取指定日期范围内沪深300的所有成分股代码（含中途纳入和剔除）

    原理:
      index_weight 返回的是调仓日的成分股权重。
      按年分段查询整个日期范围，可以覆盖所有调仓事件，
      从而收集到期间内曾经存在过的全部成分股代码。

    Returns:
        set: 所有成分股的 ts_code 集合
    """
    start_year = int(start_date[:4])
    end_year = int(end_date[:4])

    all_codes = set()
    year = start_year

    while year <= end_year:
        # 按年切分，避免单次查询跨度过大
        q_start = f"{year}0101"
        q_end = f"{year}1231"
        # 裁剪到实际日期范围
        if q_start < start_date:
            q_start = start_date
        if q_end > end_date:
            q_end = end_date

        offset = 0
        while True:
            try:
                df = pro.index_weight(
                    index_code='000300.SH',
                    start_date=q_start,
                    end_date=q_end,
                    offset=offset,
                    limit=2000
                )
            except Exception as e:
                print(f"  [警告] index_weight 查询失败 (offset={offset}): {e}")
                break

            if df is None or df.empty:
                break

            all_codes.update(df['con_code'].unique().tolist())

            if len(df) < 2000:
                break
            offset += 2000
            time.sleep(0.5)

        print(f"  {year}年: 累计发现 {len(all_codes)} 只成分股")
        year += 1
        time.sleep(0.3)

    return all_codes


# ======================== 复权因子 ========================

def get_adj_factors_cached(pro, ts_code, start_date, end_date, cache_dir):
    """
    获取单只股票的复权因子（带本地文件缓存）

    缓存策略:
      每只股票的复权因子保存为独立 CSV 文件。
      如果文件已存在则直接读取，避免重复调用 API。
      删除缓存文件即可强制重新获取。

    Returns:
        DataFrame: 包含 trade_date 和 adj_factor 两列
    """
    safe_name = ts_code.replace('.', '_')
    cache_file = os.path.join(cache_dir, f"{safe_name}.csv")

    # 读取缓存
    if os.path.exists(cache_file):
        try:
            cached = pd.read_csv(cache_file, dtype={'trade_date': str})
            if not cached.empty and 'adj_factor' in cached.columns:
                return cached[['trade_date', 'adj_factor']]
        except Exception as e:
            print(f"    [提示] 缓存文件损坏，重新获取: {e}")
        # 缓存无效，删除后从 API 重新获取
        try:
            os.remove(cache_file)
        except OSError:
            pass

    # 从 tushare 获取
    all_dfs = []
    for batch_start in _date_batches(start_date, end_date):
        batch_end = _batch_end_date(batch_start, end_date)
        try:
            df = pro.adj_factor(
                ts_code=ts_code,
                start_date=batch_start,
                end_date=batch_end
            )
            if df is not None and not df.empty:
                all_dfs.append(df)
        except Exception as e:
            print(f"    [警告] adj_factor 获取失败 ({batch_start}~{batch_end}): {e}")
        time.sleep(0.32)

    if all_dfs:
        result = pd.concat(all_dfs, ignore_index=True)[['trade_date', 'adj_factor']]
        result = result.drop_duplicates(subset=['trade_date'])
        # 写入缓存
        ensure_dir(cache_file)
        result.to_csv(cache_file, index=False)
        return result

    return pd.DataFrame(columns=['trade_date', 'adj_factor'])


def _date_batches(start_date, end_date):
    """将日期范围按年切分为多个批次的起始日期"""
    batches = []
    year = int(start_date[:4])
    end_year = int(end_date[:4])
    while year <= end_year:
        batch_start = f"{year}0101"
        if batch_start < start_date:
            batch_start = start_date
        if batch_start > end_date:
            break
        batches.append(batch_start)
        year += 1
    return batches


def _batch_end_date(batch_start, end_date):
    """计算一个批次的结束日期（当年年底或 end_date 的较小值）"""
    year = int(batch_start[:4])
    batch_end = f"{year}1231"
    return min(batch_end, end_date)


# ======================== 行情下载 ========================

def download_stock_daily(pro, ts_code, start_date, end_date, adj_df):
    """
    下载单只股票的原始日线数据，合并复权因子，计算后复权价格

    后复权公式: 后复权价 = 原始价 × 复权因子

    Returns:
        DataFrame or None: 包含后复权价格的日线数据，失败返回 None
    """
    try:
        daily = pro.daily(
            ts_code=ts_code,
            start_date=start_date,
            end_date=end_date,
            fields='ts_code,trade_date,open,high,low,close,pre_close,change,pct_chg,vol,amount'
        )
    except Exception as e:
        print(f"    [错误] daily 获取失败: {e}")
        return None

    if daily is None or daily.empty:
        return None

    # 合并复权因子
    if not adj_df.empty:
        daily = daily.merge(adj_df, on='trade_date', how='left')
    else:
        daily['adj_factor'] = None

    # 计算后复权价格
    for col in ['open', 'high', 'low', 'close', 'pre_close']:
        new_col = f'adj_{col}'
        daily[new_col] = (daily[col] * daily['adj_factor']).round(4)

    return daily


# ======================== 主流程 ========================

def main():
    parser = argparse.ArgumentParser(
        description='沪深300成分股历史行情下载（后复权）',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python download.py --start 20200101 --end 20231231 --token abc123
  python download.py --start 20150101 --end 20231231 --token abc123 --output data/hs300.csv
        """
    )
    parser.add_argument('--start', required=True, help='开始日期，格式: YYYYMMDD')
    parser.add_argument('--end', required=True, help='结束日期，格式: YYYYMMDD')
    parser.add_argument('--token', required=True, help='tushare API token')
    parser.add_argument('--output', default='data/hs300_daily_hfq.csv',
                        help='输出文件路径 (默认: data/hs300_daily_hfq.csv)')

    args = parser.parse_args()

    # 初始化 tushare
    ts.set_token(args.token)
    pro = ts.pro_api()

    print("=" * 60)
    print("  沪深300成分股历史行情下载（后复权）")
    print(f"  日期范围: {args.start} ~ {args.end}")
    print("=" * 60)

    # ---- 第1步: 获取所有成分股 ----
    print("\n[1/3] 获取沪深300历史成分股列表...")
    all_codes = get_hs300_all_constituents(pro, args.start, args.end)
    all_codes = sorted(all_codes)
    print(f"  共发现 {len(all_codes)} 只成分股（含中途纳入和剔除）")

    # 保存成分股清单
    ensure_dir(args.output)
    constituents_file = os.path.join(
        os.path.dirname(args.output) or '.',
        'hs300_constituents.csv'
    )
    pd.DataFrame({'ts_code': all_codes}).to_csv(constituents_file, index=False)
    print(f"  成分股清单已保存: {constituents_file}")

    # ---- 第2步: 逐只下载行情 + 复权因子 ----
    print(f"\n[2/3] 下载行情数据与复权因子（共 {len(all_codes)} 只股票）...")
    adj_cache_dir = '.data_cache/adj_cache'

    all_data = []
    failed = []

    for i, code in enumerate(all_codes):
        pct = (i + 1) / len(all_codes) * 100
        sys.stdout.write(f"\r  [{i+1:>3}/{len(all_codes)}] ({pct:5.1f}%) {code}              ")
        sys.stdout.flush()

        # 获取复权因子（带缓存）
        adj_df = get_adj_factors_cached(pro, code, args.start, args.end, adj_cache_dir)

        # 下载行情并计算后复权
        df = download_stock_daily(pro, code, args.start, args.end, adj_df)

        if df is not None and not df.empty:
            all_data.append(df)
        else:
            failed.append(code)

        # 频率控制（tushare 限频约 200次/分钟）
        time.sleep(0.32)

    print()  # 换行

    if failed:
        print(f"  [提示] {len(failed)} 只股票获取失败，已跳过: {failed[:10]}"
              + (" ..." if len(failed) > 10 else ""))

    # ---- 第3步: 汇总输出 ----
    print(f"\n[3/3] 汇总并保存...")

    if all_data:
        result = pd.concat(all_data, ignore_index=True)
        # 排序: 先按股票代码，再按日期
        result = result.sort_values(['ts_code', 'trade_date']).reset_index(drop=True)

        ensure_dir(args.output)
        result.to_csv(args.output, index=False)

        print(f"\n  输出文件: {args.output}")
        print(f"  总行数:   {len(result):,}")
        print(f"  股票数:   {result['ts_code'].nunique()}")
        print(f"  日期范围: {result['trade_date'].min()} ~ {result['trade_date'].max()}")
        print(f"  列名:     {list(result.columns)}")
    else:
        print("\n  [错误] 未获取到任何数据，请检查日期范围和 token 是否有效。")

    print("\n" + "=" * 60)
    print("  下载完成！")
    print("=" * 60)


if __name__ == '__main__':
    import sys

    # ==================== 调试配置 ====================
    # 直接运行脚本时使用以下配置，无需命令行参数
    # 将 DEBUG_MODE 设为 True 并填写 TOKEN 即可直接运行
    DEBUG_MODE = True
    TOKEN = "<已脱敏>"  # <-- 在这里填写你的 tushare token
    START_DATE = "20200101"
    END_DATE = "20241231"
    OUTPUT_FILE = "data/hs300_daily_hfq.csv"
    # ==================================================

    if DEBUG_MODE:
        if not TOKEN:
            print("[错误] 请先在脚本顶部的 TOKEN 变量中填写你的 tushare token")
            sys.exit(1)
        sys.argv = [
            sys.argv[0],
            '--start', START_DATE,
            '--end', END_DATE,
            '--token', TOKEN,
            '--output', OUTPUT_FILE,
        ]

    main()
