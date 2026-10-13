"""Salary slip PDF (A4 portrait) — shop logo + details, employee block with photo, attendance,
earnings/deductions side by side, net pay in words, advance status, signatures."""
import io
import base64
import datetime
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image, KeepTogether
from ..utils import rupees_in_words
from ..defaults import company_settings
from ..assets import HAYAT_EMBLEM_B64, HAYAT_NAME, HAYAT_PHONES

NAVY = colors.HexColor("#14213D")
ACCENT = colors.HexColor("#0E9F6E")
LIGHT = colors.HexColor("#F3F5F9")
LINE = colors.HexColor("#D5DBE5")
GREY = colors.HexColor("#5B6475")
SALARY_TYPES = {"monthly": "Monthly", "daily": "Daily wages", "hourly": "Hourly"}


def _money(x):
    return f"{float(x or 0):,.2f}"


def _ps(name, size=9, bold=False, color=colors.black, align=0, leading=None):
    return ParagraphStyle(name, fontName="Helvetica-Bold" if bold else "Helvetica", fontSize=size,
                          leading=leading or size * 1.25, textColor=color, alignment=align)


def _img(data_b64_or_bytes, max_w, max_h):
    try:
        raw = data_b64_or_bytes if isinstance(data_b64_or_bytes, bytes) else base64.b64decode(data_b64_or_bytes)
        w, h = ImageReader(io.BytesIO(raw)).getSize()
        s = min(max_w / w, max_h / h)
        return Image(io.BytesIO(raw), width=w * s, height=h * s)
    except Exception:
        return None


def _logo(company, max_w, max_h):
    logo = company.get("logo") or ""
    if "," not in logo:
        return None
    try:
        return _img(base64.b64decode(logo.split(",", 1)[1]), max_w, max_h)
    except Exception:
        return None


def _fmt_date(s):
    try:
        return datetime.date.fromisoformat(s).strftime("%d-%m-%Y")
    except Exception:
        return s or "-"


def _roundoff(slip):
    exact = round(float(slip["gross"]) - float(slip["total_deductions"]), 2)
    diff = round(float(slip["net"]) - exact, 2)
    return f"  (rounded off {'+' if diff > 0 else ''}{diff:.2f})" if abs(diff) >= 0.01 and exact > 0 else ""


