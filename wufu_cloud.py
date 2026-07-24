#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
五福 v1.1 云端版 (wufu_cloud.py)
=====================================================================
- 决策内核完全移植自 backtest_wufu_jq.py（即聚宽 5f_rbq2025.py 的复刻）
  wls_score 加权最小二乘打分×R² / B型主线豁免 / 相关性守卫 / score^1.5 仓位
  / 弱市切避险池 / H72 动态窗口
- 固定池内嵌自 5f_rbq2025.py（global 21 + china 97，去重约 114 只）
- 数据源：东财/新浪公开行情（云端无内网 PanWatch，用公开源替代）
- 成本模型与轮动V1 统一：佣金万0.5（无最低5元）+ 单边0.1%滑点
- 每个交易日 13:10（北京时间）由 GitHub Actions 调用决策+按13:10价交易；
  15:10 再次调用（SETTLE=1）仅用收盘价重算当日净值/收益，不改变持仓
- 最后更新: 2026-07-24 (v3)
- 本次改动: 加"收盘结算"模式(SETTLE=1)：不交易，只用收盘价重算当日净值与
  收益率；网页收盘后为"13:10买卖价 + 当日收盘价"口径。买卖保持100股整手
---------------------------------------------------------------------
已知近似（不影响未来对比公平性）：
1. 动态池（回测里取全市场成交前150）云端改为只用固定池——固定池已覆盖商品/海外/
   港股/宽基/主要行业，114只足够动量选股；与回测绝对收益会有偏差，但两策略同台
   对比用统一口径，比较结论有效。
