"""Product Advisors design system, applied to a Streamlit page.

Tokens are copied from CompanyOS
workspaces/product-advisors/00_brand/design-system/system/tokens.css and
the four operating principles in its README drive every choice here:
specificity over aesthetics, one accent used twice, bands not sections, and
numbers as the visual vocabulary. Yellow appears once on this page: the
market-status dot.

Everything that produces markup is a pure function of its inputs so it can
be tested without Streamlit. Streamlit only ever receives strings.
"""

from __future__ import annotations

from html import escape

INK = "#1d1d1f"
BLACK = "#0a0a0a"
WHITE = "#ffffff"
GRAY_50 = "#f5f5f7"
GRAY_100 = "#e5e5ea"
GRAY_300 = "#c7c7cc"
GRAY_500 = "#8a8a92"
GRAY_600 = "#6e6e73"
GRAY_DARK = "#2d2d30"
GRAY_MUTED = "#b8b8bd"
PURPLE = "#503AA8"
PURPLE_SOFT = "#8a6ff0"
YELLOW = "#FFEE58"
# Semantic status colours are not part of the brand palette; they are kept
# desaturated so they read as information, not decoration.
UP = "#1f8a4c"
DOWN = "#c2413b"

FONT = ("-apple-system, BlinkMacSystemFont, 'SF Pro Text', 'SF Pro Display', "
        "'Helvetica Neue', Helvetica, Arial, sans-serif")

