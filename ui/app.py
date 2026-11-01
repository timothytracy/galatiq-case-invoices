"""Invoice review screen. Launch: streamlit run ui/app.py"""

import sys
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st

from tests.fake_llm_eval import AS_OF
from ui.present import (
    INVOICE_FIELDS,
    OUTCOMES,
    agent_trail,
    detail_index,
    format_amount,
    format_money,
    holds,
    invoice_view,
    summarize,
    table_rows,
)
from ui.reviews import REVIEW_PATH, load_reviews, record_review
from ui.runner import (
    DEFAULT_RUN,
    invoice_files,
    load_run,
    parse_payload,
    resolve_path,
    run_one,
    save_run,
)

SCREENS = ("Run or load", "Results", "Invoice detail", "Hold queue", "Metrics")
BADGE = {"approve": "green", "reject": "red", "hold": "orange"}
SEVERITY = {"blocking": "reject", "warning": "hold", "info": "info"}

st.set_page_config(page_title="Invoice review", layout="wide")
st.markdown(
    """
    <style>
    .review-table { width: 100%; border-collapse: collapse; font-size: 14px; }
    .results-table { table-layout: fixed; }
    .review-table th { text-align: left; padding: 8px 10px; border-bottom: 2px solid #d9d3c5;
        font-size: 12px; letter-spacing: 0.03em; text-transform: lowercase; }
    .review-table td { padding: 8px 10px; border-bottom: 1px solid #eee; vertical-align: top; }
    .badge { display: inline-block; padding: 2px 8px; border-radius: 999px; color: white;
        font-size: 12px; font-weight: 700; letter-spacing: 0.04em; }
    .badge-approve { background: #1f7a4d; }
    .badge-reject { background: #9d2c2c; }
    .badge-hold { background: #8a5a12; }
    .badge-info { background: #5c6b73; }
    .file { font-weight: 600; }
    .sub, .null { color: #666; }
    .reason { max-width: 100%; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .num, .nowrap { white-space: nowrap; }
    .missing { color: #777; font-style: italic; }
    #MainMenu, footer, div[data-testid="stToolbar"] { visibility: hidden; }
    header[data-testid="stHeader"] { background: transparent; }
    </style>
    """,
    unsafe_allow_html=True,
)


def main() -> None:
    st.title("Invoice review")
    st.caption(
        f"LangGraph invoice pipeline. Live runs use the fake LLM. Agents are told today is {AS_OF.isoformat()}."
    )
    entries = st.session_state.setdefault("entries", [])
    page = st.sidebar.radio("Screen", SCREENS)
    if page == "Run or load":
        screen_run()
    elif not entries:
        st.info("Process an invoice or load a run file on Run or load.")
    elif page == "Results":
        screen_results(entries)
    elif page == "Invoice detail":
        screen_detail(entries)
    elif page == "Hold queue":
        screen_holds(entries)
    else:
        screen_metrics(entries)


def screen_run() -> None:
    st.header("Run or load")
    left, right = st.columns(2)
    with left:
        st.subheader("Process")
        st.text_input("invoice_path", value="data/invoices/invoice_1001.txt", key="invoice_path")
        one, folder = st.columns(2)
        with one:
            process_one = st.button("Process this invoice", type="primary")
        with folder:
            process_folder = st.button("Process data/invoices")
        st.caption("Payment will not run twice for the same invoice in this session.")
    with right:
        st.subheader("Load")
        st.text_input("run file", value="", placeholder="output/runs/review_run.json", key="run_path")
        load_clicked = st.button("Load run file")
        st.caption("Or upload a JSON file.")
        uploaded = st.file_uploader("run file upload", type=["json"], label_visibility="collapsed")
    if process_one:
        _process_one(st.session_state["invoice_path"])
    if process_folder:
        _process_folder()
    if load_clicked:
        _load_path(st.session_state["run_path"])
    if uploaded is not None:
        token = getattr(uploaded, "file_id", None) or (uploaded.name, uploaded.size)
        if st.session_state.get("upload_id") != token:
            st.session_state["upload_id"] = token
            _load_upload(uploaded.getvalue().decode("utf-8"), uploaded.name)
    _flash()
    entries = st.session_state.get("entries") or []
    if entries:
        counts = summarize(entries)["counts"]
        tally = ", ".join(f"{name} {counts[name]}" for name in OUTCOMES)
        st.write(f"This session has {len(entries)} invoices ({tally}).")
        if st.button("Save run file"):
            destination = save_run(DEFAULT_RUN, entries)
            st.success(f"Saved {destination}")


