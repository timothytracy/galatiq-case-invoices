"""Invoice review screen. Launch: streamlit run ui/app.py"""

import json
import sys
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st

from llm_client import MissingAPIKey
from ui.present import (
    INVOICE_FIELDS, LINE_FIELDS, OUTCOMES, action_text, agent_trail, blank, detail_index,
    format_amount, format_money, holds, invoice_view, label, payment_text, reason_code_text,
    show_text, summarize, table_rows,
)
from ui.reviews import REVIEW_PATH, load_reviews, record_review
from ui.runner import (
    DEFAULT_RUN, grok_key_set, invoice_files, load_run, mode_label, parse_payload, resolve_path,
    run_one, save_run,
)

SCREENS = ("Run or load", "Results", "Invoice detail", "Hold queue", "Metrics")
MODES = ("Live (Grok)", "Offline (fake LLM)")
BADGE = {"approve": "green", "reject": "red", "hold": "orange", "blocking": "red", "warning": "orange", "info": "gray"}
DOLLARS = {"approve": "Dollars approved", "reject": "Dollars rejected", "hold": "Dollars held"}

st.set_page_config(page_title="Invoice review", layout="wide")
st.markdown(
    "<style>.review-table{width:100%;border-collapse:collapse;font-size:14px}"
    ".review-table th{text-align:left;padding:8px 10px;border-bottom:2px solid #d9d3c5}"
    ".review-table td{padding:8px 10px;border-bottom:1px solid #eee;vertical-align:top}"
    ".badge{display:inline-block;padding:2px 8px;border-radius:999px;color:#fff;font-size:12px;font-weight:700}"
    ".badge-approve{background:#1f7a4d}.badge-reject,.badge-blocking{background:#9d2c2c}"
    ".badge-hold,.badge-warning{background:#8a5a12}.badge-info,.badge-gray{background:#5c6b73}"
    ".file{font-weight:600}.sub,.note{color:#555}.reason{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}"
    "#MainMenu,footer,div[data-testid='stToolbar']{visibility:hidden}</style>",
    unsafe_allow_html=True,
)

def main() -> None:
    st.title("Invoice review")
    live = st.sidebar.radio("Mode", MODES, index=0 if grok_key_set() else 1).startswith("Live")
    page = st.sidebar.radio("Screen", SCREENS)
    used = mode_label(st.session_state.get("entries") or [])
    if used:
        st.caption(f"This run used {used}.")
    entries = st.session_state.setdefault("entries", [])
    screens = {
        "Run or load": lambda: screen_run(live),
        "Results": lambda: screen_results(entries),
        "Invoice detail": lambda: screen_detail(entries),
        "Hold queue": lambda: screen_holds(entries),
        "Metrics": lambda: screen_metrics(entries),
    }
    if page != "Run or load" and not entries:
        st.info("Process an invoice or load a run file on Run or load.")
    else:
        screens[page]()

def screen_run(live: bool) -> None:
    st.header("Run or load")
    left, right = st.columns(2)
    with left:
        st.subheader("Process")
        st.text_input("Invoice path", value="data/invoices/invoice_1001.txt", key="invoice_path")
        one, folder = st.columns(2)
        process_one = one.button("Process this invoice", type="primary")
        process_folder = folder.button("Process data/invoices")
        st.caption("Payment will not run twice for the same invoice in this session.")
    with right:
        st.subheader("Load")
        st.text_input("Run file", placeholder="output/runs/review_run.json", key="run_path")
        load_clicked = st.button("Load run file")
        uploaded = st.file_uploader("Upload a JSON run file", type=["json"])
    if process_one:
        _process([resolve_path(st.session_state["invoice_path"])], live, single=True)
    if process_folder:
        _process(invoice_files(), live, single=False)
    if load_clicked:
        _load_path(st.session_state.get("run_path") or "")
    token = None if uploaded is None else getattr(uploaded, "file_id", uploaded.name)
    if token is not None and st.session_state.get("upload_id") != token:
        st.session_state["upload_id"] = token
        _load_text(uploaded.getvalue().decode("utf-8"), uploaded.name)
    _flash()
    entries = st.session_state.get("entries") or []
    if not entries:
        return
    counts = summarize(entries)["counts"]
    tally = ", ".join(f"{name} {counts[name]}" for name in OUTCOMES)
    st.write(f"This session has {len(entries)} invoices ({tally}).")
    if st.button("Save run file"):
        st.success(f"Saved {save_run(DEFAULT_RUN, entries)}")

