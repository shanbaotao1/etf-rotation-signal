#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_compare_page.py
=====================================================================
读取轮动V1(stats.json) 与 五福(stats_wufu.json) 的统计数据，
统一抓取沪深300(159300) 基准，生成合并对比页 index.html：
  - 三条收益曲线：轮动V1(红) / 五福(紫) / 沪深300(蓝虚线)
  - 对照表：两策略 累计收益/年化/最大回撤/夏普/胜率/超额收益/交易次数
  - 各自每日买卖表
  - 区间选择（本周/本月/近30天/全部/自定义）
- 最后更新: 2026-07-24
- 本次改动: 单点策略也用圆点标记(可看到五福仅1天时的位置)；工作流定时改为13:10北京时间
"""
import json, os, re
from datetime import datetime, timezone, timedelta
import requests

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
V1_STATS = os.path.join(SCRIPT_DIR, 'stats.json')
WF_STATS = os.path.join(SCRIPT_DIR, 'stats_wufu.json')
S357_STATS = os.path.join(SCRIPT_DIR, 'stats_357.json')
OUT_HTML = os.path.join(SCRIPT_DIR, 'index.html')

BENCHMARK_INDEX = '159300.XSHE'   # 沪深300ETF
BENCHMARK_NAME = '沪深300'

COMMISSION_RATE = 0.00005
SLIPPAGE_RATE = 0.001


def _log(*a):
    print('[merge]', *a, flush=True)


def _bj_now():
    return datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=8)))


def _em_secid(code):
    num = code.split('.')[0]
    return f"{'1' if code.endswith('XSHG') else '0'}.{num}"


def fetch_index_klines(code, limit=260):
    num = code.split('.')[0]
    market = 'sh' if code.endswith('XSHG') else 'sz'
    symbol = f"{market}{num}"
    try:
        url = (f"https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
               f"CN_MarketData.getKLineData?symbol={symbol}&scale=240&datalen={limit}&ma=no")
        r = requests.get(url, timeout=12)
        arr = r.json()
        out = []
        for item in arr:
            close = float(item['close'])
            if close <= 0:
                continue
            out.append({'date': item['day'], 'close': close})
        if out:
            return out
    except Exception as e:
        _log(f"新浪指数K线 {code} 失败: {e}")
    try:
        url2 = ("https://push2his.eastmoney.com/api/qt/stock/kline/get"
                f"?secid={_em_secid(code)}&fields1=f1,f2,f3,f4,f5,f6"
                "&fields2=f51,f53&klt=101&fqt=1&end=20500101"
                f"&lmt={limit}")
        r = requests.get(url2, timeout=12)
        kl = r.json().get('data', {}).get('klines', [])
        out = []
        for row in kl:
            p = row.split(',')
            close = float(p[1])
            if close <= 0:
                continue
            out.append({'date': p[0], 'close': close})
        return out
    except Exception as e:
        _log(f"东财指数K线 {code} 失败: {e}")
    return []


def fetch_benchmark_closes(start_date, end_date):
    out = {}
    try:
        d0 = datetime.strptime(start_date, '%Y-%m-%d')
        d1 = datetime.strptime(end_date, '%Y-%m-%d')
        limit = max(60, int((d1 - d0).days / 1.4) + 30)
        for k in fetch_index_klines(BENCHMARK_INDEX, limit):
            out[k['date']] = k['close']
    except Exception as e:
        _log(f"获取基准({BENCHMARK_NAME})失败: {e}")
    return out


def _nearest(sorted_items, target_date):
    res = None
    for d, c in sorted_items:
        if d <= target_date:
            res = c
        else:
            break
    return res


def annotate_idx(daily, closes_map):
    """给 daily 每一日加 idx_cumret（沪深300 累计收益）。"""
    if not daily or not closes_map:
        for d in daily:
            d['idx_cumret'] = None
        return
    sorted_items = sorted(closes_map.items())
    base = _nearest(sorted_items, daily[0]['date'])
    if not base:
        for d in daily:
            d['idx_cumret'] = None
        return
    for d in daily:
        c = _nearest(sorted_items, d['date'])
        d['idx_cumret'] = (c / base - 1) if c else None


def load_stats(path):
    if not os.path.exists(path):
        return None
    try:
        return json.load(open(path, encoding='utf-8'))
    except Exception:
        return None


# ==================== HTML 模板 ====================
CSS = """
* { box-sizing: border-box; }
body { font-family: -apple-system, 'Segoe UI', 'Microsoft YaHei', sans-serif;
       background:#f5f6f8; color:#222; margin:0; padding:18px; }
.wrap { max-width: 920px; margin: 0 auto; }
h1 { font-size: 20px; margin: 0 0 4px; }
.sub { color:#888; font-size:13px; margin-bottom:14px; }
.card { background:#fff; border-radius:10px; padding:16px; margin-bottom:14px;
        box-shadow:0 1px 3px rgba(0,0,0,0.06); }
h3 { font-size:15px; margin: 0 0 10px; }
.stbig { font-size:15px; margin-bottom:10px; }
.stbig span { font-weight:700; }
.chips { display:flex; flex-wrap:wrap; gap:8px; margin-bottom:10px; }
.chip { background:#f3f4f6; border-radius:8px; padding:6px 10px; font-size:12px; color:#555; }
.chip b { color:#222; margin-left:4px; }
table { width:100%; border-collapse:collapse; font-size:13px; }
th,td { border-bottom:1px solid #eee; padding:7px 6px; text-align:left; }
th { background:#fafafa; color:#666; font-weight:600; }
.pbar { margin: 6px 0 4px; display:flex; flex-wrap:wrap; gap:6px; align-items:center; }
.pbtn { border:1px solid #ddd; background:#fff; border-radius:7px; padding:5px 12px;
        cursor:pointer; font-size:13px; color:#444; }
.pbtn.on { background:#e53935; color:#fff; border-color:#e53935; }
.cust { font-size:12px; color:#777; margin-left:6px; }
.cust input { font-size:12px; padding:3px; }
.ctable { width:100%; border-collapse:collapse; font-size:13px; margin-bottom:6px; }
.ctable th, .ctable td { border:1px solid #eee; padding:8px 6px; text-align:center; }
.ctable th { background:#fafafa; }
.pos { color:#e53935; font-weight:700; }
.neg { color:#2e7d32; font-weight:700; }
.note { font-size:12px; color:#999; margin-top:8px; line-height:1.6; }
"""
def build_html(v1_stats, wf_stats, s357_stats, bj_str):
    v1 = v1_stats or {'params': {'strategy': '2_轮动V1(单持仓)'}, 'daily': [], 'summary': {}}
    wf = wf_stats or {'params': {'strategy': '3_五福v1.1(多持仓)'}, 'daily': [], 'summary': {}}
    s357 = s357_stats or {'params': {'strategy': '1_357ETF(双持仓)'}, 'daily': [], 'summary': {}}
    data = {
        'v1': v1, 'wufu': wf, 's357': s357,
        'benchmark_index': BENCHMARK_INDEX, 'benchmark_name': BENCHMARK_NAME,
        'updated': bj_str,
    }
    data_json = json.dumps(data, ensure_ascii=False)

    top = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>三策略对比 · 1_357ETF vs 2_轮动V1 vs 3_五福</title>
<style>{CSS}</style></head>
<body><div class="wrap">
<h1>📈 三策略实盘对比 · 1_357ETF vs 2_轮动V1 vs 3_五福</h1>
<div class="sub">更新于 {bj_str} ｜ 2_轮动V1/3_五福自 2026-07-23 起、1_357ETF 自 2026-07-26 起、初始资金 ¥50,000、成本统一（佣金万0.5无最低 + 单边0.1%滑点）｜ 基准：{BENCHMARK_NAME}</div>

<div class="card">
  <h3>📊 收益对照表（含成本，基准 {BENCHMARK_NAME}）</h3>
  <table class="ctable">
    <thead><tr>
      <th>策略</th><th>累计收益</th><th>年化</th><th>最大回撤</th><th>夏普</th><th>胜率</th><th>超额收益*</th><th>交易次数</th>
    </tr></thead>
    <tbody>
      <tr><td>1_357ETF</td><td id="cS357Ret">—</td><td id="cS357Cagr">—</td><td id="cS357Mdd">—</td><td id="cS357Sharpe">—</td><td id="cS357Win">—</td><td id="cS357Alpha">—</td><td id="cS357Tr">—</td></tr>
      <tr><td>2_轮动V1</td><td id="cV1Ret">—</td><td id="cV1Cagr">—</td><td id="cV1Mdd">—</td><td id="cV1Sharpe">—</td><td id="cV1Win">—</td><td id="cV1Alpha">—</td><td id="cV1Tr">—</td></tr>
      <tr><td>3_五福</td><td id="cWfRet">—</td><td id="cWfCagr">—</td><td id="cWfMdd">—</td><td id="cWfSharpe">—</td><td id="cWfWin">—</td><td id="cWfAlpha">—</td><td id="cWfTr">—</td></tr>
    </tbody>
  </table>
  <div class="chips" style="margin-top:10px">
    <div class="chip"><span>区间</span><b><span id="rFrom">—</span> ~ <span id="rTo">—</span></b></div>
    <div class="chip"><span>{BENCHMARK_NAME}收益</span><b id="rIdx">—</b></div>
  </div>
  <div class="pbar">
    <button class="pbtn on" data-r="all">全部</button>
    <button class="pbtn" data-r="month">本月</button>
    <button class="pbtn" data-r="week">本周</button>
    <button class="pbtn" data-r="30">近30天</button>
    <span class="cust">自定义 <input id="dfrom" type="date"> ~ <input id="dto" type="date"> <button id="applyCustom" class="pbtn">应用</button></span>
  </div>
  <div id="chart" style="margin-top:8px"></div>
  <div class="note">* 超额收益 = 策略累计收益 − {BENCHMARK_NAME}累计收益（正=跑赢基准）。曲线：红=2_轮动V1，橙=1_357ETF，紫=3_五福，蓝虚线=沪深300。</div>
</div>

<div class="card">
  <h3>1_357ETF · 每日买卖记录</h3>
  <table><thead><tr><th>日期</th><th>方向</th><th>代码</th><th>名称</th><th>股数</th><th>价格</th></tr></thead><tbody id="tbS357"></tbody></table>
</div>

<div class="card">
  <h3>2_轮动V1 · 每日买卖记录</h3>
  <table><thead><tr><th>日期</th><th>方向</th><th>代码</th><th>名称</th><th>股数</th><th>价格</th></tr></thead><tbody id="tbV1"></tbody></table>
</div>

<div class="card">
  <h3>3_五福 · 每日买卖记录</h3>
  <table><thead><tr><th>日期</th><th>方向</th><th>代码</th><th>名称</th><th>股数</th><th>价格</th></tr></thead><tbody id="tbWf"></tbody></table>
</div>

</div>
"""

    script = (
    "const DATA = " + data_json + ";\n"
    "const V1 = DATA.v1.daily, WF = DATA.wufu.daily, S357 = DATA.s357.daily;\n"
    "let curRange = 'all';\n"
    "function parseDate(s){ return new Date(s+'T00:00:00'); }\n"
    "function fmtPct(x){ return (x*100).toFixed(2)+'%'; }\n"
    "function unionDates(){ const s=new Set(); V1.forEach(d=>s.add(d.date)); WF.forEach(d=>s.add(d.date)); S357.forEach(d=>s.add(d.date)); return Array.from(s).sort(); }\n"
    "function rangeDates(){\n"
    "  const all=unionDates(); if(!all.length) return [];\n"
    "  const today=all[all.length-1];\n"
    "  if(curRange==='all') return all;\n"
    "  if(curRange==='month'){ const ym=today.slice(0,7); return all.filter(d=>d.slice(0,7)===ym); }\n"
    "  if(curRange==='week'){ const t=parseDate(today),cut=new Date(t.getTime()-6*86400000); return all.filter(d=>parseDate(d)>=cut); }\n"
    "  if(curRange==='30'){ const t=parseDate(today),cut=new Date(t.getTime()-29*86400000); return all.filter(d=>parseDate(d)>=cut); }\n"
    "  if(curRange==='custom'){ const a=document.getElementById('dfrom').value,b=document.getElementById('dto').value; if(!a||!b) return all; return all.filter(d=>d>=a&&d<=b); }\n"
    "  return all;\n"
    "}\n"
    "function sliceOf(daily, rdates){ const set=new Set(rdates); return daily.filter(d=>set.has(d.date)); }\n"
    "function computeStats(slice){\n"
    "  if(!slice.length) return null;\n"
    "  const startEq=slice[0].equity_prev, endEq=slice[slice.length-1].equity;\n"
    "  const totalRet=endEq/startEq-1;\n"
    "  let peak=slice[0].equity, mdd=0;\n"
    "  for(const d of slice){ if(d.equity>peak)peak=d.equity; const dd=d.equity/peak-1; if(dd<mdd)mdd=dd; }\n"
    "  const rets=slice.map(d=>d.ret);\n"
    "  const mean=rets.reduce((a,b)=>a+b,0)/rets.length;\n"
    "  const varr=rets.reduce((a,b)=>a+(b-mean)*(b-mean),0)/rets.length;\n"
    "  const std=Math.sqrt(varr);\n"
    "  const sharpe=std>0?mean/std*Math.sqrt(252):0;\n"
    "  const win=rets.filter(r=>r>0).length;\n"
    "  const winRate=win/rets.length;\n"
    "  const d0=parseDate(slice[0].date), d1=parseDate(slice[slice.length-1].date);\n"
    "  const days=Math.max((d1-d0)/86400000,1);\n"
    "  const cagr=Math.pow(endEq/startEq,365/days)-1;\n"
    "  const trades=slice.reduce((a,d)=>a+d.buy.length+d.sell.length,0);\n"
    "  const hasIdx = slice.length && slice.every(d=>d.idx_cumret!==undefined && d.idx_cumret!==null);\n"
    "  let idxTotalRet=null, idxMdd=null, alpha=null;\n"
    "  if(hasIdx){\n"
    "    idxTotalRet = slice[slice.length-1].idx_cumret;\n"
    "    let pk=1+slice[0].idx_cumret, pmdd=0;\n"
    "    for(const d of slice){ const v=1+d.idx_cumret; if(v>pk)pk=v; const dd=v/pk-1; if(dd<pmdd)pmdd=dd; }\n"
    "    idxMdd=pmdd; alpha=totalRet - idxTotalRet;\n"
    "  }\n"
    "  return {totalRet,endEq,mdd,sharpe,winRate,cagr,trades, idxTotalRet, idxMdd, alpha};\n"
    "}\n"
    "function drawChart(rdates){\n"
    "  const el=document.getElementById('chart');\n"
    "  if(!rdates.length){ el.innerHTML='<p style=\"color:#999\">暂无数据</p>'; return; }\n"
    "  const vmap={}, wmap={}, smap={}, imap={};\n"
    "  sliceOf(V1,rdates).forEach(d=>vmap[d.date]=d.cumret);\n"
    "  sliceOf(WF,rdates).forEach(d=>wmap[d.date]=d.cumret);\n"
    "  sliceOf(S357,rdates).forEach(d=>smap[d.date]=d.cumret);\n"
    "  sliceOf(V1,rdates).forEach(d=>{ if(d.idx_cumret!==undefined&&d.idx_cumret!==null) imap[d.date]=d.idx_cumret; });\n"
    "  const n=rdates.length;\n"
    "  function arrOf(map){ const a=[]; for(const dt of rdates) a.push(map[dt]!==undefined?map[dt]*100:null); return a; }\n"
    "  const vVals=arrOf(vmap), wVals=arrOf(wmap), sVals=arrOf(smap), iVals=arrOf(imap);\n"
    "  let mn=0,mx=0;\n"
    "  for(const arr of [vVals,wVals,sVals,iVals]) for(const v of arr) if(v!==null){ if(v<mn)mn=v; if(v>mx)mx=v; }\n"
    "  if(mx-mn<0.5){ mx+=0.5; mn-=0.5; }\n"
    "  const W=700,H=260,padL=46,padR=14,padT=24,padB=30;\n"
    "  const x=i=> padL+(n<=1?0:i*(W-padL-padR)/(n-1));\n"
    "  const y=v=> padT+(H-padT-padB)*(1-(v-mn)/(mx-mn));\n"
    "  let grid=''; const ticks=4;\n"
    "  for(let k=0;k<=ticks;k++){ const v=mn+(mx-mn)*k/ticks, yy=y(v);\n"
    "    grid+='<line x1=\"'+padL+'\" y1=\"'+yy.toFixed(1)+'\" x2=\"'+(W-padR)+'\" y2=\"'+yy.toFixed(1)+'\" stroke=\"#eee\"/>'+\n"
    "          '<text x=\"'+(padL-6)+'\" y=\"'+(yy+4).toFixed(1)+'\" text-anchor=\"end\" font-size=\"10\" fill=\"#999\">'+v.toFixed(1)+'%</text>'; }\n"
    "  const zy=y(0);\n"
    "  grid+='<line x1=\"'+padL+'\" y1=\"'+zy.toFixed(1)+'\" x2=\"'+(W-padR)+'\" y2=\"'+zy.toFixed(1)+'\" stroke=\"#bbb\" stroke-dasharray=\"3,3\"/>';\n"
    "  function poly(arr,color,dash){ let pts=''; let marks=''; for(let i=0;i<n;i++){ if(arr[i]===null) continue; pts+=x(i).toFixed(1)+','+y(arr[i]).toFixed(1)+' '; marks+='<circle cx=\"'+x(i).toFixed(1)+'\" cy=\"'+y(arr[i]).toFixed(1)+'\" r=\"3\" fill=\"'+color+'\"/>'; }\n"
    "    if(!pts) return marks; return '<polyline points=\"'+pts+'\" fill=\"none\" stroke=\"'+color+'\" stroke-width=\"2\" '+(dash?'stroke-dasharray=\"5,3\"':'')+'/>'+marks; }\n"
    "  let xlab=''; const step=Math.max(1,Math.floor(n/6));\n"
    "  for(let i=0;i<n;i+=step){ xlab+='<text x=\"'+x(i).toFixed(1)+'\" y=\"'+(H-8)+'\" text-anchor=\"middle\" font-size=\"10\" fill=\"#999\">'+rdates[i].slice(5)+'</text>'; }\n"
    "  let legend='<rect x=\"'+padL+'\" y=\"5\" width=\"11\" height=\"11\" fill=\"#e53935\"/>'+\n"
    "             '<text x=\"'+(padL+15)+'\" y=\"14\" font-size=\"11\" fill=\"#444\">2_轮动V1</text>'+\n"
    "             '<rect x=\"'+(padL+84)+'\" y=\"5\" width=\"11\" height=\"11\" fill=\"#f57c00\"/>'+\n"
    "             '<text x=\"'+(padL+99)+'\" y=\"14\" font-size=\"11\" fill=\"#444\">1_357ETF</text>'+\n"
    "             '<rect x=\"'+(padL+168)+'\" y=\"5\" width=\"11\" height=\"11\" fill=\"#7b1fa2\"/>'+\n"
    "             '<text x=\"'+(padL+183)+'\" y=\"14\" font-size=\"11\" fill=\"#444\">3_五福</text>'+\n"
    "             '<rect x=\"'+(padL+244)+'\" y=\"5\" width=\"11\" height=\"11\" fill=\"#1565c0\"/>'+\n"
    "             '<text x=\"'+(padL+259)+'\" y=\"14\" font-size=\"11\" fill=\"#444\">__BN__</text>';\n"
    "  el.innerHTML='<svg viewBox=\"0 0 '+W+' '+H+'\" width=\"100%\" preserveAspectRatio=\"xMidYMid meet\">'+legend+grid+\n"
    "    poly(iVals,'#1565c0',true)+poly(vVals,'#e53935',false)+poly(sVals,'#f57c00',false)+poly(wVals,'#7b1fa2',false)+xlab+'</svg>';\n"
    "}\n"
    "function renderTable(tbId, daily, rdates){\n"
    "  const tb=document.getElementById(tbId); tb.innerHTML='';\n"
    "  const rows=sliceOf(daily,rdates).slice().reverse();\n"
    "  for(const d of rows){\n"
    "    const ops=[];\n"
    "    for(const b of d.buy) ops.push(['买',b]);\n"
    "    for(const s of d.sell) ops.push(['卖',s]);\n"
    "    if(!ops.length) ops.push(['持',null]);\n"
    "    for(const op of ops){\n"
    "      const act=op[0], item=op[1];\n"
    "      if(act==='持'){ const holds=(d.hold||[]).map(h=>h.name+'('+h.code+')').join('、')||'空仓';\n"
    "        tb.innerHTML+='<tr><td>'+d.date+'</td><td style=\"color:#888\">持有</td><td colspan=\"4\">'+holds+'</td></tr>'; }\n"
    "      else { const color=act==='买'?'#e53935':'#2e7d32';\n"
    "        tb.innerHTML+='<tr><td>'+d.date+'</td><td style=\"color:'+color+';font-weight:700\">'+act+'</td><td>'+item.code+'</td><td>'+item.name+'</td><td>'+item.shares+'</td><td>'+item.price+'</td></tr>'; }\n"
    "    }\n"
    "  }\n"
    "  if(!rows.length) tb.innerHTML='<tr><td colspan=\"6\" style=\"color:#999\">该区间无数据</td></tr>';\n"
    "}\n"
    "function setCell(id,val,good){ const e=document.getElementById(id); e.textContent=val; if(good!==undefined) e.className=good?'pos':'neg'; }\n"
    "function update(){\n"
    "  const rd=rangeDates();\n"
    "  const s1=computeStats(sliceOf(V1,rd)), s2=computeStats(sliceOf(WF,rd)), s3=computeStats(sliceOf(S357,rd));\n"
    "  if(s1){ setCell('cV1Ret',fmtPct(s1.totalRet),s1.totalRet>=0); setCell('cV1Cagr',fmtPct(s1.cagr),s1.cagr>=0);\n"
    "    setCell('cV1Mdd',fmtPct(s1.mdd),false); setCell('cV1Sharpe',s1.sharpe.toFixed(2),s1.sharpe>=0);\n"
    "    setCell('cV1Win',fmtPct(s1.winRate),s1.winRate>=0); setCell('cV1Tr',s1.trades,undefined);\n"
    "    if(s1.idxTotalRet!==null){ setCell('cV1Alpha',fmtPct(s1.alpha),s1.alpha>=0); } else setCell('cV1Alpha','—',undefined); }\n"
    "  if(s2){ setCell('cWfRet',fmtPct(s2.totalRet),s2.totalRet>=0); setCell('cWfCagr',fmtPct(s2.cagr),s2.cagr>=0);\n"
    "    setCell('cWfMdd',fmtPct(s2.mdd),false); setCell('cWfSharpe',s2.sharpe.toFixed(2),s2.sharpe>=0);\n"
    "    setCell('cWfWin',fmtPct(s2.winRate),s2.winRate>=0); setCell('cWfTr',s2.trades,undefined);\n"
    "    if(s2.idxTotalRet!==null){ setCell('cWfAlpha',fmtPct(s2.alpha),s2.alpha>=0); } else setCell('cWfAlpha','—',undefined); }\n"
    "  if(s3){ setCell('cS357Ret',fmtPct(s3.totalRet),s3.totalRet>=0); setCell('cS357Cagr',fmtPct(s3.cagr),s3.cagr>=0);\n"
    "    setCell('cS357Mdd',fmtPct(s3.mdd),false); setCell('cS357Sharpe',s3.sharpe.toFixed(2),s3.sharpe>=0);\n"
    "    setCell('cS357Win',fmtPct(s3.winRate),s3.winRate>=0); setCell('cS357Tr',s3.trades,undefined);\n"
    "    if(s3.idxTotalRet!==null){ setCell('cS357Alpha',fmtPct(s3.alpha),s3.alpha>=0); } else setCell('cS357Alpha','—',undefined); }\n"
    "  const idxTxt = (s1&&s1.idxTotalRet!==null)?fmtPct(s1.idxTotalRet):(s2&&s2.idxTotalRet!==null?fmtPct(s2.idxTotalRet):(s3&&s3.idxTotalRet!==null?fmtPct(s3.idxTotalRet):'—'));\n"
    "  const ie=document.getElementById('rIdx'); ie.textContent=idxTxt; const iePos=(s1&&s1.idxTotalRet!==null&&s1.idxTotalRet>=0)||(s2&&s2.idxTotalRet!==null&&s2.idxTotalRet>=0)||(s3&&s3.idxTotalRet!==null&&s3.idxTotalRet>=0); ie.className=iePos?'pos':'neg';\n"
    "  document.getElementById('rFrom').textContent = rd.length?rd[0]:'-';\n"
    "  document.getElementById('rTo').textContent = rd.length?rd[rd.length-1]:'-';\n"
    "  drawChart(rd); renderTable('tbV1',V1,rd); renderTable('tbS357',S357,rd); renderTable('tbWf',WF,rd);\n"
    "}\n"
    "document.addEventListener('DOMContentLoaded',function(){\n"
    "  document.querySelectorAll('.pbtn').forEach(function(b){ b.addEventListener('click',function(){ if(b.id==='applyCustom')return; document.querySelectorAll('.pbtn').forEach(x=>x.classList.remove('on')); b.classList.add('on'); curRange=b.dataset.r; update(); }); });\n"
    "  document.getElementById('applyCustom').addEventListener('click',function(){ document.querySelectorAll('.pbtn').forEach(x=>x.classList.remove('on')); curRange='custom'; update(); });\n"
    "  update();\n"
    "});\n"
    )
    script = script.replace('__BN__', BENCHMARK_NAME)
    html = top + '<script>\n' + script + '</script>\n</body></html>'
    return html


def main():
    bj = _bj_now().strftime('%Y-%m-%d %H:%M')
    v1 = load_stats(V1_STATS)
    wf = load_stats(WF_STATS)
    s357 = load_stats(S357_STATS)

    # 汇总两策略覆盖的日期范围，统一取一次基准
    all_dates = []
    for st in (v1, wf, s357):
        if st and st.get('daily'):
            all_dates += [d['date'] for d in st['daily']]
    if all_dates:
        start, end = min(all_dates), max(all_dates)
        closes = fetch_benchmark_closes(start, end)
        _log(f"基准 {BENCHMARK_NAME} 区间 {start}~{end}，{len(closes)} 个交易日")
        if v1 and v1.get('daily'):
            annotate_idx(v1['daily'], closes)
        if wf and wf.get('daily'):
            annotate_idx(wf['daily'], closes)
        if s357 and s357.get('daily'):
            annotate_idx(s357['daily'], closes)
    html = build_html(v1, wf, s357, bj)
    open(OUT_HTML, 'w', encoding='utf-8').write(html)
    _log(f"已写 index.html（{bj}），V1日={len(v1['daily']) if v1 else 0} 357日={len(s357['daily']) if s357 else 0} 五福日={len(wf['daily']) if wf else 0}")


if __name__ == '__main__':
    main()
