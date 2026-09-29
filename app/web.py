"""Web 界面(标准库 http.server,无第三方依赖)。

六个视图:冷柜总览 / 货道库存 / 温度曲线 / 临期队列 / 换货单 / 回收对账。
配色:香草白底、海军蓝主色、草莓红告警。所有写操作经领域层,
门店由 ?store= 过滤并由领域层二次强制隔离。
"""
from __future__ import annotations

import html
import sqlite3
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

from . import domain, tasks
from .tasks import TIER_LABELS

# ---------------------------------------------------------------- 配色

VANILLA = "#FBF7EE"
NAVY = "#16285B"
NAVY2 = "#27407B"
STRAWBERRY = "#E23A4E"
AMBER = "#E8930C"
GREEN = "#2E9E66"
INK = "#23283A"
MUTED = "#6B7285"
LINE = "#E7E1D3"

KIND_META = {
    "temp_breach": ("温度超限", STRAWBERRY),
    "outage": ("冷柜断电", STRAWBERRY),
    "mixed_batch": ("批次混放", STRAWBERRY),
    "expiry": ("临期未换", AMBER),
    "package_damage": ("包装破损", STRAWBERRY),
    "recycle_mismatch": ("回收数量不符", STRAWBERRY),
}
TIER_COLOR = {
    "expired": STRAWBERRY, "urgent": STRAWBERRY, "warning": AMBER,
    "watch": NAVY2, "fresh": GREEN,
}
REASON_LABEL = {"expiry": "临期换货", "package_damage": "包装破损", "temp_breach": "温控换货"}
EX_STATUS = {
    "pending": "待店员确认", "staff_confirmed": "待配送员签收",
    "courier_confirmed": "双向签收完成", "rejected": "已拒收",
}
RC_STATUS = {"open": "待回收", "partial": "部分到货", "settled": "已结清", "mismatch": "数量不符"}


def esc(v) -> str:
    return html.escape("" if v is None else str(v), quote=True)


# ---------------------------------------------------------------- 查询

def freezers_overview(conn: sqlite3.Connection, store_id: str) -> list[dict]:
    out = []
    for f in conn.execute("SELECT * FROM freezers WHERE store_id=? ORDER BY code", (store_id,)):
        d = dict(f)
        last = conn.execute(
            "SELECT temp_c, ts FROM temp_readings WHERE device_id=? ORDER BY ts DESC LIMIT 1",
            (f["id"],),
        ).fetchone()
        d["last_temp"] = last["temp_c"] if last else None
        d["last_ts"] = last["ts"] if last else None
        d["in_range"] = (last is not None and f["temp_min"] <= last["temp_c"] <= f["temp_max"])
        agg = conn.execute(
            """SELECT COUNT(DISTINCT cb.channel_id) chans,
                      COALESCE(SUM(cb.qty_remaining),0) qty
                 FROM channel_batches cb WHERE cb.active=1 AND cb.qty_remaining>0
                  AND cb.channel_id IN (SELECT id FROM channels WHERE freezer_id=?)""",
            (f["id"],),
        ).fetchone()
        d["used_chans"] = agg["chans"]
        d["in_cabinet_qty"] = agg["qty"]
        d["open_tasks"] = conn.execute(
            "SELECT kind, COUNT(*) c FROM tasks WHERE store_id=? AND status='open' AND ref_id IN "
            "(SELECT CAST(id AS TEXT) FROM temp_incidents WHERE freezer_id=? "
            " UNION SELECT CAST(id AS TEXT) FROM channels WHERE freezer_id=? "
            " UNION SELECT batch_id FROM channel_batches cb JOIN channels c ON c.id=cb.channel_id WHERE c.freezer_id=?) "
            "GROUP BY kind",
            (store_id, f["id"], f["id"], f["id"]),
        ).fetchall()
        out.append(d)
    return out


def channel_matrix(conn: sqlite3.Connection, freezer_id: str) -> list[list[dict]]:
    f = conn.execute("SELECT * FROM freezers WHERE id=?", (freezer_id,)).fetchone()
    grid: list[list[dict]] = []
    for r in range(f["rows_n"]):
        row = []
        for c in range(f["cols_n"]):
            ch = conn.execute(
                "SELECT * FROM channels WHERE freezer_id=? AND row_idx=? AND col_idx=?",
                (freezer_id, r, c),
            ).fetchone()
            cell = {"pos": ch["pos"], "capacity": ch["capacity"], "bindings": []}
            for cb in conn.execute(
                """SELECT cb.*, b.batch_no, b.expiry_date, b.locked, b.locked_reason,
                          p.name product_name, p.spec, p.brand, p.id product_id
                     FROM channel_batches cb
                     JOIN batches b ON b.id=cb.batch_id
                     JOIN products p ON p.id=b.product_id
                    WHERE cb.channel_id=? AND cb.active=1 ORDER BY cb.loaded_ts""",
                (ch["id"],),
            ):
                bd = dict(cb)
                dl = tasks.days_left(cb["expiry_date"])
                bd["days_left"] = dl
                bd["tier"] = tasks.expiry_tier(dl)
                cell["bindings"].append(bd)
            cell["mixed"] = sum(1 for b in cell["bindings"] if b["qty_remaining"] > 0) >= 2
            cell["id"] = ch["id"]
            row.append(cell)
        grid.append(row)
    return grid