def screen_results(entries: list[dict]) -> None:
    st.header("Results")
    choice = st.segmented_control(
        "Outcome", ["all", *OUTCOMES], default="all",
        format_func=lambda value: "All" if value == "all" else value.upper(),
    )
    if isinstance(choice, list):
        choice = choice[0] if choice else "all"
    rows = table_rows(entries, None if choice in (None, "all") else choice)
    st.caption(f"{len(rows)} of {len(entries)}")
    if not rows:
        st.info("No invoices for this outcome.")
        return
    body = []
    for row in rows:
        outcome = row["outcome"] or ""
        number = f"<div class='sub'>{escape(row['invoice_number'])}</div>" if row.get("invoice_number") else ""
        reason = escape((row["reason"] or "").replace("\n", " "))
        body.append(
            f"<tr><td><div class='file'>{escape(row['file'])}</div>{number}</td>"
            f"<td>{_badge(outcome, outcome.upper())}</td><td>{escape(reason_code_text(row))}</td>"
            f"<td>{escape(format_amount(row['amount'], row['currency']))}</td>"
            f"<td><div class='reason' title='{reason}'>{reason}</div></td></tr>"
        )
    _table(["Invoice", "Outcome", "Reason code", "Amount", "Reason"], body)

def _facts(titles, values) -> None:
    columns = st.columns(len(titles))
    for column, title, value in zip(columns, titles, values):
        column.caption(title)
        if isinstance(value, tuple):
            column.badge(value[0], color=value[1])
        else:
            column.write(value)

def screen_detail(entries: list[dict]) -> None:
    st.header("Invoice detail")
    views = [invoice_view(entry) for entry in entries]
    names = [view["file"] for view in views]
    view = views[names.index(st.selectbox("Invoice", names, index=detail_index(views), key="invoice_detail"))]
    outcome, paid = view["outcome"] or "", view["payment_status"] == "paid"
    _facts(
        ("Outcome", "Reason code", "Total", "Payment"),
        (
            (outcome.upper(), BADGE.get(outcome, "gray")),
            reason_code_text(view),
            format_amount(view["amount"], view["currency"]),
            (payment_text(view["payment_status"]), "green" if paid else "gray"),
        ),
    )
    for caption, value in (
        ("Reason", view["reason"]),
        ("Next action", action_text(view)),
        ("Payment detail", (view.get("payment") or {}).get("detail")),
    ):
        if not blank(value):
            st.caption(caption)
            st.write(value)
    st.write("Payment happened." if paid else "Payment did not happen.")
    for title, block in (
        ("Extracted invoice", lambda: _invoice_block(view.get("invoice") or {})),
        ("Findings", lambda: _findings_block(view["findings"])),
        ("Agent trail", lambda: _trail_block(view)),
    ):
        st.subheader(title)
        block()

def screen_holds(entries: list[dict]) -> None:
    st.header("Hold queue")
    _flash()
    st.caption(
        "A review is a local note. It does not call payment or change the pipeline outcome. "
        f"Saved to {REVIEW_PATH.relative_to(ROOT)}."
    )
    queued = holds(entries)
    if not queued:
        st.info("No held invoices in this run.")
        return
    try:
        saved = {row.get("file"): row for row in load_reviews(REVIEW_PATH)}
    except (OSError, ValueError, TypeError) as exc:
        st.error(str(exc))
        saved = {}
    for index, view in enumerate(queued):
        st.subheader(view["file"])
        _facts(
            ("Reason code", "Amount", "Payment"),
            (reason_code_text(view), format_amount(view["amount"], view["currency"]), payment_text(view["payment_status"])),
        )
        if action_text(view):
            st.caption("Next action")
            st.write(action_text(view))
        existing = saved.get(view["file"])
        if existing:
            st.info(
                f"Recorded {existing.get('reviewer_outcome')}. "
                f"Pipeline outcome is still {existing.get('pipeline_outcome')}. Note: {existing.get('note')}"
            )
        with st.form(f"review-{index}-{view['file']}"):
            choice = st.radio(
                "Reviewer choice", ["approve", "reject"], index=None, horizontal=True,
                format_func=str.capitalize, key=f"choice-{index}-{view['file']}",
            )
            note = st.text_area("Note", key=f"note-{index}-{view['file']}")
            if st.form_submit_button("Save review"):
                _save_hold(view, choice, note)

def screen_metrics(entries: list[dict]) -> None:
    st.header("Metrics")
    summary = summarize(entries)
    for column, name in zip(st.columns(3), OUTCOMES):
        column.metric(label(name), summary["counts"][name])
    for column, name in zip(st.columns(3), OUTCOMES):
        column.metric(DOLLARS[name], f"{format_money(summary['dollars'][name])} USD")
    average = summary["average_seconds"]
    st.metric("Average time per invoice", "n/a" if average is None else f"{average:.3f} s")
    if summary["timed_invoices"]:
        st.caption(f"Average across {summary['timed_invoices']} invoices.")
    if not summary["other"]:
        return
    st.caption("Not included in the dollar totals. Nothing is converted.")
    for currency, bucket in summary["other"].items():
        st.write(", ".join(f"{name} {format_money(bucket[name])} {currency}" for name in OUTCOMES if bucket[name]))

