from __future__ import annotations

from html import escape

# ---------------------------------------------------------------------------
# Single-run report renderer
# ---------------------------------------------------------------------------

_SINGLE_CSS = """\
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
  background: #f4f6f9;
  color: #202124;
  font-size: 14px;
}
.page-header {
  background: linear-gradient(135deg, #1a56db 0%, #0d47a1 100%);
  color: #fff;
  padding: 24px 32px 20px;
}
.page-header h1 { font-size: 20px; font-weight: 700; letter-spacing: -.2px; margin-bottom: 4px; }
.page-header .meta { font-size: 12px; opacity: .75; margin-bottom: 12px; }
.page-header .pills { display: flex; flex-wrap: wrap; gap: 6px; }
.pill {
  background: rgba(255,255,255,.15);
  border: 1px solid rgba(255,255,255,.3);
  border-radius: 4px;
  padding: 3px 10px;
  font-size: 12px;
  font-weight: 600;
}
.pill span { font-weight: 400; opacity: .85; }
.content { max-width: 1400px; margin: 0 auto; padding: 24px 28px; }
.cards {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
  gap: 14px;
  margin-bottom: 28px;
}
.card {
  background: #fff;
  border: 1px solid #e0e4ea;
  border-radius: 10px;
  padding: 16px 18px;
  box-shadow: 0 1px 3px rgba(0,0,0,.06);
}
.card-label { font-size: 10px; font-weight: 700; color: #80868b; text-transform: uppercase; letter-spacing: .6px; margin-bottom: 8px; }
.card-value { font-size: 26px; font-weight: 700; line-height: 1; }
.card-unit  { font-size: 12px; font-weight: 400; color: #aaa; margin-left: 3px; }
.chart-wrap {
  background: #fff;
  border: 1px solid #e0e4ea;
  border-radius: 10px;
  padding: 8px;
  box-shadow: 0 1px 3px rgba(0,0,0,.06);
}
.chart-group { margin-bottom: 32px; }
.group-header { display: flex; align-items: center; gap: 12px; margin-bottom: 14px; }
.group-header h2 { font-size: 13px; font-weight: 700; text-transform: uppercase; letter-spacing: .8px; color: #5f6368; white-space: nowrap; }
.group-header .group-badge { font-size: 11px; font-weight: 600; padding: 2px 9px; border-radius: 12px; white-space: nowrap; }
.group-badge.memtier  { background: #e8f0fe; color: #1a56db; }
.group-badge.infra    { background: #e6f4ea; color: #188038; }
.group-badge.latency  { background: #fce8e6; color: #b3261e; }
.group-badge.deepdive { background: #f3e5f5; color: #6a1b9a; }
.group-header::after { content: ''; flex: 1; height: 1px; background: #e0e4ea; }
#copy-btn {
  position: fixed; top: 16px; right: 20px; z-index: 9999;
  display: flex; align-items: center; gap: 6px;
  padding: 8px 16px;
  background: rgba(255,255,255,.18);
  border: 1px solid rgba(255,255,255,.4);
  border-radius: 6px; color: #fff; font-size: 13px; font-weight: 600;
  cursor: pointer; backdrop-filter: blur(4px);
  transition: background .15s, transform .1s;
}
#copy-btn:hover { background: rgba(255,255,255,.28); }
#copy-btn:active { transform: scale(.97); }
#copy-btn svg { flex-shrink: 0; }
#copy-btn:disabled { opacity: .55; cursor: not-allowed; pointer-events: none; }
#copy-btn.copied { background: #188038; border-color: #188038; }
"""

_SINGLE_COPY_BUTTON = """\
<button id="copy-btn" onclick="copyReport()" title="Copy body content for Drupal Source editor">
  <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
    <rect x="9" y="9" width="13" height="13" rx="2"/>
    <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>
  </svg>
  <span id="copy-label">Copy HTML</span>
</button>
"""

_SINGLE_JS = """\
function copyReport() {
  var btn = document.getElementById('copy-btn');
  var lbl = document.getElementById('copy-label');
  btn.disabled = true;
  lbl.textContent = 'Rendering\u2026';
  var plots = Array.from(document.querySelectorAll('.js-plotly-plot'));
  Promise.all(plots.map(function(el) {
    return Plotly.toImage(el, {format: 'png', width: el.offsetWidth || 900, height: el.offsetHeight || 500});
  })).then(function(urls) {
    var clone = document.body.cloneNode(true);
    var cb = clone.querySelector('#copy-btn');
    if (cb) { cb.parentNode.removeChild(cb); }
    Array.from(clone.querySelectorAll('.js-plotly-plot')).forEach(function(el, i) {
      var img = document.createElement('img');
      img.src = urls[i];
      img.style.cssText = 'width:100%;display:block;';
      el.parentNode.replaceChild(img, el);
    });
    Array.from(clone.querySelectorAll('script')).forEach(function(el) {
      el.parentNode.removeChild(el);
    });
    var st = document.querySelector('head style');
    var html = (st ? '<style>' + st.innerHTML + '</style>' : '') + clone.innerHTML;
    return navigator.clipboard.writeText(html);
  }).then(function() {
    btn.classList.add('copied');
    lbl.textContent = 'Copied!';
    btn.disabled = false;
    setTimeout(function() {
      btn.classList.remove('copied');
      lbl.textContent = 'Copy HTML';
    }, 2200);
  }).catch(function(err) {
    btn.disabled = false;
    lbl.textContent = 'Copy HTML';
    alert('Copy failed: ' + err);
  });
}
"""


