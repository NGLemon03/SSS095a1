# ** coding: utf-8 **
'''
Optuna-based hyper-parameter optimization for 00631L strategies (Version 10, adjusted)
--------------------------------------------------
* 最佳化目標: 優先追求完整回測報酬(>300%)、交易次數(15-60次),允許輕微過擬合,其次考慮 walk_forward 期間最差報酬、夏普比率、最大回撤,最後考慮壓力測試平均報酬.
* 本腳本可直接放在 analysis/ 目錄後以 'python {version}.py' 執行.
* 搜尋空間與權重在 'PARAM_SPACE' 與 'SCORE_WEIGHTS' 中設定,方便日後微調.
* 使用 sklearn 的 TimeSeriesSplit 替代 mlfinlab 的 CPCV, 自定義 PBO 函數, 確保策略在 OOS 期間穩定表現, 並分析交易時機.
v4  數據源,策略分流: 透過 WEIGHTS 設定抽樣比例,增加 WF、Sharpe、MDD 指標,Min-Max Scaling 使高報酬更具區分度.過擬合懲罰
v5  修正日誌衝突: 移除全局日誌處理器覆蓋, 改用獨立日誌模組 (logging_config)；確保多版並行不互相影響.
v6  修正數據載入問題: 統一預載數據, 避免重複加載導致 SMAA 快取不一致.
    CPCV + PBO: 使用 total_return 作主要 metric, 並同時檢查 profit_factor、max_drawdown、OOS 標準差.
    SRA 簡化: 比較策略與 buy-and-hold 的 Sharpe Ratio, p-value >0.05 扣 10% 分數.
    交易時機分析: 計算「買/賣距離高低點天數」, 超過門檻扣分.
    交易日限制: 確保所有時序切割只在交易日索引上執行.
v7  修正壓力測試期間: 新增交易日檢查, 確保 valid_stress_periods 非空, 避免無效或空白計算導致指標失真.
v8  修正 _backtest_once 返回結構: 確保所有分支統一回傳 6+1 欄()含 equity_curve), 避免解包錯誤.
v9  修正 Equity Curve 缺失: 新增 equity_curve 傳遞與驗證, 修復壓力測試相關函式計算.
    加入多散點圖: 分別繪製 Sharpe vs Return、MDD vs Return, 視覺化各策略群表現.
fix 改進 Stress MDD 計算: 改用 Excess Return in Stress 取代 fail_ratio
    Robust Score 計算
    錯誤處理強化: 調整 CSV/JSON 輸出邏輯, 避免空值與索引錯誤.
v10 支援單一數據源模式 (fixed)、隨機 (random)、依序遍歷 (sequential) 三種選擇.
    新增平均持倉天數指標: 回測時計算 avg_hold_days, 並可做硬篩或懲罰.
    參數‐指標相關係數分析: 對各策略試驗結果計算皮爾森相關係數, 並輸出 CSV/熱圖.
    與最佳化參數作相關性測試: 將最終 trial_results 與 best_params 做交叉檢驗.
* 命令列範例: 
# 隨機選數據源
python optuna_10.py --strategy RMA --n_trials 10000  
# 指定數據源
python optuna_10.py --strategy single --data_source "Factor (^TWII / 2412.TW)" --data_source_mode fixed --n_trials 1000  
# 依序遍歷所有數據源
python optuna_10.py --strategy ssma_turn --data_source_mode sequential --n_trials 2000  
'''
from typing import Tuple, List, Dict, Optional
import logging
from logging_config import setup_logging
import optuna
import numpy as np
import re
import sys
import pandas as pd
import shutil
from datetime import datetime, timedelta
from sklearn.neighbors import NearestNeighbors
from sklearn.preprocessing import MinMaxScaler
from sklearn.model_selection import TimeSeriesSplit
import argparse
from pathlib import Path
from scipy.stats import ttest_ind
import matplotlib.pyplot as plt
from scipy.stats import pearsonr
import seaborn as sns
from scipy.spatial.distance import cdist
import ast
import json
from metrics import calculate_sharpe, calculate_max_drawdown, calculate_profit_factor
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from analysis import config as cfg
from analysis import data_loader
import SSSv095b2 as SSS
from SSSv095b2 import load_data, compute_ssma_turn_combined, compute_single, compute_dual, compute_RMA, backtest_unified

parser = argparse.ArgumentParser(description='Optuna 最佳化 00631L 策略')
parser.add_argument('--strategy', type=str, choices=['single', 'dual', 'RMA', 'ssma_turn', 'all'], default='all', help='指定單一策略進行最佳化 (預設: all)')
parser.add_argument('--n_trials', type=int, default=5000, help='試驗次數 (預設: 5000)')
parser.add_argument('--data_source', type=str, choices=cfg.SOURCES, default=None, help='指定單一數據源, 僅在 --data_source_mode=fixed 時有效')
parser.add_argument('--data_source_mode', type=str, choices=['random', 'fixed', 'sequential'], default='random', help='數據源選擇模式: random(隨機)、fixed(指定)、sequential(依序遍歷)')
args = parser.parse_args()

EVENT_COL = "Event Type"
RESULT_FLAG = "試驗結果"
# 在檔案頂部修改
REQUIRED_COLS = {"trial_number", "score", "strategy", "data_source"}
TIMESTAMP = datetime.now().strftime('%Y%m%d_%H%M%S')
results_log = []
events_log = []
top_trials = []  # 儲存分數前 20 名試驗的 equity_curve
# 版本與紀錄
version = 'optuna_13'
setup_logging()
logger = logging.getLogger('optuna_13')
# 設定 matplotlib 字體
plt.rcParams['font.family'] = 'sans-serif'
plt.rcParams['font.sans-serif'] = ['Microsoft YaHei', 'SimHei', 'DejaVu Sans']  # 優先使用中文支援字體
plt.rcParams['axes.unicode_minus'] = False


TICKER = cfg.TICKER
COST_PER_SHARE = cfg.BUY_FEE + cfg.SELL_FEE
COOLDOWN_BARS = cfg.TRADE_COOLDOWN_BARS
WF_PERIODS = [(p["test"][0], p["test"][1]) for p in cfg.WF_PERIODS]
STRESS_PERIODS = cfg.STRESS_PERIODS
STRAT_FUNC_MAP = {'single': SSS.compute_single, 'dual': SSS.compute_dual, 'RMA': SSS.compute_RMA, 'ssma_turn': SSS.compute_ssma_turn_combined}
DATA_SOURCES = cfg.SOURCES
DATA_SOURCES_WEIGHTS = {'Self': 1/3, 'Factor (^TWII / 2412.TW)': 1/3, 'Factor (^TWII / 2414.TW)': 1/3} # 每個數據來源的權重
STRATEGY_WEIGHTS = {'single': 0.25, 'dual': 0.25, 'RMA': 0.25, 'ssma_turn': 0.25} # 每個策略的權重
MIN_NUM_TRADES = 10 # 最小交易次數
MAX_NUM_TRADES = 240 # 最大交易次數
CPCV_NUM_SPLITS = 7 # CPCV 的分割數
CPCV_EMBARGO_DAYS = 15 # CPCV 的 embargo 天數
min_splits = 3 # CPCV 的最小分割數
pct_threshold_self = 5 # pick_topN_by_diversity 的歐式距離門檻
setlimit = False # 是否適用資料過濾
setminsharpe = 0.45 # 資料過濾的最小 Sharpe
setmaxsharpe = 0.75 # 資料過濾的最大 Sharpe
setminmdd = -0.2 # 資料過濾的最小 MDD
setmaxmdd = -0.4 # 資料過濾的最大 MDD

PARAM_SPACE = {
    "single": dict(
        linlen=(5, 240, 1),smaalen=(7, 240, 5),devwin=(5, 180, 1),
        factor=(40, 40, 1),buy_mult=(0.1, 2.5, 0.05),sell_mult=(0.5, 4.0, 0.05),stop_loss=(0.00, 0.55, 0.2),),
    "dual": dict(
        linlen=(5, 240, 1),smaalen=(7, 240, 5),short_win=(10, 100, 5),long_win=(40, 240, 10),
        factor=(40, 40, 1),buy_mult=(0.2, 2, 0.05),sell_mult=(0.5, 4.0, 0.05),stop_loss=(0.00, 0.55, 0.1),),
    "RMA": dict(
        linlen=(5, 240, 1),smaalen=(7, 240, 5),rma_len=(20, 100, 5),dev_len=(10, 100, 5),
        factor=(40, 40, 1),buy_mult=(0.2, 2, 0.05),sell_mult=(0.5, 4.0, 0.05),stop_loss=(0.00, 0.55, 0.1),),
    "ssma_turn": dict(
        linlen=(10, 240, 5),smaalen=(10, 240, 5),factor=(40.0, 40.0, 1),prom_factor=(5, 70, 1),
        min_dist=(5, 20, 1),buy_shift=(0, 7, 1),exit_shift=(0, 7, 1),vol_window=(5, 90, 5),quantile_win=(5, 180, 10),
        signal_cooldown_days=(1, 7, 1),buy_mult=(0.5, 2, 0.05),sell_mult=(0.2, 3, 0.1),stop_loss=(0.00, 0.55, 0.1),),
}

IND_BT_KEYS = {strat: cfg.STRATEGY_PARAMS[strat]["ind_keys"] + cfg.STRATEGY_PARAMS[strat]["bt_keys"] for strat in PARAM_SPACE}
SCORE_WEIGHTS = dict(total_return=2.5, profit_factor=0.2, wf_min_return=0.2, sharpe_ratio=0.2, max_drawdown=0.1)
def serialize_equity_curve(equity_curve: pd.Series, default_start: str = '2014-10-23', default_end: str = '2025-06-17') -> dict:
    """
    將 equity_curve 序列化為 JSON 相容的字典，確保索引為字串格式。
    
    Args:
        equity_curve: 包含資產曲線的 pandas Series，索引為日期。
        default_start: 預設起始日期，若 equity_curve 無效。
        default_end: 預設結束日期，若 equity_curve 無效。
    
    Returns:
        dict: 鍵為字串日期（YYYY-MM-DD），值為浮點數的字典。
    """
    if not isinstance(equity_curve, pd.Series) or equity_curve.empty:
        logger.warning("equity_curve 無效，使用預設值")
        index = pd.date_range(start=default_start, end=default_end, freq='B')
        equity_curve = pd.Series(index=index, data=100000.0)
    
    # 確保索引為字串格式，處理 NaT 值
    equity_curve = equity_curve.copy()  # 避免修改原始資料
    
    # SSS 已經正確處理了 equity_curve，不需要額外的 NaN 填充
    # 只需要安全地轉換索引為字串格式
    
    # 安全地轉換索引為字串，處理 NaT 值
    def safe_strftime(x):
        try:
            if pd.isna(x) or x is None:
                return '2014-10-23'  # 預設日期
            # 檢查是否為 NaT
            if hasattr(x, 'strftime'):
                return x.strftime('%Y-%m-%d')
            else:
                return '2014-10-23'  # 預設日期
        except:
            return '2014-10-23'  # 預設日期
    
    equity_curve.index = equity_curve.index.map(safe_strftime)
    
    return {idx: float(val) for idx, val in equity_curve.items()}
# 新增多樣性篩選函數
def pick_topN_by_diversity(trials: List[Dict], ind_keys: List[str], top_n: int = 20, pct_threshold: int = 25) -> List[Dict]:
    """
    先按 score 排序，再用歐氏距離過濾參數相似度。
    
    Args:
        trials: 試驗結果列表，每個試驗包含 score 和 parameters。
        ind_keys: 用於計算距離的參數鍵。
        top_n: 最終選取的試驗數量。
        pct_threshold: 距離門檻的百分位數。
    
    Returns:
        List[Dict]: 篩選後的試驗列表。
    """
    trials_sorted = sorted(trials, key=lambda t: -t["score"])
    vectors = []
    chosen = []
    for tr in trials_sorted:
        vec = np.array([tr["parameters"].get(k, 0) for k in ind_keys])
        if not vectors:
            vectors.append(vec)
            chosen.append(tr)
            continue
        dists = cdist([vec], vectors, metric="euclidean").ravel()
        if np.min(dists) >= np.percentile(dists, pct_threshold):
            vectors.append(vec)
            chosen.append(tr)
        if len(chosen) == top_n:
            break
    return chosen

def sanitize(data: str) -> str:
    """
    過濾檔案名稱中的特殊字符，將非字母數字字符替換為單一底線。
    Args:
        data: 要過濾的字符串。
    Returns:
        str: 安全的檔案名稱字符串。
    """
    # 將所有非字母數字字符替換為底線
    name = re.sub(r'[^0-9A-Za-z]', '_', data)
    # 移除連續底線
    name = re.sub(r'_+', '_', name).strip('_')
    return name

def _avg_holding_days(trades):
    """
    計算交易的平均持倉天數.
    Args:
        trades: 交易列表, 每筆交易為 (entry_date, return, exit_date).
    Returns:
        float: 平均持倉天數, 若無交易則返回 0.0.
    """
    if not trades:
        return 0.0
    d = [(pd.Timestamp(t[2]) - pd.Timestamp(t[0])).days for t in trades if len(t) > 2]

    return float(np.mean(d)) if d else 0.0

