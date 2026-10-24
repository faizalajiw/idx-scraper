"""Render dossier satu emiten ke PDF (ReportLab platypus).

Modul ini hanya memformat data dari ``dossier.build_dossier``. Tidak ada
perhitungan baru. Setiap bagian menampilkan tanggal sesinya dan penanda
stale bila data lebih lama dari sesi terakhir.
"""

from __future__ import annotations

import io
from typing import Any

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

SECTION_TITLES = {
    "profil": "Profil",
    "valuasi": "Valuasi",
    "faktor": "Faktor & Verdict",
    "arus_asing": "Arus Asing",
    "aktivitas_broker": "Aktivitas Broker",
    "sentimen": "Sentimen",
    "catatan_risiko": "Catatan Risiko",
}

ACCENT = colors.HexColor("#1F2A44")
MUTED = colors.HexColor("#6B7280")
STALE = colors.HexColor("#B45309")


def format_value(value: Any) -> str:
    """Format satu nilai skalar untuk laporan. None -> '—'."""
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "Ya" if value else "Tidak"
    if isinstance(value, float):
        return f"{value:,.2f}"
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def _scalar_rows(data: dict[str, Any], prefix: str = "") -> list[tuple[str, str]]:
    """Pasangan (label, nilai) dari field skalar. Objek bersarang diratakan dengan
    prefiks; daftar teks ditampilkan gabung, daftar objek sebagai jumlah item."""
    rows: list[tuple[str, str]] = []
    for key, value in data.items():
        label = f"{prefix}{key}"
        if isinstance(value, dict):
            rows.extend(_scalar_rows(value, prefix=f"{label} "))
            continue
        if isinstance(value, list):
            if all(isinstance(x, str) for x in value) and value:
                rows.append((label, "; ".join(value)))
            else:
                rows.append((label, f"{len(value)} item"))
            continue
        rows.append((label, format_value(value)))
    return rows


def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "t",
            parent=base["Title"],
            fontName="Helvetica-Bold",
            fontSize=18,
            textColor=ACCENT,
            spaceAfter=2,
        ),
        "sub": ParagraphStyle(
            "s", parent=base["Normal"], fontSize=9, textColor=MUTED, spaceAfter=10
        ),
        "h2": ParagraphStyle(
            "h",
            parent=base["Heading2"],
            fontName="Helvetica-Bold",
            fontSize=12,
            textColor=ACCENT,
            spaceBefore=10,
            spaceAfter=4,
        ),
        "meta": ParagraphStyle(
            "m", parent=base["Normal"], fontSize=8, textColor=MUTED, spaceAfter=4
        ),
        "warn": ParagraphStyle(
            "w", parent=base["Normal"], fontSize=8, textColor=STALE, spaceAfter=4
        ),
        "body": ParagraphStyle("b", parent=base["Normal"], fontSize=9, leading=12),
    }


def _section_block(name: str, section: dict[str, Any], st: dict[str, ParagraphStyle]) -> list[Any]:
    title = SECTION_TITLES.get(name, name)
    flow: list[Any] = [Paragraph(title, st["h2"])]
    session = section.get("session_date") or "tanpa tanggal"
    flow.append(Paragraph(f"Data sesi: {session}", st["meta"]))
    if section.get("stale"):
        flow.append(Paragraph("Data lebih lama dari sesi terakhir pasar.", st["warn"]))
    if section.get("error"):
        flow.append(Paragraph(f"Bagian ini gagal dimuat: {section['error']}", st["warn"]))
        return flow

    data = section.get("data") or {}
    rows = _scalar_rows(data) if isinstance(data, dict) else []
    if not rows:
        flow.append(Paragraph("Tidak ada data.", st["body"]))
        return flow

    table_rows = [
        [Paragraph(k.replace("_", " "), st["body"]), Paragraph(v, st["body"])] for k, v in rows
    ]
    table = Table(table_rows, colWidths=[55 * mm, 115 * mm])
    table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.HexColor("#E5E7EB")),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ]
        )
    )
    flow.append(table)
    return flow


def render_dossier_pdf(dossier: dict[str, Any]) -> bytes:
    """Bangun PDF dari hasil ``build_dossier``. Mengembalikan bytes PDF."""
    st = _styles()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=16 * mm,
        bottomMargin=16 * mm,
        title=f"Dossier {dossier.get('code', '')}",
        author="Lucif",
    )

    code = dossier.get("code", "")
    verdict = dossier.get("verdict") or {}
    story: list[Any] = [
        Paragraph(f"Dossier {code}", st["title"]),
        Paragraph(
            f"Sesi terakhir pasar: {format_value(dossier.get('latest_session'))} · "
            f"Dibuat: {format_value(dossier.get('generated_at'))}",
            st["sub"],
        ),
    ]

    if verdict:
        story.append(Paragraph("Verdict dasar (hold-check)", st["h2"]))
        scalar = [(k, v) for k, v in _scalar_rows(verdict)]
        story.append(
            Table(
                [
                    [Paragraph(k.replace("_", " "), st["body"]), Paragraph(v, st["body"])]
                    for k, v in scalar
                ],
                colWidths=[55 * mm, 115 * mm],
            )
        )

    for name in SECTION_TITLES:
        section = (dossier.get("sections") or {}).get(name)
        if section is None:
            continue
        story.extend(_section_block(name, section, st))

    story.append(Spacer(1, 8))
    story.append(
        Paragraph(
            "Laporan ini ringkasan data dari sumber yang sudah ada, bukan rekomendasi beli atau jual. "
            "Gunakan untuk pertimbangan pribadi.",
            st["meta"],
        )
    )

    doc.build(story)
    return buf.getvalue()
