"""
模拟股票交易 - 全局常量配置
"""

# ======================== 资金与账户 ========================
INITIAL_CAPITAL = 100_000       # 初始资金（元）
CURRENCY = "CNY"                  # 币种

# ======================== 交易成本 ========================
COMMISSION_RATE = 0.0003          # 佣金费率（万三，单边）
COMMISSION_MIN = 5.0              # 最低佣金（元）
STAMP_TAX_RATE = 0.0005           # 印花税率（万五，仅卖出）
TRANSFER_FEE_RATE = 0.00001       # 过户费费率（万分之一）
SLIPPAGE = 0.001                  # 滑点（0.1%）

# ======================== 交易规则 ========================
LOT_SIZE = 100                    # 最小交易单位（手 = 100股）
PRICE_LIMIT = 0.10               # 涨跌停板幅度（10%，创业板/科创板另设）
PRICE_LIMIT_STAR = 0.20          # 创业板/科创板涨跌停（20%）
T_PLUS_N = 1                     # T+1 交易制度

# ======================== 基准与指数 ========================
BENCHMARK_INDEX = "000300.SH"    # 基准指数（沪深300）
RISK_FREE_RATE = 0.025           # 无风险利率（年化，约2.5%）

# ======================== 回测参数 ========================
REBALANCE_FREQ = "daily"       # 调仓频率: daily / weekly / monthly
LOOKBACK_DAYS = 250              # 回看窗口期（交易日）
MAX_POSITION_RATIO = 0.10        # 单只股票最大仓位占比
MAX_TOTAL_POSITION = 1.00        # 最大总仓位

# ======================== 冲量策略参数 ========================
NUM_STOCK_TYPES = 50             # 持股种类上限（同时持有的不同股票数量）
SELL_PROFIT_THRESHOLD_LOW = 0.025   # 低止盈阈值（3%），需冲量<0才卖出
SELL_PROFIT_THRESHOLD_HIGH = 0.08  # 高止盈阈值（8%），需冲量>0才卖出（涨势中获利了结）
BUY_MOMENTUM_THRESHOLD = 0       # 买入冲量阈值（冲量 > 此值才买入，0 表示收盘>开盘）
SELL_MOMENTUM_THRESHOLD = 0      # 卖出冲量阈值（冲量 < 此值才卖出，0 表示收盘<开盘）
INITIAL_RANDOM_SEED = 42         # 初始选股随机种子（可设为 None 使用随机值）
MOMENTUM_USE_CLOSE_TO_CLOSE = True  # 冲量计算模式: True=今日收盘-昨日收盘, False=今日收盘-今日开盘
MAX_CONSECUTIVE_LOSS_RATIO = 0.1     # 连续亏损提前终止比例（连续亏损笔数 > 总交易笔数 × 此比例 → 终止）

# ======================== 数据参数 ========================
DATA_DIR = "data"                # 数据存放目录
CACHE_DIR = ".data_cache"        # 缓存目录
ADJ_METHOD = "hfq"              # 复权方式: hfq(后复权) / qfq(前复权)
ADJ_NORM = True                  # True=后复权(归一化到首日价, 消除除权跳空); False=原始价