def store_kpis(conn: sqlite3.Connection, store_id: str) -> dict:
    today = date.today()
    incab = tasks.in_cabinet_batches(conn, store_id, today)
    return {
        "in_cabinet": sum(b["qty_remaining"] for b in incab),
        "near7": sum(b["qty_remaining"] for b in incab if b["days_left"] <= 7),
        "expiring_batches": sum(1 for b in incab if b["days_left"] <= 7),
        "wait_courier": conn.execute(
            "SELECT COUNT(*) c FROM exchange_orders WHERE store_id=? AND status='staff_confirmed'",
            (store_id,),
        ).fetchone()["c"],
        "recycle_mismatch": conn.execute(
            "SELECT COUNT(*) c FROM recycling_orders WHERE store_id=? AND status='mismatch'",
            (store_id,),
        ).fetchone()["c"],
        "open_tasks": conn.execute(
            "SELECT COUNT(*) c FROM tasks WHERE store_id=? AND status='open'", (store_id,)
        ).fetchone()["c"],
    }


# ---------------------------------------------------------------- SVG 温度曲线

def temp_chart_svg(conn: sqlite3.Connection, freezer_id: str, hours: float = 30.0) -> str:
    f = conn.execute("SELECT * FROM freezers WHERE id=?", (freezer_id,)).fetchone()
    # 最近 N 条(30h/15min ≈ 121 点,叠加事件注入点);迟到补传点同样在此。
    rows = conn.execute(
        "SELECT * FROM temp_readings WHERE device_id=? ORDER BY ts DESC LIMIT 160",
        (freezer_id,),
    ).fetchall()[::-1]
    if not rows:
        return '<div class="empty">暂无温度读数</div>'

    W, H = 920, 300
    L, Rr, T, B = 56, 18, 22, 42
    pw, ph = W - L - Rr, H - T - B

    def parse(ts: str) -> datetime:
        return datetime.fromisoformat(ts)

    t0, t1 = parse(rows[0]["ts"]), parse(rows[-1]["ts"])
    temps = [r["temp_c"] for r in rows] + [f["temp_min"], f["temp_max"]]
    ymin, ymax = min(temps) - 1.5, max(temps) + 1.5
    span_t = max((t1 - t0).total_seconds(), 1)

    def x(tt: datetime) -> float:
        return L + (tt - t0).total_seconds() / span_t * pw

    def y(v: float) -> float:
        return T + (ymax - v) / (ymax - ymin) * ph

    parts = [f'<svg viewBox="0 0 {W} {H}" class="chart" role="img" aria-label="温度曲线">']
    # 合格区间带
    parts.append(
        f'<rect x="{L}" y="{y(f["temp_max"]):.1f}" width="{pw}" '
        f'{y(f["temp_min"]) - y(f["temp_max"]):.1f}" '
        f'height="{y(f["temp_min"]) - y(f["temp_max"]):.1f}" class="ok-band" />'
    )
    parts.append(
        f'<line x1="{L}" y1="{y(f["temp_max"]):.1f}" x2="{L + pw}" y2="{y(f["temp_max"]):.1f}" '
        f'class="ok-line"/><line x1="{L}" y1="{y(f["temp_min"]):.1f}" x2="{L + pw}" '
        f'y2="{y(f["temp_min"]):.1f}" class="ok-line"/>'
    )
    parts.append(
        f'<text x="{L + 6}" y="{y(f["temp_max"]) - 5:.1f}" class="ok-label">合格上限 {f["temp_max"]:g}℃</text>'
    )
    parts.append(
        f'<text x="{L + 6}" y="{y(f["temp_min"]) + 15:.1f}" class="ok-label">合格下限 {f["temp_min"]:g}℃</text>'
    )
    # 网格 + Y 轴刻度
    steps = 5
    for i in range(steps + 1):
        v = ymin + (ymax - ymin) * i / steps
        yy = y(v)
        parts.append(f'<line x1="{L}" y1="{yy:.1f}" x2="{L + pw}" y2="{yy:.1f}" class="grid"/>')
        parts.append(f'<text x="{L - 8}" y="{yy + 4:.1f}" class="axis" text-anchor="end">{v:.0f}℃</text>')
    # X 轴时间刻度
    for i in range(6):
        tt = t0 + (t1 - t0) * i / 5
        xx = x(tt)
        parts.append(f'<text x="{xx:.1f}" y="{H - 16}" class="axis" text-anchor="middle">{tt:%m-%d %H:%M}</text>')
    # 折线
    pts = " ".join(f"{x(parse(r['ts'])):.1f},{y(r['temp_c']):.1f}" for r in rows)
    parts.append(f'<polyline points="{pts}" fill="none" class="temp-line"/>')
    # 点:超限草莓红实点,迟到读数空心环
    for r in rows:
        xx, yy = x(parse(r["ts"])), y(r["temp_c"])
        oor = r["temp_c"] < f["temp_min"] or r["temp_c"] > f["temp_max"]
        cls = "pt-bad" if oor else "pt-ok"
        tip = f'{r["ts"][5:16].replace("T", " ")} {r["temp_c"]:+.1f}℃' + ("(迟到补传)" if r["late"] else "")
        if r["late"]:
            parts.append(
                f'<circle cx="{xx:.1f}" cy="{yy:.1f}" r="4.5" class="pt-late"><title>{esc(tip)}</title></circle>')
        else:
            parts.append(
                f'<circle cx="{xx:.1f}" cy="{yy:.1f}" r="3" class="{cls}"><title>{esc(tip)}</title></circle>')
    parts.append("</svg>")
    legend = (
        '<div class="legend"><span><i class="dot pt-ok"></i>合格读数</span>'
        '<span><i class="dot pt-bad"></i>超限读数</span>'
        '<span><i class="dot pt-late"></i>迟到补传(仅入账)</span>'
        '<span><i class="band"></i>合格温度区间</span></div>'
    )
    return "".join(parts) + legend