CSS = f"""
<style>
:root {{
  --pa-ink:{INK}; --pa-black:{BLACK}; --pa-white:{WHITE}; --pa-gray-50:{GRAY_50};
  --pa-gray-100:{GRAY_100}; --pa-gray-300:{GRAY_300}; --pa-gray-500:{GRAY_500};
  --pa-gray-600:{GRAY_600}; --pa-gray-dark:{GRAY_DARK}; --pa-gray-muted:{GRAY_MUTED};
  --pa-purple:{PURPLE}; --pa-purple-soft:{PURPLE_SOFT}; --pa-yellow:{YELLOW};
  --pa-up:{UP}; --pa-down:{DOWN}; --pa-font:{FONT};
  --pa-radius-card:18px; --pa-radius-outcome:16px; --pa-radius-chip:999px;
  --pa-shadow-soft:0 8px 28px rgba(0,0,0,0.07); --pa-shadow-hover:0 20px 60px rgba(0,0,0,0.12);
  --pa-ease:cubic-bezier(.4,.0,.2,1);
}}
html, body, .stApp, [class*="css"], [data-testid="stAppViewContainer"] *:not([data-testid^="stIcon"]) {{
  font-family: var(--pa-font) !important;
}}
/* Streamlit's expander/status/toast chevrons are a ligature glyph font
   (Material Symbols): the string "keyboard_arrow_right" only renders as an
   arrow under that specific font. The universal override above forced our
   own font onto those spans too, so the browser had nothing to map the
   ligature onto and printed the icon's NAME as literal text, overlapping
   the label next to it. Carving stIcon* out of the override above and
   leaving it unset here lets Streamlit's own stylesheet supply that font
   again, which is all any of these icons ever needed. */
.stApp {{ background: var(--pa-white); color: var(--pa-ink); }}
/* Streamlit chrome: judges see a product, not a framework */
#MainMenu, footer, header[data-testid="stHeader"] {{ visibility: hidden; height: 0; }}
[data-testid="stToolbar"], [data-testid="stDecoration"], [data-testid="stStatusWidget"] {{ display: none; }}
.block-container {{ max-width: 1200px; padding: 0 40px 96px; }}
[data-testid="stAppViewContainer"] > .main {{ padding-top: 0; }}

/* Type: tight tracking is the PA signature */
h1, h2, h3 {{ color: var(--pa-ink); letter-spacing: -0.02em; font-weight: 700; }}
h2 {{ font-size: 28px !important; margin: 0 0 6px !important; }}
h3 {{ font-size: 22px !important; letter-spacing: -0.01em; }}
p, li, .stMarkdown {{ color: var(--pa-ink); font-size: 16px; line-height: 1.6; }}
[data-testid="stCaptionContainer"] p {{ color: var(--pa-gray-600); font-size: 13px; }}

/* Eyebrow: 13px, 0.19em, uppercase, purple */
.pa-eyebrow {{ font-size: 13px !important; font-weight: 700 !important; letter-spacing: 0.19em; text-transform: uppercase;
  color: var(--pa-purple); margin: 0 0 12px; }}
.pa-eyebrow--onink {{ color: var(--pa-gray-500) !important; }}
.pa-hero * {{ color: inherit; }}
.pa-hero .pa-stat__v, .pa-hero .pa-stat__l, .pa-hero .pa-spark__meta {{ color: inherit; }}

/* Hero band */
.pa-hero {{ background: var(--pa-black); color: var(--pa-white); margin: 0 -40px 0; padding: 56px 40px 44px;
  border-bottom: 1px solid var(--pa-gray-dark); }}
.pa-hero__wrap {{ max-width: 1120px; margin: 0 auto; display: grid; grid-template-columns: 1.15fr 1fr; gap: 48px; align-items: end; }}
.pa-hero__equity {{ font-size: 80px !important; font-weight: 700 !important; letter-spacing: -0.04em; line-height: 1 !important; margin: 0 !important; color: var(--pa-white) !important; font-variant-numeric: tabular-nums; }}
.pa-hero__sub {{ margin-top: 18px; display: flex; gap: 36px; flex-wrap: wrap; }}
.pa-stat__v {{ font-size: 26px !important; font-weight: 700 !important; letter-spacing: -0.02em; line-height: 1.05; font-variant-numeric: tabular-nums; }}
.pa-stat__l {{ font-size: 12px; font-weight: 600; letter-spacing: 0.12em; text-transform: uppercase; color: var(--pa-gray-500); margin-top: 6px; }}
.pa-hero .pa-up {{ color: #6fd39a !important; }} .pa-hero .pa-down {{ color: #f08b86 !important; }}
.pa-hero__status {{ display: inline-flex; align-items: center; gap: 8px; font-size: 12px; font-weight: 600; letter-spacing: 0.14em;
  text-transform: uppercase; color: var(--pa-gray-500); padding: 8px 14px; border-radius: var(--pa-radius-chip);
  background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.09); margin-bottom: 22px; }}
.pa-dot {{ width: 7px; height: 7px; border-radius: 50%; background: var(--pa-yellow); box-shadow: 0 0 10px var(--pa-yellow); }}
.pa-dot--off {{ background: var(--pa-gray-500); box-shadow: none; }}
.pa-spark {{ width: 100%; height: 150px; display: block; }}
.pa-spark__meta {{ display: flex; justify-content: space-between; font-size: 12px; color: var(--pa-gray-500);
  letter-spacing: 0.08em; text-transform: uppercase; margin-top: 8px; }}

/* Sleeve outcome cards on the light band */
.pa-band {{ background: var(--pa-gray-50); margin: 0 -40px; padding: 44px 40px; border-bottom: 1px solid var(--pa-gray-100); }}
.pa-band__wrap {{ max-width: 1120px; margin: 0 auto; }}
.pa-cards {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; }}
.pa-card {{ background: var(--pa-white); border: 1px solid var(--pa-gray-100); border-radius: var(--pa-radius-outcome);
  padding: 22px 22px 18px; transition: transform .2s var(--pa-ease), box-shadow .2s var(--pa-ease); }}
.pa-card:hover {{ transform: translateY(-2px); box-shadow: var(--pa-shadow-soft); }}
.pa-card__label {{ font-size: 12px; font-weight: 700; letter-spacing: 0.14em; text-transform: uppercase; color: var(--pa-purple); }}
.pa-card__v {{ font-size: 40px !important; font-weight: 700 !important; letter-spacing: -0.03em; line-height: 1; margin: 12px 0 6px; font-variant-numeric: tabular-nums; }}
.pa-card__s {{ font-size: 13px; color: var(--pa-gray-600); }}
.pa-bar {{ height: 4px; background: var(--pa-gray-100); border-radius: 2px; margin: 14px 0 10px; overflow: hidden; }}
.pa-bar > i {{ display: block; height: 100%; background: var(--pa-ink); border-radius: 2px; }}
.pa-bar > i.pa-bar--over {{ background: var(--pa-down); }}
.pa-chip {{ display: inline-block; font-size: 11px; font-weight: 600; letter-spacing: 0.03em; padding: 4px 10px; border-radius: var(--pa-radius-chip);
  background: var(--pa-gray-50); border: 1px solid var(--pa-gray-100); color: var(--pa-ink); }}
.pa-chip--armed {{ color: var(--pa-purple); border-color: #d9d0f2; background: #f3effc; }}
.pa-chip--over {{ color: var(--pa-down); border-color: #f1c9c6; background: #fdf1f0; }}
.pa-chip--off {{ color: var(--pa-gray-600); }}

/* Ledger */
.pa-ledger-wrap {{ overflow-x: auto; }}
.pa-ledger {{ width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; }}
.pa-ledger th {{ text-align: left; font-size: 11px; font-weight: 700; letter-spacing: 0.14em; text-transform: uppercase;
  color: var(--pa-gray-600); padding: 10px 12px; border-bottom: 1px solid var(--pa-gray-100); }}
.pa-ledger td {{ padding: 12px; border-bottom: 1px solid var(--pa-gray-100); font-size: 15px; }}
.pa-ledger td.num, .pa-ledger th.num {{ text-align: right; }}
.pa-ledger tr:hover td {{ background: var(--pa-gray-50); }}
.pa-ledger .sym {{ font-weight: 700; letter-spacing: -0.01em; }}
.pa-ledger .pos {{ color: var(--pa-up); }} .pa-ledger .neg {{ color: var(--pa-down); }}
.pa-ledger .grp td {{ padding-top: 22px; border-bottom: none; }}
.pa-ledger tfoot td {{ font-weight: 700; border-top: 1px solid var(--pa-ink); border-bottom: none; }}

/* Notification rows: a hand-authored <details>/<summary> list rather than
   st.expander, so severity color and a hover tooltip are ours to set and
   nothing here depends on Streamlit's own icon font. */
.pa-notif-list {{ border-top: 1px solid var(--pa-gray-100); }}
.pa-notif-row {{ border-bottom: 1px solid var(--pa-gray-100); }}
.pa-notif-row summary {{ display: flex; align-items: center; gap: 12px; padding: 11px 4px; cursor: pointer;
  list-style: none; font-size: 14px; }}
.pa-notif-row summary::-webkit-details-marker {{ display: none; }}
.pa-notif-row summary::before {{ content: "\203A"; display: inline-block; width: 12px; font-size: 16px;
  color: var(--pa-gray-500); transition: transform 0.15s var(--pa-ease); flex-shrink: 0; }}
.pa-notif-row[open] summary::before {{ transform: rotate(90deg); }}
.pa-notif-row:hover summary {{ background: var(--pa-gray-50); }}
.pa-notif-row .pa-notif-when {{ color: var(--pa-gray-500); font-variant-numeric: tabular-nums; flex-shrink: 0; width: 92px; }}
.pa-notif-row .pa-notif-title {{ flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-weight: 500; }}
.pa-notif-row .pa-notif-via {{ color: var(--pa-gray-500); font-size: 12px; flex-shrink: 0; }}
.pa-notif-row .pa-notif-body {{ padding: 2px 4px 16px 28px; color: var(--pa-gray-600); font-size: 14px; line-height: 1.5;
  white-space: pre-wrap; }}
.pa-notif-row .pa-notif-body .pa-notif-meta {{ margin-top: 8px; font-size: 12px; color: var(--pa-gray-500); }}
.pa-chip--sev-high {{ color: var(--pa-down); border-color: #f1c9c6; background: #fdf1f0; }}
.pa-chip--sev-default {{ color: var(--pa-purple); border-color: #d9d0f2; background: #f3effc; }}
.pa-chip--sev-low {{ color: var(--pa-gray-600); }}

/* Session-report content inside an expanded notification row: typeset
   stats and sleeve blocks, not a markdown source dump. */
.pa-notif-lead {{ font-style: italic; color: var(--pa-gray-600); margin: 0 0 12px; }}
.pa-notif-stats {{ display: flex; gap: 28px; margin-bottom: 16px; }}
.pa-notif-stat__l {{ font-size: 11px; font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase; color: var(--pa-gray-500); }}
.pa-notif-stat__v {{ font-size: 18px; font-weight: 700; letter-spacing: -0.01em; font-variant-numeric: tabular-nums; }}
.pa-notif-stat__v.pos {{ color: var(--pa-up); }} .pa-notif-stat__v.neg {{ color: var(--pa-down); }}
.pa-notif-sleeve {{ padding: 10px 0; border-top: 1px solid var(--pa-gray-100); }}
.pa-notif-sleeve__hd {{ display: flex; align-items: center; justify-content: space-between; gap: 12px; }}
.pa-notif-sleeve__name {{ font-weight: 600; }}
.pa-notif-sleeve__sub {{ font-size: 13px; color: var(--pa-gray-600); margin-top: 2px; }}
.pa-notif-sleeve__sub .pos {{ color: var(--pa-up); }} .pa-notif-sleeve__sub .neg {{ color: var(--pa-down); }}
.pa-notif-syms {{ margin-top: 8px; display: flex; flex-wrap: wrap; gap: 6px; }}
.pa-notif-sym {{ font-size: 11px; font-weight: 600; letter-spacing: 0.01em; padding: 3px 8px; border-radius: 6px;
  background: var(--pa-gray-50); color: var(--pa-gray-600); font-variant-numeric: tabular-nums; }}
.pa-notif-orders {{ margin: 6px 0 0; padding-left: 18px; font-size: 13px; color: var(--pa-gray-600); }}
.pa-notif-orders li {{ margin-bottom: 3px; }}
.pa-notif-trailer {{ margin-top: 12px; font-size: 14px; color: var(--pa-gray-600); }}

/* Streamlit widgets, restyled to the system */
/* Streamlit >= 1.36 renders its own tabs: div[role="tab"] inside div[role="tablist"],
   no data-baseweb attributes, and the label sits in a <p> with the theme's ink. */
.stTabs [role="tablist"] {{ gap: 28px; border-bottom: 1px solid var(--pa-gray-100); }}
.stTabs [role="tab"] {{ height: 52px; padding: 0 2px; border-bottom: 3px solid transparent; color: var(--pa-gray-600); }}
.stTabs [role="tab"] p {{ font-size: 16px !important; font-weight: 600 !important; letter-spacing: 0.01em; color: inherit !important; margin: 0 !important; }}
.stTabs [role="tab"]:hover {{ color: var(--pa-ink); }}
.stTabs [role="tab"][aria-selected="true"] {{ color: var(--pa-purple); border-bottom-color: var(--pa-purple); }}
.stTabs [data-baseweb="tab-highlight"], .stTabs [data-baseweb="tab-border"] {{ display: none; }}
.stButton > button {{ border-radius: var(--pa-radius-chip); background: var(--pa-ink); color: var(--pa-white); border: 1px solid var(--pa-ink);
  font-weight: 600; letter-spacing: 0.01em; padding: 10px 22px; transition: background .2s var(--pa-ease), transform .2s var(--pa-ease); }}
.stButton > button:hover {{ background: var(--pa-purple); border-color: var(--pa-purple); color: var(--pa-white); transform: translateY(-1px); }}
.stButton > button[kind="primary"] {{ background: var(--pa-purple); border-color: var(--pa-purple); }}
/* Streamlit renders the label in its own <p>, which keeps the theme's ink text
   color under a dark button: black on black. Inherit from the button. */
.stButton > button {{ color: var(--pa-white) !important; }}
.stButton > button p, .stButton > button span, .stButton > button div {{ color: inherit !important; }}
.stButton > button:disabled {{ background: var(--pa-gray-100); border-color: var(--pa-gray-100); color: var(--pa-gray-600) !important; }}
.stDownloadButton > button, .stFormSubmitButton > button {{ color: var(--pa-white) !important; }}
.stDownloadButton > button p, .stFormSubmitButton > button p {{ color: inherit !important; }}
[data-testid="stExpander"] {{ border: 1px solid var(--pa-gray-100); border-radius: var(--pa-radius-card); background: var(--pa-white); }}
[data-testid="stExpander"] summary {{ font-weight: 600; }}
[data-testid="stMetric"] {{ background: var(--pa-white); border: 1px solid var(--pa-gray-100); border-radius: var(--pa-radius-outcome); padding: 16px 18px; }}
[data-testid="stMetricLabel"] p {{ font-size: 12px !important; font-weight: 700; letter-spacing: 0.14em; text-transform: uppercase; color: var(--pa-purple); }}
[data-testid="stMetricValue"] {{ font-size: 30px; font-weight: 700; letter-spacing: -0.02em; font-variant-numeric: tabular-nums; }}
[data-testid="stDataFrame"] {{ border: 1px solid var(--pa-gray-100); border-radius: var(--pa-radius-outcome); overflow: hidden; }}
.stAlert {{ border-radius: var(--pa-radius-outcome); }}
hr {{ border-color: var(--pa-gray-100) !important; margin: 40px 0 !important; }}
@media (max-width: 860px) {{
  .pa-hero__wrap {{ grid-template-columns: 1fr; }} .pa-hero__equity {{ font-size: 56px; }}
  .pa-cards {{ grid-template-columns: 1fr 1fr; }} .block-container {{ padding: 0 20px 64px; }}
  .pa-hero, .pa-band {{ margin: 0 -20px; padding-left: 20px; padding-right: 20px; }}
}}
</style>
"""


