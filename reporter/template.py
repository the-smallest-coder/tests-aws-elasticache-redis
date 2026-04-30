from __future__ import annotations

from html import escape


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