# ---------------------------------------------------------------- 页面渲染

CSS = """
:root{--vanilla:#FBF7EE;--navy:#16285B;--navy2:#27407B;--berry:#E23A4E;--amber:#E8930C;
--green:#2E9E66;--ink:#23283A;--muted:#6B7285;--line:#E7E1D3;}
*{box-sizing:border-box}body{margin:0;background:var(--vanilla);color:var(--ink);
font:14px/1.55 -apple-system,"PingFang SC","Microsoft YaHei",Segoe UI,sans-serif}
a{color:var(--navy2);text-decoration:none}a:hover{text-decoration:underline}
header.top{background:var(--navy);color:#fff;padding:0 24px;display:flex;align-items:center;
gap:18px;flex-wrap:wrap}header.top .brand{font-weight:700;font-size:17px;padding:15px 0;
display:flex;align-items:center;gap:9px}.logo{width:24px;height:24px;border-radius:7px;
background:var(--berry);display:inline-block;position:relative}.logo:after{content:"";position:absolute;
width:9px;height:9px;border-radius:50%;background:#fff;left:7.5px;top:7.5px}
nav a{color:#cdd7f2;padding:16px 10px;display:inline-block;border-bottom:3px solid transparent;
font-weight:600}nav a.active,nav a:hover{color:#fff;border-bottom-color:var(--berry);text-decoration:none}
.store-switch{margin-left:auto;display:flex;align-items:center;gap:8px;font-size:13px}
.store-switch select{padding:6px 8px;border-radius:7px;border:1px solid #46568c;background:#0f1e49;
color:#fff;font-weight:600}
.wrap{max-width:1180px;margin:22px auto;padding:0 22px 60px}
h1{font-size:21px;color:var(--navy);margin:4px 0 2px}h2{font-size:16px;color:var(--navy);margin:26px 0 10px}
.sub{color:var(--muted);margin:0 0 18px}
.kpis{display:grid;grid-template-columns:repeat(6,1fr);gap:12px;margin-bottom:20px}
.kpi{background:#fff;border:1px solid var(--line);border-radius:12px;padding:14px}
.kpi .v{font-size:24px;font-weight:800;color:var(--navy)}.kpi .v.berry{color:var(--berry)}
.kpi .l{color:var(--muted);font-size:12px;margin-top:2px}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:14px}
.card{background:#fff;border:1px solid var(--line);border-radius:14px;padding:16px}
.card h3{margin:0;font-size:15px;color:var(--navy);display:flex;align-items:center;gap:8px}
.card .meta{color:var(--muted);font-size:12px;margin:2px 0 10px}
.temp-big{font-size:30px;font-weight:800}.temp-big.ok{color:var(--green)}
.temp-big.bad{color:var(--berry)}.temp-big.off{color:var(--muted)}
.chip{display:inline-block;padding:2px 9px;border-radius:999px;font-size:12px;font-weight:700;
color:#fff;margin:2px 4px 2px 0}.chip.gray{background:#8a93a8}
.rowline{display:flex;justify-content:space-between;border-top:1px dashed var(--line);
padding-top:8px;margin-top:8px;color:var(--muted);font-size:12.5px}
table{width:100%;border-collapse:collapse;background:#fff;border:1px solid var(--line);
border-radius:12px;overflow:hidden}th,td{padding:9px 11px;text-align:left;border-bottom:1px solid var(--line);
font-size:13px;vertical-align:middle}th{background:#f3eee2;color:var(--navy);font-weight:700;white-space:nowrap}
tr:last-child td{border-bottom:none}tr.berry-row td{background:#fdf0f2}
.badge{display:inline-block;padding:2px 8px;border-radius:6px;font-size:12px;font-weight:700;color:#fff}
.tier-expired,.tier-urgent,.st-berry{background:var(--berry)}.tier-warning{background:var(--amber)}
.tier-watch{background:var(--navy2)}.tier-fresh,.st-green{background:var(--green)}
.st-gray{background:#8a93a8}.st-navy{background:var(--navy)}
.matrix{display:grid;gap:8px;margin:10px 0 4px}.cell{background:#fff;border:2px solid var(--line);
border-radius:10px;padding:8px 9px;min-height:92px;position:relative}.cell.empty-cell{background:transparent;
border-style:dashed;color:#b3ac9c;display:flex;align-items:center;justify-content:center;font-size:12px}
.cell .pos{position:absolute;top:6px;right:8px;font-size:11px;color:var(--muted);font-weight:700}
.cell .pname{font-weight:700;font-size:12.5px;color:var(--navy);padding-right:26px}
.cell .bno{font-size:11px;color:var(--muted)}.cell .qty{font-size:15px;font-weight:800;margin-top:3px}
.cell .exp{font-size:11px;margin-top:1px}.cell.mixed{border-color:var(--berry);background:#fdf0f2}
.cell.locked{opacity:.72}.locktag{position:absolute;top:6px;left:8px;font-size:10px;color:var(--berry);
font-weight:800}.mixflag{color:var(--berry);font-weight:800;font-size:11px}
.chart{width:100%;height:auto;background:#fff;border:1px solid var(--line);border-radius:12px}
.ok-band{fill:rgba(46,158,102,.12)}.ok-line{stroke:var(--green);stroke-width:1;stroke-dasharray:5 4}
.ok-label{fill:var(--green);font-size:11px;font-weight:700}.grid{stroke:#eee8d9;stroke-width:1}
.axis{fill:var(--muted);font-size:10.5px}.temp-line{stroke:var(--navy);stroke-width:2}
.pt-ok{fill:var(--navy)}.pt-bad{fill:var(--berry)}.pt-lad,.pt-late{fill:#fff;stroke:var(--berry);
stroke-width:2}.legend{display:flex;gap:18px;flex-wrap:wrap;color:var(--muted);font-size:12px;
margin:8px 4px}.legend .dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:5px}
.legend .band{display:inline-block;width:14px;height:9px;background:rgba(46,158,102,.25);
border:1px solid var(--green);margin-right:5px;border-radius:2px}
.tier-section{margin:16px 0}.tier-head{font-weight:800;padding:8px 12px;border-radius:8px;color:#fff;
display:flex;gap:10px;align-items:center}.tier-head .cnt{background:rgba(255,255,255,.25);
border-radius:999px;padding:0 9px;font-size:12px}
.sign{display:inline-flex;flex-direction:column;gap:1px;font-size:11.5px}.sign .ok{color:var(--green);
font-weight:700}.sign .wait{color:var(--amber);font-weight:700}.sign .no{color:#b3ac9c}
form.inline{display:inline}input[type=text],input[type=number]{padding:5px 8px;border:1px solid #d8d1c0;
border-radius:7px;font-size:12.5px;width:96px}button,.btn{background:var(--navy);color:#fff;border:0;
border-radius:8px;padding:6px 12px;font-size:12.5px;font-weight:700;cursor:pointer}
button.berry{background:var(--berry)}button.ghost{background:#fff;color:var(--navy);border:1px solid #c9c1ad}
button:hover{opacity:.9}.toolbar{display:flex;gap:10px;align-items:center;margin:6px 0 14px;flex-wrap:wrap}
.flash{background:#e9f5ee;border:1px solid #bfe2cf;color:#1d6b45;padding:9px 13px;border-radius:9px;
margin-bottom:14px}.flash.err{background:#fdeef0;border-color:#f3c6cd;color:#a01f31}
.empty{color:#b3ac9c;padding:26px;text-align:center;background:#fff;border:1px dashed var(--line);
border-radius:12px}.taskline{display:flex;gap:10px;align-items:center;padding:9px 12px;border-bottom:1px solid var(--line)}
.taskline:last-child{border-bottom:none}.sev-high{color:var(--berry);font-weight:800;font-size:11px}
.sev-normal{color:var(--amber);font-weight:700;font-size:11px}.muted{color:var(--muted)}
.diff-pos{color:var(--berry);font-weight:800}.diff-zero{color:var(--green);font-weight:700}
.small{font-size:12px}.frozen-title{display:flex;align-items:center;gap:10px;margin:22px 0 2px}
"""


