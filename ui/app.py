"""Invoice review screen. Launch: streamlit run ui/app.py"""

import json
import os
import sys
import tempfile
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st

from llm_client import MissingAPIKey
from ui.pay import approve_and_pay, extracted_total, saved_result
from ui.present import (
    INVOICE_FIELDS, LINE_FIELDS, OUTCOMES, action_text, agent_trail, blank, can_pay, confirm_label,
    detail_index, format_amount, format_money, invoice_view, label, pay_block, pay_message,
    payment_text, reason_code_text, show_text, summarize, table_rows, concern_text,
)
from ui.reviews import REVIEW_PATH, load_reviews, record_review
from ui.runner import (
    DEFAULT_RUN, PAYMENTS_DB, grok_key_set, invoice_files, is_real_payments_db, load_run,
    mode_label, parse_payload, recording_files, resolve_path, run_one, save_run, temporary_ledger,
)

SCREENS = ("Run or load", "Results", "Invoice detail", "Hold queue", "Metrics")
MODES = ("Live (Grok)", "Offline (fake LLM)", "Replay (recorded Grok)")
REPLAY_BANNER = "Replay of a recorded Grok run. This makes no live model calls."
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
    ".badge-repeat{background:#3d5a80}.badge-blocked{background:#6b3fa0}"
    ".file{font-weight:600}.sub,.note{color:#555}.reason{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}"
    ".tool-call{margin:2px 0 6px}"
    "#MainMenu,footer,div[data-testid='stToolbar']{visibility:hidden}</style>",
    unsafe_allow_html=True,
)

def main() -> None:
    st.title("Invoice review")
    mode = _mode_name(st.sidebar.radio("Mode", MODES, index=0 if grok_key_set() else 1))
    live = mode == "live"
    _offline_releases(live)
    page = st.sidebar.radio("Screen", SCREENS)
    if mode == "replay":
        st.info(REPLAY_BANNER)
    used = mode_label(st.session_state.get("entries") or [])
    if used:
        st.caption(f"This run used {used}.")
    entries = st.session_state.setdefault("entries", [])
    screens = {
        "Run or load": lambda: screen_run(mode),
        "Results": lambda: screen_results(entries),
        "Invoice detail": lambda: screen_detail(entries),
        "Hold queue": lambda: screen_holds(entries, live),
        "Metrics": lambda: screen_metrics(entries),
    }
    if page != "Run or load" and not entries:
        st.info("Process an invoice or load a run file on Run or load.")
    else:
        screens[page]()

def _offline_releases(live: bool) -> None:
    """Offline runs keep the pipeline's release records out of logs/.

    The pipeline writes the record itself. This only points INVOICE_RELEASE_DIR
    at one temporary folder for the session. Live mode leaves the default logs/.
    """
    if live:
        os.environ.pop("INVOICE_RELEASE_DIR", None)
        return
    folder = st.session_state.get("release_dir")
    path = Path(folder) if folder else None
    if path is None or not path.is_dir():
        path = Path(tempfile.mkdtemp(prefix="review-releases-"))
        st.session_state["release_dir"] = str(path)
    os.environ["INVOICE_RELEASE_DIR"] = str(path)

def _mode_name(choice: str) -> str:
    if choice.startswith("Live"):
        return "live"
    if choice.startswith("Replay"):
        return "replay"
    return "offline"


def _session_ledger(live: bool) -> Path:
    """Fallback ledger for a loaded entry that has no ledger of its own.

    A processed result carries the ledger its run wrote. Pay uses that path.
    Offline keeps one temporary file here only for entries without one.
    Live uses payments.db.
    """
    if live:
        return PAYMENTS_DB
    current = st.session_state.get("offline_ledger")
    path = Path(current) if current else None
    if path is None or not path.is_file() or is_real_payments_db(path):
        path = temporary_ledger()
        st.session_state["offline_ledger"] = str(path)
    return path


def _recording_picker() -> Path | None:
    files = recording_files()
    if not files:
        st.error("No committed recordings were found.")
        return None
    names = [path.name for path in files]
    default = names.index("samples.jsonl") if "samples.jsonl" in names else 0
    chosen = st.selectbox("Recording", names, index=default, key="recording")
    return next(path for path in files if path.name == chosen)

def screen_run(mode: str) -> None:
    st.header("Run or load")
    replay = _recording_picker() if mode == "replay" else None
    left, right = st.columns(2)
    with left:
        st.subheader("Process")
        st.text_input("Invoice path", value="data/invoices/invoice_1001.txt", key="invoice_path")
        one, folder = st.columns(2)
        process_one = one.button("Process this invoice", type="primary")
        process_folder = folder.button("Process data/invoices")
        st.caption("Each offline or replay run keeps the temporary ledger that run wrote.")
    with right:
        st.subheader("Load")
        st.text_input("Run file", placeholder="output/runs/review_run.json", key="run_path")
        load_clicked = st.button("Load run file")
        uploaded = st.file_uploader("Upload a JSON run file", type=["json"])
    if process_one:
        _process([resolve_path(st.session_state["invoice_path"])], mode, replay, single=True)
    if process_folder:
        _process(invoice_files(), mode, replay, single=False)
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
        ("Tool calls", lambda: _tool_block(view)),
    ):
        st.subheader(title)
        block()

