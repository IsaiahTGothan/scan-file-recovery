"""HTML and CSV reports written next to the recovered files."""

from __future__ import annotations

import csv
import html
import io
import os
import time
from typing import TYPE_CHECKING

from .. import __version__
from ..branding import APP_FULL_NAME, PUBLISHER
from ..errors import describe
from ..util import format_duration, format_size
from .destination import display_path, long_path

if TYPE_CHECKING:
    from .engine import FileTask, RecoveryJob, RecoverySummary

HTML_ROW_LIMIT = 5000
REPORT_HTML = "Lifeboat Report.html"
REPORT_CSV = "Lifeboat Report.csv"

_STATUS_STYLE = {
    "ok": ("Recovered", "ok"),
    "partial": ("Damaged", "warn"),
    "failed": ("Failed", "bad"),
    "skipped": ("Not processed", "muted"),
    "pending": ("Not processed", "muted"),
}


def _ranges_text(task: FileTask, limit: int = 5) -> str:
    ranges = task.damaged_ranges()
    if not ranges:
        return ""
    parts = [f"{s:,}-{e - 1:,}" for s, e in ranges[:limit]]
    if len(ranges) > limit:
        parts.append(f"+{len(ranges) - limit} more")
    return "; ".join(parts)


def _atomic_write(path: str, data: bytes) -> None:
    tmp = path + ".tmp"
    with open(long_path(tmp), "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(long_path(tmp), long_path(path))


def build_csv(tasks: list[FileTask]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Status", "Original path", "Recovered as", "Size (bytes)", "Recovered bytes",
                     "Unreadable bytes", "Unreadable byte ranges", "SHA-256", "Code", "Message", "Notes"])
    for task in tasks:
        writer.writerow([
            _STATUS_STYLE.get(task.status, (task.status, ""))[0],
            task.source_path,
            task.rel_path if task.status in ("ok", "partial") else "",
            task.size,
            task.ok_bytes if task.status in ("ok", "partial") else 0,
            task.bad_bytes if task.status == "partial" else (task.size if task.status == "failed" else 0),
            _ranges_text(task, 20) if task.status == "partial" else "",
            task.sha256,
            task.code,
            task.message,
            " | ".join(task.notes),
        ])
    return buf.getvalue().encode("utf-8-sig")


def build_html(job: RecoveryJob, summary: RecoverySummary) -> bytes:
    esc = html.escape
    tasks = summary.tasks
    outcome = summary.outcome
    banner = {
        "success": ("ok", "Every selected file was recovered and verified."),
        "warning": ("warn", "Some files are damaged, failed or were not processed. They are listed first below."),
        "failed": ("bad", "The recovery did not succeed. See the problems below."),
    }[outcome]
    if summary.cancelled:
        banner = ("warn", "The recovery was stopped before it finished. Files not processed are listed below; "
                          "start the recovery again to continue.")
    src = job.source
    rows_problem = [t for t in tasks if t.status in ("failed", "partial")]
    rows_other = [t for t in tasks if t.status not in ("failed", "partial")]
    codes = sorted({t.code for t in tasks if t.code})

    def row(task: FileTask) -> str:
        label, cls = _STATUS_STYLE.get(task.status, (task.status, "muted"))
        detail = task.message
        if task.status == "partial":
            detail = f"{task.message}. Unreadable bytes: {_ranges_text(task)}"
        notes = "<br>".join(esc(n) for n in task.notes)
        return (
            f"<tr><td><span class='pill {cls}'>{esc(label)}</span></td>"
            f"<td class='path'>{esc(task.source_path)}</td>"
            f"<td class='path'>{esc(task.rel_path) if task.status in ('ok', 'partial') else ''}</td>"
            f"<td class='num'>{esc(format_size(task.size))}</td>"
            f"<td>{esc(detail)}{'<br><small>' + notes + '</small>' if notes else ''}</td>"
            f"<td class='mono'>{esc(task.code)}</td></tr>"
        )

    other_html = "".join(row(t) for t in rows_other[:HTML_ROW_LIMIT])
    more = len(rows_other) - HTML_ROW_LIMIT
    more_html = (f"<p class='muted'>{more:,} more files are listed in {esc(REPORT_CSV)}.</p>"
                 if more > 0 else "")
    code_rows = "".join(
        f"<tr><td class='mono'>{esc(c)}</td><td>{esc(describe(c).title)}</td><td>{esc(describe(c).hint)}</td></tr>"
        for c in codes)
    stamp = time.strftime("%Y-%m-%d %H:%M")
    doc = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Lifeboat recovery report</title>
