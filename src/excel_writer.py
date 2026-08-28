"""
excel_writer.py — turns an LBOResult (from lbo_engine.py) into a formatted,
multi-tab .xlsx workbook. This file only formats numbers that were already
computed elsewhere — it does no math of its own beyond simple display
transforms (e.g., rounding for display).
"""

from __future__ import annotations

from typing import Optional

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from edgar import CompanyFinancials
from lbo_engine import LBOAssumptions, LBOResult

NAVY = "1F3864"
LIGHT_GRAY = "F2F2F2"
WHITE = "FFFFFF"
RED_FLAG = "FFC7CE"

HEADER_FONT = Font(color=WHITE, bold=True, size=11)
HEADER_FILL = PatternFill(fill_type="solid", fgColor=NAVY)
TITLE_FONT = Font(bold=True, size=14, color=NAVY)
SUBTITLE_FONT = Font(italic=True, size=9, color="808080")
BOLD = Font(bold=True)
THIN_BORDER = Border(bottom=Side(style="thin", color="BFBFBF"))
BAND_FILL = PatternFill(fill_type="solid", fgColor=LIGHT_GRAY)

USD0 = '#,##0;(#,##0)'
USD1 = '#,##0.0;(#,##0.0)'
PCT1 = '0.0%'
MULT1 = '0.00"x"'


def _style_header_row(ws: Worksheet, row: int, ncols: int, start_col: int = 1):
    for c in range(start_col, start_col + ncols):
        cell = ws.cell(row=row, column=c)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(horizontal="center" if c > start_col else "left")


def _title(ws: Worksheet, text: str, subtitle: str = ""):
    ws["A1"] = text
    ws["A1"].font = TITLE_FONT
    if subtitle:
        ws["A2"] = subtitle
        ws["A2"].font = SUBTITLE_FONT


def _autofit(ws: Worksheet, widths: dict[str, int]):
    for col, w in widths.items():
        ws.column_dimensions[col].width = w


def _write_assumptions_sheet(ws: Worksheet, company: CompanyFinancials, ltm_revenue: float,
                              ltm_ebitda: float, a: LBOAssumptions):
    _title(ws, f"{company.company_name} ({company.ticker}) — LBO Assumptions",
           f"CIK {company.cik} · SEC EDGAR (free, public data) · built by an autonomous Claude Code agent")
    row = 4
    ws.cell(row=row, column=1, value="LTM Financials (from SEC 10-K filings)").font = BOLD
    row += 1
    for label, val, fmt in [
        ("LTM Revenue", ltm_revenue, USD0),
        ("LTM EBITDA", ltm_ebitda, USD0),
        ("LTM EBITDA Margin", (ltm_ebitda / ltm_revenue if ltm_revenue else 0), PCT1),
    ]:
        ws.cell(row=row, column=1, value=label)
        c = ws.cell(row=row, column=2, value=val)
        c.number_format = fmt
        row += 1

    if company.data_quality_flags:
        row += 1
        ws.cell(row=row, column=1, value="Data quality flags").font = BOLD
        row += 1
        for f in company.data_quality_flags:
            ws.cell(row=row, column=1, value=f"⚠ {f}").fill = PatternFill(fill_type="solid", fgColor=RED_FLAG)
            row += 1

    row += 1
    ws.cell(row=row, column=1, value="Deal Assumptions").font = BOLD
    row += 1
    deal_rows = [
        ("Entry EV / EBITDA multiple", a.entry_ev_multiple, MULT1),
        ("Exit EV / EBITDA multiple", a.exit_ev_multiple, MULT1),
        ("Hold period (years)", a.hold_period_years, '0'),
        ("Total leverage (x EBITDA)", a.leverage_multiple, MULT1),
        ("Revenue growth (annual)", a.revenue_growth_rate, PCT1),
        ("EBITDA margin (held flat unless noted)", a.ebitda_margin, PCT1),
        ("D&A (% of revenue)", a.da_pct_revenue, PCT1),
        ("CapEx (% of revenue)", a.capex_pct_revenue, PCT1),
        ("NWC change (% of revenue growth $)", a.nwc_pct_of_revenue_change, PCT1),
        ("Cash tax rate", a.tax_rate, PCT1),
        ("Transaction fees (% of EV)", a.transaction_fee_pct_of_ev, PCT1),
        ("Financing fees (% of new debt)", a.financing_fee_pct_of_debt, PCT1),
        ("Cash sweep %", a.cash_sweep_pct, PCT1),
        ("Management rollover (% of equity value)", a.management_rollover_pct, PCT1),
    ]
    for label, val, fmt in deal_rows:
        ws.cell(row=row, column=1, value=label)
        c = ws.cell(row=row, column=2, value=val)
        c.number_format = fmt
        row += 1

    row += 1
    ws.cell(row=row, column=1, value="Debt Structure").font = BOLD
    row += 1
    headers = ["Tranche", "% of Total Debt", "Interest Rate", "Mandatory Amort (%/yr of principal)", "Priority"]
    for i, h in enumerate(headers, start=1):
        ws.cell(row=row, column=i, value=h)
    _style_header_row(ws, row, len(headers))
    row += 1
    for t in a.tranches:
        ws.cell(row=row, column=1, value=t.name)
        ws.cell(row=row, column=2, value=t.pct_of_total_debt).number_format = PCT1
        ws.cell(row=row, column=3, value=t.interest_rate).number_format = PCT1
        ws.cell(row=row, column=4, value=t.mandatory_amort_pct_of_principal).number_format = PCT1
        ws.cell(row=row, column=5, value=t.priority)
        row += 1

    _autofit(ws, {"A": 42, "B": 16, "C": 16, "D": 30, "E": 10})