def inject_theme() -> None:
    import streamlit as st
    st.markdown(CSS, unsafe_allow_html=True)


def money(v: float, signed: bool = False) -> str:
    s = f"{abs(v):,.2f}"
    if signed:
        return ("+" if v >= 0 else "−") + "$" + s
    return "$" + s


def pct(v: float) -> str:
    return ("+" if v >= 0 else "−") + f"{abs(v):.2f}%"


def sparkline_svg(values: list[float], width: int = 520, height: int = 150,
                  baseline: float | None = None) -> str:
    """Session equity as one line on the ink band, with the prior close as a
    hairline so the eye reads 'above or below yesterday' without a legend."""
    vals = [float(v) for v in values if v is not None]
    if len(vals) < 2:
        return (f'<svg class="pa-spark" viewBox="0 0 {width} {height}" role="img" aria-label="no session data">'
                f'<text x="0" y="{height // 2}" fill="{GRAY_500}" font-size="12">Session data arrives after the open.</text></svg>')
    lo, hi = min(vals + ([baseline] if baseline else [])), max(vals + ([baseline] if baseline else []))
    span = (hi - lo) or 1.0
    pad_t, pad_b = 10, 14
    def y(v): return pad_t + (hi - v) / span * (height - pad_t - pad_b)
    step = width / (len(vals) - 1)
    pts = [(i * step, y(v)) for i, v in enumerate(vals)]
    d = "M" + " L".join(f"{x:.1f},{yy:.1f}" for x, yy in pts)
    area = d + f" L{width:.1f},{height - pad_b} L0,{height - pad_b} Z"
    up = vals[-1] >= (baseline if baseline is not None else vals[0])
    stroke = "#6fd39a" if up else "#f08b86"
    base_line = ""
    if baseline is not None:
        by = y(baseline)
        base_line = (f'<line x1="0" y1="{by:.1f}" x2="{width}" y2="{by:.1f}" stroke="{GRAY_DARK}" '
                     f'stroke-dasharray="3 5"/><text x="{width}" y="{by - 5:.1f}" text-anchor="end" '
                     f'fill="{GRAY_500}" font-size="11" letter-spacing="1">PRIOR CLOSE</text>')
    end_x, end_y = pts[-1]
    return (f'<svg class="pa-spark" viewBox="0 0 {width} {height}" preserveAspectRatio="none" role="img" '
            f'aria-label="session equity">'
            f'<defs><linearGradient id="pa-fill" x1="0" x2="0" y1="0" y2="1">'
            f'<stop offset="0" stop-color="{stroke}" stop-opacity="0.22"/><stop offset="1" stop-color="{stroke}" stop-opacity="0"/>'
            f'</linearGradient></defs>{base_line}'
            f'<path d="{area}" fill="url(#pa-fill)"/>'
            f'<path d="{d}" fill="none" stroke="{stroke}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>'
            f'<circle cx="{end_x:.1f}" cy="{end_y:.1f}" r="3.5" fill="{stroke}"/></svg>')