<style>
:root {{ --bg:#f5f7fa; --card:#fff; --ink:#132033; --muted:#5b6b80; --line:#dde3ea; --accent:#ff6a1a;
  --ok:#17803d; --okbg:#e5f6ea; --warn:#9a5b00; --warnbg:#fff3dc; --bad:#b42318; --badbg:#fde8e7; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#0e1622; --card:#152131; --ink:#e7edf5; --muted:#9db0c6;
  --line:#26364b; --okbg:#123322; --warnbg:#3a2a0e; --badbg:#3b1514; --ok:#5bd28a; --warn:#ffbf5c; --bad:#ff8a80; }} }}
body {{ margin:0; font:14px/1.5 "Segoe UI", system-ui, sans-serif; background:var(--bg); color:var(--ink); }}
main {{ max-width:1200px; margin:0 auto; padding:24px 16px 64px; }}
header {{ display:flex; align-items:center; gap:14px; margin-bottom:20px; }}
header h1 {{ font-size:22px; margin:0; }} header p {{ margin:0; color:var(--muted); }}
.banner {{ padding:14px 16px; border-radius:10px; margin:16px 0; font-weight:600; }}
.banner.ok {{ background:var(--okbg); color:var(--ok); }} .banner.warn {{ background:var(--warnbg); color:var(--warn); }}
.banner.bad {{ background:var(--badbg); color:var(--bad); }}
.cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(170px,1fr)); gap:12px; }}
.card {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:12px 14px; }}
.card b {{ display:block; font-size:22px; }} .card span {{ color:var(--muted); font-size:12px; }}
table {{ width:100%; border-collapse:collapse; background:var(--card); border:1px solid var(--line);
  border-radius:10px; overflow:hidden; margin-top:8px; }}
th, td {{ text-align:left; padding:7px 10px; border-bottom:1px solid var(--line); vertical-align:top; }}
th {{ font-size:12px; color:var(--muted); font-weight:600; }}
.path {{ word-break:break-all; }} .num {{ white-space:nowrap; text-align:right; }}
.mono {{ font-family:Consolas, monospace; font-size:12px; white-space:nowrap; }}
.pill {{ display:inline-block; padding:1px 8px; border-radius:99px; font-size:12px; font-weight:600; white-space:nowrap; }}
.pill.ok {{ background:var(--okbg); color:var(--ok); }} .pill.warn {{ background:var(--warnbg); color:var(--warn); }}
.pill.bad {{ background:var(--badbg); color:var(--bad); }} .pill.muted {{ background:var(--line); color:var(--muted); }}
h2 {{ font-size:16px; margin:28px 0 6px; }} .muted {{ color:var(--muted); }}
dl {{ display:grid; grid-template-columns:max-content 1fr; gap:4px 16px; margin:0; }}
dt {{ color:var(--muted); }} dd {{ margin:0; word-break:break-all; }}
@media (max-width:700px) {{ th:nth-child(3), td:nth-child(3), th:nth-child(6), td:nth-child(6) {{ display:none; }} }}
</style></head><body><main>
<header>
<svg width="44" height="44" viewBox="0 0 64 64" aria-hidden="true"><rect width="64" height="64" rx="14" fill="#0e1a2b"/>
<circle cx="32" cy="32" r="19" fill="none" stroke="#fff" stroke-width="9"/>
<circle cx="32" cy="32" r="19" fill="none" stroke="#ff6a1a" stroke-width="9" stroke-dasharray="14.9 14.9"
 transform="rotate(-22 32 32)"/></svg>
