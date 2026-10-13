"""Report exports: the same table as CSV, Excel (.xlsx) or PDF, with the shop's name and logo on top."""
import io
import csv
import base64
import datetime
from fastapi import HTTPException
from fastapi.responses import Response

STATUS_FILL = {"P": "D1FAE5", "A": "FEE2E2", "HD": "FEF3C7", "MIS": "FFEDD5", "IN": "FFEDD5", "WO": "E5E7EB",
               "H": "DBEAFE", "L": "EDE9FE", "LWP": "FCE7F3"}
MIME = {"csv": "text/csv", "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "pdf": "application/pdf"}


def logo_bytes(company):
    logo = (company or {}).get("logo") or ""
    if "," in logo:
        try:
            return base64.b64decode(logo.split(",", 1)[1])
        except Exception:
            return None
    return None


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


class Table:
    """header: list of column titles. rows: list of lists. total: optional last row.
    status_cols: indexes whose values are attendance status codes (colour coded).
    money_cols: indexes printed with 2 decimals. widths: relative widths (characters)."""

    def __init__(self, title, subtitle, header, rows, total=None, status_cols=(), money_cols=(), widths=None,
                 landscape=True, legend=None):
        self.title, self.subtitle, self.header, self.rows, self.total = title, subtitle, header, rows, total
        self.status_cols, self.money_cols = set(status_cols), set(money_cols)
        self.widths = widths or [max(6, min(28, max([len(str(header[i]))] + [len(str(r[i])) for r in rows[:300]]) + 2))
                                 for i in range(len(header))]
        self.landscape, self.legend = landscape, legend


def send(table: Table, company, fmt: str, filename_base: str):
    fmt = (fmt or "csv").lower()
    if fmt not in MIME:
        raise HTTPException(400, "Format must be csv, xlsx or pdf")
    data = {"csv": to_csv, "xlsx": to_xlsx, "pdf": to_pdf}[fmt](table, company)
    return Response(data, media_type=MIME[fmt],
                    headers={"Content-Disposition": f'attachment; filename="{filename_base}.{fmt}"'})


# ---------------- CSV ----------------
def to_csv(t: Table, company):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(t.header)
    w.writerows(t.rows)
    if t.total:
        w.writerow(t.total)
    return buf.getvalue().encode("utf-8-sig")


# ---------------- Excel ----------------
def to_xlsx(t: Table, company):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = (t.title or "Report")[:31]
    ncol = len(t.header)
    last = get_column_letter(ncol)
    start = 1
    img_bytes = logo_bytes(company)
    if img_bytes:
        try:
            from openpyxl.drawing.image import Image as XLImage
            from PIL import Image as PImage
            im = PImage.open(io.BytesIO(img_bytes))
            im.thumbnail((180, 54))
            b = io.BytesIO()
            im.save(b, "PNG")
            b.seek(0)
            xi = XLImage(b)
            ws.add_image(xi, f"{last}1" if ncol > 3 else "A1")
        except Exception:
            pass
    ws.merge_cells(f"A1:{get_column_letter(max(1, ncol - 2))}1")
    ws["A1"] = (company or {}).get("name") or ""
    ws["A1"].font = Font(size=15, bold=True, color="14213D")
    ws.merge_cells(f"A2:{get_column_letter(max(1, ncol - 2))}2")
    ws["A2"] = " | ".join(x for x in [(company or {}).get("address"), (company or {}).get("phone")] if x)
    ws["A2"].font = Font(size=9, color="5B6475")
    ws.merge_cells(f"A3:{get_column_letter(max(1, ncol - 2))}3")
    ws["A3"] = f"{t.title} — {t.subtitle}" if t.subtitle else t.title
    ws["A3"].font = Font(size=12, bold=True, color="0E9F6E")
    ws.row_dimensions[1].height = 24
    start = 5

    thin = Side(style="thin", color="D9DEE8")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    head_fill = PatternFill("solid", fgColor="14213D")
    for i, h in enumerate(t.header, 1):
        c = ws.cell(row=start, column=i, value=h)
        c.font = Font(bold=True, color="FFFFFF", size=9)
        c.fill = head_fill
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        c.border = border
    ws.row_dimensions[start].height = 30
    fills = {k: PatternFill("solid", fgColor=v) for k, v in STATUS_FILL.items()}
    zebra = PatternFill("solid", fgColor="F7F9FC")
    r = start
    for ri, row in enumerate(t.rows):
        r += 1
        for ci, v in enumerate(row):
            c = ws.cell(row=r, column=ci + 1, value=v if v != "" else None)
            c.border = border
            c.font = Font(size=9)
            if ci in t.status_cols:
                c.alignment = Alignment(horizontal="center")
                if v in fills:
                    c.fill = fills[v]
            elif ci in t.money_cols:
                c.number_format = "#,##0.00"
            elif ri % 2:
                c.fill = zebra
    if t.total:
        r += 1
        for ci, v in enumerate(t.total):
            c = ws.cell(row=r, column=ci + 1, value=v if v != "" else None)
            c.font = Font(bold=True, size=9)
            c.fill = PatternFill("solid", fgColor="E8F5EF")
            c.border = border
            if ci in t.money_cols:
                c.number_format = "#,##0.00"
    if t.legend:
        r += 2
        ws.cell(row=r, column=1, value=t.legend).font = Font(size=8, italic=True, color="5B6475")
    r += 1
    ws.cell(row=r, column=1, value=f"Generated {datetime.datetime.now():%d-%m-%Y %H:%M} · Hayat HRMS").font = Font(size=8, color="8A93A6")
    for i, w in enumerate(t.widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = ws.cell(row=start + 1, column=3 if ncol > 6 else 1)
    ws.print_options.horizontalCentered = True
    ws.page_setup.orientation = "landscape" if t.landscape else "portrait"
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.print_title_rows = f"{start}:{start}"
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


# ---------------- PDF ----------------
def to_pdf(t: Table, company):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.units import mm
    from reportlab.platypus import SimpleDocTemplate, Table as RLTable, TableStyle, Paragraph, Spacer, Image
    from reportlab.lib.styles import ParagraphStyle
    from ..assets import HAYAT_EMBLEM_B64

    size = landscape(A4) if t.landscape else A4
    W = size[0] - 16 * mm
    buf = io.BytesIO()

    def footer(c, doc):
        c.saveState()
        c.setFont("Helvetica", 6.5)
        c.setFillColor(colors.HexColor("#8A93A6"))
        c.drawString(8 * mm, 6 * mm, f"{(company or {}).get('name', '')} · {t.title} · {t.subtitle}")
        c.drawRightString(size[0] - 8 * mm, 6 * mm, f"Page {doc.page}  ·  Generated by Hayat HRMS — Hayat Enterprises")
        try:
            from reportlab.lib.utils import ImageReader
            c.drawImage(ImageReader(io.BytesIO(base64.b64decode(HAYAT_EMBLEM_B64))), size[0] / 2 - 2.5 * mm, 4.5 * mm,
                        5 * mm, 5 * mm, mask="auto")
        except Exception:
            pass
        c.restoreState()

    doc = SimpleDocTemplate(buf, pagesize=size, leftMargin=8 * mm, rightMargin=8 * mm, topMargin=8 * mm,
                            bottomMargin=12 * mm, title=f"{t.title} {t.subtitle}")
    st_name = ParagraphStyle("n", fontName="Helvetica-Bold", fontSize=14, textColor=colors.HexColor("#14213D"), leading=17)
    st_sub = ParagraphStyle("s", fontName="Helvetica", fontSize=8, textColor=colors.HexColor("#5B6475"), leading=10)
    st_title = ParagraphStyle("t", fontName="Helvetica-Bold", fontSize=11, textColor=colors.HexColor("#0E9F6E"),
                              alignment=2, leading=14)
    head_left = [Paragraph((company or {}).get("name") or "", st_name),
                 Paragraph(" | ".join(x for x in [(company or {}).get("address"), (company or {}).get("phone"),
                                                  (company or {}).get("gstin") and f"GSTIN {company['gstin']}"] if x), st_sub)]
    lb = logo_bytes(company)
    logo_cell = ""
    if lb:
        try:
            from PIL import Image as PImage
            im = PImage.open(io.BytesIO(lb))
            ratio = im.size[0] / im.size[1]
            h = 14 * mm
            logo_cell = Image(io.BytesIO(lb), width=min(h * ratio, 40 * mm), height=min(h, 40 * mm / ratio))
        except Exception:
            logo_cell = ""
    title_cell = [Paragraph(t.title, st_title),
                  Paragraph(t.subtitle or "", ParagraphStyle("s2", parent=st_sub, alignment=2, fontSize=9))]
    lw = 44 * mm if logo_cell else 0.1
    top = RLTable([[logo_cell, head_left, title_cell]], colWidths=[lw, W - lw - 70 * mm, 70 * mm])
    top.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                             ("LINEBELOW", (0, 0), (-1, 0), 1.2, colors.HexColor("#14213D")),
                             ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]))

    n = len(t.header)
    tot_w = sum(t.widths)
    col_w = [W * w / tot_w for w in t.widths]
    fs = 8 if n <= 12 else 7 if n <= 20 else 5.6
    cell = ParagraphStyle("c", fontName="Helvetica", fontSize=fs, leading=fs + 1.6)
    headp = ParagraphStyle("h", fontName="Helvetica-Bold", fontSize=fs, leading=fs + 1.6, textColor=colors.white,
                           alignment=1)

    def fmt(ci, v):
        if ci in t.money_cols and _num(v):
            return f"{v:,.2f}"
        if _num(v):
            return f"{v:g}"
        return "" if v is None else str(v)

    data = [[Paragraph(str(h), headp) for h in t.header]]
    for row in t.rows:
        data.append([Paragraph(fmt(ci, v), cell) if ci not in t.status_cols and not _num(v) and len(str(v)) > 14
                     else fmt(ci, v) for ci, v in enumerate(row)])
    if t.total:
        data.append([fmt(ci, v) for ci, v in enumerate(t.total)])
    style = [("FONT", (0, 1), (-1, -1), "Helvetica", fs), ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#14213D")),
             ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#C9D0DC")),
             ("LEFTPADDING", (0, 0), (-1, -1), 2), ("RIGHTPADDING", (0, 0), (-1, -1), 2),
             ("TOPPADDING", (0, 0), (-1, -1), 1.6), ("BOTTOMPADDING", (0, 0), (-1, -1), 1.6)]
    for ci in t.status_cols:
        style.append(("ALIGN", (ci, 1), (ci, -1), "CENTER"))
    for ci in range(n):
        if ci not in t.status_cols and any(_num(r[ci]) for r in t.rows[:50]):
            style.append(("ALIGN", (ci, 1), (ci, -1), "RIGHT"))
    for ri, row in enumerate(t.rows, 1):
        if ri % 2 == 0:
            style.append(("BACKGROUND", (0, ri), (-1, ri), colors.HexColor("#F7F9FC")))
        for ci in t.status_cols:
            v = row[ci]
            if v in STATUS_FILL:
                style.append(("BACKGROUND", (ci, ri), (ci, ri), colors.HexColor("#" + STATUS_FILL[v])))
    if t.total:
        style += [("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#E8F5EF")),
                  ("FONT", (0, -1), (-1, -1), "Helvetica-Bold", fs)]
    body = RLTable(data, colWidths=col_w, repeatRows=1)
    body.setStyle(TableStyle(style))
    story = [top, Spacer(1, 5 * mm), body]
    if t.legend:
        story += [Spacer(1, 3 * mm), Paragraph(t.legend, ParagraphStyle("l", fontName="Helvetica", fontSize=7,
                                                                        textColor=colors.HexColor("#5B6475")))]
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()