def penalize_hold(avg_hold_days: float, target: float = 150, span: float = 90, max_penalty: float = 0.3) -> float:
    """
    計算平均持倉天數的懲罰值, 目標範圍 60 - 240 天.
    Args:
        avg_hold_days: 平均持倉天數.
        target: 目標持倉天數(中心值) = 150.
        span: 容忍範圍(±span) = 90.
        max_penalty: 最大懲罰比例 = 0.3.
    Returns:
        float: 懲罰值 = 0 ~ 0.3 (max_penalty)
    """
    penalty = max(0, abs(avg_hold_days - target) - span) / span
    return min(penalty * max_penalty, max_penalty)



def log_to_results(event_type: str, details: str, **kwargs):
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S,%f')[:-3]
    record = {"Timestamp": timestamp, "Event Type": event_type, "Details": details, **kwargs}
    logger.debug(f"記錄事件: event_type={event_type}, 記錄鍵={list(record.keys())}")
    if event_type in ["試驗結果", "試驗被剔除", "試驗完成"]:  # 新增試驗被剔除和試驗完成
        results_log.append(record)
    events_log.append(record)  # 所有事件均記錄至 events_log        

def load_results_to_list(csv_file: Path) -> list:
    if not csv_file.exists():
        logger.warning(f"結果檔案 {csv_file} 不存在, 無法轉回列表")
        return []
    df_results = pd.read_csv(csv_file)
    return df_results.to_dict('records')
def _sample_params(trial: optuna.Trial, strat: str) -> dict:
    space = PARAM_SPACE[strat]
    params = {}
    for k, v in space.items():
        if isinstance(v[0], int):
            low, high, step = int(v[0]), int(v[1]), int(v[2])
            params[k] = trial.suggest_int(k, low, high, step=step)
        else:
            low, high, step = v
            params[k] = round(trial.suggest_float(k, low, high, step=step), 3)
    return params

def minmax(x, lo, hi, clip=True):
    y = (x - lo) / (hi - lo)
    return np.clip(y, 0, 1) if clip else y

def buy_and_hold_return(df_price: pd.DataFrame, start: str, end: str) -> float:
    try:
        if df_price.empty or 'close' not in df_price.columns:
            logger.error("df_price 無效")
            return 1.0
        start_ts = pd.Timestamp(start)
        end_ts = pd.Timestamp(end)
        if pd.isna(start_ts) or pd.isna(end_ts):
            logger.error(f"無效的日期格式: {start} → {end}")
            return 1.0
        if start_ts not in df_price.index or end_ts not in df_price.index:
            logger.error(f"start/end 不在交易日序列: {start_ts} → {end_ts}")
            return 1.0
        start_p, end_p = df_price.at[start_ts, 'close'], df_price.at[end_ts, 'close']
        if pd.isna(start_p) or pd.isna(end_p) or start_p == 0:
            logger.warning(f"價格缺失或為 0: {start_p}, {end_p}")
            return 1.0
        return end_p / start_p
    except Exception as e:
        logger.error(f"買入並持有計算錯誤: {e}")
        return 1.0

def get_fold_period(test_blocks: list) -> tuple:
    starts = [b[0] for b in test_blocks]
    ends = [b[1] for b in test_blocks]
    return min(starts), max(ends)

def analyze_trade_timing(df_price: pd.DataFrame, trades: list, window=20) -> tuple:
    buy_distances = []
    sell_distances = []
    for t in trades:
        entry, _, exit = t[0], t[1], t[2] if len(t) > 2 else (None, None, None)
        if not entry or not exit or entry not in df_price.index or exit not in df_price.index:
            logger.warning(f"無效交易日期: entry={entry}, exit={exit}")
            continue
        window_data = df_price.loc[:entry, 'close'].tail(window)
        low_idx = window_data.idxmin()
        buy_dist = (pd.Timestamp(entry) - pd.Timestamp(low_idx)).days if low_idx else 0
        buy_distances.append(buy_dist)
        window_data = df_price.loc[:exit, 'close'].tail(window)
        high_idx = window_data.idxmax()
        sell_dist = (pd.Timestamp(exit) - pd.Timestamp(high_idx)).days if high_idx else 0
        sell_distances.append(sell_dist)
    return np.mean(buy_distances) if buy_distances else 20.0, np.mean(sell_distances) if sell_distances else 20.0

def compute_simplified_sra(df_price: pd.DataFrame, trades: list, test_blocks: list) -> tuple:
    try:
        strategy_returns = []
        for t in trades:
            if len(t) > 2 and t[0] in df_price.index and t[2] in df_price.index:
                period = df_price.loc[t[0]:t[2], 'close'].pct_change().dropna()
                strategy_returns.extend(period)
        test_start, test_end = get_fold_period(test_blocks)
        bh_returns = df_price.loc[test_start:test_end, 'close'].pct_change().dropna()
        strategy_sharpe = np.mean(strategy_returns) / np.std(strategy_returns) * np.sqrt(252) if np.std(strategy_returns) > 0 else 0.0
        bh_sharpe = np.mean(bh_returns) / np.std(bh_returns) * np.sqrt(252) if np.std(bh_returns) > 0 else 0.0
        t_stat, p_value = ttest_ind(strategy_returns, bh_returns, equal_var=False)
        return strategy_sharpe, bh_sharpe, p_value
    except Exception as e:
        logger.warning(f"簡化 SRA 計算失敗: {e}")
        return 0.0, 0.0, 1.0

def compute_knn_stability(df_results: list, params: list, k: int = 5, metric: str = 'total_return') -> float:
    if len(df_results) < k + 1:
        logger.warning(f"試驗數量 {len(df_results)} 不足以計算 KNN 穩定性")
        return 0.0
    if not df_results or not isinstance(df_results[0], dict):
        logger.warning("試驗結果格式無效，無法計算 KNN 穩定性")
        return 0.0
    param_cols = [f"param_{p}" for p in params if f"param_{p}" in df_results[0]]
    if not param_cols:
        logger.warning(f"無有效參數用於 KNN 穩定性計算, 參數: {params}, 可用欄位: {list(df_results[0].keys())}")
        return 0.0
    X = np.array([[r[p] for p in param_cols] for r in df_results])
    y = np.array([r[metric] for r in df_results])
    scaler = MinMaxScaler()
    X_scaled = scaler.fit_transform(X)
    nbrs = NearestNeighbors(n_neighbors=k + 1).fit(X_scaled)
    distances, indices = nbrs.kneighbors(X_scaled)
    stability_scores = []
    for i, idx in enumerate(indices):
        roi = y[i]
        roi_neighbors = np.mean(y[idx[1:]])
        diff = min(abs(roi - roi_neighbors), 2 * roi_neighbors if roi_neighbors > 0 else 2.0)
        stability_scores.append(diff)
    return float(np.mean(stability_scores))

def compute_pbo_score(oos_returns: list) -> float:
    if not oos_returns or len(oos_returns) < 3:
        return 0.0
    try:
        oos = np.array(oos_returns)
        mean_ret = np.mean(oos)
        median_ret = np.median(oos)
        std_ret = np.std(oos)
        skew = abs(mean_ret - median_ret) / std_ret if std_ret > 0 else 0.0
        pbo = min(1.0, skew / 2.0)
        return float(pbo)
    except Exception as e:
        logger.warning(f"自定義 PBO 計算失敗: {e}")
        return 0.0

def compute_period_return(df_price: pd.DataFrame, trades: list, start: str, end: str) -> tuple:
    try:
        start_ts = pd.Timestamp(start)
        end_ts = pd.Timestamp(end)
        if pd.isna(start_ts) or pd.isna(end_ts):
            logger.warning(f"無效的日期格式: {start} → {end}")
            return 0.0, 0
        if start_ts not in df_price.index or end_ts not in df_price.index:
            logger.warning(f"期間 {start_ts} → {end_ts} 不在價格數據索引中")
            return 0.0, 0
        period_trades = [t for t in trades if len(t) > 2 and not pd.isna(pd.Timestamp(t[0])) and not pd.isna(pd.Timestamp(t[2])) and pd.Timestamp(t[0]) >= start_ts and pd.Timestamp(t[2]) <= end_ts]
        if not period_trades:
            logger.info(f"期間 {start_ts} → {end_ts} 無交易")
            return 0.0, 0
        returns = [t[1] for t in period_trades]
        total_return = np.prod([1 + r for r in returns]) - 1
        num_trades = len(period_trades)
        logger.info(f"期間 {start_ts} → {end_ts}: 報酬={total_return:.2f}, 交易數={num_trades}")
        return total_return, num_trades
    except Exception as e:
        logger.warning(f"計算期間報酬失敗: {start} → {end}, 錯誤: {e}")
        return 0.0, 0

def equity_period_return(equity: pd.Series, start: str, end: str) -> float:
    """計算指定期間的 Equity Curve 報酬率."""
    start_ts, end_ts = pd.Timestamp(start), pd.Timestamp(end)
    if start_ts not in equity.index or end_ts not in equity.index:
        logger.warning(f"Equity Curve 未涵蓋 {start_ts} → {end_ts}")
        return np.nan
    return equity.loc[end_ts] / equity.loc[start_ts] - 1 if equity.loc[start_ts] != 0 else 0.0

def compute_stress_metrics(equity: pd.Series, df_price: pd.DataFrame, stress_periods: list) -> dict:
    stress_metrics = {}
    valid_dates = equity.index
    for start, end in stress_periods:
        start_ts = pd.Timestamp(start)
        end_ts = pd.Timestamp(end)
        if pd.isna(start_ts) or pd.isna(end_ts):
            logger.warning(f"無效的壓力測試日期: {start} → {end}")
            period_return = 0.0
            period_mdd = 0.0
            bh_return = buy_and_hold_return(df_price, start, end) - 1
            excess_return = period_return - bh_return if not np.isnan(bh_return) else 0.0
        elif start_ts not in valid_dates or end_ts not in valid_dates or start_ts not in df_price.index or end_ts not in df_price.index:
            logger.warning(f"壓力測試期間無效: {start_ts} → {end_ts}, 數據範圍: {valid_dates[0]} ~ {valid_dates[-1]}")
            period_return = 0.0  # 無交易或無效期間記錄 0% 報酬率
            period_mdd = 0.0
            bh_return = buy_and_hold_return(df_price, start, end) - 1
            excess_return = period_return - bh_return if not np.isnan(bh_return) else 0.0
        else:
            period_equity = equity.loc[start_ts:end_ts]
            if period_equity.empty or len(period_equity) < 2:
                logger.info(f"壓力測試期間 {start_ts} → {end_ts} 無交易，記錄 0% 報酬率")
                period_return = 0.0
                period_mdd = 0.0
                bh_return = buy_and_hold_return(df_price, start, end) - 1
                excess_return = period_return - bh_return if not np.isnan(bh_return) else 0.0
            else:
                period_return = equity_period_return(equity, start, end)
                period_mdd = calculate_max_drawdown(period_equity)
                bh_return = buy_and_hold_return(df_price, start, end) - 1
                excess_return = period_return - bh_return if not np.isnan(period_return) else 0.0
        stress_metrics[(start_ts, end_ts)] = {'return': period_return, 'mdd': period_mdd, 'excess_return': excess_return}
        logger.info(f"壓力測試期間 {start_ts} → {end_ts}: 報酬率={period_return:.3f}, 回撤={period_mdd:.3f}, 超額報酬={excess_return:.3f}")
    if not stress_metrics:
        logger.error("無有效壓力測試數據，返回空字典")
    return stress_metrics

def _stress_avg_return(strat: str, params: dict, data_source: str, df_price: pd.DataFrame, equity_curve: pd.Series) -> float:
    """
    使用 OS 的做法：基於完整的 equity_curve 進行時間切割
    特別處理崩跌期間可能很短的情況，避免過度調整破壞壓力測試本質
    """
    try:
        if df_price.empty or not isinstance(equity_curve, pd.Series) or equity_curve.empty:
            logger.warning(f"壓力測試數據為空,策略: {strat}")
            return np.nan
        
        valid_returns = []
        for start, end in STRESS_PERIODS:
            try:
                start_ts = pd.Timestamp(start)
                end_ts = pd.Timestamp(end)
                
                # 檢查日期是否在 equity_curve 範圍內
                if start_ts not in equity_curve.index or end_ts not in equity_curve.index:
                    logger.warning(f"壓力測試期間超出數據範圍: {start} → {end}")
                    continue
                
                # 使用 OS 的方法：直接切割 equity_curve
                period_equity = equity_curve.loc[start_ts:end_ts]
                if len(period_equity) < 2:
                    logger.warning(f"壓力測試期間過短: {start_ts} → {end_ts} (僅 {len(period_equity)} 個數據點)")
                    # 對於崩跌期間，即使很短也要計算，因為崩跌可能只有幾天
                    if len(period_equity) >= 1:
                        period_return = (period_equity.iloc[-1] / period_equity.iloc[0] - 1)
                        logger.info(f"崩跌期間 {start_ts} 至 {end_ts}: 報酬={period_return:.3f} (短期崩跌)")
                        valid_returns.append(period_return)
                    continue
                
                # 計算期間報酬率（OS 的做法）
                period_return = (period_equity.iloc[-1] / period_equity.iloc[0] - 1)
                logger.info(f"壓力測試時段 {start_ts} 至 {end_ts}: 報酬={period_return:.3f}")
                valid_returns.append(period_return)
                
            except Exception as e:
                logger.warning(f"壓力測試期間計算失敗: {start} → {end}, 錯誤: {e}")
                continue
        
        if not valid_returns:
            logger.warning(f"無有效壓力測試報酬, 策略: {strat}")
            return np.nan
        
        return float(np.mean(valid_returns))
        
    except Exception as e:
        logger.error(f"壓力測試失敗,策略: {strat}, 錯誤: {e}")
        return np.nan

