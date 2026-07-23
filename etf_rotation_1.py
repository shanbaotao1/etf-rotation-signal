#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ETF动量轮动策略 - 自循环版（单只满仓 + 熊市切避险池 + 动态窗口 + 防御兜底）
- 数据源：Baostock 首选，PanWatch 并发补缺，Sina 兜底
- 29只精选ETF池（低关联+全覆盖）
- 25日线性加权动量，牛市7/震荡5/熊市3动态调分，R²过滤
- 单只满仓：每日只持得分最高1只（满仓），释放收益弹性
- 熊市(②)：regime=='bear'时仅在9只避险池中选股（境外+黄金+商品+债券+红利），规避弱势A股
- 防御ETF兜底(①)：无信号时不空仓，改持货币ETF 511880（银华日利）赚票息
- 动态动量窗口(⑥)：全池R²中位数>0.65→23天窗口跟强市，否则25天，2日迟滞防抖
- 调仓时间：13:30（可配置）
- 自循环模式，无需外部定时器

最后更新: 2026-07-20
本次改动: 移植回测验证通过的增强——单只满仓(SINGLE_HOLDING)+熊市切避险池(USE_WEAK_POOL_SWITCH)+
          防御ETF兜底(USE_DEFENSIVE_ETF)+动态动量窗口(USE_DYNAMIC_WINDOW)；③④质量过滤暂不采用。