def render_html(
    cluster_id: str,
    suffix: str,
    id_label: str,
    time_range: str,
    pills_html: str,
    cards_html: str,
    chart_memtier_html: str,
    chart_infra_html: str,
    chart_client_latency_html: str,
    chart_deep_dive_html: str,
) -> str:
    """Assemble the full standalone single-run HTML report page."""
    escaped_cluster_id = escape(str(cluster_id), quote=True)
    meta_parts = [
        f"{escape(str(id_label), quote=True)}: {escaped_cluster_id}",
        f"Run: {escape(str(suffix), quote=True)}",
    ]
    if time_range:
        meta_parts.append(escape(str(time_range), quote=True))
    meta_line = "&nbsp;&nbsp;·&nbsp;&nbsp;".join(meta_parts)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>ElastiCache Report — {escaped_cluster_id}</title>
  <style>
{_SINGLE_CSS}  </style>
</head>
<body>

{_SINGLE_COPY_BUTTON}
<div class="page-header">
  <h1>ElastiCache Performance Report</h1>
  <div class="meta">{meta_line}</div>
  {pills_html}
</div>

<div class="content">
  {cards_html}

  <div class="chart-group">
    <div class="group-header">
      <h2>Memtier Benchmark</h2>
      <span class="group-badge memtier">report window</span>
    </div>
    <div class="chart-wrap">{chart_memtier_html}</div>
  </div>

  <div class="chart-group">
    <div class="group-header">
      <h2>Infrastructure</h2>
      <span class="group-badge infra">report window</span>
    </div>
    <div class="chart-wrap">{chart_infra_html}</div>
  </div>

  <div class="chart-group">
    <div class="group-header">
      <h2>Client Latency</h2>
      <span class="group-badge latency">ECS EMF</span>
    </div>
    <div class="chart-wrap">{chart_client_latency_html}</div>
  </div>

  <div class="chart-group">
    <div class="group-header">
      <h2>ElastiCache Deep-Dive</h2>
      <span class="group-badge deepdive">report window</span>
    </div>
    <div class="chart-wrap">{chart_deep_dive_html}</div>
  </div>
</div>

<script>
{_SINGLE_JS}</script>