def layout(store_id: str, active: str, title: str, body: str, flash: str = "") -> str:
    stores = [("S01", "上海南京西路店"), ("S02", "北京国贸店"), ("S03", "广州天河店")]
    nav = [("overview", "冷柜总览"), ("inventory", "货道库存"), ("temp", "温度曲线"),
           ("expiry", "临期队列"), ("exchanges", "换货单"), ("recycling", "回收对账")]
    opts = "".join(
        f'<option value="{sid}"{" selected" if sid == store_id else ""}>{name}</option>'
        for sid, name in stores
    )
    nav_html = "".join(
        f'<a href="/{p}?store={store_id}" class="{"active" if p == active else ""}">{n}</a>'
        for p, n in nav
    )
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)} · 冰淇淋冷柜温控换货</title><style>{CSS}</style></head>
<body><header class="top"><span class="brand"><i class="logo"></i>冰淇淋冷柜温控换货台</span>
<nav>{nav_html}</nav>
<form class="store-switch" method="get" action="/{active}"><span>门店</span>
<select name="store" onchange="this.form.submit()">{opts}</select></form></header>
<main class="wrap">{flash}<h1>{esc(title)}</h1>{body}</main></body></html>"""


# ---------------------------------------------------------------- 视图

def view_overview(conn, store, flash):
    k = store_kpis(conn, store)
    kpis = f"""
    <div class="kpis">
      <div class="kpi"><div class="v">{k['in_cabinet']}</div><div class="l">在柜可售(件)</div></div>
      <div class="kpi"><div class="v {'berry' if k['near7'] else ''}">{k['near7']}</div><div class="l">7天内临期(件 / {k['expiring_batches']} 批)</div></div>
      <div class="kpi"><div class="v {'berry' if k['wait_courier'] else ''}">{k['wait_courier']}</div><div class="l">待配送员签收</div></div>
      <div class="kpi"><div class="v {'berry' if k['recycle_mismatch'] else ''}">{k['recycle_mismatch']}</div><div class="l">回收数量不符单</div></div>
      <div class="kpi"><div class="v {'berry' if k['open_tasks'] else ''}">{k['open_tasks']}</div><div class="l">待处理任务</div></div>
      <div class="kpi"><div class="v">{len(freezers_overview(conn, store) or [])}</div><div class="l">在线冷柜(总数)</div></div>
    </div>"""
    cards = []
    for f in freezers_overview(conn, store) or []:
        if not f["powered"]:
            temp_html = '<div class="temp-big off">离线</div><div class="muted small">冷柜断电,设备停止上报</div>'
        elif f["last_temp"] is None:
            temp_html = '<div class="temp-big off">--</div><div class="muted small">暂无读数</div>'
        else:
            cls = "ok" if f["in_range"] else "bad"
            temp_html = (f'<div class="temp-big {cls}">{f["last_temp"]:+.1f}℃</div>'
                         f'<div class="muted small">合格 {f["temp_min"]:g}~{f["temp_max"]:g}℃'
                         f' · {esc(f["last_ts"][5:16].replace("T"," "))}</div>')
        chips = "".join(
            f'<span class="chip" style="background:{KIND_META[kd][1]}">{KIND_META[kd][0]} {r["c"]}</span>'
            for r in f["open_tasks"] for kd in (r["kind"],)
        ) or '<span class="muted small">无挂起任务</span>'
        cards.append(f"""
        <div class="card">
          <h3>{esc(f['name'])} <span class="muted small">{esc(f['code'])}</span>
            <span class="chip {'gray' if f['powered'] else ''}"
             style="{'background:#8a93a8' if not f['powered'] else 'background:' + GREEN}">
             {'● 供电中' if f['powered'] else '● 断电'}</span></h3>
          <div class="meta">{f['rows_n']}×{f['cols_n']} 货道矩阵</div>
          {temp_html}
          <div class="rowline"><span>已用货道</span><span>{f['used_chans']} / {f['rows_n']*f['cols_n']}</span></div>
          <div class="rowline"><span>在柜件数</span><span>{f['in_cabinet_qty']}</span></div>
          <div style="margin-top:8px">{chips}</div>
          <div class="rowline"><span><a href="/temp?store={store}&fz={f['id']}">温度曲线 →</a></span>
          <span><a href="/inventory?store={store}&fz={f['id']}">货道库存 →</a></span></div>
        </div>""")
    tasks_rows = conn.execute(
        "SELECT * FROM tasks WHERE store_id=? AND status='open' ORDER BY "
        "CASE severity WHEN 'high' THEN 0 ELSE 1 END, created_ts DESC", (store,),
    ).fetchall()
    tlines = "".join(
        f'<div class="taskline"><span class="sev-{"high" if t["severity"]=="high" else "normal"}">'
        f'{"高" if t["severity"]=="high" else "普"}</span>'
        f'<span class="badge" style="background:{KIND_META[t["kind"]][1]}">{KIND_META[t["kind"]][0]}</span>'
        f'<span><b>{esc(t["title"])}</b><div class="muted small">{esc(t["detail"] or "")}</div></span></div>'
        for t in tasks_rows
    )
    task_panel = (f'<h2>挂起任务({len(tasks_rows)})</h2><div class="card" style="padding:4px 14px">'
                  f'{tlines or "<div class=empty>没有挂起任务</div>"}</div>')
    body = kpis + f'<div class="cards">{"".join(cards)}</div>' + task_panel
    return layout(store, "overview", "冷柜总览", body, flash)


def _matrix_html(conn, f, store):
    grid = channel_matrix(conn, f["id"])
    style = f'style="grid-template-columns:repeat({f["cols_n"]},1fr)"'
    rows_html = []
    for row in grid:
        for cell in row:
            if not cell["bindings"]:
                rows_html.append(f'<div class="cell empty-cell">{cell["pos"]}<br>空货道</div>')
                continue
            tops = []
            for b in cell["bindings"]:
                tops.append(
                    f'<div class="pname">{esc(b["product_name"])}</div>'
                    f'<div class="bno">{esc(b["batch_no"])} · {esc(b["spec"])}</div>'
                    f'<div class="qty">{b["qty_remaining"]}<span class="muted">/{cell["capacity"]} 件</span></div>'
                    f'<div class="exp"><span class="badge tier-{b["tier"]}">'
                    f'{TIER_LABELS[b["tier"]]}</span> 到期 {esc(b["expiry_date"])}'
                    f'({b["days_left"]:+d}天)</div>'
                )
            cls = "cell" + (" mixed" if cell["mixed"] else "") + (
                " locked" if any(b["locked"] for b in cell["bindings"]) else "")
            flag = '<div class="mixflag">⚠ 批次混放</div>' if cell["mixed"] else ""
            lock = '<div class="locktag">🔒已锁定</div>' if any(b["locked"] for b in cell["bindings"]) else ""
            rows_html.append(
                f'<div class="{cls}"><span class="pos">{cell["pos"]}</span>{lock}{flag}'
                f'{"".join(tops)}</div>')
    return f'<div class="matrix" {style}>{"".join(rows_html)}</div>'


def view_inventory(conn, store, fz_id, flash):
    fzs = conn.execute("SELECT * FROM freezers WHERE store_id=? ORDER BY code", (store,)).fetchall()
    if not fz_id:
        fz_id = fzs[0]["id"] if fzs else ""
    blocks = []
    for f in fzs:
        if fz_id and f["id"] != fz_id:
            continue
        blocks.append(
            f'<div class="frozen-title"><h2 style="margin:0">{esc(f["name"])} '
            f'<span class="muted small">{esc(f["code"])}</span></h2>'
            f'<span class="badge {"st-gray" if not f["powered"] else "st-green"}">'
            f'{"断电" if not f["powered"] else "供电中"}</span></div>'
            + _matrix_html(conn, f, store))
    opts = "".join(
        f'<option value="{f["id"]}"{" selected" if f["id"]==fz_id else ""}>'
        f'{esc(f["code"])} {esc(f["name"])}</option>' for f in fzs)
    toolbar = (f'<form class="toolbar" method="get"><input type="hidden" name="store" value="{store}">'
               f'<span>选择冷柜</span><select name="fz" class="store-switch select" '
               f'style="padding:6px 8px;border-radius:7px;border:1px solid #c9c1ad">'
               f'{opts}</select><button>查看</button></form>')
    if not fzs:
        body = '<div class="empty">该门店暂无冷柜</div>'
    else:
        body = toolbar + "".join(blocks)
    return layout(store, "inventory", "货道库存", body, flash)


def view_temp(conn, store, fz_id, flash):
    fzs = conn.execute("SELECT * FROM freezers WHERE store_id=? ORDER BY code", (store,)).fetchall()
    if not fz_id and fzs:
        fz_id = fzs[0]["id"]
    f = conn.execute("SELECT * FROM freezers WHERE id=?", (fz_id,)).fetchone() if fz_id else None
    opts = "".join(
        f'<option value="{x["id"]}"{" selected" if x["id"]==fz_id else ""}>'
        f'{esc(x["code"])} {esc(x["name"])}</option>' for x in fzs)
    toolbar = (f'<form class="toolbar" method="get"><input type="hidden" name="store" value="{store}">'
               f'<select name="fz" style="padding:6px 8px;border-radius:7px;border:1px solid #c9c1ad">'
               f'{opts}</select><button>切换冷柜</button></form>')
    if not f:
        return layout(store, "temp", "温度曲线", '<div class="empty">暂无冷柜</div>', flash)
    chart = temp_chart_svg(conn, f["id"])
    incs = conn.execute(
        "SELECT * FROM temp_incidents WHERE freezer_id=? ORDER BY ts DESC LIMIT 8", (f["id"],)
    ).fetchall()
    inc_html = []
    for i in incs:
        state = ('<span class="badge st-green">已处置:'
                 f'{esc(i["resolution"])}</span> <span class="muted small">{esc(i["handled_by"])} '
                 f'@{esc(i["handled_ts"][5:16].replace("T"," "))}</span>') if i["handled_ts"] else \
                '<span class="badge st-berry">未处置</span>'
        kind_label = {"excursion": "温度超限", "outage": "断电", "restored": "恢复供电"}[i["kind"]]
        inc_html.append(
            f'<div class="taskline"><span class="badge {"" if i["handled_ts"] else "st-berry"}" '
            f'style="background:{NAVY2 if i["handled_ts"] else STRAWBERRY}">{kind_label}</span>'
            f'<span><b>{esc(i["detail"] or "")}</b><div class="small muted">{esc(i["ts"][5:19].replace("T"," "))}</div></span>'
            f'<span style="margin-left:auto">{state}</span></div>')
    body = (toolbar +
            f'<p class="sub">{esc(f["name"])} · 合格区间 {f["temp_min"]:g}~{f["temp_max"]:g}℃ · '
            f'{"供电中" if f["powered"] else "断电离线(停止实时上报)"} · 绿色色带为合格区间</p>'
            + chart +
            f'<h2>温控事件({len(inc_html)})</h2><div class="card" style="padding:4px 14px">'
            f'{"".join(inc_html) or "<div class=empty>无事件</div>"}</div>')
    return layout(store, "temp", "温度曲线", body, flash)


def view_expiry(conn, store, flash):
    rows = [b for b in tasks.in_cabinet_batches(conn, store) if b["tier"] != "fresh"]
    groups = [("expired", STRAWBERRY), ("urgent", STRAWBERRY), ("warning", AMBER), ("watch", NAVY2)]
    sections = []
    for tier, color in groups:
        items = [b for b in rows if b["tier"] == tier]
        if not items:
            continue
        trs = "".join(
            f'<tr class="{"berry-row" if tier in ("expired","urgent") else ""}"><td>{esc(b["freezer_code"])}'
            f' / {esc(b["channel_pos"])}</td><td><b>{esc(b["product_name"])}</b>'
            f'<div class="muted small">{esc(b["brand"])} · {esc(b["spec"])}</div></td>'
            f'<td>{esc(b["batch_no"])}<div class="muted small">生产 {esc(b["prod_date"])}</div></td>'
            f'<td>{esc(b["expiry_date"])}</td>'
            f'<td><span class="badge tier-{tier}">{b["days_left"]:+d} 天</span></td>'
            f'<td style="text-align:right"><b>{b["qty_remaining"]}</b> 件</td></tr>'
            for b in items)
        sections.append(
            f'<div class="tier-section"><div class="tier-head" style="background:{color}">'
            f'{TIER_LABELS[tier]}<span class="cnt">{len(items)} 批 · '
            f'{sum(b["qty_remaining"] for b in items)} 件</span></div>'
            f'<table><thead><tr><th>冷柜/货道</th><th>商品</th><th>批次</th><th>到期日</th>'
            f'<th>剩余天数</th><th style="text-align:right">在柜数量</th></tr></thead>'
            f'<tbody>{trs}</tbody></table></div>')
    body = "".join(sections) or '<div class="empty">没有临期批次,库存新鲜 🍦</div>'
    body += ('<p class="sub" style="margin-top:16px">分层规则:已过期 / ≤3天(草莓红,立即换货)、'
             '≤7天(琥珀)、≤14天(海军蓝);进程内任务引擎每 60 秒重扫。</p>')
    return layout(store, "expiry", "临期队列", body, flash)


def view_exchanges(conn, store, flash):
    orders = conn.execute(
        """SELECT o.*, p.name product_name, p.spec, b.batch_no, s.name supplier_name,
                  c.pos channel_pos, f.code freezer_code
             FROM exchange_orders o
             JOIN batches b ON b.id=o.batch_id
             JOIN products p ON p.id=b.product_id
             JOIN channels c ON c.id=o.channel_id
             JOIN freezers f ON f.id=c.freezer_id
             LEFT JOIN product_suppliers ps ON ps.product_id=p.id
             LEFT JOIN suppliers s ON s.id=ps.supplier_id
            WHERE o.store_id=? ORDER BY o.created_ts DESC""", (store,),
    ).fetchall()
    trs = []
    for o in orders:
        if o["status"] == "courier_confirmed":
            st = '<span class="badge st-green">双向签收完成</span>'
        elif o["status"] == "rejected":
            st = '<span class="badge st-gray">已拒收(数量回柜)</span>'
        else:
            st = '<span class="badge st-berry">待配送员签收</span>'
        staff = (f'<span class="sign"><span class="ok">✓ 店员 {esc(o["staff_user"])}</span>'
                 f'<span class="muted">{esc((o["staff_ts"] or "")[5:16].replace("T"," "))}</span></span>')
        if o["courier_user"]:
            courier = (f'<span class="sign"><span class="ok">✓ 配送员 {esc(o["courier_user"])}</span>'
                       f'<span class="muted">{esc((o["courier_ts"] or "")[5:16].replace("T"," "))}</span></span>')
        else:
            courier = '<span class="sign"><span class="wait">待配送员签收</span></span>'
        actions = ""
        if o["status"] == "staff_confirmed":
            actions = (
                f'<form class="inline" method="post" action="/action/courier_confirm">'
                f'<input type="hidden" name="order_id" value="{esc(o["id"])}">'
                f'<input type="hidden" name="store" value="{store}">'
                f'<input type="text" name="user" placeholder="配送员" '
                f'value="{esc(conn.execute("SELECT courier FROM suppliers s JOIN product_suppliers ps ON ps.supplier_id=s.id JOIN batches b ON b.product_id=ps.product_id WHERE b.id=?", (o["batch_id"],)).fetchone()["courier"])}" '
                f'required> <button>配送员签收</button></form> '
                f'<form class="inline" method="post" action="/action/reject">'
                f'<input type="hidden" name="order_id" value="{esc(o["id"])}">'
                f'<input type="hidden" name="store" value="{store}">'
                f'<button class="ghost">拒收</button></form>')
        rc = (f'<a href="/recycling?store={store}">{esc(o["recycle_id"])}</a>'
              if o["recycle_id"] else '<span class="muted">—</span>')
        trs.append(
            f'<tr><td><b>{esc(o["id"])}</b><div class="muted small">'
            f'{esc(o["created_ts"][5:16].replace("T"," "))}</div></td>'
            f'<td>{esc(o["freezer_code"])} / {esc(o["channel_pos"])}</td>'
            f'<td><b>{esc(o["product_name"])}</b><div class="muted small">{esc(o["spec"])}'
            f' · 批次 {esc(o["batch_no"])}</div></td>'
            f'<td style="text-align:right"><b>{o["qty"]}</b> 件</td>'
            f'<td><span class="badge st-navy">{REASON_LABEL[o["reason"]]}</span></td>'
            f'<td>{staff}</td><td>{courier}</td><td>{st}<div class="small" style="margin-top:3px">{rc}</div></td>'
            f'<td>{actions}</td></tr>')
    table = ("<table><thead><tr><th>换货单</th><th>冷柜/货道</th><th>商品 / 批次</th><th>数量</th>"
             "<th>原因</th><th>店员确认</th><th>配送员确认</th><th>状态 / 回收单</th><th>操作</th>"
             "</tr></thead><tbody>" + "".join(trs) + "</tbody></table>") \
        if trs else '<div class="empty">暂无换货单</div>'
    body = ('<p class="sub">换货单创建即店员确认并把数量划出可售库存;必须由<b>不同的配送员</b>'
            '再次签收才算完成,完成后自动开出供应商回收单。拒收则数量回柜(数量守恒)。</p>' + table)
    return layout(store, "exchanges", "换货单", body, flash)


def view_recycling(conn, store, flash):
    rows = conn.execute(
        """SELECT r.*, s.name supplier_name, b.batch_no, p.name product_name
             FROM recycling_orders r
             JOIN suppliers s ON s.id=r.supplier_id
             JOIN batches b ON b.id=r.batch_id
             JOIN products p ON p.id=b.product_id
            WHERE r.store_id=? ORDER BY r.created_ts DESC""", (store,),
    ).fetchall()
    tot_e = sum(r["qty_expected"] for r in rows)
    tot_r = sum(r["qty_received"] for r in rows)
    mm = sum(1 for r in rows if r["status"] == "mismatch")
    kpis = f"""<div class="kpis" style="grid-template-columns:repeat(4,1fr)">
      <div class="kpi"><div class="v">{tot_e}</div><div class="l">应收合计(件)</div></div>
      <div class="kpi"><div class="v">{tot_r}</div><div class="l">实收合计(件)</div></div>
      <div class="kpi"><div class="v {'berry' if tot_e-tot_r else ''}">{tot_e-tot_r:+d}</div><div class="l">对账差异</div></div>
      <div class="kpi"><div class="v {'berry' if mm else ''}">{mm}</div><div class="l">数量不符单</div></div></div>"""
    trs = []
    for r in rows:
        diff = r["qty_received"] - r["qty_expected"]
        st_cls = {"settled": "st-green", "mismatch": "st-berry", "partial": "st-navy",
                  "open": "st-gray"}[r["status"]]
        action = ""
        if r["status"] in ("open", "partial"):
            action = (
                f'<form class="inline" method="post" action="/action/recycle">'
                f'<input type="hidden" name="rid" value="{esc(r["id"])}">'
                f'<input type="hidden" name="store" value="{store}">'
                f'<input type="number" name="qty" min="1" value="{r["qty_expected"]-r["qty_received"]}" '
                f'style="width:72px" required> 件 '
                f'<label class="small"><input type="checkbox" name="final" value="1" checked> 结清</label> '
                f'<button>登记实收</button></form>')
        else:
            action = f'<span class="muted small">{esc(r["note"] or "")}</span>'
        trs.append(
            f'<tr class="{"berry-row" if r["status"]=="mismatch" else ""}">'
            f'<td><b>{esc(r["id"])}</b><div class="muted small">{esc(r["created_ts"][5:16].replace("T"," "))}</div></td>'
            f'<td>{esc(r["supplier_name"])}</td>'
            f'<td><b>{esc(r["product_name"])}</b><div class="muted small">批次 {esc(r["batch_no"])}</div></td>'
            f'<td style="text-align:right">{r["qty_expected"]}</td>'
            f'<td style="text-align:right">{r["qty_received"]}</td>'
            f'<td style="text-align:right" class="{"diff-pos" if diff else "diff-zero"}">{diff:+d}</td>'
            f'<td><span class="badge {st_cls}">{RC_STATUS[r["status"]]}</span></td>'
            f'<td>{action}</td></tr>')
    table = ("<table><thead><tr><th>回收单</th><th>供应商</th><th>商品 / 批次</th>"
             "<th>应收</th><th>实收</th><th>差异</th><th>状态</th><th>登记实收</th>"
             "</tr></thead><tbody>" + "".join(trs) + "</tbody></table>") \
        if trs else '<div class="empty">暂无回收单</div>'
    body = kpis + '<p class="sub">实收数量不能超过换货出柜数量;少于应收即结清判为<b>数量不符</b>'
    '并生成回收对账任务;部分到货挂起等待补交。</p>' + table
    return layout(store, "recycling", "回收对账", body, flash)


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "FreezerERP/1.0"

    def _db(self) -> sqlite3.Connection:
        return self.server.conn  # type: ignore[attr-defined]

    def log_message(self, fmt, *args):
        pass

    def _redirect(self, path: str):
        self.send_response(303)
        self.send_header("Location", path)
        self.end_headers()

    def _flash_redirect(self, back: str, msg: str, ok: bool = True):
        sep = "&" if "?" in back else "?"
        self._redirect(f"{back}{sep}msg={quote(msg)}&ok={1 if ok else 0}")

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/":
            self._redirect("/overview?store=S01")
            return
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        store = q.get("store", "S01")
        flash = ""
        if "msg" in q:
            flash = f'<div class="flash{"" if q.get("ok")=="1" else " err"}">{esc(q["msg"])}</div>'
        conn = self._db()
        try:
            if u.path == "/overview":
                content = view_overview(conn, store, flash)
            elif u.path == "/inventory":
                content = view_inventory(conn, store, q.get("fz", ""), flash)
            elif u.path == "/temp":
                content = view_temp(conn, store, q.get("fz", ""), flash)
            elif u.path == "/expiry":
                content = view_expiry(conn, store, flash)
            elif u.path == "/exchanges":
                content = view_exchanges(conn, store, flash)
            elif u.path == "/recycling":
                content = view_recycling(conn, store, flash)
            else:
                self.send_error(404)
                return
            body = content.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except domain.DomainError as e:
            self._render_error(store, str(e))

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        form = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode()).items()}
        store = form.get("store", "S01")
        back_map = {
            "/action/courier_confirm": f"/exchanges?store={store}",
            "/action/reject": f"/exchanges?store={store}",
            "/action/recycle": f"/recycling?store={store}",
        }
        back = back_map.get(urlparse(self.path).path, f"/overview?store={store}")
        conn = self._db()
        try:
            if self.path.startswith("/action/courier_confirm"):
                domain.courier_confirm(conn, order_id=form["order_id"], store_id=store,
                                       user=form.get("user", "").strip())
                tasks.scan_expiry(conn)
                self._flash_redirect(back, f"换货单 {form['order_id']} 配送员已签收,回收单已开出")
            elif self.path.startswith("/action/reject"):
                domain.reject_exchange(conn, order_id=form["order_id"], store_id=store, user="店员")
                self._flash_redirect(back, f"换货单 {form['order_id']} 已拒收,数量回柜")
            elif self.path.startswith("/action/recycle"):
                status = domain.receive_recycling(
                    conn, recycling_id=form["rid"], qty=int(form.get("qty", 0)),
                    final=bool(form.get("final")))
                label = RC_STATUS.get(status, status)
                self._flash_redirect(
                    back, f"回收单 {form['rid']} 已登记:{label}",
                    ok=status != "mismatch")
            else:
                self.send_error(404)
        except (domain.DomainError, KeyError, ValueError) as e:
            self._flash_redirect(back, f"操作失败:{e}", ok=False)

    def _render_error(self, store, msg):
        content = layout(store, "overview", "操作失败",
                         f'<div class="flash err">{esc(msg)}</div>'
                         f'<p><a href="/overview?store={store}">返回总览</a></p>')
        body = content.encode("utf-8")
        self.send_response(400)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)


def serve(conn: sqlite3.Connection, host: str = "127.0.0.1", port: int = 8000):
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.conn = conn  # type: ignore[attr-defined]
    print(f"✦ 冷柜温控换货台: http://{host}:{port}/overview?store=S01")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