from filelock import FileLock

def append_trial_result(trial_results: List[Dict], trial: optuna.Trial, params: Dict, score: float,
                        equity_curve: pd.Series, strategy: str, data_source: str, extra_metrics: Dict = None):
    """
    追加試驗結果至 trial_results 並更新 study.user_attrs，同時將 equity_curve 儲存至獨立 JSON 檔案。
    
    Args:
        trial_results: 試驗結果列表。
        trial: Optuna 試驗物件。
        params: 試驗參數字典。
        score: 試驗分數。
        equity_curve: 資產曲線 (pd.Series)。
        strategy: 策略名稱。
        data_source: 數據源名稱。
        extra_metrics: 額外指標字典。
    """
    params_flat = {f"param_{k}": v for k, v in params.items()}
    record = {
        "trial_number": str(trial.number).zfill(5),
        "parameters": params,
        "score": score,
        "strategy": strategy,
        "data_source": data_source,
        **params_flat
    }
    if extra_metrics:
        record.update(extra_metrics)
    
    logger.debug(f"追加試驗記錄: trial_number={record['trial_number']}, 鍵={list(record.keys())}")
    trial_results.append(record)
    trial.study.set_user_attr("trial_results", trial_results)
    
    # 將 equity_curve 儲存至獨立 JSON 檔案
    equity_json_file = cfg.RESULT_DIR / f"optuna_equity_curves_{strategy}_{sanitize(data_source)}_{TIMESTAMP}.json"
    equity_json_file.parent.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(equity_json_file) + ".lock")
    with lock:
        equity_data = {}
        if equity_json_file.exists():
            try:
                with open(equity_json_file, 'r', encoding='utf-8') as f:
                    equity_data = json.load(f)
            except json.JSONDecodeError:
                logger.warning(f"JSON 檔案 {equity_json_file} 損壞，將覆蓋")
        
        # 使用統一的序列化函數
        equity_data[record["trial_number"]] = serialize_equity_curve(equity_curve)
        
        with open(equity_json_file, 'w', encoding='utf-8') as f:
            json.dump(equity_data, f, indent=2)
        logger.debug(f"已將試驗 {record['trial_number']} 的 equity_curve 儲存至 {equity_json_file}")
def _backtest_once(strat: str, params: dict, trial_results: list, data_source: str, df_price: pd.DataFrame, df_factor: pd.DataFrame, trial: optuna.Trial = None) -> Tuple[float, int, float, float, float, List, pd.Series, float]:
    try:
        if df_price.empty:
            logger.error(f"價格數據為空, 策略: {strat}, 數據源: {data_source}")
            params_flat = {f"param_{k}": v for k, v in params.items()}
            log_to_results("試驗被剔除", f"價格數據為空, 策略: {strat}, 數據源: {data_source}", **params_flat)
            return -np.inf, 0, 0.0, 0.0, 0.0, [], pd.Series(index=pd.date_range('2014-10-23', '2025-06-17', freq='B'), data=100000.0), 0.0

        if not hasattr(df_price, 'name') or df_price.name is None:
            df_price.name = TICKER.replace(':', '_')
        if not df_factor.empty and (not hasattr(df_factor, 'name') or df_factor.name is None):
            df_factor.name = f"{TICKER}_factor"

        compute_f = STRAT_FUNC_MAP[strat]
        ind_keys = cfg.STRATEGY_PARAMS[strat]["ind_keys"]
        ind_p = {k: params[k] for k in ind_keys}
        ind_p["smaa_source"] = data_source

        if strat == "ssma_turn":
            df_ind, buys, sells = compute_f(df_price, df_factor, **ind_p)
            if df_ind.empty:
                logger.warning(f"計算指標失敗, 策略: {strat}")
                log_to_results("警告", f"計算指標失敗, 策略: {strat}")
                params_flat = {f"param_{k}": v for k, v in params.items()}
                trial_results.append({
                    "trial_number": str(trial.number).zfill(5) if trial else "unknown",
                    "total_return": -np.inf,
                    "num_trades": 0,
                    "sharpe_ratio": 0.0,
                    "max_drawdown": 0.0,
                    "profit_factor": 0.0,
                    "avg_hold_days": 0.0,
                    "stress_mdd": None,
                    "excess_return_stress": None,
                    "parameters": params,
                    "data_source": data_source,
                    "strategy": strat,
                    "score": -np.inf,
                    **params_flat
                })
                return -np.inf, 0, 0.0, 0.0, 0.0, [], pd.Series(index=pd.date_range('2014-10-23', '2025-06-17', freq='B'), data=100000.0), 0.0
            try:
                bt = SSS.backtest_unified(df_ind=df_ind, strategy_type=strat, params=params, buy_dates=buys, sell_dates=sells, discount=COST_PER_SHARE / 100, trade_cooldown_bars=COOLDOWN_BARS)
                if not bt or "metrics" not in bt:
                    logger.error(f"回測結果無效, 策略: {strat}, bt: {bt}")
                    params_flat = {f"param_{k}": v for k, v in params.items()}
                    trial_results.append({
                        "trial_number": str(trial.number).zfill(5) if trial else "unknown",
                        "total_return": -np.inf,
                        "num_trades": 0,
                        "sharpe_ratio": 0.0,
                        "max_drawdown": 0.0,
                        "profit_factor": 0.0,
                        "avg_hold_days": 0.0,
                        "stress_mdd": None,
                        "excess_return_stress": None,
                        "parameters": params,
                        "data_source": data_source,
                        "strategy": strat,
                        "score": -np.inf,
                        **params_flat
                    })
                    return -np.inf, 0, 0.0, 0.0, 0.0, [], pd.Series(index=pd.date_range('2014-10-23', '2025-06-17', freq='B'), data=100000.0), 0.0
            except Exception as e:
                logger.error(f"回測異常, 策略: {strat}, 錯誤: {str(e)}")
                params_flat = {f"param_{k}": v for k, v in params.items()}
                trial_results.append({
                    "trial_number": str(trial.number).zfill(5) if trial else "unknown",
                    "total_return": -np.inf,
                    "num_trades": 0,
                    "sharpe_ratio": 0.0,
                    "max_drawdown": 0.0,
                    "profit_factor": 0.0,
                    "avg_hold_days": 0.0,
                    "stress_mdd": None,
                    "excess_return_stress": None,
                    "parameters": params,
                    "data_source": data_source,
                    "strategy": strat,
                    "score": -np.inf,
                    **params_flat
                })
                return -np.inf, 0, 0.0, 0.0, 0.0, [], pd.Series(index=pd.date_range('2014-10-23', '2025-06-17', freq='B'), data=100000.0), 0.0
        else:
            df_ind = compute_f(df_price, df_factor, **ind_p)
            if df_ind.empty:
                logger.warning(f"計算指標失敗, 策略: {strat}")
                log_to_results("警告", f"計算指標失敗, 策略: {strat}")
                params_flat = {f"param_{k}": v for k, v in params.items()}
                trial_results.append({
                    "trial_number": str(trial.number).zfill(5) if trial else "unknown",
                    "total_return": -np.inf,
                    "num_trades": 0,
                    "sharpe_ratio": 0.0,
                    "max_drawdown": 0.0,
                    "profit_factor": 0.0,
                    "avg_hold_days": 0.0,
                    "stress_mdd": None,
                    "excess_return_stress": None,
                    "parameters": params,
                    "data_source": data_source,
                    "strategy": strat,
                    "score": -np.inf,
                    **params_flat
                })
                return -np.inf, 0, 0.0, 0.0, 0.0, [], pd.Series(index=pd.date_range('2014-10-23', '2025-06-17', freq='B'), data=100000.0), 0.0
            bt = SSS.backtest_unified(df_ind=df_ind, strategy_type=strat, params=params, discount=COST_PER_SHARE / 100, trade_cooldown_bars=COOLDOWN_BARS)
            if not bt or "metrics" not in bt:
                logger.error(f"回測結果無效, 策略: {strat}, bt: {bt}")
                params_flat = {f"param_{k}": v for k, v in params.items()}
                log_to_results("試驗被剔除", f"回測結果無效, 策略: {strat}", **params_flat)
                return -np.inf, 0, 0.0, 0.0, 0.0, [], pd.Series(index=pd.date_range('2014-10-23', '2025-06-17', freq='B'), data=100000.0), 0.0

        equity_curve = bt.get("equity_curve", pd.Series(index=df_price.index, data=100000.0))
        if not isinstance(equity_curve, pd.Series) or equity_curve.empty:
            logger.warning(f"equity_curve 無效，生成預設值, 策略: {strat}")
            equity_curve = pd.Series(index=pd.date_range('2014-10-23', '2025-06-17', freq='B'), data=100000.0)
        # 移除錯誤的 fillna 操作，SSS 已經正確處理了 equity_curve
        # equity_curve = equity_curve.fillna(100000.0)  # 這行是錯誤的
        metrics = bt["metrics"]
        trades_df = bt.get("trades_df", [])
        if trades_df is None:
            logger.warning(f"回測未返回 trades_df, 策略: {strat}")
            params_flat = {f"param_{k}": v for k, v in params.items()}
            trial_results.append({
                "trial_number": str(trial.number).zfill(5) if trial else "unknown",
                "total_return": -np.inf,
                "num_trades": 0,
                "sharpe_ratio": 0.0,
                "max_drawdown": 0.0,
                "profit_factor": 0.0,
                "avg_hold_days": 0.0,
                "stress_mdd": None,
                "excess_return_stress": None,
                "parameters": params,
                "data_source": data_source,
                "strategy": strat,
                "score": -np.inf,
                **params_flat
            })
            return -np.inf, 0, 0.0, 0.0, 0.0, [], pd.Series(index=pd.date_range('2014-10-23', '2025-06-17', freq='B'), data=100000.0), 0.0

        sharpe_ratio = calculate_sharpe(equity_curve.pct_change()) or 0.0
        max_drawdown = calculate_max_drawdown(equity_curve) or 0.0
        profit_factor = calculate_profit_factor(bt.get("trades", [])) or 0.0
        avg_hold_days = _avg_holding_days(bt.get("trades", [])) or 0.0

        stress_metrics = compute_stress_metrics(equity_curve, df_price, STRESS_PERIODS)
        stress_mdd = np.nanmean([m['mdd'] for m in stress_metrics.values()]) if stress_metrics else np.nan
        excess_return_stress = np.nanmean([m['excess_return'] for m in stress_metrics.values()]) if stress_metrics else np.nan

        params_flat = {f"param_{k}": v for k, v in params.items()}
        log_to_results("試驗完成", f"策略: {strat}, 總報酬={metrics.get('total_return', 0.0)*100:.2f}%, 交易次數={metrics.get('num_trades', 0)}", **params_flat)
        trial_results.append({
            "trial_number": str(trial.number).zfill(5) if trial else "unknown",
            "total_return": metrics.get("total_return", 0.0),
            "num_trades": metrics.get("num_trades", 0),
            "sharpe_ratio": sharpe_ratio,
            "max_drawdown": max_drawdown,
            "profit_factor": profit_factor,
            "avg_hold_days": avg_hold_days,
            "stress_mdd": stress_mdd,
            "excess_return_stress": excess_return_stress,
            "parameters": params,
            "data_source": data_source,
            "strategy": strat,
            "score": -np.inf,
            **params_flat
        })
        log_to_results("試驗完成", f"策略: {strat}, 總報酬={metrics.get('total_return', 0.0)*100:.2f}%, 交易次數={metrics.get('num_trades', 0)}", **params_flat)
        return (metrics.get("total_return", 0.0), metrics.get("num_trades", 0), sharpe_ratio,
                max_drawdown, profit_factor, bt.get("trades", []), equity_curve, avg_hold_days)
    except Exception as e:
        logger.error(f"回測失敗, 策略: {strat}, 錯誤: {e}")
        params_flat = {f"param_{k}": v for k, v in params.items()}
        log_to_results("試驗被剔除", f"回測失敗, 策略: {strat}, 錯誤: {e}", **params_flat)
        return -np.inf, 0, 0.0, 0.0, 0.0, [], pd.Series(index=pd.date_range('2014-10-23', '2025-06-17', freq='B'), data=100000.0), 0.0