def screen_holds(entries: list[dict], live: bool) -> None:
    st.header("Hold queue")
    _flash()
    st.caption(
        "Save review is a local note. It does not pay or change the pipeline outcome. "
        f"Saved to {_review_where()}. "
        "Approve and pay is separate, and it asks before paying."
    )
    queued = []
    for entry in entries:
        view = invoice_view(entry)
        if view.get("outcome") == "hold":
            queued.append((entry, view))
    if not queued:
        st.info("No held invoices in this run.")
        return
    try:
        saved = {row.get("file"): row for row in load_reviews(REVIEW_PATH)}
    except (OSError, ValueError, TypeError) as exc:
        st.error(str(exc))
        saved = {}
    for index, (entry, view) in enumerate(queued):
        st.subheader(view["file"])
        _facts(
            ("Reason code", "Amount", "Payment"),
            (reason_code_text(view), format_amount(view["amount"], view["currency"]), payment_text(view["payment_status"])),
        )
        if action_text(view):
            st.caption("Next action")
            st.write(action_text(view))
        concerns = concern_text(view.get("critique_concerns"))
        if concerns:
            st.write(concerns)
        existing = saved.get(view["file"])
        if existing:
            st.info(
                f"Recorded {existing.get('reviewer_outcome')}. "
                f"Pipeline outcome is still {existing.get('pipeline_outcome')}. Note: {existing.get('note')}"
            )
        _show_pay_result(view)
        with st.form(f"review-{index}-{view['file']}"):
            choice = st.radio(
                "Reviewer choice", ["approve", "reject"], index=None, horizontal=True,
                format_func=str.capitalize, key=f"choice-{index}-{view['file']}",
            )
            note = st.text_area("Note", key=f"note-{index}-{view['file']}")
            if st.form_submit_button("Save review"):
                _save_hold(view, choice, note)
        if can_pay(view):
            _pay_form(index, entry, view, live)
        else:
            st.caption(pay_block(view) or "This hold cannot be paid.")

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

def _tool_block(view: dict) -> None:
    shown = view.get("tool_calls") if isinstance(view.get("tool_calls"), dict) else {}
    for note in shown.get("extras") or []:
        if note:
            st.caption(str(note))
    calls = [call for call in (shown.get("calls") or []) if isinstance(call, dict)]
    if not calls:
        st.write("No tool calls.")
        return
    for call in calls:
        with st.container(border=True):
            st.markdown(_tool_heading(call), unsafe_allow_html=True)
            if call.get("repeat"):
                st.caption("Reused an earlier result. This call does not count toward the cap.")
            if call.get("arguments"):
                st.caption(f"Arguments: {call['arguments']}")
            if call.get("arguments_raw"):
                st.caption(f"Raw arguments: {call['arguments_raw']}")
            if call.get("extra"):
                st.caption(str(call["extra"]))
            detail = str(call.get("detail") or "")
            if call.get("blocked") and detail:
                st.warning(detail)
            elif call.get("error") and detail:
                st.error(detail)
            elif detail:
                st.caption(f"Result: {detail}")

def _tool_heading(call: dict) -> str:
    bits = []
    if call.get("who"):
        bits.append(escape(label(call["who"])))
    if call.get("round"):
        bits.append("round " + escape(str(call["round"])))
    if call.get("attempt"):
        bits.append("attempt " + escape(str(call["attempt"])))
    if call.get("name"):
        bits.append(escape(str(call["name"])))
    if call.get("latency"):
        bits.append(escape(str(call["latency"])))
    title = " · ".join(bits) or "Tool call"
    markers = []
    if call.get("repeat"):
        markers.append(_badge("repeat", "Repeat"))
    if call.get("blocked"):
        markers.append(_badge("blocked", "Blocked by cap"))
    if call.get("failed_attempt"):
        markers.append(_badge("info", "Failed attempt"))
    marks = f" {''.join(markers)}" if markers else ""
    return f"<div class='tool-call file'>{title}{marks}</div>"

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