def payslip_pdf(company, emp, slip):
    buf = io.BytesIO()
    W, H = A4
    m = 12 * mm
    CW = W - 2 * m
    month_label = datetime.date.fromisoformat(slip["month"] + "-01").strftime("%B %Y")
    settings = company_settings(company)

    def decorate(c, doc):
        c.saveState()
        c.setFillColor(NAVY)
        c.rect(0, H - 4 * mm, W, 4 * mm, fill=1, stroke=0)
        c.setFillColor(ACCENT)
        c.rect(0, H - 5.2 * mm, W, 1.2 * mm, fill=1, stroke=0)
        # footer: Hayat Enterprises
        c.setStrokeColor(LINE)
        c.line(m, 14 * mm, W - m, 14 * mm)
        try:
            c.drawImage(ImageReader(io.BytesIO(base64.b64decode(HAYAT_EMBLEM_B64))), m, 6.2 * mm, 6 * mm, 6 * mm, mask="auto")
        except Exception:
            pass
        c.setFillColor(GREY)
        c.setFont("Helvetica", 7)
        c.drawString(m + 7.5 * mm, 9.6 * mm, f"Powered by Hayat HRMS — {HAYAT_NAME}, Nagpur")
        c.drawString(m + 7.5 * mm, 6.6 * mm, f"CCTV · Biometrics · Security products   |   {HAYAT_PHONES}")
        c.drawRightString(W - m, 9.6 * mm, f"Slip ref: {(slip.get('id') or '')[:8].upper()}")
        c.drawRightString(W - m, 6.6 * mm, f"Printed {datetime.datetime.now():%d-%m-%Y %H:%M}")
        c.restoreState()

    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=m, rightMargin=m, topMargin=10 * mm, bottomMargin=17 * mm,
                            title=f"Salary slip {emp.get('name', '')} {month_label}", author=company.get("name", ""))
    story = []

    # ---------- shop header ----------
    logo = _logo(company, 34 * mm, 22 * mm)
    contact = " · ".join(x for x in [company.get("phone") and f"Ph: {company['phone']}", company.get("email")] if x)
    left = [Paragraph(company.get("name") or "", _ps("cn", 16, True, NAVY, leading=19))]
    if company.get("address"):
        left.append(Paragraph(company["address"], _ps("ca", 8.5, color=GREY)))
    if contact:
        left.append(Paragraph(contact, _ps("cc", 8.5, color=GREY)))
    if company.get("gstin"):
        left.append(Paragraph(f"GSTIN: {company['gstin']}", _ps("cg", 8.5, color=GREY)))
    right = [Paragraph("PAYSLIP", _ps("t1", 18, True, ACCENT, align=2, leading=21)),
             Paragraph(month_label, _ps("t2", 11, True, NAVY, align=2)),
             Paragraph("Final" if slip.get("status") == "final" else "Draft", _ps("t3", 8, color=GREY, align=2))]
    lw = 38 * mm if logo else 0.1
    head = Table([[logo or "", left, right]], colWidths=[lw, CW - lw - 45 * mm, 45 * mm])
    head.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                              ("RIGHTPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                              ("LINEBELOW", (0, 0), (-1, 0), 1.4, NAVY)]))
    story += [head, Spacer(1, 4 * mm)]

    # ---------- employee block ----------
    k = _ps("k", 7.6, color=GREY)
    v = _ps("v", 9, True, NAVY)
    fields = [("Employee name", emp.get("name") or slip.get("employee_name")), ("Employee ID", emp.get("code") or slip.get("emp_code")),
              ("Designation", emp.get("designation")), ("Department", emp.get("department")),
              ("Date of joining", _fmt_date(emp.get("joining_date"))), ("Salary type", SALARY_TYPES.get(slip.get("salary_type"), "Monthly")),
              ("Bank A/c", emp.get("bank_account")), ("IFSC", emp.get("ifsc")),
              ("UAN / PF no.", emp.get("uan")), ("ESI no.", emp.get("esi_no")),
              ("PAN", emp.get("pan")), ("Mobile", emp.get("phone"))]
    fields = [(a, b) for a, b in fields if b or a in ("Employee name", "Employee ID", "Designation", "Department",
                                                       "Date of joining", "Salary type")]
    if len(fields) % 2:
        fields.append(("", ""))
    photo = _img(emp["_photo"], 24 * mm, 29 * mm) if emp.get("_photo") else None
    gw = CW - (30 * mm if photo else 0)
    rows = []
    for i in range(0, len(fields), 2):
        (k1, v1), (k2, v2) = fields[i], fields[i + 1]
        rows.append([Paragraph(k1, k), Paragraph(str(v1 or "-") if k1 else "", v),
                     Paragraph(k2, k), Paragraph(str(v2 or "-") if k2 else "", v)])
    grid = Table(rows, colWidths=[gw * .18, gw * .32, gw * .18, gw * .32])
    grid.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("TOPPADDING", (0, 0), (-1, -1), 2.2),
                              ("BOTTOMPADDING", (0, 0), (-1, -1), 2.2), ("LEFTPADDING", (0, 0), (-1, -1), 5)]))
    if photo:
        emp_tbl = Table([[grid, photo]], colWidths=[gw, 30 * mm])
    else:
        emp_tbl = Table([[grid]], colWidths=[CW])
    emp_tbl.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), LIGHT), ("BOX", (0, 0), (-1, -1), 0.6, LINE),
                                 ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("ALIGN", (-1, 0), (-1, 0), "CENTER"),
                                 ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]))
    story += [emp_tbl, Spacer(1, 4 * mm)]

    # ---------- attendance ----------
    sm = slip["summary"]
    cells = [("Days in month", slip["days_in_month"]), ("Present", f"{sm['present_days']:g}"), ("Half days", sm["HD"]),
             ("Absent", sm["A"]), ("Week off / Hol.", f"{sm['WO'] + sm['H']}"), ("Paid leave", sm["L"]),
             ("LWP", sm["LWP"]), ("Late marks", sm["late_days"]), ("OT hours", f"{sm['ot_minutes'] / 60:.1f}"),
             ("Paid days", f"{slip['payable_days']:g}")]
    att = Table([[Paragraph(a, _ps("ak", 6.8, color=GREY, align=1)) for a, _ in cells],
                 [Paragraph(str(b), _ps("av", 10.5, True, NAVY, align=1)) for _, b in cells]],
                colWidths=[CW / len(cells)] * len(cells))
    att.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.6, LINE), ("INNERGRID", (0, 0), (-1, -1), 0.4, LINE),
                             ("BACKGROUND", (-1, 0), (-1, -1), colors.HexColor("#E8F5EF")),
                             ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
    story += [Paragraph("ATTENDANCE", _ps("h1", 8, True, GREY)), Spacer(1, 1.5 * mm), att, Spacer(1, 4 * mm)]

    # ---------- earnings & deductions ----------
    earn = slip.get("earnings") or []
    ded = slip.get("deductions") or []
    n = max(len(earn), len(ded), 1)
    cell = _ps("ec", 8.8)
    amt = _ps("ea", 8.8, align=2)
    data = [[Paragraph("EARNINGS", _ps("eh", 8.5, True, colors.white)), Paragraph("AMOUNT (Rs.)", _ps("eh2", 8.5, True, colors.white, align=2)),
             Paragraph("DEDUCTIONS", _ps("dh", 8.5, True, colors.white)), Paragraph("AMOUNT (Rs.)", _ps("dh2", 8.5, True, colors.white, align=2))]]
    for i in range(n):
        e = earn[i] if i < len(earn) else None
        d = ded[i] if i < len(ded) else None
        data.append([Paragraph(e["name"], cell) if e else "", Paragraph(_money(e["amount"]), amt) if e else "",
                     Paragraph(d["name"], cell) if d else "", Paragraph(_money(d["amount"]), amt) if d else ""])
    data.append([Paragraph("Gross earnings", _ps("g", 9, True)), Paragraph(_money(slip["gross"]), _ps("g2", 9, True, align=2)),
                 Paragraph("Total deductions", _ps("t", 9, True)), Paragraph(_money(slip["total_deductions"]), _ps("t2", 9, True, align=2))])
    ew = CW / 2
    ed = Table(data, colWidths=[ew * .66, ew * .34, ew * .66, ew * .34], repeatRows=1)
    st = [("BACKGROUND", (0, 0), (-1, 0), NAVY), ("BOX", (0, 0), (-1, -1), 0.6, LINE),
          ("LINEAFTER", (1, 0), (1, -1), 0.8, LINE), ("LINEABOVE", (0, -1), (-1, -1), 0.9, NAVY),
          ("BACKGROUND", (0, -1), (-1, -1), LIGHT), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
          ("TOPPADDING", (0, 0), (-1, -1), 3.2), ("BOTTOMPADDING", (0, 0), (-1, -1), 3.2)]
    for i in range(1, n + 1):
        if i % 2 == 0:
            st.append(("BACKGROUND", (0, i), (-1, i), colors.HexColor("#FAFBFD")))
    ed.setStyle(TableStyle(st))
    story += [ed, Spacer(1, 4 * mm)]

    # ---------- net pay ----------
    net = Table([[Paragraph("NET PAY", _ps("n1", 12, True, colors.white)),
                  Paragraph("Rs. " + _money(slip["net"]), _ps("n2", 16, True, colors.white, align=2, leading=19))],
                 [Paragraph(rupees_in_words(slip["net"]) + _roundoff(slip), _ps("n3", 8.5, color=colors.white)), ""]],
                colWidths=[CW * .6, CW * .4])
    net.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), ACCENT), ("SPAN", (0, 1), (1, 1)),
                             ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0, 0), (-1, -1), 8),
                             ("RIGHTPADDING", (0, 0), (-1, -1), 8), ("TOPPADDING", (0, 0), (0, 0), 6),
                             ("BOTTOMPADDING", (0, 1), (-1, 1), 6)]))
    story.append(net)
    if slip.get("net_negative"):
        story.append(Paragraph("Deductions are more than earnings this month — net pay shown as zero.", _ps("neg", 8, color=colors.HexColor("#B42318"))))

    # ---------- advance ----------
    recs = slip.get("advance_recoveries") or []
    bal = slip.get("advance_balance")
    if recs or (bal or 0) > 0:
        cut = sum(r["amount"] for r in recs)
        txt = f"<b>Advance / loan:</b> recovered this month Rs. {_money(cut)} · balance after this slip Rs. {_money(bal or 0)}"
        a = Table([[Paragraph(txt, _ps("adv", 8.5, color=NAVY))]], colWidths=[CW])
        a.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.6, LINE), ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#FFFBEB"))]))
        story += [Spacer(1, 3 * mm), a]

    # ---------- payment + signatures ----------
    pay = f"Paid by bank transfer to A/c {emp['bank_account']}" + (f" (IFSC {emp['ifsc']})" if emp.get("ifsc") else "") \
        if emp.get("bank_account") else "Mode of payment: Cash / as per shop records"
    sig = Table([[Paragraph(pay, _ps("pm", 8, color=GREY)), ""],
                 ["", ""],
                 [Paragraph("Employee signature", _ps("s1", 8.5, color=GREY)),
                  Paragraph(f"For {company.get('name', '')}<br/>Authorised signatory", _ps("s2", 8.5, color=GREY, align=2))]],
                colWidths=[CW / 2, CW / 2], rowHeights=[None, 14 * mm, None])
    sig.setStyle(TableStyle([("SPAN", (0, 0), (1, 0)), ("LINEABOVE", (0, 2), (0, 2), 0.6, GREY),
                             ("LINEABOVE", (1, 2), (1, 2), 0.6, GREY), ("VALIGN", (0, 2), (-1, 2), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                             ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    if settings.get("payslip_signature", True):
        story += [Spacer(1, 5 * mm), KeepTogether([sig])]
    else:
        story += [Spacer(1, 3 * mm), Paragraph(pay, _ps("pm2", 8, color=GREY))]
    note = settings.get("payslip_footer") or ""
    if settings.get("payslip_signature", True) and "not require a signature" in note:
        note = "This is a computer-generated salary slip."
    if note:
        story += [Spacer(1, 4 * mm), Paragraph(note, _ps("ft", 7.5, color=GREY, align=1))]
    doc.build(story, onFirstPage=decorate, onLaterPages=decorate)
    return buf.getvalue()