2. 量能过滤的当日成交量优先用新浪实时量，缺失时回退到最近历史日成交量（保证单位一致）。
"""
import json, re, math, os, sys
from datetime import datetime, timezone, timedelta
import numpy as np
import requests

# ---------- 路径 ----------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
HISTORY_FILE = os.path.join(SCRIPT_DIR, 'history_wufu.json')
STATS_FILE   = os.path.join(SCRIPT_DIR, 'stats_wufu.json')
POS_FILE     = os.path.join(SCRIPT_DIR, 'positions_wufu.json')

# ---------- 成本（与轮动V1 统一）----------
COMMISSION_RATE = 0.00005   # 交易佣金 万分之0.5（无最低5元）
SLIPPAGE_RATE   = 0.001     # 滑点 单边 0.1%（买价×(1+滑点)，卖价×(1-滑点)）
INIT_CAPITAL = 50000.0
TOTAL_CAPITAL = 50000.0

# ============ 五福 v1.1 原参数（与聚宽脚本一致）============
LOOKBACK = 25
MAX_SCORE = 5.0
SCORE_RATIO = 0.9
R2_THRESH = 0.4
MA_LOOKBACK = 10
MA_THRESH = 1.0
VOL_LOOKBACK = 5
VOL_THRESH = 1.8
LOSS = 0.97
NORMAL_HOLD = 3
WEAK_HOLD = 2
WEAK_MA = 10
MAX_WEAK_DAYS = 20
# H72
H72_BASE = 25
H72_SHORT = 23
H72_R2_HI = 0.4
H72_R2_LO = 0.38
H72_ENTER = 2
H72_EXIT = 2
# 主线
ML_ENABLE = True
ML_SCORE_MIN = 5.0
ML_SCORE_MAX = 20.0
ML_DAYS = 5
ML_MIN_R2 = 0.85
ML_MIN_R2_AVG = 0.85
ML_MIN_VOL_AVG = 1.5
ML_MIN_SCORE_UP = 3
ML_MIN_LAP_POS = 5
ML_MIN_GROWTH = 1.3
ML_RETAIN_R2 = 0.85
ML_RETAIN_LAP = 0.0
LAPLACE_S = 0.05
# 相关性守卫
CORR_ENABLE = True
CORR_THRESH = 0.8
CORR_LOOKBACK = 60
# 仓位管理
POS_POWER = 1.5
POS_CAP_MULTI = 0.5
POS_CAP_SINGLE = 0.9
# 防御
DEFENSIVE_BASKET = ['518880', '511880']   # 黄金 + 货币
WARMUP = 50  # 仅作参考；云端每日均用完整历史重算状态，首日即建仓

# ============ 固定池（来自 5f_rbq2025.py，仅未注释项）============
GLOBAL_POOL = [
    '518880.XSHG',  # 黄金ETF
    '501018.XSHG',  # 南方原油
    '161226.XSHE',  # 国投白银LOF
    '159985.XSHE',  # 豆粕ETF华夏
    '159980.XSHE',  # 有色ETF大成
    '513310.XSHG',  # 中韩芯片
    '159518.XSHE',  # 标普油气ETF嘉实
    '159509.XSHE',  # 纳指科技ETF景顺
    '513100.XSHG',  # 纳指ETF
    '513520.XSHG',  # 日经ETF
    '513500.XSHG',  # 标普500
    '159502.XSHE',  # 标普生物科技ETF嘉实
    '513400.XSHG',  # 道琼斯
    '513030.XSHG',  # 德国ETF
    '513290.XSHG',  # 纳指生物
    '520830.XSHG',  # 沙特ETF
    '159529.XSHE',  # 标普消费ETF景顺
    '512010.XSHG',  # 医药ETF
    '510880.XSHG',  # 红利ETF
    '159915.XSHE',  # 创业板ETF
    '513180.XSHG',  # 恒指科技
]
CHINA_POOL = [
    '513090.XSHG',  # 香港证券
    '513120.XSHG',  # HK创新药
    '513180.XSHG',  # 恒指科技
    '513330.XSHG',  # 恒生互联
    '513750.XSHG',  # 港股非银
    '159892.XSHE',  # 恒生医药ETF华夏
    '513190.XSHG',  # H股金融
    '159605.XSHE',  # 中概互联ETF广发
    '513630.XSHG',  # 香港红利
    '159323.XSHE',  # 港股通汽车ETF华夏
    '510900.XSHG',  # 恒生中国
    '513920.XSHG',  # 央企40
    '513970.XSHG',  # 恒生消费
    '511380.XSHG',  # 转债ETF
    '512050.XSHG',  # A500E
    '510500.XSHG',  # 500ETF
    '159915.XSHE',  # 创业板ETF易方达
    '510300.XSHG',  # 300ETF
    '512100.XSHG',  # 1000ETF
    '159949.XSHE',  # 创业板50ETF华安
    '588080.XSHG',  # 科创板50
    '159967.XSHE',  # 创业板成长ETF华夏
    '588220.XSHG',  # 科创100F
    '563300.XSHG',  # 中证2000
    '510760.XSHG',  # 上证ETF
    '588200.XSHG',  # 科创芯片
    '515880.XSHG',  # 通信ETF
    '159981.XSHE',  # 能源化工ETF建信
    '512880.XSHG',  # 证券ETF
    '513350.XSHG',  # 油气ETF
    '159326.XSHE',  # 电网设备ETF华夏
    '159516.XSHE',  # 半导体设备ETF国泰
    '159206.XSHE',  # 卫星ETF永赢
    '512480.XSHG',  # 半导体
    '159363.XSHE',  # 创业板人工智能ETF华宝
    '159870.XSHE',  # 化工ETF鹏华
    '512400.XSHG',  # 有色ETF
    '159755.XSHE',  # 电池ETF广发
    '588170.XSHG',  # 科创半导
    '159992.XSHE',  # 创新药ETF银华
    '159995.XSHE',  # 芯片ETF华夏
    '512890.XSHG',  # 红利低波
    '515220.XSHG',  # 煤炭ETF
    '159566.XSHE',  # 储能电池ETF易方达
    '159819.XSHE',  # 人工智能ETF易方达
    '512800.XSHG',  # 银行ETF
    '512690.XSHG',  # 酒ETF
    '515050.XSHG',  # 5GETF
    '562500.XSHG',  # 机器人
    '512170.XSHG',  # 医疗ETF
    '517520.XSHG',  # 黄金股
    '159869.XSHE',  # 游戏ETF华夏
    '512070.XSHG',  # 证券保险
    '159611.XSHE',  # 电力ETF广发
    '562800.XSHG',  # 稀有金属
    '515120.XSHG',  # 创新药
    '512010.XSHG',  # 医药ETF
    '510880.XSHG',  # 红利ETF
    '515790.XSHG',  # 光伏ETF
    '515980.XSHG',  # 人工智能
    '512660.XSHG',  # 军工ETF
    '159928.XSHE',  # 消费ETF汇添富
    '512710.XSHG',  # 军工龙头
    '560860.XSHG',  # 工业有色
    '515030.XSHG',  # 新汽车
    '159766.XSHE',  # 旅游ETF富国
    '159218.XSHE',  # 卫星ETF招商
    '159852.XSHE',  # 软件ETF嘉实
    '516160.XSHG',  # 新能源
    '516150.XSHG',  # 稀土基金
    '159227.XSHE',  # 航空航天ETF华夏
    '159583.XSHE',  # 通信ETF富国
    '588790.XSHG',  # 科创智能
    '159865.XSHE',  # 养殖ETF国泰
    '512980.XSHG',  # 传媒ETF
    '159851.XSHE',  # 金融科技ETF华宝
    '561360.XSHG',  # 石油ETF
    '561980.XSHG',  # 芯片设备
    '562590.XSHG',  # 半导材料
    '512200.XSHG',  # 地产ETF
    '159732.XSHE',  # 消费电子ETF华夏
    '159667.XSHE',  # 工业母机ETF国泰
    '516510.XSHG',  # 云计算
    '159840.XSHE',  # 锂电池ETF工银
    '159998.XSHE',  # 计算机ETF天弘
    '159825.XSHE',  # 农业ETF富国
    '512670.XSHG',  # 国防ETF
    '159883.XSHE',  # 医疗器械ETF永赢
    '515210.XSHG',  # 钢铁ETF
    '515400.XSHG',  # 大数据
    '159256.XSHE',  # 创业板软件ETF华夏
    '561330.XSHG',  # 矿业ETF
    '515170.XSHG',  # 食品饮料
    '159638.XSHE',  # 高端装备ETF嘉实
    '516520.XSHG',  # 智能驾驶
    '513360.XSHG',  # 教育ETF
    '516190.XSHG',  # 文娱ETF
]
# 固定池 = global + china，去重保序
FIXED_POOL = list(dict.fromkeys(GLOBAL_POOL + CHINA_POOL))
GLOBAL_SET = set(GLOBAL_POOL)
# 走弱指数代理（与回测一致）
WEAK_IDX = ['510300.XSHG', '510500.XSHG', '159915.XSHE', '512050.XSHG']
# 额外需抓取的数据（货币ETF 511880 不在固定池内）
EXTRA_FETCH = ['511880.XSHG']
ALL_CODES = sorted(set(FIXED_POOL) | set(WEAK_IDX) | set(EXTRA_FETCH))

NAME_CACHE = {}   # code -> name（实时行情填充）


def _log(*a):
    print('[wufu]', *a, flush=True)


# ==================== 数据获取（公开源） ====================
def _em_secid(code):
    num = code.split('.')[0]
    return f"{'1' if code.endswith('XSHG') else '0'}.{num}"


def fetch_klines_em(code, limit=130):
    """东财日K（前复权），返回 [{date,close,volume}]。"""
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get"
           f"?secid={_em_secid(code)}&fields1=f1,f2,f3,f4,f5,f6"
           "&fields2=f51,f52,f53,f54,f55,f56&klt=101&fqt=1&end=20500101"
           f"&lmt={limit}")
    try:
        r = requests.get(url, timeout=12)
        kl = r.json().get('data', {}).get('klines', [])
        out = []
        for row in kl:
            p = row.split(',')
            close = float(p[2]); vol = float(p[5])
            if close <= 0:
                continue
            out.append({'date': p[0], 'close': close, 'volume': vol})
        return out
    except Exception as e:
        _log(f"东财K线 {code} 失败: {e}")
        return []


def fetch_klines_sina(code, limit=130):
    """新浪日K，返回 [{date,close,volume}]。"""
    num = code.split('.')[0]
    market = 'sh' if code.endswith('XSHG') else 'sz'
    symbol = f"{market}{num}"
    url = (f"https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           f"CN_MarketData.getKLineData?symbol={symbol}&scale=240&datalen={limit}&ma=no")
    try:
        r = requests.get(url, timeout=12)
        arr = r.json()
        out = []
        for item in arr:
            close = float(item['close']); vol = float(item.get('volume', 0))
            if close <= 0:
                continue
            out.append({'date': item['day'], 'close': close, 'volume': vol})
        return out
    except Exception as e:
        _log(f"新浪K线 {code} 失败: {e}")
        return []


def fetch_klines_batch(codes, limit=130):
    results = {}
    for code in codes:
        kl = fetch_klines_em(code, limit)
        if len(kl) < 20:
            kl = fetch_klines_sina(code, limit)
        if kl:
            results[code] = kl[-limit:]
    _log(f"K线获取: {len(results)}/{len(codes)} 只")
    return results


def get_quotes_sina(symbols):
    items = []
    for code in symbols:
        num = code.split('.')[0]
        market = 'sh' if code.endswith('XSHG') else 'sz'
        items.append(f"{market}{num}")
    quotes = {}
    for attempt in range(2):
        try:
            r = requests.get("https://hq.sinajs.cn/list=" + ",".join(items),
                             headers={"Referer": "https://finance.sina.com.cn"}, timeout=15)
            for line in r.text.replace('\n', '').split(';'):
                line = line.strip()
                if '=' not in line:
                    continue
                varp, datap = line.split('=', 1)
                m = re.search(r'hq_str_(sh|sz)(\d+)', varp)
                if not m:
                    continue
                full = f"{m.group(2)}.{'XSHG' if m.group(1) == 'sh' else 'XSHE'}"
                f = datap.strip('"').split(',')
                if len(f) < 9 or not f[3]:
                    continue
                try:
                    price = float(f[3])
                except Exception:
                    continue
                if price > 0:
                    vol = 0.0
                    try:
                        vol = float(f[8]) if f[8] else 0.0
                    except Exception:
                        vol = 0.0
                    quotes[full] = {'current_price': price, 'name': f[0], 'volume': vol}
                    NAME_CACHE[full] = f[0]
        except Exception as e:
            _log(f"新浪实时行情失败(第{attempt+1}次): {e}")
        if len(quotes) >= int(len(symbols) * 0.8):
            break
    return quotes


def get_quotes_em(symbols):
    quotes = {}
    for code in symbols:
        try:
            r = requests.get(
                f"https://push2.eastmoney.com/api/qt/stock/get?secid={_em_secid(code)}"
                f"&fields=f43,f47,f57,f58", timeout=10)
            d = r.json().get('data', {})
            if not d or not d.get('f43'):
                continue
            price = d['f43'] / 1000.0
            if price > 0:
                vol = float(d.get('f47', 0) or 0)
                quotes[code] = {'current_price': price, 'name': d.get('f58', code), 'volume': vol}
                NAME_CACHE[code] = d.get('f58', code)
        except Exception as e:
            _log(f"东财实时 {code} 失败: {e}")
    return quotes


def get_quotes_cloud(symbols):
    q = get_quotes_sina(symbols)
    missing = [s for s in symbols if s not in q]
    if missing:
        _log(f"新浪缺 {len(missing)} 只，补东财")
        for k, v in get_quotes_em(missing).items():
            q.setdefault(k, v)
    return q


def _bj_now():
    return datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=8)))


# ==================== 决策内核（移植自 backtest_wufu_jq.py） ====================
def strip(code):
    return code.replace('.XSHG', '').replace('.XSHE', '')


def wls_score(series, lookback):
    """加权最小二乘动量得分，复刻 calculate_momentum_score。"""
    series = np.asarray(series, float)
    if len(series) < lookback + 1:
        return None, None, None
    y = np.log(series[-(lookback + 1):])
    x = np.arange(len(y))
    w = np.linspace(1, 2, len(y))
    W = w ** 2
    Ws = np.sum(W)
    xb = np.sum(W * x) / Ws
    yb = np.sum(W * y) / Ws
    dx = x - xb
    dy = y - yb
    vx = np.sum(W * dx * dx)
    if vx == 0:
        return 0.0, 0.0, 0.0
    slope = np.sum(W * dx * dy) / vx
    intercept = yb - slope * xb
    ann = math.exp(slope * 250) - 1
    yp = slope * x + intercept
    ssr = np.sum(w * (y - yp) ** 2)
    sst = np.sum(w * (y - np.mean(y)) ** 2)
    r2 = 1 - ssr / sst if sst > 0 else 0.0
    return ann * r2, ann, r2


def laplace_filter(price, s=LAPLACE_S):
    alpha = 1 - np.exp(-s)
    L = np.zeros(len(price))
    L[0] = price[0]
    for t in range(1, len(price)):
        L[t] = alpha * price[t] + (1 - alpha) * L[t - 1]
    return L


def series_at(hist, current, offset):
    hist = np.asarray(hist, float)
    if offset == 0:
        return np.append(hist, float(current))
    cut = offset - 1
    if cut == 0:
        return hist
    if len(hist) <= cut:
        return None
    return hist[:-cut]


def hist_vol_ratio(vols, offset, lb):
    vols = np.asarray(vols, float)
    idx = len(vols) - offset
    if idx <= 0 or idx >= len(vols):
        return None
    start = idx - lb
    if start < 0:
        return None
    base = vols[start:idx]
    if len(base) == 0 or np.mean(base) <= 0:
        return None
    return vols[idx] / np.mean(base)


def eval_mainline(hist_closes, hist_vols, current, cur_vr, mom_lb):
    """复刻 evaluate_super_mainline（B型主线，P6 放宽）。hist 不含当日。"""
    if not ML_ENABLE:
        return False, {}
    hist_closes = np.asarray(hist_closes, float)
    hist_vols = np.asarray(hist_vols, float)
    scores, r2s, vrs, laps = [], [], [], []
    for offset in range(ML_DAYS - 1, -1, -1):
        s = series_at(hist_closes, current, offset)
        if s is None or len(s) < int(mom_lb * 0.8):
            return False, {'reason': f'series_short@{offset}'}
        sc, _, r2 = wls_score(s, mom_lb)
        if sc is None or r2 is None:
            return False, {'reason': f'score_nan@{offset}'}
        scores.append(sc)
        r2s.append(r2)
        try:
            lv = laplace_filter(s, LAPLACE_S)
            lap = lv[-1] - lv[-2] if len(lv) >= 2 else 0.0
        except Exception:
            lap = 0.0
        laps.append(lap)
        if offset == 0:
            vrs.append(cur_vr)
        else:
            vrs.append(hist_vol_ratio(hist_vols, offset, VOL_LOOKBACK))
    valid = [v for v in vrs if v is not None and not (isinstance(v, float) and np.isnan(v))]
    has_missing = len(valid) < len(vrs)
    if has_missing:
        if len(valid) < 3:
            return False, {'reason': 'volume_none'}
        fv = float(np.mean(valid))
        vrs = [fv if (v is None or (isinstance(v, float) and np.isnan(v))) else v for v in vrs]
    cur_score = scores[-1]
    cur_r2 = r2s[-1]
    r2_avg = float(np.mean(r2s))
    vol_avg = float(np.mean([v for v in vrs if v is not None]))
    score_up = sum(1 for i in range(1, len(scores)) if scores[i] >= scores[i - 1])
    lap_pos = sum(1 for v in laps if v > 0)
    start_score = scores[0]
    growth = (cur_score / start_score) if start_score > 0 else (float('inf') if cur_score > 0 else 0.0)
    fails = []
    if not (ML_SCORE_MIN < cur_score <= ML_SCORE_MAX):
        fails.append('score_range')
    if cur_r2 < ML_MIN_R2:
        fails.append('r2_cur')
    if r2_avg < ML_MIN_R2_AVG:
        fails.append('r2_avg')
    if vol_avg < ML_MIN_VOL_AVG:
        fails.append('vol_avg')
    if score_up < ML_MIN_SCORE_UP:
        fails.append('score_up')
    if lap_pos < ML_MIN_LAP_POS:
        fails.append('lap_pos')
    if isinstance(growth, float) and np.isinf(growth):
        pass
    elif growth < ML_MIN_GROWTH:
        fails.append('score_growth')
    return (len(fails) == 0), {'reason': 'pass' if not fails else '+'.join(fails)}


def compute_metrics(code, t, close_mat, vol_mat, weak, weak_lb):
    """完全对照 calculate_all_metrics_for_etf。"""
    cm = close_mat[code]
    vm = vol_mat[code]
    idxs_all = np.where(~np.isnan(cm[:t + 1]))[0]
    if len(idxs_all) == 0:
        return None
    idxs_hist = idxs_all[idxs_all < t]
    if len(idxs_hist) == 0:
        return None
    hist_closes = cm[idxs_hist]
    hist_vols = vm[idxs_hist]
    current = cm[t]
    if np.isnan(current):
        return None
    price_series = np.append(hist_closes, current)
    mom_lb_use = weak_lb if weak else LOOKBACK
    if len(price_series) < mom_lb_use * 0.8:
        return None
    score, ann, r2 = wls_score(price_series, mom_lb_use)
    if score is None:
        return None
    passed_momentum = (0 <= score <= MAX_SCORE)
    today_vol = vm[t]
    vr = None
    if not np.isnan(today_vol) and today_vol > 0:
        base = hist_vols[-VOL_LOOKBACK:] if len(hist_vols) >= VOL_LOOKBACK else None
        if base is not None and len(base) == VOL_LOOKBACK and np.mean(base) > 0 and not np.any(base <= 0):
            vr = today_vol / np.mean(base)
    passed_loss = True
    if len(price_series) >= 4:
        d1 = price_series[-1] / price_series[-2]
        d2 = price_series[-2] / price_series[-3]
        d3 = price_series[-3] / price_series[-4]
        if min(d1, d2, d3) < LOSS:
            passed_loss = False
    passed_r2 = r2 > R2_THRESH
    passed_ma = (current > np.mean(price_series[-MA_LOOKBACK:])) if len(price_series) >= MA_LOOKBACK else False
    passed_volume = (vr is None) or (vr < VOL_THRESH)
    lap_slope = 0.0
    try:
        lv = laplace_filter(price_series, LAPLACE_S)
        if len(lv) >= 2:
            lap_slope = lv[-1] - lv[-2]
    except Exception:
        pass
    passed_mainline, ml_info = eval_mainline(hist_closes, hist_vols, current, vr, mom_lb_use)
    return {
        'code': code, 'score': score, 'ann': ann, 'r2': r2, 'price': current,
        'vr': vr, 'lap_slope': lap_slope,
        'passed_momentum': passed_momentum, 'passed_r2': passed_r2,
        'passed_ma': passed_ma, 'passed_volume': passed_volume,
        'passed_loss': passed_loss, 'passed_mainline': passed_mainline,
    }


def apply_filters(metrics, weak):
    if weak:
        conds = [('momentum', lambda m: m['passed_momentum']),
                 ('r2', lambda m: m['passed_r2'])]
    else:
        conds = [('momentum', lambda m: m['passed_momentum']),
                 ('r2', lambda m: m['passed_r2']),
                 ('ma', lambda m: m['passed_ma']),
                 ('vol', lambda m: m['passed_volume']),
                 ('loss', lambda m: m['passed_loss'])]
    filtered = metrics
    for _, cond in conds:
        filtered = [m for m in filtered if cond(m)]
    return filtered


def corr_guard(ordered, already_selected, need, close_mat, t):
    """复刻 apply_correlation_guard。"""
    if need <= 0 or not ordered:
        return []
    chosen = list(already_selected)
    ret = {}
    for c in chosen + [m['code'] for m in ordered]:
        s = close_mat[c][max(0, t - CORR_LOOKBACK):t + 1]
        m = np.diff(np.log(s[~np.isnan(s)]))
        ret[c] = m
    L = CORR_LOOKBACK - 1
    selected = []
    for m in ordered:
        if len(selected) >= need:
            break
        etf = m['code']
        rm = ret.get(etf)
        if rm is None or len(rm) < L:
            selected.append(m)
            chosen.append(etf)
            continue
        max_c = None
        conflict = None
        for held in chosen:
            rh = ret.get(held)
            if rh is None or len(rh) < L:
                continue
            if len(rh) != len(rm):
                n = min(len(rh), len(rm))
                rh = rh[-n:]; rm2 = rm[-n:]
            else:
                rm2 = rm
            if np.std(rh) == 0 or np.std(rm2) == 0:
                continue
            c = np.corrcoef(rh, rm2)[0, 1]
            if max_c is None or c > max_c:
                max_c = c; conflict = held
        if max_c is not None and max_c >= CORR_THRESH:
            continue
        selected.append(m)
        chosen.append(etf)
    return selected


def select_final(filtered, metrics, held_set, weak, close_mat, t):
    """完整对照 get_final_ranked_etfs 第四步。"""
    filtered = filtered[:]
    normal_codes = {m['code'] for m in filtered}
    mainline_list = [m for m in metrics
                     if m['passed_mainline'] and m['code'] not in normal_codes
                     and m['passed_loss']
                     and (not weak or m['passed_ma'])]
    filtered = filtered + mainline_list
    retain_list = []
    fl_codes = {m['code'] for m in filtered}
    for m in metrics:
        if m['code'] in held_set and m['code'] not in fl_codes:
            if m['score'] > ML_SCORE_MAX and m['r2'] >= ML_RETAIN_R2 and m['lap_slope'] > ML_RETAIN_LAP and m['passed_loss']:
                retain_list.append(m)
    filtered = filtered + retain_list
    filtered.sort(key=lambda x: x['score'], reverse=True)
    top_10 = filtered[:10]
    if not top_10:
        return []
    N = WEAK_HOLD if weak else NORMAL_HOLD
    if len(top_10) >= N:
        ref = top_10[N - 1]['score']
        thr = ref * (SCORE_RATIO if not weak else 1.0)
        candidate = [m for m in top_10 if m['score'] >= thr]
    else:
        candidate = top_10
    retained = [c for c in candidate if c['code'] in held_set]
    if len(retained) >= N:
        final = sorted(retained, key=lambda x: x['score'], reverse=True)[:N]
    else:
        need = N - len(retained)
        rest = [c for c in candidate if c['code'] not in {r['code'] for r in retained}]
        if CORR_ENABLE and N > 1:
            additional = corr_guard(rest, [r['code'] for r in retained], need, close_mat, t)
        else:
            additional = rest[:need]
        final = retained + additional
    return [m['code'] for m in final]


def position_weights(codes, score_map, total_val):
    """复刻 compute_target_values + _apply_position_cap。"""
    if not codes:
        return {}
    n = len(codes)
    raw = {}
    for c in codes:
        v = max(float(score_map.get(c, 0) or 0), 0.0)
        raw[c] = v ** POS_POWER if v > 0 else 0.0
    total = float(np.sum(list(raw.values())))
    if total <= 0:
        weights = {c: 1.0 / n for c in codes}
    else:
        weights = {c: raw[c] / total for c in codes}
    cap = POS_CAP_SINGLE if n == 1 else POS_CAP_MULTI
    w = dict(weights)
    uncapped = set(codes)
    for _ in range(n + 1):
        over = [c for c in uncapped if w[c] > cap + 1e-12]
        if not over:
            break
        excess = float(np.sum([w[c] - cap for c in over]))
        for c in over:
            w[c] = cap
            uncapped.discard(c)
        if not uncapped:
            break
        sub = float(np.sum([w[c] for c in uncapped]))
        if sub <= 0:
            break
        for c in uncapped:
            w[c] += excess * (w[c] / sub)
    for c in codes:
        if w[c] > cap:
            w[c] = cap
    return {c: total_val * w[c] for c in codes}


# ==================== 状态演化 + 当日决策 ====================
def compute_regime_state(close_mat, dates):
    """回放 update_weak + update_h72，返回 (weak, weak_lb)。"""
    N = len(dates)
    weak = False
    weak_days = 0
    weak_lb = H72_BASE
    r2_hi = 0
    r2_lo = 0
    global_pool = [c for c in GLOBAL_POOL if c in close_mat]
    weak_idx = [c for c in WEAK_IDX if c in close_mat]
    for t in range(N):
        # update_weak
        if t >= WEAK_MA:
            below = above = 0
            for idx in weak_idx:
                cl = close_mat[idx]
                win = cl[t - WEAK_MA + 1:t + 1]
                if np.isnan(win).any():
                    continue
                ma = np.mean(win)
                if cl[t] > ma:
                    above += 1
                elif cl[t] < ma:
                    below += 1
            cond = below >= 3
            exit_cond = above >= 3
            if weak:
                weak_days += 1
                if weak_days >= MAX_WEAK_DAYS:
                    weak, weak_days = False, 0
                elif exit_cond:
                    weak, weak_days = False, 0
                elif cond:
                    weak_days = 0
            else:
                if cond:
                    weak, weak_days = True, 1
        # update_h72
        r2s = []
        for c in global_pool:
            cl = close_mat[c][:t + 1]
            cl = cl[~np.isnan(cl)]
            if len(cl) < LOOKBACK + 1:
                continue
            _, _, r2 = wls_score(cl, LOOKBACK)
            if r2 is not None:
                r2s.append(r2)
        if r2s:
            pool_r2 = float(np.mean(r2s))
            if pool_r2 > H72_R2_HI:
                r2_hi += 1; r2_lo = 0
            elif pool_r2 < H72_R2_LO:
                r2_lo += 1; r2_hi = 0
            else:
                r2_hi = 0; r2_lo = 0
            if weak_lb == H72_BASE and r2_hi >= H72_ENTER:
                weak_lb = H72_SHORT
            elif weak_lb == H72_SHORT and r2_lo >= H72_EXIT:
                weak_lb = H72_BASE
    return weak, weak_lb


def decide_last(close_mat, vol_mat, dates, held_set):
    """对最后一天做决策，返回 (target_codes, score_map, weak, defensive, weak_lb)。"""
    N = len(dates)
    t = N - 1
    weak, weak_lb = compute_regime_state(close_mat, dates)
    global_pool = [c for c in GLOBAL_POOL if c in close_mat]
    if weak:
        pool = global_pool
    else:
        pool = [c for c in FIXED_POOL if c in close_mat]
    metrics = []
    for c in pool:
        m = compute_metrics(c, t, close_mat, vol_mat, weak, weak_lb)
        if m:
            metrics.append(m)
    metrics.sort(key=lambda x: x['score'], reverse=True)
    filtered = apply_filters(metrics, weak)
    target = select_final(filtered, metrics, held_set, weak, close_mat, t)
    defensive = False
    if not target:
        basket = []
        for b in DEFENSIVE_BASKET:
            code = b + ('.XSHG' if b[0] in '56' else '.XSHE')
            if code in close_mat and not np.isnan(close_mat[code][t]):
                basket.append(code)
        if basket:
            target = basket
            defensive = True
        else:
            target = []
    score_map = {m['code']: m['score'] for m in metrics}
    return target, score_map, weak, defensive, weak_lb


# ==================== 执行（再平衡 + 成本） ====================
def execute_today(target, score_map, defensive, quotes, positions, cash, cost):
    """从当前持仓再平衡到 target。返回 (positions, cash, cost, equity, buys, sells, hold)。"""
    total_val = cash
    for c, sh in positions.items():
        px = quotes.get(c, {}).get('current_price', 0) or 0
        if px > 0:
            total_val += sh * px
    if defensive or not target:
        target_vals = {c: total_val / len(target) for c in target} if target else {}
    else:
        target_vals = position_weights(target, score_map, total_val)
    buys, sells = [], []
    target_set = set(target_vals.keys())
    # 卖出不在目标中的持仓
    for c in list(positions.keys()):
        if c not in target_set:
            q = quotes.get(c)
            px = q.get('current_price', 0) if q else 0
            if px and px > 0:
                sh = positions[c]
                proceeds = sh * px * (1 - SLIPPAGE_RATE)
                comm = proceeds * COMMISSION_RATE
                cash += proceeds - comm
                sells.append({'code': c, 'name': NAME_CACHE.get(c, c),
                              'shares': round(sh, 2), 'price': round(px, 4),
                              'amount': round(proceeds - comm, 2)})
                positions.pop(c, None)
                cost.pop(c, None)
    # 买入/再平衡
    for c, tv in target_vals.items():
        q = quotes.get(c)
        px = q.get('current_price', 0) if q else 0
        if not px or px <= 0:
            continue
        desired = int(tv / (px * (1 + SLIPPAGE_RATE)) / 100) * 100   # 整百股
        cur = positions.get(c, 0)
        delta = desired - cur
        # 按整手对齐增减仓（清仓走上面的全卖分支，可含零股）
        if delta > 0:
            delta = int(delta / 100) * 100
        else:
            delta = -(int(-delta / 100) * 100)
        if delta == 0 or abs(delta * px) <= 10:   # 微小变动忽略
            continue
        if delta > 0:
            amt = delta * px * (1 + SLIPPAGE_RATE)
            comm = amt * COMMISSION_RATE
            tot = amt + comm
            if tot > cash:
                affordable = int(cash / (1 + SLIPPAGE_RATE) / (1 + COMMISSION_RATE) / px / 100) * 100
                delta = affordable
                tot = delta * px * (1 + SLIPPAGE_RATE) * (1 + COMMISSION_RATE)
                if delta <= 0:
                    continue
            cash -= tot
            old = positions.get(c, 0)
            positions[c] = old + delta
            cost[c] = (cost.get(c, 0) * old + tot) / positions[c] if positions[c] > 0 else 0
            buys.append({'code': c, 'name': NAME_CACHE.get(c, c),
                         'shares': round(delta, 2), 'price': round(px, 4),
                         'amount': round(tot, 2)})
        else:
            sell_amt = (-delta) * px * (1 - SLIPPAGE_RATE)
            comm = sell_amt * COMMISSION_RATE
            net = sell_amt - comm
            cash += net
            positions[c] = cur + delta
            if positions[c] <= 1e-9:
                positions.pop(c, None)
                cost.pop(c, None)
            else:
                cost[c] = cost.get(c, 0) * cur / positions[c]
            sells.append({'code': c, 'name': NAME_CACHE.get(c, c),
                          'shares': round(-delta, 2), 'price': round(px, 4),
                          'amount': round(net, 2)})
    # 盯市（按当日现价）
    eq = cash
    hold = []
    for c, sh in positions.items():
        q = quotes.get(c)
        px = q.get('current_price', 0) if q else 0
        if px > 0:
            eq += sh * px
            hold.append({'code': c, 'name': NAME_CACHE.get(c, c),
                         'shares': round(sh, 2), 'price': round(px, 4)})
    return positions, cash, cost, eq, buys, sells, hold


# ==================== 净值/统计 ====================
def _summarize(daily, capital):
    if not daily:
        return {}
    eqs = [d['equity'] for d in daily]
    start_eq = daily[0]['equity_prev']
    end_eq = daily[-1]['equity']
    total_ret = end_eq / start_eq - 1 if start_eq > 0 else 0
    peak = eqs[0]; mdd = 0.0
    for e in eqs:
        if e > peak:
            peak = e
        dd = e / peak - 1
        if dd < mdd:
            mdd = dd
    rets = [d['ret'] for d in daily if d['equity_prev'] > 0]
    mean = sum(rets) / len(rets) if rets else 0
    var = sum((r - mean) ** 2 for r in rets) / len(rets) if rets else 0
    std = var ** 0.5
    sharpe = (mean / std * (252 ** 0.5)) if std > 0 else 0.0
    win = sum(1 for r in rets if r > 0)
    win_rate = win / len(rets) if rets else 0
    d0 = datetime.strptime(daily[0]['date'], '%Y-%m-%d')
    d1 = datetime.strptime(daily[-1]['date'], '%Y-%m-%d')
    days = max((d1 - d0).days, 1)
    cagr = ((end_eq / start_eq) ** (365.0 / days) - 1) if start_eq > 0 and end_eq > 0 else 0
    trades = sum(len(d['buy']) + len(d['sell']) for d in daily)
    return {'total_ret': total_ret, 'equity': end_eq, 'max_dd': mdd,
            'sharpe': sharpe, 'win_rate': win_rate, 'cagr': cagr,
            'days': len(daily), 'trades': trades}


# ==================== 主流程 ====================
def run_once():
    if os.environ.get('SETTLE') == '1':
        settle_last_day()
        return
    today = _bj_now()
    today_str = today.strftime('%Y-%m-%d')
    if today.weekday() >= 5:
        _log(f"周末({today_str})，跳过")
        return
    _log(f"===== 五福云端 {today_str} =====")
    # 1) 历史日K
    kl = fetch_klines_batch(ALL_CODES, limit=130)
    if not kl:
        _log("K线获取失败，退出")
        return
    # 2) 实时行情（含今日价/量）
    quotes = get_quotes_cloud(ALL_CODES)
    if len(quotes) < int(len(ALL_CODES) * 0.5):
        _log(f"实时行情覆盖不足({len(quotes)}/{len(ALL_CODES)})，跳过")
        return
    # 3) 构建日期轴 + 矩阵
    date_set = set()
    raw = {}
    for c, arr in kl.items():
        for it in arr:
            raw.setdefault(c, {})[it['date']] = (it['close'], it['volume'])
            date_set.add(it['date'])
    dates = sorted(date_set)
    if today_str > dates[-1]:
        dates.append(today_str)
    elif today_str == dates[-1]:
        pass  # 今日已在K线中（盘中），下面用实时价覆盖
    n = len(dates)
    today_idx = dates.index(today_str)
    close_mat = {}; vol_mat = {}
    for c in ALL_CODES:
        cl = np.full(n, np.nan); vo = np.full(n, np.nan)
        m = raw.get(c, {})
        for i, d in enumerate(dates):
            if d == today_str:
                q = quotes.get(c)
                if q:
                    cl[i] = q['current_price']
                    vo[i] = q.get('volume', 0) or 0
            elif d in m:
                cl[i] = m[d][0]
                vo[i] = m[d][1]
        # 前复权（仅用于盯市，避免停牌/末日 NaN 污染净值）
        last = np.nan
        for i in range(n):
            if not np.isnan(cl[i]):
                last = cl[i]
            elif not np.isnan(last):
                cl[i] = last
        close_mat[c] = cl
        vol_mat[c] = vo
    # 4) 当日决策
    pos_state = load_positions()
    held_set = set(pos_state.get('positions', {}).keys())
    target, score_map, weak, defensive, weak_lb = decide_last(close_mat, vol_mat, dates, held_set)
    _log(f"regime weak={weak} weak_lb={weak_lb} defensive={defensive} target={target}")
    # 5) 执行再平衡
    positions = dict(pos_state.get('positions', {}))
    cash = pos_state.get('cash', INIT_CAPITAL)
    cost = dict(pos_state.get('cost', {}))
    positions, cash, cost, eq, buys, sells, hold = execute_today(
        target, score_map, defensive, quotes, positions, cash, cost)
    # 6) 写历史 + 统计
    rec = {
        'date': today_str,
        'equity': round(eq, 2),
        'regime': 'weak' if weak else 'normal',
        'defensive': defensive,
        'buy': buys, 'sell': sells, 'hold': hold,
    }
    append_stats(rec)
    save_positions({'cash': cash, 'positions': positions, 'cost': cost, 'date': today_str})
    _log(f"净值={eq:.2f} 买入{len(buys)} 卖出{len(sells)} 持仓{len(positions)}")


def load_positions():
    if os.path.exists(POS_FILE):
        try:
            return json.load(open(POS_FILE, encoding='utf-8'))
        except Exception:
            return {}
    return {}


def save_positions(state):
    json.dump(state, open(POS_FILE, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)


def append_stats(rec):
    daily = []
    if os.path.exists(STATS_FILE):
        try:
            daily = json.load(open(STATS_FILE, encoding='utf-8')).get('daily', [])
        except Exception:
            daily = []
    if daily and daily[-1]['date'] == rec['date']:
        daily[-1] = rec
    else:
        daily.append(rec)
    # 重算 equity_prev / ret / cumret
    prev_eq = INIT_CAPITAL
    for d in daily:
        d['equity_prev'] = round(prev_eq, 2)
        d['ret'] = (d['equity'] / prev_eq - 1) if prev_eq > 0 else 0.0
        d['cumret'] = (d['equity'] / INIT_CAPITAL - 1)
        prev_eq = d['equity']
    summary = _summarize(daily, INIT_CAPITAL)
    out = {
        'params': {'commission': COMMISSION_RATE, 'slippage': SLIPPAGE_RATE,
                   'capital': INIT_CAPITAL, 'start': daily[0]['date'] if daily else '',
                   'strategy': '五福v1.1(多持仓)', 'cost_note': '与轮动V1统一:佣金万0.5+滑点0.1%'},
        'daily': daily,
        'summary': summary,
    }
    json.dump(out, open(STATS_FILE, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)


def settle_last_day():
    """收盘结算（SETTLE=1）：不交易，只用收盘价重算当日净值与收益率。
    覆盖当日 13:10 的盘中估值，使网页收盘后反映"13:10买卖价+当日收盘价"口径。"""
    today = _bj_now()
    today_str = today.strftime('%Y-%m-%d')
    if today.weekday() >= 5:
        _log(f"周末({today_str})，跳过结算")
        return
    pos_state = load_positions()
    positions = pos_state.get('positions', {})
    cash = pos_state.get('cash', INIT_CAPITAL)
    if not positions:
        _log("无持仓，无需收盘结算")
        return
    codes = list(positions.keys())
    # 优先用当日日K官方收盘价（收盘后最权威），失败再回退实时行情
    kl = fetch_klines_batch(codes, limit=3)
    close_map = {}
    for c in codes:
        arr = kl.get(c)
        if arr:
            close = arr[-1].get('close')
            if close and close > 0:
                close_map[c] = close
    if not close_map:
        quotes = get_quotes_cloud(codes)
        for c in codes:
            q = quotes.get(c)
            px = q.get('current_price', 0) if q else 0
            if px > 0:
                close_map[c] = px
    if not close_map:
        _log("无法获取收盘行情，跳过结算")
        return
    eq = cash
    hold = []
    for c, sh in positions.items():
        px = close_map.get(c, 0)
        if px > 0:
            eq += sh * px
            hold.append({'code': c, 'name': NAME_CACHE.get(c, c),
                         'shares': round(sh, 2), 'price': round(px, 4)})
    daily = []
    if os.path.exists(STATS_FILE):
        try:
            daily = json.load(open(STATS_FILE, encoding='utf-8')).get('daily', [])
        except Exception:
            daily = []
    if daily and daily[-1]['date'] == today_str:
        daily[-1]['equity'] = round(eq, 2)
        daily[-1]['hold'] = hold
        # 重算整条曲线的 ret / cumret（仅末日变更，重算即可）
        prev_eq = INIT_CAPITAL
        for d in daily:
            d['equity_prev'] = round(prev_eq, 2)
            d['ret'] = (d['equity'] / prev_eq - 1) if prev_eq > 0 else 0.0
            d['cumret'] = (d['equity'] / INIT_CAPITAL - 1)
            prev_eq = d['equity']
        summary = _summarize(daily, INIT_CAPITAL)
        out = {
            'params': {'commission': COMMISSION_RATE, 'slippage': SLIPPAGE_RATE,
                       'capital': INIT_CAPITAL, 'start': daily[0]['date'] if daily else '',
                       'strategy': '五福v1.1(多持仓)', 'cost_note': '与轮动V1统一:佣金万0.5+滑点0.1%'},
            'daily': daily,
            'summary': summary,
        }
        json.dump(out, open(STATS_FILE, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
        _log(f"收盘结算 净值={eq:.2f} 持仓{len(hold)}")
    else:
        _log("今日无交易记录，无需结算")


if __name__ == '__main__':
    run_once()