def _wf_min_return(strat: str, params: dict, data_source: str, df_price: pd.DataFrame, df_factor: pd.DataFrame) -> float:
    """
    使用 OS 的做法：先計算完整回測，再基於 equity_curve 切割時間
    避免過度調整期間，保持 Walk-forward 測試的本質
    """
    try:
        if df_price.empty:
            logger.error(f"Walk-forward 測試數據為空, 策略: {strat}")
            return np.nan
        
        # 1. 先計算完整的策略指標和買賣信號
        compute_f = STRAT_FUNC_MAP[strat]
        ind_keys = cfg.STRATEGY_PARAMS[strat]["ind_keys"]
        ind_p = {k: params[k] for k in ind_keys}
        ind_p["smaa_source"] = data_source
        
        if strat == "ssma_turn":
            df_ind, buys, sells = compute_f(df_price, df_factor, **ind_p)
            if df_ind.empty:
                logger.warning(f"Walk-forward 策略計算失敗, 策略: {strat}")
                return np.nan
            bt = SSS.backtest_unified(df_ind=df_ind, strategy_type=strat, params=params, 
                                     buy_dates=buys, sell_dates=sells, 
                                     discount=COST_PER_SHARE / 100, trade_cooldown_bars=COOLDOWN_BARS)
        else:
            df_ind = compute_f(df_price, df_factor, **ind_p)
            if df_ind.empty:
                logger.warning(f"Walk-forward 策略計算失敗, 策略: {strat}")
                return np.nan
            bt = SSS.backtest_unified(df_ind=df_ind, strategy_type=strat, params=params, 
                                     discount=COST_PER_SHARE / 100, trade_cooldown_bars=COOLDOWN_BARS)
        
        if not bt or "equity_curve" not in bt:
            logger.warning(f"Walk-forward 回測結果無效, 策略: {strat}")
            return np.nan
        
        # 2. 獲取完整的 equity_curve
        equity_curve = bt["equity_curve"]
        if not isinstance(equity_curve, pd.Series) or equity_curve.empty:
            logger.warning(f"Walk-forward equity_curve 無效, 策略: {strat}")
            return np.nan
        
        # 3. 基於 equity_curve 進行時間切割（OS 的做法）
        valid_returns = []
        for start, end in WF_PERIODS:
            try:
                start_ts = pd.Timestamp(start)
                end_ts = pd.Timestamp(end)
                
                # 檢查日期是否在 equity_curve 範圍內
                if start_ts not in equity_curve.index or end_ts not in equity_curve.index:
                    logger.warning(f"Walk-forward 期間超出數據範圍: {start} → {end}")
                    continue
                
                # 使用 OS 的方法：直接切割 equity_curve
                period_equity = equity_curve.loc[start_ts:end_ts]
                if len(period_equity) < 2:
                    logger.warning(f"Walk-forward 期間過短: {start_ts} → {end_ts}")
                    continue
                
                # 計算期間報酬率（OS 的做法）
                period_return = (period_equity.iloc[-1] / period_equity.iloc[0] - 1)
                logger.info(f"Walk-forward 時段 {start_ts} 至 {end_ts}: 報酬={period_return:.3f}")
                valid_returns.append(period_return)
                
            except Exception as e:
                logger.warning(f"Walk-forward 期間計算失敗: {start} → {end}, 錯誤: {e}")
                continue
        
        if not valid_returns:
            logger.error(f"無有效 Walk-forward 報酬, 策略: {strat}")
            return np.nan
        
        return min(valid_returns)
        
    except Exception as e:
        logger.error(f"Walk-forward 測試失敗, 策略: {strat}, 錯誤: {e}")
        return np.nan

def log_trial_details(event_type: str, details: str, trial: optuna.Trial, params: Dict, score: float,
                      equity_curve: pd.Series, strategy: str, data_source: str, metrics: Dict):
    """
    記錄試驗日誌，包含詳細指標與參數，不包含 equity_curve_json。
    """
    params_flat = {f"param_{k}": v for k, v in params.items()}
    log_data = {
        "trial_number": str(trial.number).zfill(5),
        "score": score,
        "parameters": str(params),
        "strategy": strategy,
        "data_source": data_source,
        **params_flat
    }
    for key, value in metrics.items():
        if isinstance(value, float):
            log_data[key] = f"{value:.3f}"
            log_data[f"raw_{key}"] = value
        else:
            log_data[key] = str(value)
            log_data[f"raw_{key}"] = value
    merged_attrs = {**log_data, **{k: v for k, v in trial.user_attrs.items() if k not in log_data}}
    log_to_results(event_type, details, **merged_attrs)