def _write_sources_uses_sheet(ws: Worksheet, result: LBOResult):
    _title(ws, "Sources & Uses")
    su = result.sources_uses
    row = 4
    ws.cell(row=row, column=1, value="Uses").font = BOLD
    ws.cell(row=row, column=3, value="Sources").font = BOLD
    row += 1
    uses = [
        ("Purchase Enterprise Value", su.purchase_enterprise_value),
        ("Transaction Fees", su.transaction_fees),
        ("Financing Fees", su.financing_fees),
    ]
    sources = [
        ("New Debt", su.total_new_debt),
        ("Management Rollover Equity", su.management_rollover),
        ("Sponsor Equity", su.sponsor_equity),
    ]
    start = row
    for i, (label, val) in enumerate(uses):
        ws.cell(row=start + i, column=1, value=label)
        ws.cell(row=start + i, column=2, value=val).number_format = USD0
    ws.cell(row=start + len(uses), column=1, value="Total Uses").font = BOLD
    ws.cell(row=start + len(uses), column=2, value=su.total_uses).number_format = USD0
    ws.cell(row=start + len(uses), column=2).font = BOLD

    for i, (label, val) in enumerate(sources):
        ws.cell(row=start + i, column=3, value=label)
        ws.cell(row=start + i, column=4, value=val).number_format = USD0
    ws.cell(row=start + len(sources), column=3, value="Total Sources").font = BOLD
    ws.cell(row=start + len(sources), column=4, value=su.total_sources).number_format = USD0
    ws.cell(row=start + len(sources), column=4).font = BOLD

    check_row = start + max(len(uses), len(sources)) + 2
    ws.cell(row=check_row, column=1, value="Check (Sources − Uses, should be 0):").font = BOLD
    diff = round(su.total_sources - su.total_uses, 2)
    dc = ws.cell(row=check_row, column=2, value=diff)
    dc.number_format = USD0
    dc.font = BOLD
    if abs(diff) > 0.5:
        dc.fill = PatternFill(fill_type="solid", fgColor=RED_FLAG)

    _autofit(ws, {"A": 28, "B": 16, "C": 26, "D": 16})


def _write_projections_sheet(ws: Worksheet, result: LBOResult):
    _title(ws, "Operating Model & Debt Paydown")
    tranche_names = list(result.projections[0].tranche_ending_balances.keys()) if result.projections else []
    headers = ["", "Entry"] + [f"Year {p.year}" for p in result.projections]
    row_labels = [
        ("Revenue", "revenue", USD0),
        ("EBITDA", "ebitda", USD0),
        ("EBITDA Margin", "ebitda_margin", PCT1),
        ("D&A", "d_and_a", USD0),
        ("EBIT", "ebit", USD0),
        ("Interest Expense", "total_interest_expense", USD0),
        ("Cash Taxes", "cash_taxes", USD0),
        ("CapEx", "capex", USD0),
        ("Increase in NWC", "nwc_change", USD0),
        ("Levered FCF (pre-sweep)", "levered_free_cash_flow_pre_sweep", USD0),
        ("Mandatory Amortization", "mandatory_amortization", USD0),
        ("Cash Swept to Debt Paydown", "cash_swept", USD0),
        ("Total Debt (ending)", "total_debt_ending", USD0),
        ("Cash Balance (ending)", "cash_balance_ending", USD0),
    ]

    r = 4
    for i, h in enumerate(headers, start=1):
        ws.cell(row=r, column=i, value=h)
    _style_header_row(ws, r, len(headers))
    r += 1

    for label, attr, fmt in row_labels:
        ws.cell(row=r, column=1, value=label).font = BOLD
        ws.cell(row=r, column=2, value=None)  # "Entry" column intentionally blank except revenue/EBITDA context
        for j, p in enumerate(result.projections, start=3):
            val = getattr(p, attr)
            c = ws.cell(row=r, column=j, value=val)
            c.number_format = fmt
            if attr == "levered_free_cash_flow_pre_sweep" and val < 0:
                c.fill = PatternFill(fill_type="solid", fgColor=RED_FLAG)
        if r % 2 == 0:
            for c in range(1, len(headers) + 1):
                ws.cell(row=r, column=c).fill = BAND_FILL
        r += 1

    r += 1
    ws.cell(row=r, column=1, value="Debt Balance by Tranche").font = BOLD
    r += 1
    for i, h in enumerate(headers, start=1):
        ws.cell(row=r, column=i, value=h)
    _style_header_row(ws, r, len(headers))
    r += 1
    for name in tranche_names:
        ws.cell(row=r, column=1, value=name)
        for j, p in enumerate(result.projections, start=3):
            c = ws.cell(row=r, column=j, value=p.tranche_ending_balances[name])
            c.number_format = USD0
        r += 1

    widths = {"A": 30, "B": 8}
    for i in range(3, len(headers) + 1):
        widths[get_column_letter(i)] = 13
    _autofit(ws, widths)