def _process(paths: list[Path], mode: str, replay: Path | None, *, single: bool) -> None:
    if single and not paths[0].is_file():
        st.error("Invoice path must be one invoice file." if paths[0].is_dir() else f"Invoice file not found: {paths[0]}")
        return
    if mode == "replay" and replay is None:
        st.error("Choose a recording. Replay does not call the model.")
        return
    # One ledger for this click. A later click gets its own file, so a second
    # run of the same invoice is not a duplicate of the first. Pay reads the
    # path stored on that result.
    ledger = PAYMENTS_DB if mode == "live" else temporary_ledger()
    rows, progress, status = [], st.progress(0.0), st.empty()
    try:
        for index, path in enumerate(paths, start=1):
            status.write(f"{path.name} ({index}/{len(paths)})")
            rows.append(run_one(path, live=mode == "live", ledger_path=ledger, replay=replay))
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
    notice = f"Processed {paths[0].name}." if single else f"Processed {len(rows)} invoices."
    _notice(notice, error=_replay_errors(rows) if mode == "replay" else None)

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

def _replay_errors(rows: list[dict]) -> str | None:
    """The pipeline's own words when a recording has no answer for an invoice."""
    lines = []
    for entry in rows:
        view = invoice_view(entry)
        reason = str(view.get("reason") or "")
        if "replay miss" in reason.lower():
            lines.append(f"{view['file']}: {reason}")
    return "\n".join(lines) or None


def _notice(notice: str, entries: list[dict] | None = None, error: str | None = None) -> None:
    if entries is not None:
        st.session_state["entries"] = entries
    st.session_state["notice"] = notice
    if error:
        st.session_state["error"] = error
    st.rerun()

def _replace_entry(entry: dict) -> None:
    filename = (entry.get("state") or {}).get("filename")
    kept = [row for row in st.session_state.get("entries") or [] if (row.get("state") or {}).get("filename") != filename]
    kept.append(entry)
    kept.sort(key=lambda row: (row.get("state") or {}).get("filename") or "")
    st.session_state["entries"] = kept

def _review_where() -> Path:
    try:
        return REVIEW_PATH.relative_to(ROOT)
    except ValueError:
        return REVIEW_PATH

def _show_pay_result(view: dict) -> None:
    stored = (st.session_state.get("pay_results") or {}).get(view["file"])
    if not stored:
        return
    kind, text = pay_message(view, stored)
    if kind == "success":
        st.success(text)
    else:
        st.error(text)

def _pay_form(index: int, entry: dict, view: dict, live: bool) -> None:
    file = view["file"]
    reviewer_key = f"pay-reviewer-{index}-{file}"
    note_key = f"pay-note-{index}-{file}"
    confirm_key = f"pay-confirm-{index}-{file}"
    with st.form(f"pay-{index}-{file}"):
        reviewer = st.text_input("Reviewer", key=reviewer_key)
        note = st.text_area("Payment note", key=note_key)
        confirmed = st.checkbox(confirm_label(view), key=confirm_key)
        if st.form_submit_button("Approve and pay"):
            if _pay_hold(entry, view, reviewer, note, confirmed, live):
                st.rerun()

def _two_letters(value: str) -> bool:
    return sum(character.isalpha() for character in (value or "")) >= 2

def _pay_hold(entry: dict, view: dict, reviewer: str, note: str, confirmed: bool, live: bool) -> bool:
    missing = []
    if not _two_letters(reviewer):
        missing.append("A reviewer name is required.")
    if not _two_letters(note):
        missing.append("A note is required.")
    if not confirmed:
        missing.append(f"{confirm_label(view)} before paying.")
    for message in missing:
        st.error(message)
    if missing:
        return False
    pipeline = saved_result(entry)
    ledger = entry.get("ledger_path") or _session_ledger(live)
    result = approve_and_pay(
        pipeline,
        reviewer=reviewer.strip(),
        note=note.strip(),
        amount=extracted_total(pipeline),
        ledger_path=ledger,
        demo=not live,
    )
    _remember_payment(entry, result)
    st.session_state.setdefault("pay_results", {})[view["file"]] = result
    return True


def _remember_payment(entry: dict, result: dict) -> None:
    """Store a successful pay on the session record the screens render."""
    if not isinstance(result, dict) or result.get("payment_status") != "paid":
        return
    state = entry.get("state")
    if not isinstance(state, dict):
        return
    payment = dict(state.get("payment") or {})
    incoming = result.get("payment") if isinstance(result.get("payment"), dict) else {}
    payment.update({key: value for key, value in incoming.items() if value is not None})
    payment["status"] = "paid"
    invoice = state.get("invoice") if isinstance(state.get("invoice"), dict) else {}
    if payment.get("amount") in (None, ""):
        payment["amount"] = invoice.get("total")
    if not payment.get("currency"):
        payment["currency"] = invoice.get("currency")
    state["payment"] = payment

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
    error = st.session_state.pop("error", None)
    if notice:
        st.success(notice)
    if error:
        st.error(error)

def _badge(kind: str, text: str) -> str:
    return f"<span class='badge badge-{escape(kind)}'>{escape(text)}</span>"

def _table(headers: list[str], body: list[str]) -> None:
    head = "".join(f"<th>{escape(header)}</th>" for header in headers)
    st.markdown(
        f"<table class='review-table'><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>",
        unsafe_allow_html=True,
    )

main()