def objective(trial: optuna.Trial):
    global top_trials
    import heapq
    import numpy as np
    import pandas as pd
    from datetime import timedelta

    params = {}  # 初始化空字典以確保異常處理安全
    try:
        # 選擇策略
        if args.strategy == 'all':
            strat = np.random.choice(list(STRATEGY_WEIGHTS.keys()), p=list(STRATEGY_WEIGHTS.values()))
        else:
            strat = args.strategy
        trial.set_user_attr("strategy", strat)

        # 選擇數據源
        if args.data_source_mode == 'sequential':
            data_source = trial.study.user_attrs["data_source"]
        else:
            data_source = trial.study.user_attrs.get("data_source")
            if not data_source:
                if args.data_source_mode == 'fixed':
                    if not args.data_source:
                        logger.error("固定數據源模式下必須指定 --data_source")
                        raise ValueError("缺少 --data_source 參數")
                    data_source = args.data_source
                else:
                    data_source = np.random.choice(list(DATA_SOURCES_WEIGHTS.keys()), p=list(DATA_SOURCES_WEIGHTS.values()))
        trial.set_user_attr("data_source", data_source)

        # 採樣參數
        params = _sample_params(trial, strat)

        # 記錄試驗開始
        logger.info(f"試驗 {trial.number} 開始，策略: {strat}, 數據源: {data_source}, 參數: {params}")
        log_trial_details("試驗開始", f"試驗 {trial.number} 開始，策略: {strat}, 數據源: {data_source}, 參數: {params}",
                          trial, params, -np.inf, pd.Series(), strat, data_source, {})

        trial_results = trial.study.user_attrs.get("trial_results", [])
        if len(trial_results) > 1000:
            trial_results = trial_results[-500:]  # 限制記憶體使用
            trial.study.set_user_attr("trial_results", trial_results)

        # 載入數據
        df_price, df_factor = data_loader.load_data(TICKER, start_date=cfg.START_DATE, end_date="2025-06-17", smaa_source=data_source)
        logger.info(f"數據範圍: df_price {df_price.index[0]} ~ {df_price.index[-1]}, 長度: {len(df_price)}")
        if df_price.empty:
            logger.error(f"價格數據為空, 策略: {strat}")
            log_trial_details("試驗被剔除", f"價格數據為空, 策略: {strat}",
                              trial, params, -np.inf, pd.Series(), strat, data_source, {})
            append_trial_result(trial_results, trial, params, -np.inf, pd.Series(), strat, data_source)
            return -np.inf
        if trial.number == 0:
            safe_ds = sanitize(data_source)
            out_csv = cfg.RESULT_DIR / f"optuna_factor_data_{strat}_{TIMESTAMP}.csv"
            out_csv.parent.mkdir(parents=True, exist_ok=True)
            df_factor.to_csv(out_csv)

        # 執行回測
        (total_ret, n_trades, sharpe_ratio, max_drawdown, profit_factor, trades,
         equity_curve, avg_hold_days) = _backtest_once(strat, params, trial_results, data_source, df_price, df_factor, trial)

        trial.set_user_attr("avg_hold_days", avg_hold_days)

        # 放寬篩選條件 - 降低門檻讓更多試驗通過
        if total_ret == -np.inf or not (2 <= n_trades <= MAX_NUM_TRADES) or total_ret <= 0.2 or max_drawdown < -0.8 or profit_factor < 0.1:
            log_trial_details("試驗被剔除", f"條件未滿足, 策略: {strat}, 總報酬={total_ret*100:.2f}%, 交易次數={n_trades}",
                              trial, params, -np.inf, equity_curve, strat, data_source, {
                                  "total_return": total_ret,
                                  "num_trades": n_trades,
                                  "sharpe_ratio": sharpe_ratio,
                                  "max_drawdown": max_drawdown,
                                  "profit_factor": profit_factor,
                                  "avg_hold_days": avg_hold_days
                              })
            append_trial_result(trial_results, trial, params, -np.inf, equity_curve, strat, data_source, {
                "total_return": total_ret,
                "num_trades": n_trades,
                "sharpe_ratio": sharpe_ratio,
                "max_drawdown": max_drawdown,
                "profit_factor": profit_factor,
                "avg_hold_days": avg_hold_days
            })
            return -np.inf

        # 檢查試驗結果並計算其他指標
        params_flat = {f"param_{k}": v for k, v in params.items()}
        min_wf_ret = _wf_min_return(strat, params, data_source, df_price, df_factor)
        # 暫時註釋掉 Walk-forward 剔除機制，讓更多試驗通過
        # if pd.isna(min_wf_ret):
        #     log_trial_details("試驗被剔除", f"Walk-forward 測試無效, 策略: {strat}",
        #                       trial, params, -np.inf, equity_curve, strat, data_source, {
        #                           "total_return": total_ret,
        #                           "num_trades": n_trades,
        #                           "sharpe_ratio": sharpe_ratio,
        #                           "max_drawdown": max_drawdown,
        #                           "profit_factor": profit_factor,
        #                           "avg_hold_days": avg_hold_days
        #                       })
        #     append_trial_result(trial_results, trial, params, -np.inf, equity_curve, strat, data_source, {
        #         "total_return": total_ret,
        #         "num_trades": n_trades,
        #         "sharpe_ratio": sharpe_ratio,
        #         "max_drawdown": max_drawdown,
        #         "profit_factor": profit_factor,
        #         "avg_hold_days": avg_hold_days
        #     })
        #     return -np.inf
        avg_stress_ret = _stress_avg_return(strat, params, data_source, df_price, equity_curve)
        avg_buy_dist, avg_sell_dist = analyze_trade_timing(df_price, trades)
        trial.set_user_attr("avg_buy_dist", avg_buy_dist)
        trial.set_user_attr("avg_sell_dist", avg_sell_dist)

        # CPCV 檢查
        if df_price.empty:
            logger.error(f"CPCV 價格數據為空, 策略: {strat}")
            log_trial_details("試驗被剔除", f"CPCV 價格數據為空, 策略: {strat}",
                              trial, params, -np.inf, equity_curve, strat, data_source, {
                                  "total_return": total_ret,
                                  "num_trades": n_trades,
                                  "sharpe_ratio": sharpe_ratio,
                                  "max_drawdown": max_drawdown,
                                  "profit_factor": profit_factor,
                                  "avg_hold_days": avg_hold_days
                              })
            append_trial_result(trial_results, trial, params, -np.inf, equity_curve, strat, data_source, {
                "total_return": total_ret,
                "num_trades": n_trades,
                "sharpe_ratio": sharpe_ratio,
                "max_drawdown": max_drawdown,
                "profit_factor": profit_factor,
                "avg_hold_days": avg_hold_days
            })
            return -np.inf
        event_times = [(t[0], t[0]) for t in trades if t[0]]
        if not event_times:
            log_trial_details("試驗被剔除", f"無交易記錄, 策略: {strat}",
                              trial, params, -np.inf, equity_curve, strat, data_source, {
                                  "total_return": total_ret,
                                  "num_trades": n_trades,
                                  "sharpe_ratio": sharpe_ratio,
                                  "max_drawdown": max_drawdown,
                                  "profit_factor": profit_factor,
                                  "avg_hold_days": avg_hold_days
                              })
            append_trial_result(trial_results, trial, params, -np.inf, equity_curve, strat, data_source, {
                "total_return": total_ret,
                "num_trades": n_trades,
                "sharpe_ratio": sharpe_ratio,
                "max_drawdown": max_drawdown,
                "profit_factor": profit_factor,
                "avg_hold_days": avg_hold_days
            })
            return -np.inf
        n_splits = min(CPCV_NUM_SPLITS, len(df_price) // (CPCV_EMBARGO_DAYS + 60))
        min_test_len = (params.get('smaalen', 60) + params.get('quantile_win', 60)) * 1.5
        if len(df_price) / n_splits < min_test_len:
            n_splits = max(3, int(len(df_price) // min_test_len))
            logger.info(f"調整 CPCV 分割數至 {n_splits}")
        tscv = TimeSeriesSplit(n_splits=n_splits)
        folds = []
        valid_dates = df_price.index
        for train_idx, test_idx in tscv.split(df_price):
            train_start = df_price.index[train_idx[0]]
            train_end = df_price.index[train_idx[-1]]
            test_start = df_price.index[test_idx[0]] + pd.Timedelta(days=CPCV_EMBARGO_DAYS)
            test_end = df_price.index[test_idx[-1]]
            test_start_candidates = valid_dates[valid_dates >= test_start]
            test_end_candidates = valid_dates[valid_dates <= test_end]
            if test_start_candidates.empty or test_end_candidates.empty:
                logger.warning(f"跳過無效 fold: test_start={test_start}, test_end={test_end}")
                continue
            adjusted_start = test_start_candidates[0]
            adjusted_end = test_end_candidates[-1]
            folds.append(([train_start, train_end], [adjusted_start, adjusted_end]))
        if setlimit:
            if not (setminsharpe <= sharpe_ratio <= setmaxsharpe and setmaxmdd <= max_drawdown <= setminmdd):
                log_trial_details("試驗被剔除", f"Sharpe Ratio 或 MDD 未達標, 策略: {strat}, Sharpe={sharpe_ratio:.3f}, MDD={max_drawdown:.3f}",
                                  trial, params, -np.inf, equity_curve, strat, data_source, {
                                      "total_return": total_ret,
                                      "num_trades": n_trades,
                                      "sharpe_ratio": sharpe_ratio,
                                      "max_drawdown": max_drawdown,
                                      "profit_factor": profit_factor,
                                      "avg_hold_days": avg_hold_days
                                  })
                append_trial_result(trial_results, trial, params, -np.inf, equity_curve, strat, data_source, {
                    "total_return": total_ret,
                    "num_trades": n_trades,
                    "sharpe_ratio": sharpe_ratio,
                    "max_drawdown": max_drawdown,
                    "profit_factor": profit_factor,
                    "avg_hold_days": avg_hold_days
                })
                return -np.inf
        oos_returns = []
        excess_returns = []
        sra_scores = []
        for train_block, test_block in folds:
            # 使用 OS 的做法：基於完整的 equity_curve 進行時間切割
            test_start, test_end = test_block
            start_ts = pd.Timestamp(test_start)
            end_ts = pd.Timestamp(test_end)
            
            # 檢查日期是否在 equity_curve 範圍內
            if start_ts not in equity_curve.index or end_ts not in equity_curve.index:
                logger.warning(f"CPCV 期間無效: {test_start} → {test_end}")
                continue
            
            # 使用 OS 的方法：直接切割 equity_curve
            period_equity = equity_curve.loc[start_ts:end_ts]
            if len(period_equity) < 2:
                logger.warning(f"CPCV 期間過短: {start_ts} → {end_ts}")
                continue
            
            # 計算期間報酬率（OS 的做法）
            strategy_return = (period_equity.iloc[-1] / period_equity.iloc[0] - 1)
            bh_return = buy_and_hold_return(df_price, test_start, test_end)
            excess_return = strategy_return - bh_return
            excess_returns.append(excess_return)
            oos_returns.append(strategy_return)
            
            # 計算 SRA（基於切割後的 equity_curve）
            strategy_sharpe, bh_sharpe, p_value = compute_simplified_sra(df_price, trades, [test_block])
            sra_scores.append((strategy_sharpe, p_value))

        if not excess_returns:
            log_trial_details("試驗被剔除", f"無有效 excess_returns, 策略: {strat}",
                              trial, params, -np.inf, equity_curve, strat, data_source, {
                                  "total_return": total_ret,
                                  "num_trades": n_trades,
                                  "sharpe_ratio": sharpe_ratio,
                                  "max_drawdown": max_drawdown,
                                  "profit_factor": profit_factor,
                                  "avg_hold_days": avg_hold_days
                              })
            append_trial_result(trial_results, trial, params, -np.inf, equity_curve, strat, data_source, {
                "total_return": total_ret,
                "num_trades": n_trades,
                "sharpe_ratio": sharpe_ratio,
                "max_drawdown": max_drawdown,
                "profit_factor": profit_factor,
                "avg_hold_days": avg_hold_days
            })
            return -np.inf
        excess_return_mean = np.mean(excess_returns)
        stress_metrics = compute_stress_metrics(equity_curve, df_price, STRESS_PERIODS)
        stress_mdd = np.nanmean([m['mdd'] for m in stress_metrics.values()]) if stress_metrics else np.nan
        excess_return_stress = np.nanmean([m['excess_return'] for m in stress_metrics.values()]) if stress_metrics else np.nan
        pbo_score = compute_pbo_score(oos_returns)
        trial.set_user_attr("total_return", total_ret)
        trial.set_user_attr("max_drawdown", max_drawdown)
        trial.set_user_attr("sharpe_ratio", sharpe_ratio)
        trial.set_user_attr("stress_mdd", stress_mdd)
        trial.set_user_attr("excess_return", excess_return_mean)
        trial.set_user_attr("excess_return_stress", excess_return_stress)
        trial.set_user_attr("pbo_score", pbo_score)

        # 更新 trial_results
        append_trial_result(trial_results, trial, params, -np.inf, equity_curve, strat, data_source, {
            "total_return": total_ret,
            "num_trades": n_trades,
            "sharpe_ratio": sharpe_ratio,
            "max_drawdown": max_drawdown,
            "profit_factor": profit_factor,
            "avg_hold_days": avg_hold_days,
            "stress_mdd": stress_mdd,
            "excess_return_stress": excess_return_stress
        })

        logger.info(f"PBO 分數: {pbo_score:.3f}, 策略: {strat}")
        log_to_results("PBO 計算", f"PBO 分數: {pbo_score:.3f}, 策略: {strat}")

        # Min-Max Scaling
        tr_s = minmax(total_ret, 5, 25)
        pf_s = minmax(profit_factor, 0.5, 8)
        wf_s = minmax(min_wf_ret, 0, 2)
        sh_s = minmax(sharpe_ratio, 0.5, 0.8)
        mdd_s = 1 - minmax(abs(max_drawdown), 0, 0.3)
        excess_return_scaled = minmax(excess_return_mean, -1, 1)
        stress_mdd_scaled = minmax(abs(stress_mdd), 0, 0.5) if not np.isnan(stress_mdd) else 0.0
        pbo_score_scaled = pbo_score

        # Robust Score
        robust_score = 0.4 * excess_return_scaled + 0.3 * (1 - stress_mdd_scaled) + 0.3 * (1 - pbo_score_scaled)
        trial.set_user_attr("robust_score", robust_score)

        # 加權分數
        score = (SCORE_WEIGHTS["total_return"] * tr_s + SCORE_WEIGHTS["profit_factor"] * pf_s +
                 SCORE_WEIGHTS["sharpe_ratio"] * sh_s + SCORE_WEIGHTS["max_drawdown"] * mdd_s +
                 SCORE_WEIGHTS["wf_min_return"] * wf_s + robust_score)

        trade_penalty = 0.05 * max(0, MIN_NUM_TRADES - n_trades)
        score -= trade_penalty
        if excess_returns:
            excess_ranks = np.argsort(excess_returns)
            bottom_20_percent = int(0.2 * len(excess_returns))
            for idx in excess_ranks[:bottom_20_percent]:
                score *= 0.9
            logger.info(f"超額報酬懲罰: 倒數 20% fold 數={bottom_20_percent}, 分數調整={score:.3f}")
            log_to_results("超額報酬懲罰", f"倒數 20% fold 數={bottom_20_percent}, 分數調整={score:.3f}")
        avg_p_value = np.mean([score[1] for score in sra_scores]) if sra_scores else 1.0
        trial.set_user_attr("sra_p_value", avg_p_value)

        # 平均持倉懲罰
        penalty = penalize_hold(avg_hold_days)
        score *= (1 - penalty)
        logger.info(f"持倉懲罰: 平均持倉={avg_hold_days:.1f}天, 懲罰={penalty:.3f}, 分數調整={score:.3f}")
        log_to_results("持倉懲罰", f"平均持倉={avg_hold_days:.1f}天, 懲罰={penalty:.3f}, 分數調整={score:.3f}")

        if pbo_score > 0.6:
            penalty = min(0.10, pbo_score / 2)
            score *= (1 - penalty)
            logger.info(f"柔性懲罰: PBO={pbo_score:.3f}, 懲罰={penalty:.3f}, 分數調整={score:.3f}")
            log_to_results("柔性懲罰", f"PBO={pbo_score:.3f}, 懲罰={penalty:.3f}, 分數調整={score:.3f}")

        stab = 0.0
        if len(trial_results) >= 6:
            stab = compute_knn_stability(trial_results, params=['linlen', 'smaalen', 'buy_mult'], k=5)
            if stab > 0.5:
                alpha = 0.2
                penalty_mult = alpha * (stab - 0.5)
                score *= (1 - min(penalty_mult, 0.10))
                logger.info(f"KNN 過擬合懲罰: 穩定性得分={stab:.3f}, 懲罰乘數={penalty_mult:.3f}, 分數調整={score:.3f}")
                log_to_results("KNN 過擬合懲罰", f"穩定性得分={stab:.3f}, 懲罰乘數={penalty_mult:.3f}, 分數調整={score:.3f}")
            trial.set_user_attr("total_return_scaled", tr_s)
            trial.set_user_attr("num_trades", n_trades)
            trial.set_user_attr("sharpe_ratio_scaled", sh_s)
            trial.set_user_attr("max_drawdown_scaled", mdd_s)
            trial.set_user_attr("profit_factor_scaled", pf_s)
            trial.set_user_attr("min_wf_return_scaled", wf_s)
            trial.set_user_attr("stability_score", float(stab))

        # 更新 top_trials
        trial_entry = {
            "score": score,
            "trial_number": str(trial.number).zfill(5),
            "strategy": strat,
            "data_source": data_source
        }
        if len(top_trials) < 20:
            heapq.heappush(top_trials, (score, trial.number, trial_entry))
        else:
            heapq.heappushpop(top_trials, (score, trial.number, trial_entry))
        top_trials.sort(key=lambda x: (x[0], x[1]), reverse=True)

        # 更新 trial_results 的 score
        trial_results[-1]["score"] = score
        trial.study.set_user_attr("trial_results", trial_results)

        # 構建試驗細節
        trial_details = (
            f"試驗 {str(trial.number).zfill(5)}, 策略: {strat}, 數據源: {data_source}, 分數: {score:.3f}, "
            f"參數: {params}, 總報酬={total_ret*100:.2f}%, 交易次數={n_trades}, 夏普比率={sharpe_ratio:.3f}, "
            f"最大回撤={max_drawdown:.3f}, 盈虧因子={profit_factor:.2f}, WF最差報酬(分段)={min_wf_ret:.2f}, "
            f"壓力平均報酬(整段)={avg_stress_ret:.3f}, 穩定性得分={stab:.2f}, Robust Score={robust_score:.3f}, "
            f"Excess Return in Stress={excess_return_stress:.3f}, Stress MDD={stress_mdd:.3f}, "
            f"PBO 分數={pbo_score:.2f}, SRA p-value={avg_p_value:.3f}, 買入距離低點={avg_buy_dist:.1f}天, "
            f"賣出距離高點={avg_sell_dist:.1f}天, 平均持倉天數={avg_hold_days:.1f}天"
        )
        logger.info(trial_details)

        # 記錄試驗結果
        log_trial_details("試驗結果", trial_details, trial, params, score, equity_curve, strat, data_source, {
            "total_return": total_ret,
            "num_trades": n_trades,
            "sharpe_ratio": sharpe_ratio,
            "max_drawdown": max_drawdown,
            "profit_factor": profit_factor,
            "min_wf_return": min_wf_ret,
            "avg_stress_return": avg_stress_ret,
            "stability_score": stab,
            "robust_score": robust_score,
            "excess_return_stress": excess_return_stress,
            "stress_mdd": stress_mdd,
            "pbo_score": pbo_score,
            "sra_p_value": avg_p_value,
            "avg_buy_dist": avg_buy_dist,
            "avg_sell_dist": avg_sell_dist,
            "avg_hold_days": avg_hold_days
        })
        return score
    except Exception as e:
        logger.error(f"試驗 {trial.number} 異常，錯誤: {str(e)}")
        log_trial_details("試驗異常", f"試驗 {trial.number} 失敗，錯誤: {str(e)}",
                          trial, params, -np.inf, pd.Series(), strat, data_source, {})
        return -np.inf
def plot_all_scatter(study, timestamp: str, data_source: str, strategy: str):
    results = study.user_attrs.get("trial_results", [])
    if not results:
        logger.warning("無試驗結果，無法生成散點圖")
        return
    def convert_to_numeric(value, default=np.nan):
        try:
            if isinstance(value, str) and '%' in value:
                return float(value.strip('%')) / 100
            return pd.to_numeric(value, errors='coerce')
        except:
            return default
    total_returns = [convert_to_numeric(r.get("total_return", 0.0)) for r in results]
    mdds = [convert_to_numeric(r.get("max_drawdown", 0.0)) for r in results]
    sharpes = [convert_to_numeric(r.get("sharpe_ratio", 0.0)) for r in results]
    stress_mdds = [convert_to_numeric(r.get("stress_mdd", np.nan)) for r in results]
    excess_returns = [convert_to_numeric(r.get("excess_return_stress", np.nan)) for r in results]
    valid_mdds = [x for x in mdds if not np.isnan(x)]
    valid_sharpes = [x for x in sharpes if not np.isnan(x)]
    valid_stress_mdds = [x for x in stress_mdds if not np.isnan(x)]
    valid_excess_returns = [x for x in excess_returns if not np.isnan(x)]
    logger.info(f"有效數據量: MDD={len(valid_mdds)}, Sharpe={len(valid_sharpes)}, Stress MDD={len(valid_stress_mdds)}, Excess Return={len(valid_excess_returns)}")
    safe_ds = sanitize(data_source)
    safe_strat = sanitize(strategy)
    plot_files = [
        f"{safe_strat}_{safe_ds}_mdd_vs_total_return_{timestamp}.png",
        f"{safe_strat}_{safe_ds}_sharpe_vs_total_return_{timestamp}.png",
        f"{safe_strat}_{safe_ds}_stress_mdd_vs_total_return_{timestamp}.png",
        f"{safe_strat}_{safe_ds}_excess_vs_total_return_{timestamp}.png"
    ]
    if len(valid_mdds) > 3:
        plt.figure(figsize=(10, 6))
        plt.scatter(valid_mdds, total_returns[:len(valid_mdds)], alpha=0.4)
        plt.xlabel("MDD")
        plt.ylabel("Total Return")
        plt.title(f"策略: {strategy} 數據源: {data_source}\nTotal Return vs MDD")
        plt.savefig(cfg.RESULT_DIR / plot_files[0])
        plt.close()
        logger.info(f"生成散點圖：{plot_files[0]}")
    if len(valid_sharpes) > 3:
        plt.figure(figsize=(10, 6))
        plt.scatter(valid_sharpes, total_returns[:len(valid_sharpes)], alpha=0.4)
        plt.xlabel("Sharpe Ratio")
        plt.ylabel("Total Return")
        plt.title(f"策略: {strategy} 數據源: {data_source}\nTotal Return vs Sharpe Ratio")
        plt.savefig(cfg.RESULT_DIR / plot_files[1])
        plt.close()
        logger.info(f"生成散點圖：{plot_files[1]}")
    if len(valid_stress_mdds) > 3:
        plt.figure(figsize=(10, 6))
        plt.scatter(valid_stress_mdds, total_returns[:len(valid_stress_mdds)], alpha=0.4)
        plt.xlabel("Stress MDD")
        plt.ylabel("Total Return")
        plt.title(f"策略: {strategy} 數據源: {data_source}\nTotal Return vs Stress MDD")
        plt.savefig(cfg.RESULT_DIR / plot_files[2])
        plt.close()
        logger.info(f"生成散點圖：{plot_files[2]}")
    if len(valid_excess_returns) > 3:
        plt.figure(figsize=(10, 6))
        plt.scatter(valid_excess_returns, total_returns[:len(valid_excess_returns)], alpha=0.4)
        plt.xlabel("Excess Return in Stress")
        plt.ylabel("Total Return")
        plt.title(f"策略: {strategy} 數據源: {data_source}\nTotal Return vs Excess Return in Stress")
        plt.savefig(cfg.RESULT_DIR / plot_files[3])
        plt.close()
        logger.info(f"生成散點圖：{plot_files[3]}")

def generate_preset_equity_curve(ticker: str, start_date: str, end_date: str, preset_params: Dict, cache_dir: str = str(cfg.SMAA_CACHE_DIR)) -> Optional[pd.Series]:
    """
    為指定的 param_presets 生成 equity curve。
    
    Args:
        ticker: 股票代號。
        start_date: 數據起始日期。
        end_date: 數據結束日期。
        preset_params: 來自 SSSv095b2.py 的 param_presets 單一參數組。
        cache_dir: SMAA 快取目錄。
    
    Returns:
        pd.Series: equity curve，若生成失敗則返回 None。
    """
    try:
        strategy_type = preset_params.get('strategy_type')
        smaa_source = preset_params.get('smaa_source', 'Self')
        logger.info(f"生成 {strategy_type} 策略的 equity curve，數據源: {smaa_source}")

        # 載入數據
        df_price, df_factor = load_data(ticker, start_date=start_date, end_date=end_date, smaa_source=smaa_source)
        if df_price.empty:
            logger.error(f"價格數據為空，無法生成 equity curve，策略: {strategy_type}")
            return None

        # 根據策略生成 df_ind 與買賣信號
        if strategy_type == 'ssma_turn':
            calc_keys = ['linlen', 'factor', 'smaalen', 'prom_factor', 'min_dist', 'buy_shift', 'exit_shift', 
                         'vol_window', 'quantile_win', 'signal_cooldown_days']
            ssma_params = {k: v for k, v in preset_params.items() if k in calc_keys}
            backtest_params = ssma_params.copy()
            backtest_params['stop_loss'] = preset_params.get('stop_loss', 0.0)
            backtest_params['buy_mult'] = preset_params.get('buy_mult', 0.5)
            backtest_params['sell_mult'] = preset_params.get('sell_mult', 0.5)
            df_ind, buy_dates, sell_dates = compute_ssma_turn_combined(
                df_price, df_factor, **ssma_params, smaa_source=smaa_source, cache_dir=cache_dir
            )
            if df_ind.empty:
                logger.warning(f"{strategy_type} 策略計算失敗，數據不足")
                return None
            result = SSS.backtest_unified(
                df_ind=df_ind, strategy_type=strategy_type, params=preset_params, 
                buy_dates=buy_dates, sell_dates=sell_dates,
                discount=0.30, trade_cooldown_bars=3, bad_holding=False
            )
        else:
            if strategy_type == 'single':
                df_ind = compute_single(
                    df_price, df_factor, preset_params['linlen'], preset_params['factor'], 
                    preset_params['smaalen'], preset_params['devwin'], smaa_source=smaa_source, cache_dir=cache_dir
                )
            elif strategy_type == 'dual':
                df_ind = compute_dual(
                    df_price, df_factor, preset_params['linlen'], preset_params['factor'], 
                    preset_params['smaalen'], preset_params['short_win'], preset_params['long_win'], 
                    smaa_source=smaa_source, cache_dir=cache_dir
                )
            elif strategy_type == 'RMA':
                df_ind = compute_RMA(
                    df_price, df_factor, preset_params['linlen'], preset_params['factor'], 
                    preset_params['smaalen'], preset_params['rma_len'], preset_params['dev_len'], 
                    smaa_source=smaa_source, cache_dir=cache_dir
                )
            if df_ind.empty:
                logger.warning(f"{strategy_type} 策略計算失敗，數據不足")
                return None
            result = SSS.backtest_unified(
                df_ind=df_ind, strategy_type=strategy_type, params=preset_params, 
                buy_dates=buy_dates, sell_dates=sell_dates,
                discount=0.30, trade_cooldown_bars=3, bad_holding=False
            )

        equity_curve = result.get('equity_curve')
        if not isinstance(equity_curve, pd.Series) or equity_curve.empty:
            logger.error(f"生成的 equity curve 無效，策略: {strategy_type}")
            return None
        
        # 確保索引為 pd.Timestamp 格式，並在序列化時由 serialize_equity_curve 處理
        equity_curve.index = pd.to_datetime(equity_curve.index)
        return equity_curve.rename(f"preset_{preset_params.get('name', strategy_type)}")

    except Exception as e:
        logger.error(f"生成 equity curve 失敗，策略: {strategy_type}，錯誤: {e}")
        return None

def compute_equity_correlations_with_presets(
    trial_results: List[Dict],
    param_presets: Dict,
    top_n: int = 20,
    ind_keys: Optional[List[str]] = None,
    ticker: str = '00631L.TW',
    start_date: str = '2010-01-01',
    end_date: str = '2025-06-06',
    output_dir: Path = Path('results'),
    data_source: str = 'Self',
    TIMESTAMP: Optional[str] = None
) -> pd.DataFrame:
    global top_trials
    TIMESTAMP = TIMESTAMP or datetime.now().strftime('%Y%m%d_%H%M%S')
    output_dir.mkdir(parents=True, exist_ok=True)
    
    if ind_keys is None:
        ind_keys = cfg.STRATEGY_PARAMS['single']['ind_keys'] + cfg.STRATEGY_PARAMS['single']['bt_keys']
    
    # 轉換 parameters 並進行多樣性篩選
    ready = []
    for tr in trial_results:
        if not all(key in tr for key in ["parameters", "score", "trial_number"]):
            logger.warning(f"試驗 {tr.get('trial_number', 'unknown')} 缺少必要鍵: {set(['parameters', 'score', 'trial_number']) - set(tr.keys())}，跳過")
            continue
        try:
            params = ast.literal_eval(tr["parameters"]) if isinstance(tr["parameters"], str) else tr["parameters"]
            if not isinstance(params, dict):
                logger.warning(f"試驗 {tr.get('trial_number', 'unknown')} 的 parameters 格式無效，跳過")
                continue
            ready.append(dict(tr, parameters=params))
        except (ValueError, SyntaxError) as e:
            logger.warning(f"試驗 {tr.get('trial_number', 'unknown')} 的參數解析失敗：{e}，跳過")
            continue
    if not ready:
        logger.error("無有效試驗數據，無法計算相關性")
        return pd.DataFrame()
    
    top_diverse = pick_topN_by_diversity(ready, ind_keys, top_n, pct_threshold=pct_threshold_self)
    logger.info(f"從 {len(ready)} 筆試驗中篩選出 {len(top_diverse)} 筆多樣性試驗")
    if len(top_diverse) < top_n:
        logger.warning(f"篩選試驗數量 ({len(top_diverse)}) 小於預期 top_n ({top_n})，建議降低 pct_threshold 或增加 n_trials")

    # 載入 equity curves 從獨立 JSON 檔案
    equity_json_file = output_dir / f"optuna_equity_curves_{sanitize(top_diverse[0]['strategy'])}_{sanitize(data_source)}_{TIMESTAMP}.json"
    equity_data = {}
    if equity_json_file.exists():
        try:
            with open(equity_json_file, 'r', encoding='utf-8') as f:
                equity_data = json.load(f)
        except json.JSONDecodeError:
            logger.error(f"無法解析 JSON 檔案 {equity_json_file}")
            return pd.DataFrame()
    else:
        logger.error(f"Equity curve JSON 檔案 {equity_json_file} 不存在")
        return pd.DataFrame()

    # 收集試驗的 equity curve
    df_list = []
    names = []
    for trial in top_diverse:
        trial_num = trial.get('trial_number', 'unknown')
        eq_json = equity_data.get(trial_num)
        if eq_json is None:
            logger.warning(f"試驗 {trial_num} 的 equity curve 缺失，跳過")
            continue
        try:
            # 反序列化為 pd.Series，索引為 pd.Timestamp
            eq = pd.Series(eq_json)
            eq.index = pd.to_datetime(eq.index)
            if eq.empty:
                logger.warning(f"試驗 {trial_num} 的 equity curve 無效，跳過")
                continue
            ser = eq.pct_change().rename(f"trial_{trial_num}")
            df_list.append(ser)
            names.append(f"trial_{trial_num}")
        except ValueError as e:
            logger.warning(f"試驗 {trial_num} 的 equity curve 反序列化失敗: {e}，跳過")
            continue

    # 生成 param_presets 的 equity curve
    for preset_name, preset_params in param_presets.items():
        preset_params['name'] = preset_name
        eq = generate_preset_equity_curve(ticker, start_date, end_date, preset_params)
        if eq is not None:
            # 在此處序列化並立即反序列化以模擬一致性（可選，僅為測試）
            eq_dict = serialize_equity_curve(eq)
            eq = pd.Series(eq_dict)
            eq.index = pd.to_datetime(eq.index)
            ser = eq.pct_change().rename(f"preset_{preset_name}")
            df_list.append(ser)
            names.append(f"preset_{preset_name}")

    if not df_list:
        logger.warning("無有效試驗 equity curve，僅輸出 presets 或返回空相關性矩陣")
        return pd.DataFrame()

    eq_df = pd.concat(df_list, axis=1).fillna(0)
    logger.info(f"合併後的 equity curve DataFrame 形狀: {eq_df.shape}")

    corr_matrix = eq_df.corr()
    
    corr_file = output_dir / f"equity_corr_top_{top_n}_{sanitize(ticker)}_{sanitize(data_source)}_{TIMESTAMP}.csv"
    corr_matrix.to_csv(corr_file, encoding='utf-8-sig')
    logger.info(f"相關性矩陣已儲存至 {corr_file}")

    # 動態計算最小值和最大值，並設定自適應刻度
    corr_min = corr_matrix.min().min()
    corr_max = corr_matrix.max().max()
    vmin = min(corr_min, -1.0)
    vmax = max(corr_max, 1.0)
    abs_max = max(abs(vmin), abs(vmax))
    vmin, vmax = -abs_max, abs_max

    plt.figure(figsize=(15, 12))
    sns.heatmap(
        corr_matrix,
        annot=False,
        cmap="RdYlGn",
        vmin=vmin,
        vmax=vmax,
        center=0,
        cbar_kws={'ticks': np.linspace(vmin, vmax, 11)}
    )
    plt.title(f"Top {top_n} Trials and Param Presets Equity Curve Daily Return Correlation")
    plt.tight_layout()
    heatmap_file = output_dir / f"equity_corr_top_{top_n}_{sanitize(ticker)}_{sanitize(data_source)}_{TIMESTAMP}.png"
    plt.savefig(heatmap_file)
    plt.close()
    logger.info(f"熱圖已儲存至 {heatmap_file}")

    return corr_matrix

def smart_adjust_period(start: str, end: str, equity_curve: pd.Series, min_days: int = 30, max_adjust_days: int = 10) -> tuple:
    """
    智能調整期間日期，但避免過度調整破壞測試本質
    
    Args:
        start: 原始開始日期
        end: 原始結束日期
        equity_curve: 權益曲線
        min_days: 最小天數要求
        max_adjust_days: 最大調整天數（避免過度調整）
    
    Returns:
        tuple: (adjusted_start, adjusted_end, is_valid, reason)
    """
    try:
        start_ts = pd.Timestamp(start)
        end_ts = pd.Timestamp(end)
        valid_dates = equity_curve.index
        
        # 檢查原始日期是否有效
        if pd.isna(start_ts) or pd.isna(end_ts):
            return None, None, False, "無效的日期格式"
        
        # 檢查原始期間長度
        original_days = (end_ts - start_ts).days
        if original_days < min_days:
            return None, None, False, f"原始期間過短: {original_days} 天 < {min_days} 天"
        
        # 檢查日期是否在數據範圍內
        if start_ts not in valid_dates or end_ts not in valid_dates:
            # 計算需要調整的天數
            if start_ts < valid_dates[0]:
                adjust_start_days = (valid_dates[0] - start_ts).days
            else:
                adjust_start_days = 0
                
            if end_ts > valid_dates[-1]:
                adjust_end_days = (end_ts - valid_dates[-1]).days
            else:
                adjust_end_days = 0
            
            # 檢查調整是否過度
            total_adjust_days = adjust_start_days + adjust_end_days
            if total_adjust_days > max_adjust_days:
                return None, None, False, f"調整天數過多: {total_adjust_days} 天 > {max_adjust_days} 天"
            
            # 進行調整
            if start_ts < valid_dates[0]:
                adjusted_start = valid_dates[0]
                logger.warning(f"開始日期 {start_ts.strftime('%Y-%m-%d')} 早於數據開始，調整為 {adjusted_start.strftime('%Y-%m-%d')} (調整 {adjust_start_days} 天)")
            else:
                adjusted_start = valid_dates[valid_dates >= start_ts][0]
            
            if end_ts > valid_dates[-1]:
                adjusted_end = valid_dates[-1]
                logger.warning(f"結束日期 {end_ts.strftime('%Y-%m-%d')} 晚於數據結束，調整為 {adjusted_end.strftime('%Y-%m-%d')} (調整 {adjust_end_days} 天)")
            else:
                adjusted_end = valid_dates[valid_dates <= end_ts][-1]
        else:
            adjusted_start = start_ts
            adjusted_end = end_ts
        
        # 檢查調整後的期間長度
        adjusted_days = (adjusted_end - adjusted_start).days
        if adjusted_days < min_days:
            return adjusted_start, adjusted_end, False, f"調整後期間過短: {adjusted_days} 天 < {min_days} 天"
        
        # 檢查期間內是否有足夠的數據點
        period_equity = equity_curve.loc[adjusted_start:adjusted_end]
        if len(period_equity) < 2:
            return adjusted_start, adjusted_end, False, "期間內數據點不足"
        
        return adjusted_start, adjusted_end, True, "期間有效"
        
    except Exception as e:
        logger.error(f"期間調整失敗: {e}")
        return None, None, False, f"調整失敗: {e}"

def detect_hedge_periods(equity_curve: pd.Series, trades: list, periods: list) -> dict:
    """
    檢測避險期間和初始無交易區段
    
    Args:
        equity_curve: 權益曲線
        trades: 交易記錄
        periods: 期間列表
    
    Returns:
        dict: 包含避險掩碼和初始區段信息
    """
    hedge_info = {}
    
    for i, (start, end) in enumerate(periods):
        try:
            adjusted_start, adjusted_end, is_valid, reason = smart_adjust_period(start, end, equity_curve)
            if not is_valid:
                logger.warning(f"期間 {start} → {end} 無效: {reason}")
                continue
            
            # 切割期間權益曲線
            period_equity = equity_curve.loc[adjusted_start:adjusted_end]
            
            # 計算期間報酬率和 MDD
            period_return = (period_equity.iloc[-1] / period_equity.iloc[0] - 1) * 100
            period_mdd = calculate_max_drawdown(period_equity) * 100
            
            # 檢測避險：報酬率和 MDD 都接近 0
            is_hedge = (abs(period_return) < 0.01) and (abs(period_mdd) < 0.01)
            
            # 檢測初始無交易區段：權益曲線完全平坦
            equity_std = period_equity.std()
            is_initial_no_trade = equity_std < 1.0  # 權益標準差小於1元
            
            # 檢測該期間是否有交易
            period_trades = [t for t in trades if len(t) > 2 and 
                           adjusted_start <= pd.Timestamp(t[0]) <= adjusted_end and
                           adjusted_start <= pd.Timestamp(t[2]) <= adjusted_end]
            
            hedge_info[(adjusted_start, adjusted_end)] = {
                'is_hedge': is_hedge,
                'is_initial_no_trade': is_initial_no_trade,
                'has_trades': len(period_trades) > 0,
                'period_return': period_return,
                'period_mdd': period_mdd,
                'trade_count': len(period_trades),
                'equity_std': equity_std
            }
            
            logger.info(f"期間 {adjusted_start.strftime('%Y-%m-%d')} → {adjusted_end.strftime('%Y-%m-%d')}: "
                       f"避險={is_hedge}, 初始無交易={is_initial_no_trade}, 有交易={len(period_trades) > 0}")
            
        except Exception as e:
            logger.error(f"檢測期間 {start} → {end} 失敗: {e}")
            continue
    
    return hedge_info

def calculate_adjusted_metrics(equity_curve: pd.Series, trades: list, periods: list, 
                              exclude_initial_no_trade: bool = True) -> dict:
    """
    計算調整後的指標，排除初始無交易區段和避險期間
    
    Args:
        equity_curve: 權益曲線
        trades: 交易記錄
        periods: 期間列表
        exclude_initial_no_trade: 是否排除初始無交易區段
    
    Returns:
        dict: 調整後的指標
    """
    hedge_info = detect_hedge_periods(equity_curve, trades, periods)
    
    valid_returns = []
    valid_mdds = []
    excluded_periods = []
    
    for (start, end), info in hedge_info.items():
        # 排除條件
        if exclude_initial_no_trade and info['is_initial_no_trade']:
            excluded_periods.append(f"初始無交易: {start.strftime('%Y-%m-%d')} → {end.strftime('%Y-%m-%d')}")
            continue
        
        if info['is_hedge']:
            excluded_periods.append(f"避險期間: {start.strftime('%Y-%m-%d')} → {end.strftime('%Y-%m-%d')}")
            continue
        
        if not info['has_trades']:
            excluded_periods.append(f"無交易期間: {start.strftime('%Y-%m-%d')} → {end.strftime('%Y-%m-%d')}")
            continue
        
        # 收集有效指標
        valid_returns.append(info['period_return'])
        valid_mdds.append(info['period_mdd'])
    
    return {
        'valid_returns': valid_returns,
        'valid_mdds': valid_mdds,
        'excluded_periods': excluded_periods,
        'total_periods': len(hedge_info),
        'valid_periods': len(valid_returns),
        'exclusion_rate': len(excluded_periods) / len(hedge_info) if hedge_info else 0.0
    }

def calculate_hedge_score(equity_curve: pd.Series, df_price: pd.DataFrame, stress_periods: list, hedge_weight: float = 0.03) -> float:
    """
    計算壓力測試期間的避險分數。若大盤跌幅<-10%，且策略跌幅顯著小於大盤（<大盤跌幅一半），則視為避險成功。
    Args:
        equity_curve: 策略權益曲線
        df_price: 價格資料（需有 'close' 欄）
        stress_periods: 壓力測試期間列表
        hedge_weight: 避險分數權重（預設0.03）
    Returns:
        float: hedge_score，最大不超過 hedge_weight
    """
    hedge_success_count = 0
    valid_stress_count = 0
    for start, end in stress_periods:
        try:
            start_ts = pd.Timestamp(start)
            end_ts = pd.Timestamp(end)
            if start_ts not in equity_curve.index or end_ts not in equity_curve.index:
                continue
            if start_ts not in df_price.index or end_ts not in df_price.index:
                continue
            # 大盤跌幅
            px_start = df_price.at[start_ts, 'close']
            px_end = df_price.at[end_ts, 'close']
            if px_start == 0 or pd.isna(px_start) or pd.isna(px_end):
                continue
            market_return = (px_end / px_start) - 1
            # 策略跌幅
            eq_start = equity_curve.loc[start_ts]
            eq_end = equity_curve.loc[end_ts]
            if eq_start == 0 or pd.isna(eq_start) or pd.isna(eq_end):
                continue
            strat_return = (eq_end / eq_start) - 1
            # 判斷避險成功
            if market_return < -0.10 and strat_return > 0.5 * market_return:
                hedge_success_count += 1
            valid_stress_count += 1
        except Exception as e:
            logger.warning(f"避險分數計算失敗: {start} → {end}, 錯誤: {e}")
            continue
    if valid_stress_count == 0:
        return 0.0
    return hedge_weight * (hedge_success_count / valid_stress_count)

def calculate_adjusted_score(total_ret: float, n_trades: int, sharpe_ratio: float, max_drawdown: float, 
                           profit_factor: float, equity_curve: pd.Series, trades: list,
                           wf_periods: list, stress_periods: list, df_price: pd.DataFrame) -> dict:
    """
    計算調整後的總體分數，採用 OS 的邏輯，納入避險分數
    Args:
        ...
        df_price: 價格資料（需有 'close' 欄）
    Returns:
        dict: 調整後的分數和指標
    """
    # 1. 計算調整後的 Walk-forward 指標
    wf_metrics = calculate_adjusted_metrics(equity_curve, trades, wf_periods, exclude_initial_no_trade=True)
    wf_min_return = min(wf_metrics['valid_returns']) if wf_metrics['valid_returns'] else np.nan
    wf_exclusion_rate = wf_metrics['exclusion_rate']
    # 2. 計算調整後的壓力測試指標
    stress_metrics = calculate_adjusted_metrics(equity_curve, trades, stress_periods, exclude_initial_no_trade=False)
    stress_avg_return = np.mean(stress_metrics['valid_returns']) if stress_metrics['valid_returns'] else np.nan
    stress_exclusion_rate = stress_metrics['exclusion_rate']
    # 3. 計算避險分數
    hedge_score = calculate_hedge_score(equity_curve, df_price, stress_periods, hedge_weight=0.03)
    # 4. 計算避險懲罰
    hedge_penalty = 0.0
    if wf_exclusion_rate > 0.5:
        hedge_penalty = 0.2 * wf_exclusion_rate
        logger.warning(f"Walk-forward 排除率過高: {wf_exclusion_rate:.2f}, 懲罰: {hedge_penalty:.3f}")
    if stress_exclusion_rate > 0.5:
        hedge_penalty += 0.1 * stress_exclusion_rate
        logger.warning(f"壓力測試排除率過高: {stress_exclusion_rate:.2f}, 懲罰: {hedge_penalty:.3f}")
    # 5. 計算數據質量分數
    data_quality_score = 1.0 - (wf_exclusion_rate + stress_exclusion_rate) / 2.0
    # 6. 計算調整後的基本指標
    tr_s = minmax(total_ret, 5, 25)
    pf_s = minmax(profit_factor, 0.5, 8)
    wf_s = minmax(wf_min_return, 0, 2) if not np.isnan(wf_min_return) else 0.0
    sh_s = minmax(sharpe_ratio, 0.5, 0.8)
    mdd_s = 1 - minmax(abs(max_drawdown), 0, 0.3)
    base_score = (SCORE_WEIGHTS["total_return"] * tr_s + 
                  SCORE_WEIGHTS["profit_factor"] * pf_s +
                  SCORE_WEIGHTS["sharpe_ratio"] * sh_s + 
                  SCORE_WEIGHTS["max_drawdown"] * mdd_s +
                  SCORE_WEIGHTS["wf_min_return"] * wf_s)
    # 7. 應用數據質量調整與避險分數
    adjusted_score = base_score * data_quality_score * (1 - hedge_penalty) + hedge_score
    logger.info(f"調整後分數計算: 基本分數={base_score:.3f}, 數據質量={data_quality_score:.3f}, 避險懲罰={hedge_penalty:.3f}, 避險分數={hedge_score:.3f}, 最終分數={adjusted_score:.3f}")
    logger.info(f"  WF排除率: {wf_exclusion_rate:.2f}, 壓力排除率: {stress_exclusion_rate:.2f}")
    return {
        'adjusted_score': adjusted_score,
        'base_score': base_score,
        'data_quality_score': data_quality_score,
        'hedge_penalty': hedge_penalty,
        'hedge_score': hedge_score,
        'wf_min_return': wf_min_return,
        'stress_avg_return': stress_avg_return,
        'wf_exclusion_rate': wf_exclusion_rate,
        'stress_exclusion_rate': stress_exclusion_rate,
        'wf_valid_periods': len(wf_metrics['valid_returns']),
        'stress_valid_periods': len(stress_metrics['valid_returns']),
        'excluded_periods': wf_metrics['excluded_periods'] + stress_metrics['excluded_periods']
    }

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Optuna 最佳化 00631L 策略')
    parser.add_argument('--strategy', type=str, choices=['single', 'dual', 'RMA', 'ssma_turn', 'all'], default='all')
    parser.add_argument('--n_trials', type=int, default=5000)
    parser.add_argument('--data_source', type=str, choices=cfg.SOURCES, default=None)
    parser.add_argument('--data_source_mode', type=str, choices=['random', 'fixed', 'sequential'], default='random')
    args = parser.parse_args()

    cache_dir = Path("C:/Stock_reserach/SSS095a1/cache/price")
    if cache_dir.exists():
        shutil.rmtree(cache_dir)
        logger.info(f"已清空快取目錄: {cache_dir}")
    cache_dir.mkdir(parents=True, exist_ok=True)

    RUN_TS = datetime.now().strftime('%Y%m%d_%H%M%S')
    if args.data_source_mode == 'sequential':
        for data_source in cfg.SOURCES:
            logger.info(f"開始針對數據源 {data_source} 進行最佳化, 策略: {args.strategy}")
            safe_ds = sanitize(data_source)
            optuna_sqlite = cfg.RESULT_DIR / f"optuna_{args.strategy}_{safe_ds}_{RUN_TS}.sqlite3"

            results_log.clear()
            events_log.clear()
            top_trials = []

            study = optuna.create_study(
                study_name=f"00631L_optuna_{args.strategy}_{safe_ds}",
                direction="maximize",
                sampler=optuna.samplers.TPESampler(seed=42),
                pruner=optuna.pruners.MedianPruner(n_warmup_steps=20),
                storage=f"sqlite:///{optuna_sqlite}"
            )
            study.set_user_attr("data_source", data_source)
            study.optimize(objective, n_trials=args.n_trials, n_jobs=1, show_progress_bar=True)
            plot_all_scatter(study, RUN_TS, data_source, args.strategy)
            trial_results_all = study.user_attrs.get("trial_results", [])
            if len(trial_results_all) > 1000:
                trial_results_all = trial_results_all[-500:]
                study.set_user_attr("trial_results", trial_results_all)

            try:
                ind_keys = cfg.STRATEGY_PARAMS[args.strategy]['ind_keys'] + cfg.STRATEGY_PARAMS[args.strategy]['bt_keys']
                corr_matrix = compute_equity_correlations_with_presets(
                    trial_results=trial_results_all,
                    param_presets=SSS.param_presets,
                    top_n=20,
                    ind_keys=ind_keys,
                    ticker=cfg.TICKER,
                    start_date=cfg.START_DATE,
                    end_date="2025-06-06",
                    output_dir=cfg.RESULT_DIR,
                    data_source=data_source,
                    TIMESTAMP=RUN_TS
                )
            except (ValueError, AttributeError) as e:
                logger.error(f"相關性計算失敗，數據源: {data_source}，錯誤: {e}")
                continue

            # 從 results_log 生成結果
            if not results_log:
                logger.warning(f"results_log 為空，數據源: {data_source}，跳過結果輸出")
                continue
            df_results = pd.json_normalize([r for r in results_log if r["Event Type"] in ["試驗結果", "試驗被剔除"]], sep='_')
            logger.info(f"df_results 形狀: {df_results.shape}, 欄位: {list(df_results.columns)}")
            if df_results.empty:
                logger.warning(f"df_results 為空，數據源: {data_source}，可能所有試驗被剔除")
                continue
            missing_cols = REQUIRED_COLS - set(df_results.columns)
            if missing_cols:
                logger.error(f"df_results 缺少必要欄位: {missing_cols}，數據源: {data_source}")
                continue
            df_results = df_results.sort_values("score", ascending=False)

            result_csv_file = cfg.RESULT_DIR / f"optuna_results_{args.strategy}_{safe_ds}_{RUN_TS}.csv"
            df_results.to_csv(result_csv_file, index=False, encoding="utf-8-sig")
            logger.info(f"試驗結果已保存至 {result_csv_file}")

            best = study.best_trial
            logger.info(f"最佳試驗(數據源: {data_source}): ")
            logger.info(f"策略: {best.user_attrs['strategy']}")
            logger.info(f"數據源: {best.user_attrs['data_source']}")
            logger.info(f"參數: {{ {', '.join(f'{k}: {v:.3f}' if isinstance(v, float) else f'{k}: {v}' for k, v in best.params.items() if k not in ['strategy', 'data_source'])} }}")
            logger.info(f"穩健分數: {best.value:.3f}")
            logger.info(f"其他指標: {{ {', '.join(f'{k}: {v:.3f}' if isinstance(v, float) else f'{k}: {v}' for k, v in best.user_attrs.items())} }}")
            best_trial_details = (
                f"策略: {best.user_attrs['strategy']}, 數據源: {best.user_attrs['data_source']}, "
                f"參數: {{ {', '.join(f'{k}: {v:.3f}' if isinstance(v, float) else f'{k}: {v}' for k, v in best.params.items() if k not in ['strategy', 'data_source'])} }}, "
                f"穩健分數: {best.value:.3f}, 其他指標: {{ {', '.join(f'{k}: {v:.3f}' if isinstance(v, float) else f'{k}: {v}' for k, v in best.user_attrs.items())} }}"
            )
            log_to_results("最佳試驗資訊", best_trial_details)

            results = {
                "best_robust_score": best.value,
                "best_strategy": best.user_attrs["strategy"],
                "best_data_source": best.user_attrs["data_source"],
                "best_params": {k: round(v, 3) if isinstance(v, float) else v for k, v in best.params.items() if k not in ["strategy", "data_source"]},
                "best_metrics": {k: round(v, 3) if isinstance(v, float) else v for k, v in best.user_attrs.items()},
                "best_avg_hold_days": round(best.user_attrs.get("avg_hold_days", 0.0), 1)
            }
            result_file = cfg.RESULT_DIR / f"optuna_best_params_{args.strategy}_{safe_ds}_{RUN_TS}.json"
            result_file.parent.mkdir(parents=True, exist_ok=True)
            pd.Series(results).to_json(result_file, indent=2)
            logger.info(f"最佳參數已保存至 {result_file}")
            event_csv_file = cfg.RESULT_DIR / f"optuna_events_{args.strategy}_{safe_ds}_{RUN_TS}.csv"
            df_events = pd.DataFrame(events_log)
            df_events.to_csv(event_csv_file, index=False, encoding='utf-8-sig', na_rep='0.0')
            logger.info(f"事件紀錄已保存至 {event_csv_file}")

            logger.info("前 5 筆試驗記錄:")
            for record in results_log[:5]:
                logger.info(f"[{record['Timestamp']}] - {record['Event Type']} - {record['Details']}")
            results_list = load_results_to_list(result_csv_file)
            logger.info(f"從 {result_csv_file} 載入 {len(results_list)} 筆記錄")

            results_log.clear()
            events_log.clear()
    else:
        top_trials = []
        optuna_sqlite = cfg.RESULT_DIR / f"optuna_{args.strategy}_{TIMESTAMP}.sqlite3"
        optuna_sqlite.parent.mkdir(parents=True, exist_ok=True)
        study = optuna.create_study(
            study_name=f"00631L_optuna_{args.strategy}_{args.data_source_mode}",
            direction="maximize",
            sampler=optuna.samplers.TPESampler(seed=42),
            pruner=optuna.pruners.MedianPruner(n_warmup_steps=20),
            storage=f"sqlite:///{optuna_sqlite}"
        )
        study.optimize(objective, n_trials=args.n_trials, n_jobs=1, show_progress_bar=True)
        plot_all_scatter(study, TIMESTAMP, args.data_source if args.data_source else "unknown", args.strategy)

        trial_results_all = study.user_attrs.get("trial_results", [])
        if len(trial_results_all) > 1000:
            trial_results_all = trial_results_all[-500:]
            study.set_user_attr("trial_results", trial_results_all)

        try:
            corr_matrix = compute_equity_correlations_with_presets(
                trial_results=trial_results_all,
                param_presets=SSS.param_presets,
                top_n=20,
                ticker=TICKER,
                start_date=cfg.START_DATE,
                end_date="2025-06-06",
                output_dir=cfg.RESULT_DIR
            )
        except (ValueError, AttributeError) as e:
            logger.error(f"相關性計算失敗，錯誤: {e}")

        # 從 results_log 生成結果
        if not results_log:
            logger.warning("results_log 為空，跳過結果輸出")
            df_results = pd.DataFrame()
        else:
            df_results = pd.json_normalize([r for r in results_log if r["Event Type"] in ["試驗結果", "試驗被剔除"]], sep='_')
            logger.info(f"df_results 形狀: {df_results.shape}, 欄位: {list(df_results.columns)}")
            if df_results.empty:
                logger.warning("df_results 為空，可能所有試驗被剔除")
                df_results = pd.DataFrame()
            else:
                missing_cols = REQUIRED_COLS - set(df_results.columns)
                if missing_cols:
                    logger.error(f"df_results 缺少必要欄位: {missing_cols}")
                    df_results = pd.DataFrame()
                else:
                    df_results = df_results.sort_values("score", ascending=False)

        result_csv_file = cfg.RESULT_DIR / f"optuna_results_{args.strategy}_{TIMESTAMP}.csv"
        df_results.to_csv(result_csv_file, index=False, encoding="utf-8-sig")
        logger.info(f"試驗結果已保存至 {result_csv_file}")

        best = study.best_trial
        logger.info("最佳試驗:")
        logger.info(f"策略: {best.user_attrs['strategy']}")
        logger.info(f"數據源: {best.user_attrs['data_source']}")
        logger.info(f"參數: {{ {', '.join(f'{k}: {v:.3f}' if isinstance(v, float) else f'{k}: {v}' for k, v in best.params.items() if k not in ['strategy', 'data_source'])} }}")
        logger.info(f"穩健分數: {best.value:.3f}")
        logger.info(f"其他指標: {{ {', '.join(f'{k}: {v:.3f}' if isinstance(v, float) else f'{k}: {v}' for k, v in best.user_attrs.items())} }}")
        best_trial_details = (
            f"策略: {best.user_attrs['strategy']}, 數據源: {best.user_attrs['data_source']}, "
            f"參數: {{ {', '.join(f'{k}: {v:.3f}' if isinstance(v, float) else f'{k}: {v}' for k, v in best.params.items() if k not in ['strategy', 'data_source'])} }}, "
            f"穩健分數: {best.value:.3f}, 其他指標: {{ {', '.join(f'{k}: {v:.3f}' if isinstance(v, float) else f'{k}: {v}' for k, v in best.user_attrs.items())} }}"
        )
        log_to_results("最佳試驗資訊", best_trial_details)

        strategies = [args.strategy] if args.strategy != 'all' else list(PARAM_SPACE.keys())
        data_sources = DATA_SOURCES
        trial_results = [entry for entry in results_log if entry["Event Type"] in ["試驗結果", "試驗被剔除"]]
        for strategy in strategies:
            for data_source in data_sources:
                strategy_source_trials = [entry for entry in trial_results if entry.get("strategy") == strategy and entry.get("data_source") == data_source]
                if not strategy_source_trials:
                    logger.info(f"策略 {strategy} 與數據源 {data_source} 無有效試驗結果")
                    continue
                trial_scores = [(entry.get("trial_number"), float(entry.get("score") or -np.inf), entry["Details"]) for entry in strategy_source_trials if entry.get("score") is not None]
                trial_scores.sort(key=lambda x: x[1], reverse=True)
                top_10_trials = trial_scores[:10]
                if top_10_trials:
                    logger.info(f"策略 {strategy} 與數據源 {data_source} 前 10 名試驗:")
                    for trial_num, score, details in top_10_trials:
                        logger.info(details)
                        log_to_results(f"前 10 名 {strategy} 搭配 {data_source} 試驗", details)
        for strategy in strategies:
            for data_source in data_sources:
                strategy_source_trials = [entry for entry in trial_results if entry.get("strategy") == strategy and entry.get("data_source") == data_source]
                if not strategy_source_trials:
                    logger.info(f"策略 {strategy} 與數據源 {data_source} 無有效試驗結果 (分組前 5 名)")
                    continue
                trial_scores = [(entry.get("trial_number"), float(entry.get("score") or -np.inf), entry["Details"]) for entry in strategy_source_trials if entry.get("score") is not None]
                trial_scores.sort(key=lambda x: x[1], reverse=True)
                top_5_trials = trial_scores[:5]
                if top_5_trials:
                    logger.info(f"策略 {strategy} 與數據源 {data_source} 分組前 5 名試驗:")
                    for trial_num, score, details in top_5_trials:
                        logger.info(details)
                        log_to_results(f"前 5 名 {strategy} 搭配 {data_source} 分組試驗", details)

        results = {
            "best_robust_score": best.value,
            "best_strategy": best.user_attrs["strategy"],
            "best_data_source": best.user_attrs["data_source"],
            "best_params": {k: round(v, 3) if isinstance(v, float) else v for k, v in best.params.items() if k not in ["strategy", "data_source"]},
            "best_metrics": {k: round(v, 3) if isinstance(v, float) else v for k, v in best.user_attrs.items()},
            "best_avg_hold_days": round(best.user_attrs.get("avg_hold_days", 0.0), 1)
        }
        result_file = cfg.RESULT_DIR / f"optuna_best_params_{args.strategy}_{TIMESTAMP}.json"
        result_file.parent.mkdir(parents=True, exist_ok=True)
        pd.Series(results).to_json(result_file, indent=2)
        logger.info(f"最佳參數已保存至 {result_file}")
        event_csv_file = cfg.RESULT_DIR / f"optuna_events_{args.strategy}_{TIMESTAMP}.csv"
        df_events = pd.DataFrame(events_log)
        df_events.to_csv(event_csv_file, index=False, encoding='utf-8-sig', na_rep='0.0')
        logger.info(f"事件紀錄已保存至 {event_csv_file}")

        logger.info("前 5 筆試驗記錄:")
        for record in results_log[:5]:
            logger.info(f"[{record['Timestamp']}] - {record['Event Type']} - {record['Details']}")
        results_list = load_results_to_list(result_csv_file)
        logger.info(f"從 {result_csv_file} 載入 {len(results_list)} 筆記錄")