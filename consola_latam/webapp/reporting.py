from __future__ import annotations

import html
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


RED = colors.HexColor("#9A1413")
RED_DARK = colors.HexColor("#6C0D0C")
RED_SOFT = colors.HexColor("#F5E7E7")
BLUE = colors.HexColor("#13398A")
BLUE_DARK = colors.HexColor("#0C2A66")
BLUE_SOFT = colors.HexColor("#E7ECF5")
TEXT = colors.HexColor("#1A1A1A")
MUTED = colors.HexColor("#666666")
LINE = colors.HexColor("#E6E6E6")


def _p(text: str | int | None) -> str:
    value = "" if text is None else str(text)
    value = html.unescape(value)
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_client_report(snapshot: dict, output_path: Path, sede: str = "peru") -> None:
    accent, accent_dark, accent_soft = (BLUE, BLUE_DARK, BLUE_SOFT) if sede == "ecuador" else (RED, RED_DARK, RED_SOFT)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=letter,
        rightMargin=0.6 * inch,
        leftMargin=0.6 * inch,
        topMargin=0.55 * inch,
        bottomMargin=0.55 * inch,
        title="Informe ejecutivo de cartera",
    )
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle("TitleRed", parent=styles["Title"], fontName="Helvetica-Bold", fontSize=20, textColor=accent_dark, spaceAfter=8))
    styles.add(ParagraphStyle("Section", parent=styles["Heading2"], fontName="Helvetica-Bold", fontSize=12, textColor=accent, spaceBefore=14, spaceAfter=8))
    styles.add(ParagraphStyle("Small", parent=styles["BodyText"], fontSize=8.5, leading=11, textColor=MUTED))
    styles.add(ParagraphStyle("BodyClean", parent=styles["BodyText"], fontSize=9.5, leading=12.5, textColor=TEXT))

    client = snapshot["client"]
    story = [
        Paragraph(f"Informe ejecutivo - {_p(client.get('name'))}", styles["TitleRed"]),
        Paragraph(f"Generado: {_p(snapshot.get('generated_at', '').replace('T', ' '))}", styles["Small"]),
        Spacer(1, 8),
    ]

    kpis = [
        ["Procesos", snapshot["total_processes"]],
        ["Manual pendiente", snapshot["manual_pending"]],
        ["Notificaciones sin leer", snapshot["unread_notifications"]],
        ["Importancia", client.get("importance", "")],
    ]
    story.append(_table(kpis, widths=[2.4 * inch, 1.2 * inch], header=False, accent=accent, accent_dark=accent_dark, accent_soft=accent_soft))

    story.append(Paragraph("Resumen operativo", styles["Section"]))
    story.append(Paragraph(
        "Este reporte consolida la cartera registrada en Consola CEJ, las alertas manuales pendientes, "
        "los ultimos seguimientos y los procesos con mayor actividad.",
        styles["BodyClean"],
    ))

    story.append(Paragraph("Procesos mas activos", styles["Section"]))
    top_rows = [["Radicado", "Ultima actuacion", "Fecha", "Act."]]
    for proc in snapshot["top_processes"]:
        top_rows.append([
            _p(proc.get("radicado")),
            _p(proc.get("last_acto")),
            _p(proc.get("last_fecha")),
            _p(proc.get("actuaciones_count")),
        ])
    story.append(_table(top_rows, widths=[1.65 * inch, 3.1 * inch, 1.05 * inch, 0.45 * inch], accent=accent, accent_dark=accent_dark, accent_soft=accent_soft))

    story.append(Paragraph("Ultimas consultas", styles["Section"]))
    history_rows = [["Fecha", "Modo", "Total", "Con mov.", "Sin mov.", "Err."]]
    for item in snapshot["history"]:
        history_rows.append([
            _p((item.get("created_at") or "").replace("T", " ")),
            _p((item.get("mode") or item.get("scope") or "").upper()),
            _p(item.get("total")),
            _p(item.get("con_mov")),
            _p(item.get("sin_mov")),
            _p(item.get("errores")),
        ])
    story.append(_table(history_rows, widths=[1.55 * inch, 1.0 * inch, 0.7 * inch, 0.8 * inch, 0.8 * inch, 0.55 * inch], accent=accent, accent_dark=accent_dark, accent_soft=accent_soft))

    if snapshot["reminders"]:
        story.append(Paragraph("Agenda pendiente", styles["Section"]))
        reminder_rows = [["Fecha", "Hora", "Tipo", "Recordatorio"]]
        for reminder in snapshot["reminders"]:
            reminder_rows.append([
                _p(reminder.get("due_date")),
                _p(reminder.get("due_time")),
                _p(reminder.get("kind")),
                _p(reminder.get("title")),
            ])
        story.append(_table(reminder_rows, widths=[0.9 * inch, 0.55 * inch, 0.85 * inch, 3.5 * inch], accent=accent, accent_dark=accent_dark, accent_soft=accent_soft))

    doc.build(story)


def _table(
    rows: list[list],
    widths: list[float],
    header: bool = True,
    accent: colors.Color = RED,
    accent_dark: colors.Color = RED_DARK,
    accent_soft: colors.Color = RED_SOFT,
) -> Table:
    body_style = ParagraphStyle("TableBody", fontName="Helvetica", fontSize=8.2, leading=10, textColor=TEXT)
    header_style = ParagraphStyle("TableHeader", fontName="Helvetica-Bold", fontSize=8.2, leading=10, textColor=colors.white)
    key_style = ParagraphStyle("TableKey", fontName="Helvetica-Bold", fontSize=8.2, leading=10, textColor=accent_dark)
    data = []
    for r_idx, row in enumerate(rows):
        converted = []
        for c_idx, cell in enumerate(row):
            style = header_style if header and r_idx == 0 else key_style if (not header and c_idx == 0) else body_style
            converted.append(Paragraph(_p(cell), style))
        data.append(converted)
    table = Table(data, colWidths=widths, hAlign="LEFT", repeatRows=1 if header else 0)
    style = [
        ("GRID", (0, 0), (-1, -1), 0.35, LINE),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, -1), 8.2),
        ("LEADING", (0, 0), (-1, -1), 10),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]
    if header:
        style.extend([
            ("BACKGROUND", (0, 0), (-1, 0), accent),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ])
    else:
        style.extend([
            ("BACKGROUND", (0, 0), (0, -1), accent_soft),
            ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
            ("TEXTCOLOR", (0, 0), (0, -1), accent_dark),
        ])
    table.setStyle(TableStyle(style))
    return table