</body>
</html>"""


# ---------------------------------------------------------------------------
# Comparison report renderer
# ---------------------------------------------------------------------------

def _render_takeaways(items: list[dict[str, str]]) -> str:
    if not items:
        return ""

    blocks = []
    for item in items:
        blocks.append(
            f"""
            <li class="takeaway tone-{escape(item['tone'])}">
              <div class="takeaway-title">{escape(item['title'])}</div>
              <div class="takeaway-text">{escape(item['text'])}</div>
            </li>
            """
        )
    return f"""
    <section class="panel">
      <div class="section-head">
        <h2>Key Takeaways</h2>
        <span class="tag">candidate vs baseline</span>
      </div>
      <ul class="takeaways">
        {''.join(blocks)}
      </ul>
    </section>
    """


def _render_topline_cards(cards: list[dict[str, str]]) -> str:
    blocks = []
    for card in cards:
        blocks.append(
            f"""
            <article class="card tone-{escape(card['tone'])}">
              <div class="card-label">{escape(card['label'])}</div>
              <div class="card-grid">
                <div class="card-side">
                  <span class="role">Baseline</span>
                  <strong>{escape(card['baseline'])}</strong>
                </div>
                <div class="card-side">
                  <span class="role">Candidate</span>
                  <strong>{escape(card['candidate'])}</strong>
                </div>
              </div>
              <div class="delta">{escape(card['delta'])}</div>
            </article>
            """
        )
    return f"""
    <section class="topline">
      {''.join(blocks)}
    </section>
    """


def _render_run_context(run: dict[str, object]) -> str:
    rows = []
    for item in run["items"]:
        rows.append(
            f"""
            <div class="kv">
              <div class="kv-label">{escape(item['label'])}</div>
              <div class="kv-value">{escape(item['value'])}</div>
            </div>
            """
        )

    note_html = ""
    if run.get("note"):
        note_html = f"<p class=\"run-note\">{escape(run['note'])}</p>"

    return f"""
    <article class="panel run-panel">
      <div class="section-head">
        <h2>{escape(run['role'])}</h2>
        <span class="tag">{escape(run['folder'])}</span>
      </div>
      <div class="run-title">{escape(run['title'])}</div>
      <div class="kv-grid">
        {''.join(rows)}
      </div>
      {note_html}
    </article>
    """


def _render_sections(sections: list[dict[str, object]]) -> str:
    parts = []
    for section in sections:
        rows = []
        for row in section["rows"]:
            rows.append(
                f"""
                <tr class="tone-{escape(row['tone'])}">
                  <td>
                    <div class="metric-name">{escape(row['label'])}</div>
                    <div class="metric-help">{escape(row['description'])}</div>
                  </td>
                  <td>{escape(row['baseline'])}</td>
                  <td>{escape(row['candidate'])}</td>
                  <td class="delta-cell">{escape(row['delta'])}</td>
                </tr>
                """
            )

        parts.append(
            f"""
            <section class="panel">
              <div class="section-head">
                <h2>{escape(section['title'])}</h2>
                <span class="tag">{escape(section['badge'])}</span>
              </div>
              <div class="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>Metric</th>
                      <th>Baseline</th>
                      <th>Candidate</th>
                      <th>Delta</th>
                    </tr>
                  </thead>
                  <tbody>
                    {''.join(rows)}
                  </tbody>
                </table>
              </div>
            </section>
            """
        )
    return "".join(parts)


def render_report(payload: dict[str, object]) -> str:
    context_html = "".join(_render_run_context(run) for run in payload["runs"])
    takeaways_html = _render_takeaways(payload["takeaways"])
    topline_html = _render_topline_cards(payload["topline_cards"])
    sections_html = _render_sections(payload["sections"])

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(payload['title'])}</title>
  <style>
    :root {{
      --bg: #eef3f8;
      --panel: #ffffff;
      --ink: #17212b;
      --muted: #5f6b76;
      --line: #d9e1ea;
      --hero-1: #0b3a66;
      --hero-2: #0f6ab4;
      --good: #17803d;
      --bad: #c62828;
      --warn: #b26a00;
      --shadow: 0 10px 30px rgba(20, 43, 73, 0.08);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background:
        radial-gradient(circle at top right, rgba(15, 106, 180, 0.18), transparent 28%),
        linear-gradient(180deg, #f7fbff 0%, var(--bg) 32%, #f7fafc 100%);
      color: var(--ink);
      font: 14px/1.5 "Segoe UI", Arial, sans-serif;
    }}
    .hero {{
      padding: 32px 28px 26px;
      color: #fff;
      background: linear-gradient(135deg, var(--hero-1) 0%, var(--hero-2) 100%);
      box-shadow: var(--shadow);
    }}
    .hero-inner {{
      max-width: 1320px;
      margin: 0 auto;
    }}
    .hero h1 {{
      margin: 0;
      font-size: 26px;
      line-height: 1.15;
      letter-spacing: -0.02em;
    }}
    .hero-meta {{
      margin-top: 8px;
      color: rgba(255, 255, 255, 0.82);
      font-size: 13px;
    }}
    .hero-pills {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 16px;
    }}
    .pill {{
      display: inline-flex;
      gap: 6px;
      align-items: center;
      padding: 6px 10px;
      border: 1px solid rgba(255, 255, 255, 0.28);
      border-radius: 999px;
      background: rgba(255, 255, 255, 0.12);
      font-size: 12px;
      font-weight: 700;
    }}
    .pill span {{
      font-weight: 500;
      opacity: 0.9;
    }}
    .page {{
      max-width: 1320px;
      margin: 0 auto;
      padding: 24px 20px 36px;
    }}
    .topline {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(230px, 1fr));
      gap: 14px;
      margin-bottom: 18px;
    }}
    .panel,
    .card {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 16px;
      box-shadow: var(--shadow);
    }}
    .card {{
      padding: 16px 18px;
      position: relative;
      overflow: hidden;
    }}
    .card::before {{
      content: "";
      position: absolute;
      inset: 0 auto 0 0;
      width: 4px;
      background: #c7d7e7;
    }}
    .card.tone-better::before {{ background: var(--good); }}
    .card.tone-worse::before {{ background: var(--bad); }}
    .card.tone-warning::before {{ background: var(--warn); }}
    .card-label {{
      color: var(--muted);
      font-size: 11px;
      font-weight: 800;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      margin-bottom: 10px;
    }}
    .card-grid {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 12px;
      margin-bottom: 10px;
    }}
    .card-side {{
      padding: 10px 12px;
      border-radius: 12px;
      background: #f6f9fc;
      border: 1px solid #e7edf4;
    }}
    .role {{
      display: block;
      color: var(--muted);
      font-size: 11px;
      font-weight: 700;
      margin-bottom: 4px;
      text-transform: uppercase;
      letter-spacing: 0.06em;
    }}
    .card-side strong {{
      font-size: 18px;
      line-height: 1.2;
    }}
    .delta {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 5px 10px;
      border-radius: 999px;
      background: #f0f5fa;
      color: var(--ink);
      font-size: 12px;
      font-weight: 800;
    }}
    .panel {{
      padding: 18px;
      margin-bottom: 18px;
    }}
    .section-head {{
      display: flex;
      gap: 12px;
      align-items: center;
      margin-bottom: 14px;
    }}
    .section-head h2 {{
      margin: 0;
      font-size: 14px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
    }}
    .tag {{
      padding: 5px 10px;
      border-radius: 999px;
      background: #e7f1fb;
      color: #0f6ab4;
      font-size: 11px;
      font-weight: 800;
      text-transform: uppercase;
      letter-spacing: 0.06em;
    }}
    .takeaways {{
      list-style: none;
      padding: 0;
      margin: 0;
      display: grid;
      gap: 10px;
    }}
    .takeaway {{
      border: 1px solid var(--line);
      border-radius: 14px;
      padding: 12px 14px;
      background: #f8fbfd;
    }}
    .takeaway-title {{
      font-weight: 800;
      margin-bottom: 4px;
    }}
    .takeaway-text {{
      color: var(--muted);
    }}
    .tone-better .takeaway-title,
    .tone-better .delta,
    tr.tone-better .delta-cell {{
      color: var(--good);
    }}
    .tone-worse .takeaway-title,
    .tone-worse .delta,
    tr.tone-worse .delta-cell {{
      color: var(--bad);
    }}
    .tone-warning .takeaway-title,
    .tone-warning .delta,
    tr.tone-warning .delta-cell {{
      color: var(--warn);
    }}
    .tone-mixed .takeaway-title,
    .tone-mixed .delta,
    tr.tone-mixed .delta-cell {{
      color: var(--warn);
    }}
    .tone-neutral .takeaway-title,
    .tone-neutral .delta,
    tr.tone-neutral .delta-cell {{
      color: var(--muted);
    }}
    .run-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(310px, 1fr));
      gap: 16px;
      margin-bottom: 18px;
    }}
    .run-title {{
      font-size: 18px;
      font-weight: 800;
      margin-bottom: 14px;
    }}
    .kv-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
      gap: 12px;
    }}
    .kv {{
      padding: 10px 12px;
      border: 1px solid #e7edf4;
      border-radius: 12px;
      background: #f8fbfd;
    }}
    .kv-label {{
      color: var(--muted);
      font-size: 11px;
      font-weight: 800;
      text-transform: uppercase;
      letter-spacing: 0.06em;
      margin-bottom: 4px;
    }}
    .kv-value {{
      font-size: 14px;
      font-weight: 700;
      word-break: break-word;
    }}
    .run-note {{
      margin: 12px 0 0;
      color: var(--warn);
      font-size: 12px;
      font-weight: 700;
    }}
    .table-wrap {{
      overflow-x: auto;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      min-width: 760px;
    }}
    th,
    td {{
      padding: 12px 10px;
      text-align: left;
      vertical-align: top;
      border-top: 1px solid #edf2f7;
    }}
    thead th {{
      border-top: 0;
      color: var(--muted);
      font-size: 11px;
      font-weight: 800;
      text-transform: uppercase;
      letter-spacing: 0.06em;
    }}
    tbody tr:nth-child(even) {{
      background: #fafcff;
    }}
    .metric-name {{
      font-weight: 700;
      margin-bottom: 4px;
    }}
    .metric-help {{
      color: var(--muted);
      font-size: 12px;
      max-width: 520px;
    }}
    @media (max-width: 760px) {{
      .hero {{
        padding: 24px 18px 20px;
      }}
      .page {{
        padding: 18px 12px 28px;
      }}
      .card-grid,
      .kv-grid {{
        grid-template-columns: 1fr;
      }}
    }}
  </style>
</head>
<body>
  <header class="hero">
    <div class="hero-inner">
      <h1>{escape(payload['heading'])}</h1>
      <div class="hero-meta">{escape(payload['subtitle'])}</div>
      <div class="hero-pills">
        <div class="pill">Baseline <span>{escape(payload['baseline_name'])}</span></div>
        <div class="pill">Candidate <span>{escape(payload['candidate_name'])}</span></div>
        <div class="pill">Output <span>{escape(payload['comparison_direction'])}</span></div>
      </div>
    </div>
  </header>

  <main class="page">
    {topline_html}
    {takeaways_html}

    <section class="run-grid">
      {context_html}
    </section>

    {sections_html}
  </main>
</body>
</html>
"""