"""

import requests
import json
import logging
import numpy as np
import math
import os
import sys
import time
import re
from datetime import datetime, timedelta, timezone
from logging.handlers import TimedRotatingFileHandler

# ================== 尝试导入 AkShare（Sina兜底数据源） ==================
try:
    import akshare as ak
except ImportError:
    ak = None
    print("⚠️ AkShare 未安装，Sina兜底数据源不可用。请执行: pip install akshare")

# ================== 配置 ==================
PANWATCH_URL = os.environ.get("PANWATCH_URL", "http://192.168.123.156:8000")
PANWATCH_USER = os.environ.get("PANWATCH_USER", "shanbaotao")
PANWATCH_PWD = os.environ.get("PANWATCH_PWD", "wynlxx11")
PUSHPLUS_TOKEN = os.environ.get("PUSHPLUS_TOKEN", "58ce00f54cb74f0f8f6a910668dc2676")
PUSH_PREFIX = "🔵【轮动V1】"
TOTAL_CAPITAL = 50000

# ================== 云端模式（GitHub Actions 用） ==================
# 设为 "1" 时：跳过本地 PanWatch，改用新浪/东财公开行情；单次计算并写 SIGNAL.md。
CLOUD_MODE = os.environ.get("CLOUD_MODE", "0") == "1"

# 策略参数
MOMENTUM_DAYS = 25
TOP_N = 1                    # 持有数量（1或2）
MIN_SCORE = 0
MAX_SCORE = 6
ALPHA = 0.08

# 调仓时间（小时:分钟）
ADJUST_HOUR = 13
ADJUST_MINUTE = 30

# ================== 持仓模式 ==================
# 单只满仓：True=每日只持得分最高1只（满仓）；False=行业分散双持仓（最多2只各50%）
SINGLE_HOLDING = True

# ================== 借鉴聚宽五福v7 的实盘增强（均经回测验证采纳） ==================
# ① 防御ETF兜底：无信号时不空仓，改持货币ETF（白赚票息，减少现金拖累）
USE_DEFENSIVE_ETF = True
DEFENSIVE_ETF = "511880.XSHG"   # 银华日利（货币ETF）
# ② 走弱期切换避险池 + 放松过滤：熊市(regime=='bear')时只在避险池中选，且放宽R²阈值
USE_WEAK_POOL_SWITCH = True
# 避险池（与A股低相关）：境外权益 + 贵金属 + 商品 + 债券 + 低波红利（均属29只池子集）
GLOBAL_POOL = ["513100.XSHG", "513520.XSHG", "513030.XSHG", "518880.XSHG",
               "159985.XSHE", "159980.XSHE", "511090.XSHG", "511260.XSHG", "563020.XSHG"]
WEAK_R2_THRESHOLD = 0.0         # 熊市放松：R²阈值降到0（仅要求r2>0）
# ⑥ 动态动量窗口（H72思想）：全池R²质量高→窗口缩短(跟强市更快)，带迟滞防抖
USE_DYNAMIC_WINDOW = True
DYNAMIC_WIN_SHORT = 23           # 强趋势环境窗口
DYNAMIC_WIN_LONG = MOMENTUM_DAYS # 弱势/震荡窗口（=25）
DYNAMIC_R2_QUALITY = 0.65        # 全池R²中位数阈值
DYNAMIC_HYST = 2                 # 连续N日才切换窗口
# ③ 严格质量过滤（暂不启用）：R²≥阈值才入选；USE_MA_FILTER：价格>MA10 才入选
R2_FILTER_THRESHOLD = 0.0        # >0 时启用（如0.4）；0=沿用基线(等价于r2>0)
USE_MA_FILTER = False

# ================== 29只优化ETF池 ==================
ETF_POOL = [
    # ===== 进攻 - 境外（4只） =====
    "513100.XSHG",   # 纳指ETF
    "513520.XSHG",   # 日经ETF
    "513030.XSHG",   # 德国ETF
    "513130.XSHG",   # 恒生科技ETF
    # ===== 进攻 - 国内宽基（3只） =====
    "510180.XSHG",   # 上证180ETF
    "159915.XSHE",   # 创业板ETF
    "510500.XSHG",   # 中证500ETF
    # ===== 进攻 - 科技成长（5只） =====
    "588120.XSHG",   # 科创100ETF
    "515070.XSHG",   # 人工智能ETF
    "512480.XSHG",   # 半导体ETF
    "516510.XSHG",   # 云计算ETF
    "159667.XSHE",   # 工业母机ETF
    # ===== 进攻 - 新能源（2只） =====
    "159755.XSHE",   # 电池ETF
    "516160.XSHG",   # 新能源ETF
    # ===== 进攻 - 军工/高端（2只） =====
    "512710.XSHG",   # 军工龙头ETF
    "159227.XSHE",   # 航空航天ETF
    # ===== 进攻 - 周期资源（3只） =====
    "510410.XSHG",   # 资源ETF
    "159980.XSHE",   # 有色ETF
    "159985.XSHE",   # 豆粕ETF
    # ===== 进攻 - 医药（2只） =====
    "512290.XSHG",   # 生物医药ETF
    "159992.XSHE",   # 创新药ETF
    # ===== 进攻 - 消费（2只） =====
    "159928.XSHE",   # 消费ETF
    "159865.XSHE",   # 养殖ETF
    # ===== 进攻 - 金融（1只） =====
    "512000.XSHG",   # 证券ETF
    # ===== 防守 - 债券（2只） =====
    "511090.XSHG",   # 30年国债ETF
    "511260.XSHG",   # 10年国债ETF
    # ===== 防守 - 贵金属（1只） =====
    "518880.XSHG",   # 黄金ETF
    # ===== 防守 - 红利（2只） =====
    "563020.XSHG",   # 红利低波ETF
    "520810.XSHG",   # 港股红利ETF
]

# 防御ETF也纳入K线缓存（用于价格兜底）
_CACHE_EXTRA = [DEFENSIVE_ETF] if (USE_DEFENSIVE_ETF and DEFENSIVE_ETF not in ETF_POOL) else []

# ================== 行业分类（用于双持仓分散） ==================
SECTOR_KEYWORDS = {
    '海外QDII':   ['纳指', '纳斯达克', '标普', '恒生', '恒指', '日经', '德国', '法国', '油气', '石油', '中韩'],
    '科技半导体': ['半导体', '芯片', '集成电路', '电子', 'TMT', '信息技术', '信创', '软件', '计算机', '人工智能', 'AI', '智能', '机器人', '科技', '通信', '5G', '云计算'],
    '新能源':     ['新能源', '光伏', '风电', '储能', '电池', '锂电', '新能源车', '汽车', '新材料', '碳中和', '环保'],
    '医药生物':   ['医药', '医疗', '生物', '创新药'],
    '金融':       ['证券', '券商', '银行', '保险', '非银', '金融'],
    '消费':       ['消费', '食品', '饮料', '白酒', '家电', '旅游', '零售', '农业', '猪肉', '酒', '养殖'],
    '军工':       ['军工', '国防', '航天', '航空'],
    '资源周期':   ['有色', '稀土', '矿业', '煤炭', '钢铁', '化工', '电力', '黄金', '铜', '能源', '材料', '资源', '大宗商品', '工业母机', '机床', '高端制造', '豆粕'],
    '宽基指数':   ['沪深300', '中证500', '中证1000', '创业板', '科创50', '科创创业', '双创', 'A50', 'MSCI', '上证50', '上证180', '上证380', '上证指数', '国企', '成长', '价值', '央企', '科创'],
    '红利低波':   ['红利', '低波', '高股息', '国债'],
}

ETF_NAME_OVERRIDE = {
    '520810.XSHG': '港股通红利ETF',
}

def get_etf_sector(etf_name):
    for sector, keywords in SECTOR_KEYWORDS.items():
        for kw in keywords:
            if kw in etf_name:
                return sector
    return '其他'

# ================== 日志系统 ==================
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(SCRIPT_DIR, 'logs')
os.makedirs(LOG_DIR, exist_ok=True)

logger = logging.getLogger('')
logger.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - %(levelname)-7s - %(message)s')

ch = logging.StreamHandler()
ch.setLevel(logging.INFO)
ch.setFormatter(formatter)
logger.addHandler(ch)

fh = TimedRotatingFileHandler(
    os.path.join(LOG_DIR, 'rotation_v1.log'),
    when='midnight', interval=1, backupCount=30, encoding='utf-8'
)
fh.setLevel(logging.INFO)
fh.setFormatter(formatter)
logger.addHandler(fh)

logging.getLogger('urllib3').setLevel(logging.WARNING)
logging.getLogger('requests').setLevel(logging.WARNING)

# ================== 缓存管理 ==================
CACHE_FILE = os.path.join(SCRIPT_DIR, 'kline_cache.json')

def load_cache():
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            pass
    return {"date": "", "data": {}}

def save_cache(cache):
    with open(CACHE_FILE, 'w', encoding='utf-8') as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)

# ================== PanWatch 客户端 ==================
class PanWatchClient:
    def __init__(self):
        self.session = requests.Session()
        self.token = None
        self._login()

    def _login(self):
        try:
            resp = self.session.post(
                f"{PANWATCH_URL}/api/auth/login",
                json={"username": PANWATCH_USER, "password": PANWATCH_PWD},
                timeout=10
            )
            if resp.status_code == 200:
                self.token = resp.json()["data"]["token"]
                self.session.headers.update({"Authorization": f"Bearer {self.token}"})
                logger.debug("PanWatch登录成功")
        except Exception as e:
            logger.error(f"PanWatch登录异常: {e}")

    def get_quotes_batch(self, symbols):
        if not symbols:
            return {}
        if not self.token:
            self._login()
        items = [{"symbol": s.split('.')[0], "market": "CN"} for s in symbols]
        try:
            resp = self.session.post(
                f"{PANWATCH_URL}/api/quotes/batch",
                json={"items": items}, timeout=30
            )
            if resp.status_code == 200 and resp.json().get("success"):
                data = resp.json()["data"]
                quotes = {}
                for item in data:
                    code = f"{item['symbol']}.{'XSHG' if item['symbol'][0] in '56' else 'XSHE'}"
                    quotes[code] = item
                return quotes
        except Exception as e:
            logger.warning(f"PanWatch获取行情失败: {e}")
        return {}

    def get_klines(self, symbol, limit=45):
        if not self.token:
            self._login()
        code = symbol.split('.')[0]
        try:
            resp = self.session.get(
                f"{PANWATCH_URL}/api/klines/{code}",
                params={"limit": limit, "market": "CN"},
                timeout=10
            )
            if resp.status_code == 200 and resp.json().get("success"):
                return resp.json()["data"]["klines"]
        except Exception as e:
            logger.debug(f"PanWatch获取K线异常 {symbol}: {e}")
        return []

    def get_klines_batch(self, symbols, limit=45):
        """并发获取K线"""
        from concurrent.futures import ThreadPoolExecutor, as_completed
        results = {}
        with ThreadPoolExecutor(max_workers=10) as executor:
            fut_map = {executor.submit(self.get_klines, s, limit): s for s in symbols}
            for future in as_completed(fut_map):
                s = fut_map[future]
                try:
                    results[s] = future.result()
                except:
                    results[s] = []
        return results

def fetch_klines_akshare_sina(code, limit=45):
    """Sina 兜底数据源（个别Baostock/PanWatch取不到时补位）"""
    if ak is None:
        return None
    try:
        symbol = code.split('.')[0]
        # Sina需要带市场前缀：sh.51xxx / sz.15xxx
        if symbol.startswith(('51', '56', '58')):
            ak_symbol = f"sh{symbol}"
        else:
            ak_symbol = f"sz{symbol}"
        df = ak.fund_etf_hist_sina(symbol=ak_symbol)
        if df is None or df.empty:
            return None
        df = df.sort_values('date')
        df = df.tail(limit)
        klines = []
        for _, row in df.iterrows():
            close = float(row['close'])
            if close <= 0:
                continue
            klines.append({
                'close': close,
                'volume': float(row.get('volume', 0)),
                'amount': float(row.get('amount', 0)),
            })
        return klines
    except Exception as e:
        logger.debug(f"AkShare-Sina获取 {code} 失败: {e}")
        return None

# ================== 云端数据源（新浪主 + 东财兜底，GitHub Actions 用） ==================
def _em_secid(code):
    num = code.split('.')[0]
    return f"{'1' if code.endswith('XSHG') else '0'}.{num}"

def fetch_klines_sina(code, limit=60):
    """新浪日K（公开源）。返回 [{close,volume,amount}]。"""
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
            out.append({'close': close, 'volume': vol, 'amount': float(item.get('amount', 0))})
        return out
    except Exception as e:
        logger.debug(f"新浪K线 {code} 失败: {e}")
        return []

def fetch_klines_em(code, limit=60):
    """东财日K兜底（前复权）。"""
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
            out.append({'close': close, 'volume': vol})
        return out
    except Exception as e:
        logger.debug(f"东财K线 {code} 失败: {e}")
        return []

def fetch_klines_cloud(symbols, limit=45):
    results = {}
    for code in symbols:
        kl = fetch_klines_sina(code, limit + 5)
        if len(kl) < MOMENTUM_DAYS:
            kl = fetch_klines_em(code, limit + 5)
        if kl:
            results[code] = kl[-limit:] if len(kl) > limit else kl
    logger.info(f"云端K线获取: {len(results)}/{len(symbols)} 只")
    return results

def get_quotes_sina(symbols):
    """新浪实时行情（含名称+现价），需 Referer。带一次重试。"""
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
                if len(f) < 4 or not f[3]:
                    continue
                try:
                    price = float(f[3])
                except:
                    continue
                if price > 0:
                    quotes[full] = {'current_price': price, 'name': f[0],
                                    'high_limit': 0, 'low_limit': 0, 'volume': 0}
        except Exception as e:
            logger.warning(f"新浪实时行情失败(第{attempt+1}次): {e}")
        if len(quotes) >= int(len(symbols) * 0.8):
            break
    return quotes

def get_quotes_em(symbols):
    """东财实时行情兜底（新浪失败时）。f43/1000 为ETF现价。"""
    quotes = {}
    for code in symbols:
        try:
            r = requests.get(
                f"https://push2.eastmoney.com/api/qt/stock/get?secid={_em_secid(code)}&fields=f43,f57,f58",
                timeout=10)
            d = r.json().get('data', {})
            if not d or not d.get('f43'):
                continue
            price = d['f43'] / 1000.0
            if price > 0:
                quotes[code] = {'current_price': price, 'name': d.get('f58', code),
                                'high_limit': 0, 'low_limit': 0, 'volume': 0}
        except Exception as e:
            logger.debug(f"东财实时 {code} 失败: {e}")
    return quotes

def get_quotes_cloud(symbols):
    q = get_quotes_sina(symbols)
    missing = [s for s in symbols if s not in q]
    if missing:
        logger.info(f"新浪缺 {len(missing)} 只，补东财: {missing}")
        for k, v in get_quotes_em(missing).items():
            q.setdefault(k, v)
    return q

def _bj_now():
    return datetime.now(timezone.utc) + timedelta(hours=8)

def write_signal_report(regime, dyn, m_eff, eligible, targets, prev, new, quotes):
    bj = _bj_now().strftime('%Y-%m-%d %H:%M:%S')
    cache = load_cache(); cdata = cache.get('data', {})
    ok = sum(1 for c in ETF_POOL if len(cdata.get(c, [])) >= MOMENTUM_DAYS)
    regime_cn = {'bull': '牛市', 'bear': '熊市', 'sideways': '震荡市'}.get(regime, regime)

    target_codes = set(new.keys())
    sell_lines, buy_lines = [], []
    for code, pos in prev.items():
        if code not in target_codes:
            sell_lines.append(f"卖出 {pos.get('name', code)}({code}) ｜ 现价约 {quotes.get(code, {}).get('current_price', '?')}")
    for code, pos in new.items():
        price = pos.get('buy_price', 0); shares = pos.get('shares', 0)
        if code in prev:
            buy_lines.append(f"持有不动 {pos.get('name', code)}({code}) ｜ {shares}股 @ {price:.3f}")
        else:
            buy_lines.append(f"买入 {pos.get('name', code)}({code}) ｜ {shares}股 × {price:.3f} ≈ {shares*price:.0f}元")

    if not eligible:
        if DEFENSIVE_ETF in new:
            action_head = "🛡️ 无合格标的 → 建议切换防御ETF（银华日利 511880）赚票息"
        else:
            action_head = "⚪ 无合格标的 → 建议空仓"
    elif targets:
        t0 = targets[0]
        action_head = f"🎯 今日建议持有：{t0['name']}({t0['code']}) ｜ 得分 {t0['score']:.4f} ｜ 现价 {t0['price']:.3f}"
    else:
        action_head = "⚪ 无目标"

    action_block = "**【卖出】**\n" + ("\n".join(sell_lines) if sell_lines else "无")
    action_block += "\n\n**【买入/持有】**\n" + ("\n".join(buy_lines) if buy_lines else "无")

    rank_lines = []
    for i, it in enumerate(eligible[:10], 1):
        rank_lines.append(
            f"| {i} | {it['code']} | {it['name']} | {it['score']:.4f} | {it['ann']:.2%} | {it['r2']:.4f} | {it['price']:.3f} |")
    rank_md = "\n".join(rank_lines) if rank_lines else "（无）"

    md = f"""# ETF 轮动信号（云端备份）