def hero_html(*, equity: float, today: float, today_pct: float, since: float, since_pct: float,
              market_open: bool, status_text: str, clock_text: str, spark_svg: str,
              session_low: float | None, session_high: float | None) -> str:
    cls_t = "pa-up" if today >= 0 else "pa-down"
    cls_s = "pa-up" if since >= 0 else "pa-down"
    dot = "pa-dot" if market_open else "pa-dot pa-dot--off"
    rng = (f"<span>Low {money(session_low)}</span><span>High {money(session_high)}</span>"
           if session_low is not None and session_high is not None else "<span></span><span></span>")
    return f"""
<section class="pa-hero"><div class="pa-hero__wrap">
  <div>
    <div class="pa-hero__status"><i class="{dot}"></i>{escape(status_text)}&nbsp;&nbsp;·&nbsp;&nbsp;{escape(clock_text)}</div>
    <div class="pa-eyebrow pa-eyebrow--onink">Product Advisors &nbsp;·&nbsp; Alpaca paper account</div>
    <div class="pa-hero__equity">{money(equity)}</div>
    <div class="pa-hero__sub">
      <div><div class="pa-stat__v {cls_t}">{money(today, signed=True)}</div><div class="pa-stat__l">Today &nbsp;{pct(today_pct)}</div></div>
      <div><div class="pa-stat__v {cls_s}">{money(since, signed=True)}</div><div class="pa-stat__l">Since $100,000 start &nbsp;{pct(since_pct)}</div></div>
    </div>
  </div>
  <div>{spark_svg}<div class="pa-spark__meta">{rng}</div></div>
</div></section>"""