def _write_returns_sheet(ws: Worksheet, result: LBOResult, sensitivity: Optional[dict] = None):
    _title(ws, "Exit & Sponsor Returns")
    row = 4
    exit_gross_debt = result.exit_net_debt + result.exit_cash_balance
    lines = [
        ("Exit-Year EBITDA", result.exit_ebitda, USD0),
        ("Exit Enterprise Value", result.exit_enterprise_value, USD0),
        ("Less: Exit Debt", -exit_gross_debt, USD0),
        ("Add: Exit Cash Balance", result.exit_cash_balance, USD0),
        ("Exit Equity Value", result.exit_equity_value, USD0),
        (None, None, None),
        ("Initial Sponsor Equity", result.sources_uses.sponsor_equity, USD0),
        ("MOIC", result.sponsor_moic, MULT1),
        ("IRR", result.sponsor_irr, PCT1),
    ]
    for label, val, fmt in lines:
        if label is None:
            row += 1
            continue
        ws.cell(row=row, column=1, value=label).font = BOLD if label in ("Exit Equity Value", "MOIC", "IRR") else Font()
        c = ws.cell(row=row, column=2, value=val)
        c.number_format = fmt
        row += 1

    if result.warnings:
        row += 1
        ws.cell(row=row, column=1, value="Warnings").font = BOLD
        row += 1
        for w in result.warnings:
            ws.cell(row=row, column=1, value=f"⚠ {w}").fill = PatternFill(fill_type="solid", fgColor=RED_FLAG)
            row += 1

    if sensitivity:
        row += 2
        ws.cell(row=row, column=1, value="Sensitivity: IRR by Leverage (rows) x Exit Multiple (cols)").font = BOLD
        row += 1
        for j, ex in enumerate(sensitivity["exit_multiples"], start=2):
            ws.cell(row=row, column=j, value=f"{ex:.1f}x")
        _style_header_row(ws, row, len(sensitivity["exit_multiples"]), start_col=2)
        for i, lev in enumerate(sensitivity["leverage_multiples"]):
            row += 1
            ws.cell(row=row, column=1, value=f"{lev:.1f}x leverage").font = BOLD
            for j, val in enumerate(sensitivity["irr"][i], start=2):
                c = ws.cell(row=row, column=j, value=val)
                c.number_format = PCT1

        row += 2
        ws.cell(row=row, column=1, value="Sensitivity: MOIC by Leverage (rows) x Exit Multiple (cols)").font = BOLD
        row += 1
        for j, ex in enumerate(sensitivity["exit_multiples"], start=2):
            ws.cell(row=row, column=j, value=f"{ex:.1f}x")
        _style_header_row(ws, row, len(sensitivity["exit_multiples"]), start_col=2)
        for i, lev in enumerate(sensitivity["leverage_multiples"]):
            row += 1
            ws.cell(row=row, column=1, value=f"{lev:.1f}x leverage").font = BOLD
            for j, val in enumerate(sensitivity["moic"][i], start=2):
                c = ws.cell(row=row, column=j, value=val)
                c.number_format = MULT1

    _autofit(ws, {"A": 32, "B": 14, "C": 14, "D": 14, "E": 14, "F": 14})


def _write_memo_sheet(ws: Worksheet, memo_text: Optional[str]):
    _title(ws, "Investment Memo", "Written by the Claude agent from the model outputs above")
    ws["A4"] = memo_text or "(No memo generated for this run.)"
    ws["A4"].alignment = Alignment(wrap_text=True, vertical="top")
    ws.row_dimensions[4].height = 400
    ws.column_dimensions["A"].width = 110


def build_workbook(company: CompanyFinancials, ltm_revenue: float, ltm_ebitda: float,
                    assumptions: LBOAssumptions, result: LBOResult,
                    sensitivity: Optional[dict] = None, memo_text: Optional[str] = None) -> Workbook:
    wb = Workbook()
    ws1 = wb.active
    ws1.title = "Assumptions"
    _write_assumptions_sheet(ws1, company, ltm_revenue, ltm_ebitda, assumptions)

    ws2 = wb.create_sheet("Sources & Uses")
    _write_sources_uses_sheet(ws2, result)

    ws3 = wb.create_sheet("Operating Model")
    _write_projections_sheet(ws3, result)

    ws4 = wb.create_sheet("Returns")
    _write_returns_sheet(ws4, result, sensitivity)

    ws5 = wb.create_sheet("Investment Memo")
    _write_memo_sheet(ws5, memo_text)

    for ws in wb.worksheets:
        ws.sheet_view.showGridLines = False

    return wb


def save_workbook(wb: Workbook, path: str) -> str:
    wb.save(path)
    return path