> 生成时间：**{bj}**（北京时间） ｜ 数据源：新浪/东财公开行情 ｜ 模式：{'单只满仓' if SINGLE_HOLDING else '双持仓分散'}
> ⚠️ 本页由 GitHub Actions 每日 13:30(北京时间) 自动生成，作为极空间/本地脚本的**备用查看**。实际买卖请以你本地执行为准。

## 今日操作建议
{action_head}

{action_block}

## 市场状态
- 状态：**{regime_cn}**（分数上限 {dyn}）
- 动量窗口：**{m_eff} 天**（动态窗口 {'开' if USE_DYNAMIC_WINDOW else '关'}）
- 候选合格标的：**{len(eligible)}** 只

## 候选排名（前 10）
| 排名 | 代码 | 名称 | 得分 | 年化收益 | R² | 现价 |
|---|---|---|---|---|---|---|
{rank_md}

## 数据覆盖
- K线充足：**{ok}/{len(ETF_POOL)}** 只 ETF（不足者不参与打分）
- 说明：K线为前收盘价历史（东财/新浪），现价为 13:30 实时快照。

---
*本信号仅供备份参考，不构成投资建议。*
"""
    with open(os.path.join(SCRIPT_DIR, 'SIGNAL.md'), 'w', encoding='utf-8') as f:
        f.write(md)

    # ---- 涨跌色（中国习惯：涨红跌绿） ----
    def _last_close(code):
        kl = cdata.get(code, [])
        if not kl:
            return None
        last = kl[-1]
        try:
            if isinstance(last, dict):
                return float(last.get('close'))
            if isinstance(last, list) and len(last) >= 5:
                return float(last[4])
        except Exception:
            return None
        return None

    def _pct(code, price):
        lc = _last_close(code)
        return (price - lc) / lc if lc else None

    def _col(pct):
        if pct is None:
            return '#666'
        return '#e53935' if pct > 0 else ('#2e7d32' if pct < 0 else '#666')

    import re as _re
    action_html = _re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', action_block).replace('\n', '<br>')

    rows = ""
    for i, it in enumerate(eligible[:10], 1):
        pct = _pct(it['code'], it['price'])
        pct_txt = f"{pct*100:+.2f}%" if pct is not None else "—"
        col = _col(pct)
        rows += (f"<tr><td>{i}</td><td>{it['code']}</td><td>{it['name']}</td>"
                 f"<td>{it['score']:.4f}</td><td>{it['ann']:.2%}</td><td>{it['r2']:.4f}</td>"
                 f"<td style='color:{col};font-weight:700'>{it['price']:.3f}<br><small>{pct_txt}</small></td></tr>")

    regime_color = {'bull': '#e53935', 'bear': '#1565c0', 'sideways': '#757575'}.get(regime, '#757575')
    css = """
    <style>
      *{box-sizing:border-box}
      body{font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;margin:0;background:#f0f2f5;color:#1a1a1a}
      .wrap{max-width:760px;margin:0 auto;padding:16px}
      .head{background:#fff;border-radius:14px;padding:18px 20px;box-shadow:0 2px 8px rgba(0,0,0,.06)}
      .head h1{margin:0 0 4px;font-size:22px}
      .sub{color:#888;font-size:13px}
      .hero{margin-top:14px;border-radius:14px;padding:18px 20px;color:#fff;font-size:18px;font-weight:700;line-height:1.5}
      .grid{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:14px}
      .card{background:#fff;border-radius:14px;padding:16px;box-shadow:0 2px 8px rgba(0,0,0,.06)}
      .card h3{margin:0 0 10px;font-size:15px;color:#444}
      .act{font-size:14px;line-height:1.7;white-space:normal}
      .meta{font-size:13px;color:#555;line-height:1.7}
      table{width:100%;border-collapse:collapse;font-size:13px;margin-top:6px}
      th,td{border-bottom:1px solid #eee;padding:8px 6px;text-align:left}
      th{background:#fafafa;color:#666;font-weight:600}
      .foot{color:#999;font-size:12px;margin-top:18px;text-align:center;line-height:1.6}
      @media(max-width:600px){.grid{grid-template-columns:1fr}.head h1{font-size:20px}}
    </style>"""
    html = f"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>ETF轮动信号</title>{css}</head>
<body><div class="wrap">
<div class="head"><h1>📈 ETF 轮动信号 <span style="font-size:13px;color:#2b7">☁️云端备份</span></h1>
<p class="sub">生成时间：{bj}（北京时间）｜ 模式：{'单只满仓' if SINGLE_HOLDING else '双持仓分散'} ｜ 数据源：新浪/东财</p></div>
<div class="hero" style="background:{regime_color}">{action_head}</div>
<div class="grid">
<div class="card"><h3>今日操作</h3><div class="act">{action_html}</div></div>
<div class="card"><h3>市场状态</h3><p class="meta">状态：<b>{regime_cn}</b>（分数上限 {dyn}）<br>动量窗口：<b>{m_eff} 天</b>（动态窗口 {'开' if USE_DYNAMIC_WINDOW else '关'}）<br>候选合格：<b>{len(eligible)}</b> 只</p></div>
</div>
<div class="card" style="margin-top:14px"><h3>候选排名（前 10，现价红涨绿跌）</h3>
<table><tr><th>排名</th><th>代码</th><th>名称</th><th>得分</th><th>年化</th><th>R²</th><th>现价</th></tr>
{rows}
</table></div>
<p class="foot">K线充足 {ok}/{len(ETF_POOL)} 只 ETF（不足者不参与打分）。本页由 GitHub Actions 每日 13:30(北京时间) 自动更新，作为极空间/本地脚本的备用查看；实际买卖以你本地执行为准。<br>本信号仅供备份参考，不构成投资建议。</p>
</div></body></html>"""
    with open(os.path.join(SCRIPT_DIR, 'index.html'), 'w', encoding='utf-8') as f:
        f.write(html)
    logger.info(f"已写 SIGNAL.md / index.html（{bj}，合格{len(eligible)}只，覆盖{ok}/{len(ETF_POOL)}）")

def run_cloud_once():
    """云端单次运行：抓行情→算信号→写报告。"""
    mode_desc = "单只满仓" if SINGLE_HOLDING else "行业分散双持仓"
    logger.info("=" * 60)
    logger.info(f"{PUSH_PREFIX} 云端模式启动（{mode_desc}）")
    logger.info(f"ETF池: {len(ETF_POOL)}只  调仓时间: 13:30(北京时间)")
    logger.info("数据源: 新浪(主) + 东财(兜底)，跳过本地 PanWatch")
    logger.info("=" * 60)

    ensure_cache()

    quote_pool = ETF_POOL + ([DEFENSIVE_ETF] if USE_DEFENSIVE_ETF and DEFENSIVE_ETF not in ETF_POOL else [])
    quotes = get_quotes_cloud(quote_pool)
    if not quotes:
        logger.error("无法获取实时行情，退出")
        return

    regime, dynamic_max_score = get_market_regime()
    m_eff = get_effective_momentum_window() if USE_DYNAMIC_WINDOW else MOMENTUM_DAYS

    cand_codes = None
    r2_th = R2_FILTER_THRESHOLD
    if USE_WEAK_POOL_SWITCH and regime == 'bear':
        cand_codes = set(GLOBAL_POOL)
        r2_th = WEAK_R2_THRESHOLD

    eligible = filter_etfs(quotes, max_score=dynamic_max_score, m_days=m_eff,
                           candidate_codes=cand_codes, r2_threshold=r2_th)
    logger.info(f"可用标的: {len(eligible)}只 (窗口{m_eff}天, 状态{regime})")

    prev_positions = load_positions()
    new_positions = {}
    targets = []

    if eligible:
        if SINGLE_HOLDING:
            targets = [eligible[0]]
        else:
            cands = list(eligible)
            for c in cands:
                raw = quotes.get(c['code'], {}).get('name', c['code'])
                c['sector'] = get_etf_sector(ETF_NAME_OVERRIDE.get(c['code'], raw))
            if len(cands) >= 2 and cands[0]['sector'] != cands[1]['sector']:
                targets = cands[:2]
            elif cands:
                targets = [cands[0]]
        scores = [t['score'] for t in targets]
        ssum = sum(scores)
        weights = [s / ssum for s in scores] if ssum > 0 else [1.0 / len(targets)] * len(targets)
        for idx, t in enumerate(targets):
            price = t['price']
            shares = int(TOTAL_CAPITAL * weights[idx] / price / 100) * 100 if price > 0 else 0
            new_positions[t['code']] = {
                "name": t['name'], "buy_price": price, "shares": shares,
                "amount": shares * price, "score": t['score'],
                "buy_time": _bj_now().strftime('%Y-%m-%d %H:%M:%S')
            }
    else:
        if USE_DEFENSIVE_ETF:
            dq = quotes.get(DEFENSIVE_ETF)
            dp = dq.get('current_price', 0) if dq else 0
            if dp > 0:
                shares = max(int(TOTAL_CAPITAL / dp / 100) * 100, 100)
                new_positions[DEFENSIVE_ETF] = {
                    "name": ETF_NAME_OVERRIDE.get(DEFENSIVE_ETF, dq.get('name', '银华日利')),
                    "buy_price": dp, "shares": shares, "amount": shares * dp,
                    "score": 0, "buy_time": _bj_now().strftime('%Y-%m-%d %H:%M:%S')
                }
                logger.info("无合格标的，建议切换防御ETF(银华日利)")

    write_signal_report(regime, dynamic_max_score, m_eff, eligible, targets, prev_positions, new_positions, quotes)
    save_positions(new_positions)
    logger.info("云端信号已写入 SIGNAL.md / index.html，持仓已记录")

# ================== K线获取（Baostock主 → PanWatch补 → Sina兜底） ==================
def fetch_klines_all(symbols, limit=45):
    """
    三级降级获取K线：
    1. Baostock（主力，最稳定，串行29只约15秒）
    2. PanWatch（并发10线程补缺）
    3. Sina（逐个兜底）
    """
    results = {}

    # ---- 第1级：Baostock（主数据源） ----
    logger.info("Baostock 主力获取...")
    try:
        import baostock as bs
        lg = bs.login()
        if lg.error_code == '0':
            end_date = datetime.now().strftime('%Y-%m-%d')
            start_date = (datetime.now() - timedelta(days=90)).strftime('%Y-%m-%d')
            for i, code in enumerate(symbols, 1):
                num = code.split('.')[0]
                bs_code = f"sh.{num}" if code.endswith('XSHG') else f"sz.{num}"
                try:
                    rs = bs.query_history_k_data_plus(
                        bs_code, "date,close,volume,amount",
                        start_date=start_date, end_date=end_date,
                        frequency='d', adjustflag='2'
                    )
                    klines = []
                    while rs.next():
                        row = rs.get_row_data()
                        close = float(row[1])
                        if close <= 0:
                            continue
                        klines.append({
                            'close': close,
                            'volume': float(row[2]),
                            'amount': float(row[3]),
                        })
                    if len(klines) >= MOMENTUM_DAYS:
                        results[code] = klines[-limit:] if len(klines) > limit else klines
                except Exception as e:
                    logger.debug(f"Baostock {code} 异常: {e}")
                if i % 10 == 0 or i == len(symbols):
                    logger.info(f"  Baostock进度: {i}/{len(symbols)}，已获{len(results)}只")
            bs.logout()
        else:
            logger.warning(f"Baostock登录失败: {lg.error_msg}")
    except Exception as e:
        logger.warning(f"Baostock整体异常: {e}")

    bs_ok = len(results)
    logger.info(f"Baostock 完成: {bs_ok}/{len(symbols)} 只")

    # ---- 第2级：PanWatch 并发补缺 ----
    pan_missing = [c for c in symbols if c not in results or len(results.get(c, [])) < MOMENTUM_DAYS]
    if pan_missing:
        logger.info(f"PanWatch 并发补缺 {len(pan_missing)} 只...")
        pan = PanWatchClient()
        pan_results = pan.get_klines_batch(pan_missing, limit=limit)
        for code in pan_missing:
            if pan_results.get(code) and len(pan_results.get(code, [])) >= MOMENTUM_DAYS:
                results[code] = pan_results[code]
        pw_ok = sum(1 for c in pan_missing if c in results and len(results[c]) >= MOMENTUM_DAYS)
        logger.info(f"  PanWatch补了 {pw_ok}/{len(pan_missing)} 只")

    # ---- 第3级：Sina 逐个兜底 ----
    sina_missing = [c for c in symbols if c not in results or len(results.get(c, [])) < MOMENTUM_DAYS]
    if sina_missing:
        logger.info(f"Sina 逐个兜底 {len(sina_missing)} 只...")
        for code in sina_missing:
            klines = fetch_klines_akshare_sina(code, limit)
            if klines and len(klines) >= MOMENTUM_DAYS:
                results[code] = klines
                logger.info(f"  ✅ Sina补 {code}")
            else:
                logger.warning(f"  ❌ {code} 所有源均失败")
            time.sleep(0.3)

    final_ok = sum(1 for k in results.values() if len(k) >= MOMENTUM_DAYS)
    logger.info(f"最终结果: {final_ok}/{len(symbols)} 只数据充足")
    return results

# ================== 缓存更新 ==================
def update_etf_cache(force=False):
    today = datetime.now().strftime('%Y-%m-%d')
    cache = load_cache()
    if not force and cache.get('date') == today and cache.get('data'):
        logger.info(f"缓存已是最新 ({today})，跳过更新")
        return cache['data']

    if CLOUD_MODE:
        logger.info("云端模式：从新浪/东财更新ETF K线缓存...")
        klines_dict = fetch_klines_cloud(ETF_POOL + _CACHE_EXTRA, limit=45)
    else:
        logger.info("开始更新ETF K线缓存（Baostock → PanWatch → Sina 三级降级）...")
        klines_dict = fetch_klines_all(ETF_POOL + _CACHE_EXTRA, limit=45)

    filtered_data = {}
    for code, klines in klines_dict.items():
        if klines and len(klines) >= MOMENTUM_DAYS:
            filtered_data[code] = klines

    new_cache = {'date': today, 'data': filtered_data}
    save_cache(new_cache)

    # 打印详细状态
    logger.info(f"缓存更新完成: {len(filtered_data)}/{len(ETF_POOL)} 只ETF数据充足")
    logger.info("📊 各ETF K线详细状态:")
    for code in ETF_POOL:
        klines = klines_dict.get(code, [])
        length = len(klines)
        if length >= MOMENTUM_DAYS:
            status = f"✅ 可用（{length}天）"
        elif length > 0:
            status = f"⚠️ 不足（{length}/{MOMENTUM_DAYS}天）"
        else:
            status = f"❌ 无数据（0/{MOMENTUM_DAYS}天）"
        logger.info(f"  {code:15s} {status}")

    return filtered_data

def ensure_cache():
    cache = load_cache()
    today = datetime.now().strftime('%Y-%m-%d')
    if cache.get('date') != today or not cache.get('data'):
        logger.info("缓存缺失或过期，立即更新...")
        update_etf_cache(force=True)
    else:
        logger.debug("缓存已是最新")

# ================== 策略核心 ==================
def calculate_score_exponential(closes, current_price, m_days=25, alpha=0.08):
    if len(closes) < m_days:
        return 0, 0, 0
    prices = closes[-m_days:] + [current_price]
    y = np.log(prices)
    x = np.arange(len(y))
    weights = np.linspace(1, 2, len(y))
    try:
        slope, intercept = np.polyfit(x, y, 1, w=weights)
    except:
        return 0, 0, 0
    annualized_return = math.exp(slope * 250) - 1
    y_pred = slope * x + intercept
    ss_res = np.sum(weights * (y - y_pred) ** 2)
    ss_tot = np.sum(weights * (y - np.mean(y)) ** 2)
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0
    score = annualized_return * r2
    return annualized_return, r2, score

def check_recent_drop(closes, days=3, drop_threshold=0.95):
    if len(closes) < days + 1:
        return False
    for i in range(1, days + 1):
        if i >= len(closes):
            continue
        ratio = closes[-i] / closes[-i - 1] if closes[-i - 1] > 0 else 1
        if ratio < drop_threshold:
            return True
    return False

def get_market_regime():
    """读取缓存判断市场状态，返回 (regime, dynamic_max_score)"""
    PROXIES = ['510300.XSHG', '159915.XSHE', '510500.XSHG']
    cache = load_cache()
    cache_data = cache.get('data', {})
    above_ma20 = 0
    ma20_up = 0
    below_ma20 = 0
    for pc in PROXIES:
        records = cache_data.get(pc, [])
        closes = [r.get('close', 0) for r in records if r.get('close', 0) > 0]
        if len(closes) >= 30:
            ma20 = sum(closes[-20:]) / 20
            ma20_prev = sum(closes[-21:-1]) / 20
            if closes[-1] > ma20:
                above_ma20 += 1
                if ma20 > ma20_prev * 1.001:
                    ma20_up += 1
            else:
                below_ma20 += 1

    if above_ma20 >= 2 and ma20_up >= 2:
        return 'bull', 7
    elif below_ma20 >= 2:
        return 'bear', 3
    else:
        return 'sideways', 5


def get_effective_momentum_window():
    """⑥ 动态动量窗口（H72思想）：全池R²中位数>0.65→窗口缩短到23天(跟强市更快)，
    否则维持25天；连续 DYNAMIC_HYST 日才切换，迟滞防抖。状态持久化到 dyn_window_state.json。
    """
    if not USE_DYNAMIC_WINDOW:
        return MOMENTUM_DAYS
    state_file = os.path.join(SCRIPT_DIR, 'dyn_window_state.json')
    today = datetime.now().strftime('%Y-%m-%d')
    state = {'win': MOMENTUM_DAYS, 'hi': 0, 'lo': 0, 'date': ''}
    if os.path.exists(state_file):
        try:
            with open(state_file, 'r', encoding='utf-8') as f:
                state = json.load(f)
        except:
            pass
    # 当天已计算过 → 直接复用（自循环不会重复算）
    if state.get('date') == today:
        return state['win']

    # 全池R²中位数（仅用29只主池，不含防御ETF/避险池之外的额外标的）
    cache = load_cache()
    cache_data = cache.get('data', {})
    r2s = []
    for code in ETF_POOL:
        recs = cache_data.get(code, [])
        if len(recs) < 26:
            continue
        closes = [float(k.get('close', 0)) for k in recs if k.get('close', 0) > 0]
        if len(closes) < 26 or closes[-1] <= 0:
            continue
        _, r2, _ = calculate_score_exponential(closes, closes[-1], 25, ALPHA)
        if r2 > 0:
            r2s.append(r2)

    med = sorted(r2s)[len(r2s) // 2] if r2s else 0.0
    win = state['win']
    hi = state['hi']
    lo = state['lo']
    if med > DYNAMIC_R2_QUALITY:
        hi += 1
        lo = 0
        if hi >= DYNAMIC_HYST:
            win = DYNAMIC_WIN_SHORT
    else:
        lo += 1
        hi = 0
        if lo >= DYNAMIC_HYST:
            win = DYNAMIC_WIN_LONG

    try:
        with open(state_file, 'w', encoding='utf-8') as f:
            json.dump({'win': win, 'hi': hi, 'lo': lo, 'date': today}, f)
    except:
        pass
    logger.info(f"动态窗口: 全池R²中位数={med:.4f} → 窗口={win}天 (hi={hi}, lo={lo})")
    return win


def filter_etfs(quotes, max_score=MAX_SCORE, m_days=MOMENTUM_DAYS,
                candidate_codes=None, r2_threshold=0.0):
    if not quotes:
        logger.error("无行情数据")
        return []

    cache = load_cache()
    cache_data = cache.get('data', {})
    eligible = []
    excluded_log = []
    price_status = []

    for etf in ETF_POOL:
        # ② 熊市切避险池：仅候选集合内的ETF参与
        if candidate_codes is not None and etf not in candidate_codes:
            continue
        # 检查行情
        quote = quotes.get(etf)
        price = quote.get('current_price', 0) if quote else 0
        name = quote.get('name', etf) if quote else etf

        if quote and price > 0:
            price_status.append((etf, "✅", price))
        else:
            price_status.append((etf, "❌", 0))

        # 检查缓存
        if etf not in cache_data:
            excluded_log.append((etf, name, 0, "缓存中无数据"))
            continue
        klines = cache_data[etf]
        if len(klines) < m_days:
            excluded_log.append((etf, name, 0, f"K线数据不足（{len(klines)}/{m_days}天）"))
            continue

        if price <= 0:
            excluded_log.append((etf, name, 0, "无实时行情数据"))
            continue

        closes = [float(k.get('close', 0)) for k in klines if k.get('close', 0) > 0]
        if len(closes) < m_days:
            excluded_log.append((etf, name, 0, f"有效收盘价不足（{len(closes)}/{m_days}天）"))
            continue

        ann, r2, score = calculate_score_exponential(closes, price, m_days, ALPHA)
        if score == 0:
            excluded_log.append((etf, name, score, "得分计算为0"))
            continue

        # R² ≤ 0 排除
        if r2 <= 0:
            excluded_log.append((etf, name, score, f"R²为负（{r2:.4f}），趋势不成立"))
            continue

        # ③ 严格R²过滤（r2_threshold>0 时启用；回测验证对29只小池过严，默认0=不启用）
        if r2_threshold > 0 and r2 < r2_threshold:
            excluded_log.append((etf, name, score, f"R²低于阈值（{r2:.4f}<{r2_threshold}）"))
            continue

        if check_recent_drop(closes, 3, 0.95):
            excluded_log.append((etf, name, score, "近3日跌幅超5%"))
            continue

        if score <= MIN_SCORE:
            excluded_log.append((etf, name, score, f"低于0分（得分{score:.4f}）"))
            continue
        if score >= max_score:
            excluded_log.append((etf, name, score, f"超过6分（得分{score:.4f}）"))
            continue

        high = quote.get('high_limit', 0)
        low = quote.get('low_limit', 0)
        if price >= high > 0:
            excluded_log.append((etf, name, score, "触及涨停"))
            continue
        if price <= low > 0:
            excluded_log.append((etf, name, score, "触及跌停"))
            continue

        eligible.append({
            'code': etf, 'name': name, 'score': score,
            'ann': ann, 'r2': r2, 'price': price
        })

    # 打印价格获取状态
    logger.info("")
    logger.info("📊 实时价格获取状态:")
    success_count = sum(1 for _, status, _ in price_status if status == "✅")
    logger.info(f"  成功率: {success_count}/{len(ETF_POOL)}")
    failed = [(code, price) for code, status, price in price_status if status == "❌"]
    if failed:
        logger.info("  ❌ 获取失败的ETF:")
        for code, price in failed:
            logger.info(f"    {code} 价格:{price}")
    else:
        logger.info("  ✅ 全部成功获取")
    logger.info("")

    # 打印排除清单
    if excluded_log:
        logger.info("❌ 排除清单（未入选的原因）:")
        excluded_sorted = sorted(excluded_log, key=lambda x: x[2], reverse=True)
        logger.info("  {:<15} {:<12} {:>10}  {}".format("代码", "名称", "得分", "排除原因"))
        for code, name, score, reason in excluded_sorted[:20]:
            name_display = name[:10] if name else "N/A"
            score_display = f"{score:.4f}" if score != 0 else "N/A"
            logger.info(f"  {code:15s} {name_display:12s} {score_display:>10s}  {reason}")
        if len(excluded_log) > 20:
            logger.info(f"  ... 还有 {len(excluded_log)-20} 只被排除（详见日志文件）")
        logger.info("")

    eligible.sort(key=lambda x: x['score'], reverse=True)
    return eligible

# ================== 持仓管理 ==================
def load_positions():
    try:
        with open(os.path.join(SCRIPT_DIR, 'positions_v1.json'), 'r') as f:
            return json.load(f)
    except:
        return {}

def save_positions(positions):
    with open(os.path.join(SCRIPT_DIR, 'positions_v1.json'), 'w') as f:
        json.dump(positions, f, indent=4)

def record_trade(symbol, name, action, price, shares, amount, reason=""):
    cost = amount * 0.00006 + amount * 0.001 if amount > 0 else 0
    record = {
        "time": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        "symbol": symbol, "name": name, "action": action,
        "price": price, "shares": shares, "amount": amount,
        "cost": cost, "reason": reason
    }
    with open(os.path.join(SCRIPT_DIR, 'trades_v1.json'), 'a', encoding='utf-8') as f:
        f.write(json.dumps(record, ensure_ascii=False) + '\n')
    logger.info(f"{action} {name}({symbol}) {shares}股@{price:.3f} {amount:.0f}元")

def send_alert(symbol, name, action, price, shares, amount, reason=""):
    title = f"{'买入' if action=='买入' else '卖出' if action=='卖出' else '操作'} {PUSH_PREFIX}"
    content = f"{name}({symbol})\n价格:{price:.3f}\n股数:{shares}股\n金额:{amount:.0f}元\n{reason}" if shares > 0 else f"{name}({symbol})\n{reason}"
    try:
        requests.post("http://www.pushplus.plus/send",
                      json={"token": PUSHPLUS_TOKEN, "title": title, "content": content}, timeout=10)
    except:
        pass

# ================== 调仓执行 ==================
def adjust_positions():
    mode_desc = "单只满仓" if SINGLE_HOLDING else "双持仓分散"
    weak_desc = "熊市切避险池" if USE_WEAK_POOL_SWITCH else "全池"
    dyn_desc = f"动态窗口({DYNAMIC_WIN_SHORT}/{DYNAMIC_WIN_LONG})" if USE_DYNAMIC_WINDOW else f"固定{MOMENTUM_DAYS}天"
    logger.info("=" * 60)
    logger.info(f"{PUSH_PREFIX} 执行调仓 [{mode_desc} | {weak_desc} | {dyn_desc}]")
    logger.info(f"ETF池: {len(ETF_POOL)}只  动量: {MOMENTUM_DAYS}天")
    logger.info("=" * 60)

    # 行情源：主池 + 防御ETF（511880 用于无信号兜底）
    quote_pool = ETF_POOL + ([DEFENSIVE_ETF] if USE_DEFENSIVE_ETF and DEFENSIVE_ETF not in ETF_POOL else [])
    pan = PanWatchClient()
    quotes = pan.get_quotes_batch(quote_pool)
    if not quotes:
        logger.error("无法获取实时行情，退出调仓")
        return

    # ---- 判断市场状态，动态调整分数上限 ----
    regime, dynamic_max_score = get_market_regime()
    logger.info(f"市场状态: {regime}  → 分数上限调整为 {dynamic_max_score}")
    if regime == 'bull':
        logger.info("  🟢 牛市，放宽至7分")
    elif regime == 'bear':
        logger.info("  🔴 熊市，收紧至3分")
    else:
        logger.info("  🟡 震荡市，中等5分")

    # ⑥ 动态动量窗口：强趋势期缩短窗口跟涨更快
    m_eff = get_effective_momentum_window() if USE_DYNAMIC_WINDOW else MOMENTUM_DAYS

    # ② 熊市切换避险池 + 放松过滤
    cand_codes = None
    r2_th = R2_FILTER_THRESHOLD
    if USE_WEAK_POOL_SWITCH and regime == 'bear':
        cand_codes = set(GLOBAL_POOL)
        r2_th = WEAK_R2_THRESHOLD
        logger.info(f"  🛡️ 熊市切避险池: 仅 {len(GLOBAL_POOL)} 只低相关标的参与选股")

    eligible = filter_etfs(quotes, max_score=dynamic_max_score, m_days=m_eff,
                           candidate_codes=cand_codes, r2_threshold=r2_th)
    logger.info(f"可用标的: {len(eligible)}只 (窗口{m_eff}天)")

    if not eligible:
        # ---- ① 防御ETF兜底：无信号不空仓，改持货币ETF；否则清仓 ----
        current_positions = load_positions()
        old_amount = sum([pos.get("amount", 0) for pos in current_positions.values()])
        total_cash = old_amount if old_amount > 0 else TOTAL_CAPITAL
        if USE_DEFENSIVE_ETF:
            if len(current_positions) == 1 and DEFENSIVE_ETF in current_positions:
                logger.info("已持有防御ETF(银华日利)，保持不变")
                return
            def_quote = quotes.get(DEFENSIVE_ETF)
            def_price = def_quote.get('current_price', 0) if def_quote else 0
            if def_price > 0:
                new_positions = {}
                for code, pos in current_positions.items():
                    send_alert(code, pos.get("name", code), "卖出", 0, 0, 0, "无合适ETF，切换防御ETF")
                    record_trade(code, pos.get("name", code), "卖出", pos.get("buy_price", 0),
                                 pos.get("shares", 0), pos.get("amount", 0), "切换防御ETF")
                shares = int(total_cash / def_price / 100) * 100
                if shares < 100:
                    shares = 100
                actual_amount = shares * def_price
                def_name = ETF_NAME_OVERRIDE.get(DEFENSIVE_ETF, def_quote.get('name', '银华日利'))
                new_positions[DEFENSIVE_ETF] = {
                    "name": def_name, "buy_price": def_price, "shares": shares,
                    "amount": actual_amount, "score": 0,
                    "buy_time": datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                }
                send_alert(DEFENSIVE_ETF, def_name, "买入", def_price, shares, actual_amount, "防御ETF兜底")
                record_trade(DEFENSIVE_ETF, def_name, "买入", def_price, shares, actual_amount, "防御ETF兜底")
                save_positions(new_positions)
                logger.info(f"无合适ETF，全仓切换防御ETF {def_name} {shares}股@{def_price:.3f} = {actual_amount:.0f}元")
                return
        # 兜底：清仓
        logger.info("无合格标的，清仓")
        for code, pos in current_positions.items():
            send_alert(code, pos.get("name", code), "卖出", 0, 0, 0, "无合格标的")
            record_trade(code, pos.get("name", code), "卖出", pos.get("buy_price", 0), pos.get("shares", 0), pos.get("amount", 0), "清仓")
        save_positions({})
        return

    # 目标选择：单只满仓(只取得分最高1只) 或 行业分散双持仓
    targets = []
    if SINGLE_HOLDING:
        targets = [eligible[0]]
        logger.info(f"目标数量: 1只（单只满仓）")
    else:
        from copy import deepcopy
        candidates = deepcopy(eligible)
        for c in candidates:
            raw_name = quotes.get(c['code'], {}).get('name', c['code'])
            c['sector'] = get_etf_sector(ETF_NAME_OVERRIDE.get(c['code'], raw_name))
        if len(candidates) >= 2 and candidates[0]['sector'] != candidates[1]['sector']:
            targets = candidates[:2]
        elif len(candidates) >= 1:
            targets = [candidates[0]]
        logger.info(f"目标数量: {len(targets)}只")
        if len(targets) == 2:
            logger.info(f"  → 不同行业分散双持: {targets[0]['sector']} + {targets[1]['sector']}")

    # 打印候选列表
    logger.info("  {:<15} {:<10} {:>10} {:>12} {:>10}".format("代码", "名称", "得分", "年化收益", "R²"))
    for i, item in enumerate(eligible):
        mark = "⭐" if i < TOP_N else " "
        logger.info(f"{mark} {item['code']:15s} {item['name']:10s} {item['score']:10.4f} {item['ann']:12.2%} {item['r2']:10.4f}")

    # 计算总资金
    current_positions = load_positions()
    old_amount = sum([pos.get("amount", 0) for pos in current_positions.values()])
    total_cash = old_amount if old_amount > 0 else TOTAL_CAPITAL

    # 按得分权重分配
    scores = [t['score'] for t in targets]
    sum_scores = sum(scores)
    if sum_scores <= 0:
        weights = [1.0 / len(targets)] * len(targets)
    else:
        weights = [s / sum_scores for s in scores]

    new_positions = {}
    for idx, target in enumerate(targets):
        weight = weights[idx]
        buy_amount = int(total_cash * weight)
        target_price = target['price']
        shares = int(buy_amount / target_price / 100) * 100
        if shares < 100:
            continue
        actual_amount = shares * target_price
        new_positions[target['code']] = {
            "name": target['name'],
            "buy_price": target_price,
            "shares": shares,
            "amount": actual_amount,
            "score": target['score'],
            "buy_time": datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }
        logger.info(f"  分配 {target['name']} 权重 {weight:.1%}，买入 {shares}股，金额 {actual_amount:.0f}元")

    # ===== 卖出旧持仓 =====
    sell_msgs = []
    for old_code, old_pos in current_positions.items():
        if old_code not in new_positions:
            sell_price = quotes.get(old_code, {}).get('current_price', old_pos['buy_price'])
            sell_val = old_pos['shares'] * sell_price
            sell_msgs.append(f"  卖出 {old_pos['name']}({old_code}) {old_pos['shares']}股 × {sell_price:.3f} = {sell_val:.0f}元")
            send_alert(old_code, old_pos.get("name", old_code), "卖出",
                      sell_price, old_pos['shares'], old_pos['amount'], "调仓换出")
            record_trade(old_code, old_pos.get("name", old_code), "卖出",
                        sell_price, old_pos['shares'], old_pos['amount'], "调仓换出")

    # ===== 买入新持仓 =====
    buy_msgs = []
    for code, pos in new_positions.items():
        buy_msgs.append(f"  买入 {pos['name']}({code}) {pos['shares']}股 × {pos['buy_price']:.3f} = {pos['amount']:.0f}元")
        send_alert(code, pos['name'], "买入", pos['buy_price'], pos['shares'], pos['amount'],
                  f"得分:{pos['score']:.4f}")
        record_trade(code, pos['name'], "买入", pos['buy_price'], pos['shares'], pos['amount'],
                    f"得分:{pos['score']:.4f}")

    # ===== 打印操作汇总 =====
    if sell_msgs:
        logger.info("--- 卖出 ---")
        for m in sell_msgs:
            logger.info(m)
    if buy_msgs:
        logger.info("--- 买入 ---")
        for m in buy_msgs:
            logger.info(m)
    logger.info(f"--- 调仓完成，共持有 {len(new_positions)} 只ETF ---")

    # ===== 微信推送：清晰的操作指令 =====
    push_msg = ""
    if sell_msgs:
        push_msg += "【卖出】\n" + "\n".join(sell_msgs) + "\n\n"
    if buy_msgs:
        push_msg += "【买入】\n" + "\n".join(buy_msgs) + "\n"
    if push_msg:
        send_alert("系统", "调仓指令", "操作", 0, 0, 0, push_msg.strip())

    save_positions(new_positions)

# ================== 时间判断 ==================
def is_trading_day():
    return datetime.now().weekday() < 5

def should_adjust_now():
    now = datetime.now()
    if not is_trading_day():
        return False
    return now.hour == ADJUST_HOUR and now.minute == ADJUST_MINUTE

def is_data_refresh_time():
    now = datetime.now()
    if not is_trading_day():
        return False
    return now.hour == 9 and now.minute <= 10

# ================== 主程序（自循环） ==================
def main():
    logger.info("=" * 60)
    mode_desc = "单只满仓" if SINGLE_HOLDING else "行业分散双持仓"
    logger.info(f"{PUSH_PREFIX} 启动（自循环版 · {mode_desc}）")
    logger.info(f"ETF池: {len(ETF_POOL)}只")
    logger.info(f"动量天数: {MOMENTUM_DAYS}天（动态窗口: {'开' if USE_DYNAMIC_WINDOW else '关'} {DYNAMIC_WIN_SHORT}/{DYNAMIC_WIN_LONG}天）")
    logger.info(f"调仓时间: {ADJUST_HOUR:02d}:{ADJUST_MINUTE:02d}")
    logger.info(f"得分范围: {MIN_SCORE}~{MAX_SCORE}（牛7/震5/熊3动态）")
    logger.info(f"持仓模式: {mode_desc}" + ("；熊市切避险池" if USE_WEAK_POOL_SWITCH else "") +
                ("；无信号持防御ETF(511880)" if USE_DEFENSIVE_ETF else ""))
    logger.info(f"初始资金: {TOTAL_CAPITAL}元")
    logger.info("数据源: Baostock 主力 → PanWatch 补缺 → Sina 兜底")
    logger.info("=" * 60)

    if CLOUD_MODE:
        run_cloud_once()
        return
    ensure_cache()

    last_adjust_date = None
    last_refresh_date = None

    while True:
        try:
            now = datetime.now()
            today = now.strftime('%Y-%m-%d')

            # 9:00-9:10 数据刷新
            if is_data_refresh_time() and last_refresh_date != today:
                logger.info("进入数据刷新窗口，强制更新缓存...")
                update_etf_cache(force=True)
                last_refresh_date = today

            # 调仓时间
            if should_adjust_now() and last_adjust_date != today:
                logger.info(f"🕐 到达调仓时间 {ADJUST_HOUR:02d}:{ADJUST_MINUTE:02d}，执行调仓")
                adjust_positions()
                last_adjust_date = today

            time.sleep(60)

        except KeyboardInterrupt:
            logger.info("收到中断信号，退出...")
            break
        except Exception as e:
            logger.error(f"主循环异常: {e}")
            import traceback
            logger.error(traceback.format_exc())
            time.sleep(60)

if __name__ == "__main__":
    main()