def sleeve_card_html(name: str, target_pct: float, budget: float, used: float, status: str) -> str:
    """status: active | armed | retired | over"""
    frac = (used / budget) if budget > 0 else 0.0
    width = min(frac, 1.0) * 100
    over = budget > 0 and used > budget + 1
    chip_cls = {"armed": "pa-chip pa-chip--armed", "retired": "pa-chip pa-chip--off",
                "over": "pa-chip pa-chip--over"}.get("over" if over else status, "pa-chip")
    chip_txt = {"active": "Active", "armed": "Armed, no signal yet", "retired": "Retired"}.get(status, status.title())
    if over:
        chip_txt = f"Over by {money(used - budget)}"
    big = f"{frac * 100:.0f}%" if budget > 0 else "0%"
    sub = (f"{money(used)} of {money(budget)}" if budget > 0 else f"{target_pct * 100:.0f}% target, no budget")
    return (f'<div class="pa-card"><div class="pa-card__label">{escape(name)} · {target_pct * 100:.0f}%</div>'
            f'<div class="pa-card__v">{big}</div><div class="pa-card__s">{sub} deployed</div>'
            f'<div class="pa-bar"><i class="{"pa-bar--over" if over else ""}" style="width:{width:.0f}%"></i></div>'
            f'<span class="{chip_cls}">{escape(chip_txt)}</span></div>')