<div><h1>Recovery report</h1><p>{esc(APP_FULL_NAME)} {esc(__version__)} by {esc(PUBLISHER)} &middot; {esc(stamp)}</p></div>
</header>
<div class="banner {banner[0]}">{esc(banner[1])}</div>
<div class="cards">
<div class="card"><b>{summary.count("ok"):,}</b><span>files recovered and verified</span></div>
<div class="card"><b>{summary.count("partial"):,}</b><span>damaged (partly recovered)</span></div>
<div class="card"><b>{summary.count("failed"):,}</b><span>failed</span></div>
<div class="card"><b>{summary.count("skipped") + summary.count("pending"):,}</b><span>not processed</span></div>
<div class="card"><b>{esc(format_size(summary.recovered_bytes))}</b><span>of {esc(format_size(summary.total_bytes))} recovered</span></div>
<div class="card"><b>{esc(format_duration(summary.seconds))}</b><span>duration</span></div>
</div>
<h2>Details</h2>
<div class="card"><dl>
<dt>Source</dt><dd>{esc(src.title)} &middot; {esc(src.capacity_text)}{(" &middot; serial " + esc(src.serial)) if src.serial else ""}</dd>
<dt>Destination</dt><dd>{esc(display_path(summary.job_dir))}</dd>
<dt>Thoroughness</dt><dd>{esc(job.options.thoroughness.title())}{" (finished early at your request)" if summary.finished_early else ""}</dd>
<dt>Verification</dt><dd>{"Every file was read back from the destination and compared (SHA-256)." if job.options.verify else "Off"}</dd>
<dt>Unreadable data</dt><dd>{esc(format_size(summary.damaged_bytes))} inside damaged files (filled with zeros)</dd>
</dl></div>
{"<h2>Problems</h2><table><tr><th>Status</th><th>Original location</th><th>Saved as</th><th>Size</th><th>Details</th><th>Code</th></tr>" + "".join(row(t) for t in rows_problem) + "</table>" if rows_problem else ""}
{"<h2>What the codes mean</h2><table><tr><th>Code</th><th>Meaning</th><th>What to do</th></tr>" + code_rows + "</table>" if code_rows else ""}
<h2>All other files</h2>
<table><tr><th>Status</th><th>Original location</th><th>Saved as</th><th>Size</th><th>Details</th><th>Code</th></tr>
{other_html}</table>{more_html}
<p class="muted">Damaged files keep their original size; unreadable parts are filled with zeros. Many photos,
videos and documents still open. Byte ranges are listed in {esc(REPORT_CSV)}.</p>
</main></body></html>"""
    return doc.encode("utf-8")


def write_reports(job: RecoveryJob, summary: RecoverySummary) -> tuple[str, str]:
    html_path = os.path.join(job.job_dir, REPORT_HTML)
    csv_path = os.path.join(job.job_dir, REPORT_CSV)
    try:
        _atomic_write(csv_path, build_csv(summary.tasks))
        _atomic_write(html_path, build_html(job, summary))
        return html_path, csv_path
    except OSError:
        # Destination full or gone: keep the report in the user's app data folder.
        from ..logsetup import log_dir

        fallback = log_dir()
        stamp = time.strftime("%Y%m%d-%H%M%S")
        html_path = os.path.join(fallback, f"report-{stamp}.html")
        csv_path = os.path.join(fallback, f"report-{stamp}.csv")
        _atomic_write(csv_path, build_csv(summary.tasks))
        _atomic_write(html_path, build_html(job, summary))
        return html_path, csv_path
