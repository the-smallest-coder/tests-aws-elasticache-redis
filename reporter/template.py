"""HTML template renderer for the ElastiCache performance report."""

# The CSS and JS are defined as plain strings (no f-string escaping needed).
# Only the dynamic slots use .format() / f-strings at render time.

_CSS = """\
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
.page-header h1 {
  font-size: 20px;
  font-weight: 700;
  letter-spacing: -.2px;
  margin-bottom: 4px;
}
.page-header .meta {
  font-size: 12px;
  opacity: .75;
  margin-bottom: 12px;
}
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
.card-label {
  font-size: 10px;
  font-weight: 700;
  color: #80868b;
  text-transform: uppercase;
  letter-spacing: .6px;
  margin-bottom: 8px;
}
.card-value {
  font-size: 26px;
  font-weight: 700;
  line-height: 1;
}
.card-unit {
  font-size: 12px;
  font-weight: 400;
  color: #aaa;
  margin-left: 3px;
}
.chart-wrap {
  background: #fff;
  border: 1px solid #e0e4ea;
  border-radius: 10px;
  padding: 8px;
  box-shadow: 0 1px 3px rgba(0,0,0,.06);
}
.chart-group { margin-bottom: 32px; }
.group-header {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 14px;
}
.group-header h2 {
  font-size: 13px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: .8px;
  color: #5f6368;
  white-space: nowrap;
}
.group-header .group-badge {
  font-size: 11px;
  font-weight: 600;
  padding: 2px 9px;
  border-radius: 12px;
  white-space: nowrap;
}
.group-badge.memtier   { background: #e8f0fe; color: #1a56db; }
.group-badge.infra     { background: #e6f4ea; color: #188038; }
.group-badge.deepdive  { background: #f3e5f5; color: #6a1b9a; }
.group-header::after {
  content: '';
  flex: 1;
  height: 1px;
  background: #e0e4ea;
}
#copy-btn {
  position: fixed;
  top: 16px;
  right: 20px;
  z-index: 9999;
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 8px 16px;
  background: rgba(255,255,255,.18);
  border: 1px solid rgba(255,255,255,.4);
  border-radius: 6px;
  color: #fff;
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  backdrop-filter: blur(4px);
  transition: background .15s, transform .1s;
}
#copy-btn:hover { background: rgba(255,255,255,.28); }
#copy-btn:active { transform: scale(.97); }
#copy-btn svg { flex-shrink: 0; }
#copy-btn.copied {
  background: #188038;
  border-color: #188038;
}
"""

_COPY_BUTTON = """\
<button id="copy-btn" onclick="copyReport()" title="Copy body content for Drupal Source editor">
  <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
    <rect x="9" y="9" width="13" height="13" rx="2"/>
    <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>
  </svg>
  <span id="copy-label">Copy HTML</span>
</button>
"""

_JS = """\
function copyReport() {
  var btn = document.getElementById('copy-btn');
  var clone = document.body.cloneNode(true);
  clone.removeChild(clone.querySelector('#copy-btn'));
  var styles = document.querySelector('head style');
  var fragment = (styles ? '<style>' + styles.innerHTML + '</style>' : '') + clone.innerHTML;
  navigator.clipboard.writeText(fragment).then(function() {
    var btn = document.getElementById('copy-btn');
    var lbl = document.getElementById('copy-label');
    btn.classList.add('copied');
    lbl.textContent = 'Copied!';
    setTimeout(function() {
      btn.classList.remove('copied');
      lbl.textContent = 'Copy HTML';
    }, 2200);
  }).catch(function(err) {
    alert('Copy failed: ' + err);
  });
}
"""


def render_html(
    cluster_id,
    suffix,
    id_label,
    time_range,
    pills_html,
    cards_html,
    chart_memtier_html,
    chart_infra_html,
    chart_deep_dive_html,
):
    """Assemble the full standalone HTML report page."""
    meta_parts = [f"{id_label}: {cluster_id}", f"Run: {suffix}"]
    if time_range:
        meta_parts.append(time_range)
    meta_line = "&nbsp;&nbsp;·&nbsp;&nbsp;".join(meta_parts)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>ElastiCache Report — {cluster_id}</title>
  <style>
{_CSS}  </style>
</head>
<body>

{_COPY_BUTTON}
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
      <span class="group-badge memtier">benchmark window only</span>
    </div>
    <div class="chart-wrap">{chart_memtier_html}</div>
  </div>

  <div class="chart-group">
    <div class="group-header">
      <h2>Infrastructure</h2>
      <span class="group-badge infra">full CloudWatch window</span>
    </div>
    <div class="chart-wrap">{chart_infra_html}</div>
  </div>

  <div class="chart-group">
    <div class="group-header">
      <h2>ElastiCache Deep-Dive</h2>
      <span class="group-badge deepdive">full CloudWatch window</span>
    </div>
    <div class="chart-wrap">{chart_deep_dive_html}</div>
  </div>
</div>

<script>
{_JS}</script>

</body>
</html>"""