def sleeve_cards_html(cards: list[dict]) -> str:
    inner = "".join(sleeve_card_html(**c) for c in cards)
    return f'<section class="pa-band"><div class="pa-band__wrap"><div class="pa-eyebrow">Where the money is</div><div class="pa-cards">{inner}</div></div></section>'


def _one_line(text: str, limit: int) -> str:
    """Collapse all whitespace (including blank lines) to single spaces.

    For a title="" tooltip specifically: a real notify() message is multi-
    paragraph, blank-line-separated markdown. Embedding that verbatim into an
    HTML attribute is fine as far as the browser's HTML parser is concerned,
    but Streamlit's markdown pre-processing runs first and treats a raw HTML
    block as ENDED by the first blank line it finds -- even one sitting
    inside an attribute we are still in the middle of writing. Every row hit
    this: the tooltip broke exactly at "\n\n", and everything after it,
    including our own <span> markup for that row, rendered as literal text.
    A one-line tooltip has no blank line to trigger on, so this is not a
    truncation nicety, it is the actual fix.
    """
    collapsed = " ".join(text.split())
    return collapsed[:limit]


def _multiline_html(text: str) -> str:
    """Escape, then turn real newlines into <br> rather than leaving them.

    Same root cause as _one_line, applied to the expanded body instead of
    the tooltip: a message with a blank line ends the raw-HTML block Streamlit
    was rendering us as. Converting every newline to a literal <br> removes
    the blank lines from the SOURCE text entirely -- the parser never sees
    one -- while <br> still renders as the line break CSS's pre-wrap would
    otherwise have given us from a bare "\n".
    """
    return escape(text).replace("\n", "<br>")


import re

_SESSION_STAT_RE = re.compile(r"\*\*(Equity|Cash|Day P&L)\*\*\s*([+-]?\$[\d,]+\.?\d*)")
# "Unattributed" has no sleeve budget: "**Unattributed** $7,025" rather than
# "**Name** $X / $Y (Z%)". The budget/pct group is optional for that reason --
# without it, the first version silently folded Unattributed's own positions
# into whichever sleeve happened to precede it, since nothing marked where
# that sleeve's block was supposed to end.
_SLEEVE_HEADER_RE = re.compile(
    r"\*\*([^*]+?)\*\*\s*\$([\d,]+)(?:\s*/\s*\$([\d,]+)\s*\((\d+)%\))?\n"
    r"\s*unrealized\s+([+-]\$[\d,]+\.?\d*)\s*\xb7\s*(\d+)\s*position\(s\)"
)
_WORKING_ORDERS_RE = re.compile(r"\*\*Working orders\*\*\s*\((\d+)\)")