def _invoice_block(invoice: dict) -> None:
    if not invoice:
        st.write("No invoice was extracted.")
        return
    fields = [{"Field": label(name), "Value": show_text(invoice[name])} for name in INVOICE_FIELDS if not blank(invoice.get(name))]
    if fields:
        st.dataframe(fields, hide_index=True, width="stretch")
    if invoice.get("warnings"):
        st.caption("Warnings")
        st.write(show_text(invoice["warnings"]))
    source = invoice.get("source") or {}
    bits = [f"{label(key)} {source[key]}" for key in ("format", "parser") if not blank(source.get(key))]
    if bits:
        st.caption(" · ".join(bits))
    st.caption("Line items")
    items = [
        {label(name): "" if blank(line.get(name)) else show_text(line[name]) for name in LINE_FIELDS}
        for line in invoice.get("line_items") or []
    ]
    if items:
        st.dataframe(items, hide_index=True, width="stretch")
    else:
        st.write("No line items.")

def _findings_block(findings: list) -> None:
    warnings = sum(1 for item in findings if item.get("severity") == "warning")
    st.caption(f"{len(findings)} findings, {warnings} with severity warning")
    if not findings:
        st.write("No findings.")
        return
    _table(["Code", "Severity", "Item", "Message"], [
        f"<tr><td>{escape(item.get('code') or '')}</td><td>{_badge(item.get('severity') or '', item.get('severity') or '')}</td>"
        f"<td>{'' if blank(item.get('item')) else escape(str(item['item']))}</td>"
        f"<td>{escape(item.get('message') or '')}</td></tr>"
        for item in findings
    ])

def _trail_block(view: dict) -> None:
    trail = agent_trail(view["logs"], view["amount"])
    if trail["rows"]:
        st.dataframe(
            [{"Stage": label(row["stage"]), "Time": row["timestamp"], "Summary": row["summary"]} for row in trail["rows"]],
            hide_index=True, width="stretch",
        )
    if "critique" not in {row["stage"] for row in trail["rows"]}:
        st.caption(trail["critique"])
    if trail["missing"]:
        st.caption("Not in the logs: " + ", ".join(label(stage) for stage in trail["missing"]))
    if view["raw_text"]:
        with st.expander("Raw text"):
            st.text(view["raw_text"])

def _process(paths: list[Path], live: bool, *, single: bool) -> None:
    if single and not paths[0].is_file():
        st.error("Invoice path must be one invoice file." if paths[0].is_dir() else f"Invoice file not found: {paths[0]}")
        return
    rows, progress, status = [], st.progress(0.0), st.empty()
    try:
        for index, path in enumerate(paths, start=1):
            status.write(f"{path.name} ({index}/{len(paths)})")
            rows.append(run_one(path, live=live))
            progress.progress(index / len(paths))
    except (MissingAPIKey, Exception) as exc:
        st.error(str(exc))
        return
    finally:
        progress.empty()
        status.empty()
    if single:
        _replace_entry(rows[0])
    else:
        st.session_state["entries"] = rows
    _notice(f"Processed {paths[0].name}." if single else f"Processed {len(rows)} invoices.")

def _load_path(text: str) -> None:
    if not text.strip():
        st.error("Enter a path to a run file or a directory of logs.")
        return
    path = resolve_path(text)
    try:
        entries = load_run(path)
    except (OSError, ValueError) as exc:
        st.error(str(exc))
        return
    _notice(f"Loaded {len(entries)} invoices from {path.name}.", entries)

def _load_text(text: str, name: str) -> None:
    try:
        entries = parse_payload(json.loads(text), name)
    except (json.JSONDecodeError, ValueError) as exc:
        st.error(str(exc))
        return
    _notice(f"Loaded {len(entries)} invoices from {name}.", entries)

def _notice(notice: str, entries: list[dict] | None = None) -> None:
    if entries is not None:
        st.session_state["entries"] = entries
    st.session_state["notice"] = notice
    st.rerun()

def _replace_entry(entry: dict) -> None:
    filename = (entry.get("state") or {}).get("filename")
    kept = [row for row in st.session_state.get("entries") or [] if (row.get("state") or {}).get("filename") != filename]
    kept.append(entry)
    kept.sort(key=lambda row: (row.get("state") or {}).get("filename") or "")
    st.session_state["entries"] = kept

def _save_hold(view: dict, reviewer_outcome: str | None, note: str) -> None:
    if reviewer_outcome not in {"approve", "reject"}:
        st.error("Pick approve or reject before saving.")
        return
    try:
        record_review(
            file=view["file"], pipeline_outcome=view["outcome"], pipeline_reason_code=view["reason_code"],
            pipeline_action=view["action"], reviewer_outcome=reviewer_outcome, note=note, path=REVIEW_PATH,
        )
    except ValueError as exc:
        st.error(str(exc))
        return
    _notice(f"Recorded {reviewer_outcome} for {view['file']}. The pipeline outcome was not changed and nothing was paid.")

def _flash() -> None:
    notice = st.session_state.pop("notice", None)
    if notice:
        st.success(notice)

def _badge(kind: str, text: str) -> str:
    return f"<span class='badge badge-{escape(kind)}'>{escape(text)}</span>"

def _table(headers: list[str], body: list[str]) -> None:
    head = "".join(f"<th>{escape(header)}</th>" for header in headers)
    st.markdown(
        f"<table class='review-table'><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>",
        unsafe_allow_html=True,
    )

main()