def screen_results(entries: list[dict]) -> None:
    st.header("Results")
    choice = st.segmented_control(
        "outcome",
        ["all", *OUTCOMES],
        default="all",
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
        badge = f"<span class='badge badge-{escape(outcome)}'>{escape(outcome.upper())}</span>"
        number = row.get("invoice_number") or ""
        invoice = f"<div class='file'>{escape(row['file'])}</div>"
        if number:
            invoice += f"<div class='sub'>{escape(number)}</div>"
        code = escape(row["reason_code"]) if row["reason_code"] else "<span class='null'>null</span>"
        reason = escape(row["reason"]).replace("\n", " ")
        amount = escape(format_amount(row["amount"], row["currency"]))
        body.append(
            "<tr>"
            f"<td>{invoice}</td><td>{badge}</td><td>{code}</td>"
            f"<td class='num'>{amount}</td>"
            f"<td><div class='reason' title='{reason}'>{reason}</div></td>"
            "</tr>"
        )
    header = "<tr><th>invoice</th><th>outcome</th><th>reason_code</th><th>amount</th><th>reason</th></tr>"
    columns = (
        "<colgroup><col style='width:18%'><col style='width:11%'>"
        "<col style='width:16%'><col style='width:14%'><col style='width:41%'></colgroup>"
    )
    st.markdown(
        f"<table class='review-table results-table'>{columns}<thead>{header}</thead>"
        f"<tbody>{''.join(body)}</tbody></table>",
        unsafe_allow_html=True,
    )


def screen_detail(entries: list[dict]) -> None:
    st.header("Invoice detail")
    views = [invoice_view(entry) for entry in entries]
    labels = [view["file"] for view in views]
    choice = st.selectbox("invoice", labels, index=detail_index(views), key="invoice_detail")
    view = views[labels.index(choice)]
    _detail_header(view)
    st.subheader("Extracted invoice")
    _invoice_fields(view.get("invoice") or {})
    st.subheader("Findings")
    _findings_table(view["findings"])
    st.subheader("Agent trail")
    _trail(view["logs"])
    if view["raw_text"]:
        with st.expander("raw_text"):
            st.text(view["raw_text"])


def screen_holds(entries: list[dict]) -> None:
    st.header("Hold queue")
    _flash()
    st.caption(
        "A review is a local note. It does not call payment and it does not change outcome, reason_code, or action."
    )
    st.caption(f"Saved to {REVIEW_PATH.relative_to(ROOT)}")
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
        meta = st.columns(3)
        meta[0].caption("reason_code")
        meta[0].write(view["reason_code"] or "null")
        meta[1].caption("amount")
        meta[1].write(format_amount(view["amount"], view["currency"]) or "null")
        meta[2].caption("payment.status")
        meta[2].write(view["payment_status"])
        st.caption("action")
        st.write(view["action"] or "null")
        existing = saved.get(view["file"])
        if existing:
            st.info(
                f"Recorded reviewer_outcome {existing.get('reviewer_outcome')}. "
                f"pipeline_outcome is still {existing.get('pipeline_outcome')}. "
                f"Note: {existing.get('note')}"
            )
        with st.form(f"review-{index}-{view['file']}"):
            reviewer_outcome = st.radio(
                "reviewer_outcome",
                ["approve", "reject"],
                horizontal=True,
                key=f"choice-{index}-{view['file']}",
            )
            note = st.text_area("note", key=f"note-{index}-{view['file']}")
            submitted = st.form_submit_button("Save review")
        if submitted:
            _save_hold(view, reviewer_outcome, note)


def screen_metrics(entries: list[dict]) -> None:
    st.header("Metrics")
    summary = summarize(entries)
    count_cols = st.columns(3)
    for column, name in zip(count_cols, OUTCOMES):
        column.metric(name, summary["counts"][name])
    dollar_cols = st.columns(3)
    dollar_labels = {
        "approve": "Dollars approved",
        "reject": "Dollars rejected",
        "hold": "Dollars held",
    }
    for column, name in zip(dollar_cols, OUTCOMES):
        column.metric(dollar_labels[name], f"{format_money(summary['dollars'][name])} USD")
    average = summary["average_seconds"]
    shown = "n/a" if average is None else f"{average:.3f} s"
    st.metric("Average time per invoice", shown)
    if summary["timing"] == "elapsed_seconds":
        st.caption(
            f"Mean of elapsed_seconds around each run_invoice call ({summary['timed_invoices']} invoices)."
        )
    elif summary["timing"] == "timestamps":
        st.caption("Mean of the stage-log timestamp span. The log stamps are whole seconds.")
    elif summary["timing"] == "mixed":
        st.caption("Mean of elapsed_seconds where a live run stored it, otherwise the stage-log span.")
    else:
        st.caption("No timing on this run.")
    if summary["other"]:
        st.caption("Not included in the dollar totals. Nothing is converted.")
        for currency, bucket in summary["other"].items():
            parts = [f"{name} {format_money(bucket[name])} {currency}" for name in OUTCOMES if bucket[name]]
            st.write(", ".join(parts))


def _detail_header(view: dict) -> None:
    outcome = view["outcome"] or ""
    columns = st.columns(4)
    columns[0].caption("outcome")
    columns[0].badge(outcome.upper() or "null", color=BADGE.get(outcome, "gray"))
    columns[1].caption("reason_code")
    columns[1].write(view["reason_code"] or "null")
    columns[2].caption("total")
    columns[2].write(format_amount(view["amount"], view["currency"]) or "null")
    columns[3].caption("payment.status")
    paid = view["payment_status"] == "paid"
    columns[3].badge(view["payment_status"], color="green" if paid else "gray")
    st.caption("reason")
    st.write(view["reason"] or "null")
    st.caption("action")
    st.write(view["action"] or "null")
    if paid:
        st.write("Payment happened.")
    else:
        st.write("Payment did not happen.")
    detail = (view.get("payment") or {}).get("detail")
    if detail:
        st.caption("payment.detail")
        st.write(detail)


def _invoice_fields(invoice: dict) -> None:
    if not invoice:
        st.write("No invoice was extracted.")
        return
    cells = []
    for name in INVOICE_FIELDS:
        cells.append(f"<tr><th>{escape(name)}</th><td>{_show(invoice.get(name))}</td></tr>")
    st.markdown(
        f"<table class='review-table'><tbody>{''.join(cells)}</tbody></table>",
        unsafe_allow_html=True,
    )
    warnings = invoice.get("warnings") or []
    st.caption("warnings")
    st.write(", ".join(warnings) if warnings else "[]")
    source = invoice.get("source") or {}
    st.caption(f"source.format {source.get('format')} · source.parser {source.get('parser')}")
    items = invoice.get("line_items") or []
    st.caption("line_items")
    if not items:
        st.write("[]")
        return
    header = "<tr><th>item</th><th>description</th><th>quantity</th><th>unit_price</th><th>amount</th><th>note</th></tr>"
    body = []
    for line in items:
        body.append(
            "<tr>"
            + "".join(
                f"<td>{_show(line.get(name))}</td>"
                for name in ("item", "description", "quantity", "unit_price", "amount", "note")
            )
            + "</tr>"
        )
    st.markdown(
        f"<table class='review-table'><thead>{header}</thead><tbody>{''.join(body)}</tbody></table>",
        unsafe_allow_html=True,
    )


def _findings_table(findings: list) -> None:
    warnings = [item for item in findings if item.get("severity") == "warning"]
    st.caption(f"{len(findings)} findings, {len(warnings)} with severity warning")
    if not findings:
        st.write("No findings.")
        return
    header = "<tr><th>code</th><th>severity</th><th>item</th><th>message</th></tr>"
    body = []
    for finding in findings:
        severity = finding.get("severity") or ""
        kind = SEVERITY.get(severity, "info")
        badge = f"<span class='badge badge-{kind}'>{escape(severity)}</span>"
        body.append(
            "<tr>"
            f"<td class='nowrap'>{escape(finding.get('code') or '')}</td><td>{badge}</td>"
            f"<td>{_show(finding.get('item'))}</td><td>{escape(finding.get('message') or '')}</td>"
            "</tr>"
        )
    st.markdown(
        f"<table class='review-table'><thead>{header}</thead><tbody>{''.join(body)}</tbody></table>",
        unsafe_allow_html=True,
    )


def _trail(logs: list) -> None:
    trail = agent_trail(logs)
    header = "<tr><th>stage</th><th>timestamp</th><th>summary</th></tr>"
    body = []
    for row in trail["rows"]:
        body.append(
            "<tr>"
            f"<td>{escape(row['stage'])}</td>"
            f"<td>{escape(row['timestamp'])}</td>"
            f"<td>{escape(row['summary'])}</td>"
            "</tr>"
        )
    if body:
        st.markdown(
            f"<table class='review-table'><thead>{header}</thead><tbody>{''.join(body)}</tbody></table>",
            unsafe_allow_html=True,
        )
    if trail["missing"]:
        st.markdown(
            "<p class='missing'>Not in the logs: " + escape(", ".join(trail["missing"])) + "</p>",
            unsafe_allow_html=True,
        )
    for row in trail["rows"]:
        with st.expander(f"{row['stage']} {row['timestamp']} result"):
            st.json(row["result"])


def _process_one(text: str) -> None:
    path = resolve_path(text)
    if path.is_dir():
        st.error("invoice_path must be one invoice file.")
        return
    if not path.is_file():
        st.error(f"Invoice file not found: {path}")
        return
    with st.spinner(f"Running {path.name} with the fake LLM…"):
        entry = run_one(path)
    _replace_entry(entry)
    st.session_state["notice"] = f"Processed {path.name} with the fake LLM."


def _process_folder() -> None:
    paths = invoice_files()
    rows = []
    progress = st.progress(0.0)
    status = st.empty()
    try:
        for index, path in enumerate(paths, start=1):
            status.write(f"{path.name} ({index}/{len(paths)})")
            rows.append(run_one(path))
            progress.progress(index / len(paths))
    except Exception as exc:
        st.error(str(exc))
        return
    finally:
        progress.empty()
        status.empty()
    st.session_state["entries"] = rows
    st.session_state["notice"] = f"Processed {len(rows)} invoices with the fake LLM."


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
    st.session_state["entries"] = entries
    st.session_state["notice"] = f"Loaded {len(entries)} invoices from {path}."


def _load_upload(text: str, name: str) -> None:
    import json

    try:
        entries = parse_payload(json.loads(text), name)
    except (json.JSONDecodeError, ValueError) as exc:
        st.error(str(exc))
        return
    st.session_state["entries"] = entries
    st.session_state["notice"] = f"Loaded {len(entries)} invoices from {name}."


def _replace_entry(entry: dict) -> None:
    filename = (entry.get("state") or {}).get("filename")
    kept = [
        row
        for row in st.session_state.get("entries") or []
        if (row.get("state") or {}).get("filename") != filename
    ]
    kept.append(entry)
    kept.sort(key=lambda row: (row.get("state") or {}).get("filename") or "")
    st.session_state["entries"] = kept


def _save_hold(view: dict, reviewer_outcome: str, note: str) -> None:
    try:
        record_review(
            file=view["file"],
            pipeline_outcome=view["outcome"],
            pipeline_reason_code=view["reason_code"],
            pipeline_action=view["action"],
            reviewer_outcome=reviewer_outcome,
            note=note,
            path=REVIEW_PATH,
        )
    except ValueError as exc:
        st.error(str(exc))
        return
    st.session_state["notice"] = (
        f"Recorded {reviewer_outcome} for {view['file']}. "
        "The pipeline outcome was not changed and nothing was paid."
    )
    st.rerun()


def _flash() -> None:
    notice = st.session_state.pop("notice", None)
    if notice:
        st.success(notice)


def _show(value) -> str:
    if value is None:
        return "<span class='null'>null</span>"
    if isinstance(value, bool):
        return "true" if value else "false"
    return escape(str(value))


main()