def parse_session_report(message: str) -> dict | None:
    """Interpret notify()'s own session-report shape: an italic lead line,
    Equity/Cash/Day P&L, one block per sleeve (used/budget/pct, unrealized,
    position symbols), an optional Working orders count, and a trailing
    free-text sentence. Returns None for anything that doesn't match --
    a watchdog alert, the short session-starting ping -- so the caller can
    fall back to plain rendering rather than force this shape onto it.

    Built and tested against the exact messages notify() has actually sent
    (scripts/run_live.py, src/core/schedule.py), not a guessed format.
    """
    stats = dict(_SESSION_STAT_RE.findall(message))
    if "Equity" not in stats:
        return None

    lead = None
    first_line = message.split("\n", 1)[0].strip()
    if first_line.startswith("_") and first_line.endswith("_") and len(first_line) > 2:
        lead = first_line[1:-1]

    sleeves = []
    headers = list(_SLEEVE_HEADER_RE.finditer(message))
    for i, m in enumerate(headers):
        name, used, budget, pct, unrl, count = m.groups()
        block_end = headers[i + 1].start() if i + 1 < len(headers) else len(message)
        block = message[m.end():block_end]
        symbols = re.findall(r"^\s{4}(\S+)\s*$", block, re.MULTILINE)
        sleeves.append({
            "name": name.strip(), "used": used, "budget": budget,
            "pct": int(pct) if pct is not None else None,
            "unrealized": unrl, "position_count": int(count), "symbols": symbols,
        })

    working_orders = None
    wo = _WORKING_ORDERS_RE.search(message)
    if wo:
        after = message[wo.end():]
        lines = [ln.strip() for ln in after.split("\n")
                 if re.match(r"^[A-Z]{1,6}\s+(buy|sell)\b", ln.strip())]
        working_orders = lines[:int(wo.group(1))] if lines else None

    trailer = None
    tail_match = re.search(r"\n---\n(.+)$", message, re.DOTALL)
    if tail_match:
        trailer = tail_match.group(1).strip()

    return {"lead": lead, "stats": stats, "sleeves": sleeves,
            "working_orders": working_orders, "trailer": trailer}


def session_report_html(parsed: dict) -> str:
    """Render a parsed session report as PA-styled stat rows and sleeve
    cards instead of the raw markdown notify() sends to a phone."""
    out = []
    if parsed.get("lead"):
        out.append(f'<p class="pa-notif-lead">{escape(parsed["lead"])}</p>')

    stats = parsed.get("stats") or {}
    if stats:
        cells = []
        for label in ("Equity", "Cash", "Day P&L"):
            if label not in stats:
                continue
            v = stats[label]
            cls = ("pos" if v.startswith("+") else "neg" if v.startswith("-") else "") if label == "Day P&L" else ""
            cells.append(f'<div class="pa-notif-stat"><div class="pa-notif-stat__l">{escape(label)}</div>'
                        f'<div class="pa-notif-stat__v {cls}">{escape(v)}</div></div>')
        out.append(f'<div class="pa-notif-stats">{"".join(cells)}</div>')

    for sv in parsed.get("sleeves") or []:
        has_budget = sv["pct"] is not None
        over = has_budget and sv["pct"] > 100
        pct_cls = "pa-chip--sev-high" if over else "pa-chip--sev-low"
        chip = f'${sv["used"]} / ${sv["budget"]} &middot; {sv["pct"]}%' if has_budget else f'${sv["used"]}'
        unrl_cls = "pos" if sv["unrealized"].startswith("+") else "neg" if sv["unrealized"].startswith("-") else ""
        syms = "".join(f'<span class="pa-notif-sym">{escape(s)}</span>' for s in sv["symbols"])
        out.append(
            '<div class="pa-notif-sleeve">'
            f'<div class="pa-notif-sleeve__hd"><span class="pa-notif-sleeve__name">{escape(sv["name"])}</span>'
            f'<span class="pa-chip {pct_cls}">{chip}</span></div>'
            f'<div class="pa-notif-sleeve__sub">unrealized <span class="{unrl_cls}">{escape(sv["unrealized"])}</span>'
            f' &middot; {sv["position_count"]} position(s)</div>'
            + (f'<div class="pa-notif-syms">{syms}</div>' if syms else "")
            + '</div>'
        )

    if parsed.get("working_orders"):
        items = "".join(f'<li>{escape(o)}</li>' for o in parsed["working_orders"])
        out.append(f'<div class="pa-notif-lead" style="margin-top:12px">Working orders</div><ul class="pa-notif-orders">{items}</ul>')

    if parsed.get("trailer"):
        out.append(f'<p class="pa-notif-trailer">{escape(parsed["trailer"])}</p>')

    return "".join(out)


def _notification_body_html(message: str) -> str:
    """The engine's own session-report shape gets typeset; anything else
    (a watchdog alert, the short session-starting ping) still gets the safe
    escaped-and-<br>ed fallback rather than a blank page."""
    if not message:
        return "(no message body)"
    parsed = parse_session_report(message)
    return session_report_html(parsed) if parsed else _multiline_html(message)


def notification_rows_html(rows: list[dict]) -> str:
    """rows: when, severity, title, message, via, delivered (bool), error (str|None).

    Each row is a native <details>/<summary>: click to expand shows the full
    message, hover shows a one-line preview via the title= attribute, and
    neither depends on Streamlit's own expander (or its icon font).
    """
    if not rows:
        return '<p style="color:#6e6e73">No notifications yet.</p>'
    sev_cls = {"high": "pa-chip--sev-high", "default": "pa-chip--sev-default"}
    out = ['<div class="pa-notif-list">']
    for r in rows:
        sev = str(r.get("severity") or "default")
        cls = sev_cls.get(sev, "pa-chip--sev-low")
        title = str(r.get("title") or "(no title)")
        message = str(r.get("message") or "")
        tooltip = _one_line(title + (" -- " + message if message else ""), 240)
        delivered = r.get("delivered")
        status = "delivered" if delivered else ("failed: " + str(r.get("error") or "unknown")[:80] if r.get("error") else "not delivered")
        out.append(
            f'<details class="pa-notif-row"><summary title="{escape(tooltip)}">'
            f'<span class="pa-notif-when">{escape(str(r.get("when") or ""))}</span>'
            f'<span class="pa-chip {cls}">{escape(sev)}</span>'
            f'<span class="pa-notif-title">{escape(title)}</span>'
            f'<span class="pa-notif-via">{escape(str(r.get("via") or ""))} &middot; {escape(status)}</span>'
            f'</summary><div class="pa-notif-body">{_notification_body_html(message)}'
            f'<div class="pa-notif-meta">via {escape(str(r.get("via") or "unknown"))} &middot; {escape(status)}</div>'
            f'</div></details>'
        )
    out.append('</div>')
    return "".join(out)


def positions_table_html(rows: list[dict]) -> str:
    """rows: sleeve, symbol, qty, entry, last, pl, plpc, mv. Grouped by sleeve,
    each group sorted by P&L descending, totals in the footer."""
    if not rows:
        return '<p style="color:#6e6e73">No open positions.</p>'
    order = ["SixFold", "CSP", "Pendulum", "Vampire", "Other"]
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(r.get("sleeve") or "Other", []).append(r)
    out = ['<div class="pa-ledger-wrap"><table class="pa-ledger"><thead><tr><th>Position</th><th class="num">Qty</th><th class="num">Entry</th>'
           '<th class="num">Last</th><th class="num">P&amp;L</th><th class="num">%</th><th class="num">Value</th></tr></thead><tbody>']
    tot_pl = tot_mv = 0.0
    for g in [g for g in order if g in groups] + [g for g in groups if g not in order]:
        rs = sorted(groups[g], key=lambda r: -float(r["pl"]))
        gpl = sum(float(r["pl"]) for r in rs)
        out.append(f'<tr class="grp"><td colspan="4"><span class="pa-eyebrow" style="margin:0">{escape(g)}</span></td>'
                   f'<td class="num {"pos" if gpl >= 0 else "neg"}" colspan="3">{money(gpl, signed=True)}</td></tr>')
        for r in rs:
            pl, mv = float(r["pl"]), float(r["mv"]); tot_pl += pl; tot_mv += mv
            cls = "pos" if pl >= 0 else "neg"
            out.append(f'<tr><td class="sym">{escape(str(r["symbol"]))}</td><td class="num">{float(r["qty"]):g}</td>'
                       f'<td class="num">{float(r["entry"]):,.2f}</td><td class="num">{float(r["last"]):,.2f}</td>'
                       f'<td class="num {cls}">{money(pl, signed=True)}</td><td class="num {cls}">{pct(float(r["plpc"]))}</td>'
                       f'<td class="num">{money(mv)}</td></tr>')
    out.append(f'</tbody><tfoot><tr><td colspan="4">Total</td><td class="num {"pos" if tot_pl >= 0 else "neg"}">{money(tot_pl, signed=True)}</td>'
               f'<td></td><td class="num">{money(tot_mv)}</td></tr></tfoot></table></div>')
    return "".join(out)
