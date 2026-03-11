"""
MCP stdio server providing PDF report generation tools.

Tools:
  - generate_pdf_report: Compose a multi-section PDF document with text, tables,
                         and optional embedded chart images (supplied as base64 PNG).
"""

import asyncio
import base64
import io
import json
import textwrap
from datetime import datetime

from fpdf import FPDF
from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server

server = Server("pdf-report-tools")

_PAGE_W = 210
_PAGE_H = 297
_MARGIN = 20
_CONTENT_W = _PAGE_W - 2 * _MARGIN

_COLOURS = {
    "primary": (33, 150, 243),
    "heading": (55, 71, 79),
    "body": (33, 33, 33),
    "muted": (117, 117, 117),
    "rule": (224, 224, 224),
    "table_header_bg": (227, 242, 253),
    "table_row_alt": (245, 245, 245),
}


class _PDF(FPDF):
    def __init__(self, title: str, subtitle: str = ""):
        super().__init__()
        self._doc_title = title
        self._doc_subtitle = subtitle
        self.set_margins(_MARGIN, _MARGIN, _MARGIN)
        self.set_auto_page_break(auto=True, margin=_MARGIN)
        self.set_title(title)

    def header(self):
        if self.page_no() == 1:
            return
        self.set_font("Helvetica", "I", 8)
        r, g, b = _COLOURS["muted"]
        self.set_text_color(r, g, b)
        self.cell(0, 6, self._doc_title, align="L")
        self.set_text_color(0, 0, 0)
        self.ln(1)
        r, g, b = _COLOURS["rule"]
        self.set_draw_color(r, g, b)
        self.line(_MARGIN, self.get_y(), _PAGE_W - _MARGIN, self.get_y())
        self.ln(4)

    def footer(self):
        self.set_y(-15)
        self.set_font("Helvetica", "I", 8)
        r, g, b = _COLOURS["muted"]
        self.set_text_color(r, g, b)
        generated = datetime.now().strftime("Generated %Y-%m-%d")
        self.cell(0, 6, f"{generated}   ·   Page {self.page_no()}", align="C")
        self.set_text_color(0, 0, 0)

    def cover_page(self):
        self.add_page()
        self.ln(40)
        r, g, b = _COLOURS["primary"]
        self.set_fill_color(r, g, b)
        self.rect(_MARGIN, self.get_y(), _CONTENT_W, 2, "F")
        self.ln(8)

        self.set_font("Helvetica", "B", 28)
        r, g, b = _COLOURS["heading"]
        self.set_text_color(r, g, b)
        self.multi_cell(_CONTENT_W, 12, self._doc_title, align="L")
        self.ln(4)

        if self._doc_subtitle:
            self.set_font("Helvetica", "", 14)
            r, g, b = _COLOURS["muted"]
            self.set_text_color(r, g, b)
            self.multi_cell(_CONTENT_W, 8, self._doc_subtitle, align="L")
            self.ln(4)

        self.set_font("Helvetica", "", 10)
        r, g, b = _COLOURS["muted"]
        self.set_text_color(r, g, b)
        self.cell(0, 6, datetime.now().strftime("%B %d, %Y"), align="L")
        self.set_text_color(0, 0, 0)

    def section_heading(self, text: str):
        self.ln(4)
        self.set_font("Helvetica", "B", 14)
        r, g, b = _COLOURS["heading"]
        self.set_text_color(r, g, b)
        self.multi_cell(_CONTENT_W, 8, text, align="L")
        r, g, b = _COLOURS["rule"]
        self.set_draw_color(r, g, b)
        self.line(_MARGIN, self.get_y(), _PAGE_W - _MARGIN, self.get_y())
        self.set_text_color(0, 0, 0)
        self.ln(3)

    def body_text(self, text: str):
        self.set_font("Helvetica", "", 10)
        r, g, b = _COLOURS["body"]
        self.set_text_color(r, g, b)
        for paragraph in text.split("\n\n"):
            paragraph = paragraph.strip()
            if not paragraph:
                continue
            if paragraph.startswith("- ") or paragraph.startswith("* "):
                for line in paragraph.splitlines():
                    bullet = line.lstrip("-* ").strip()
                    if bullet:
                        x = self.get_x()
                        self.set_x(_MARGIN + 4)
                        self.cell(4, 6, "\u2022")
                        self.multi_cell(_CONTENT_W - 8, 6, bullet)
                        self.set_x(x)
            else:
                self.multi_cell(_CONTENT_W, 6, paragraph)
                self.ln(2)
        self.set_text_color(0, 0, 0)

    def add_table(self, headers: list[str], rows: list[list[str]]):
        if not headers and not rows:
            return

        col_count = len(headers) if headers else (len(rows[0]) if rows else 0)
        if col_count == 0:
            return

        col_w = _CONTENT_W / col_count

        if headers:
            r, g, b = _COLOURS["table_header_bg"]
            self.set_fill_color(r, g, b)
            self.set_font("Helvetica", "B", 9)
            r, g, b = _COLOURS["heading"]
            self.set_text_color(r, g, b)
            for h in headers:
                self.cell(col_w, 7, str(h)[:30], border=1, fill=True, align="C")
            self.ln()

        self.set_font("Helvetica", "", 9)
        r, g, b = _COLOURS["body"]
        self.set_text_color(r, g, b)
        for i, row in enumerate(rows):
            if i % 2 == 1:
                r2, g2, b2 = _COLOURS["table_row_alt"]
                self.set_fill_color(r2, g2, b2)
                fill = True
            else:
                self.set_fill_color(255, 255, 255)
                fill = True
            for cell in row:
                self.cell(col_w, 6, str(cell)[:30], border=1, fill=fill)
            self.ln()

        self.set_text_color(0, 0, 0)
        self.ln(2)

    def add_chart(self, image_bytes: bytes, caption: str = ""):
        available_h = self.h - self.get_y() - _MARGIN - (10 if caption else 0)
        target_w = min(_CONTENT_W, 160)
        target_h = min(available_h, 90)
        if target_h < 30:
            self.add_page()
            target_h = 90

        self.image(io.BytesIO(image_bytes), x=_MARGIN, w=target_w, h=target_h)
        self.ln(2)
        if caption:
            self.set_font("Helvetica", "I", 8)
            r, g, b = _COLOURS["muted"]
            self.set_text_color(r, g, b)
            self.multi_cell(_CONTENT_W, 5, caption, align="C")
            self.set_text_color(0, 0, 0)
        self.ln(4)


def _build_pdf(
    title: str,
    subtitle: str,
    sections: list[dict],
) -> bytes:
    pdf = _PDF(title, subtitle)
    pdf.cover_page()

    for section in sections:
        heading = section.get("heading", "")
        body = section.get("body", "")
        table_headers = section.get("table_headers") or []
        table_rows = section.get("table_rows") or []
        chart_b64 = section.get("chart_base64")
        chart_caption = section.get("chart_caption", "")

        if heading:
            pdf.add_page() if pdf.page_no() > 1 and pdf.get_y() > _PAGE_H * 0.7 else None
            pdf.section_heading(heading)

        if body:
            pdf.body_text(body)

        if table_headers or table_rows:
            pdf.add_table(table_headers, table_rows)

        if chart_b64:
            try:
                image_bytes = base64.b64decode(chart_b64)
                pdf.add_chart(image_bytes, chart_caption)
            except Exception as exc:
                pdf.body_text(f"[Chart could not be embedded: {exc}]")

    return bytes(pdf.output())


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="generate_pdf_report",
            description=(
                "Generate a formatted PDF report with a cover page, text sections, "
                "data tables, and optional embedded chart images. "
                "Returns the PDF as a downloadable artifact. "
                "Use this to produce summary reports, weekly home overviews, "
                "analysis write-ups, or any multi-section document."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Main report title shown on the cover page and page headers.",
                    },
                    "subtitle": {
                        "type": "string",
                        "description": "Optional subtitle shown on the cover page below the title.",
                    },
                    "sections": {
                        "type": "string",
                        "description": textwrap.dedent("""\
                            JSON array of section objects. Each object may have:
                            - heading (str): Section heading
                            - body (str): Paragraph text. Separate paragraphs with blank lines.
                              Lines starting with '- ' are rendered as bullet lists.
                            - table_headers (list[str]): Column headers for a data table (optional)
                            - table_rows (list[list[str]]): 2-D array of table cell values (optional)
                            - chart_base64 (str): Base64-encoded PNG image to embed (optional)
                            - chart_caption (str): Caption shown below the chart (optional)

                            Example:
                            [
                              {
                                "heading": "Summary",
                                "body": "Overall the system is performing well.\\n\\nKey findings:\\n- Energy usage is down 12%\\n- Three automations triggered overnight"
                              },
                              {
                                "heading": "Energy Usage",
                                "body": "Daily consumption for the past week.",
                                "chart_base64": "<base64 PNG>",
                                "chart_caption": "Fig 1 — Daily kWh consumption"
                              },
                              {
                                "heading": "Device Status",
                                "table_headers": ["Device", "Status", "Last Seen"],
                                "table_rows": [["Living Room Light", "On", "5 min ago"], ["Thermostat", "Heating", "1 min ago"]]
                              }
                            ]
                        """),
                    },
                    "output_filename": {
                        "type": "string",
                        "description": "Filename for the output PDF artifact (default: report.pdf).",
                    },
                },
                "required": ["title", "sections"],
                "additionalProperties": False,
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.ContentBlock]:
    if name != "generate_pdf_report":
        return [types.TextContent(type="text", text=f"Unknown tool: {name}")]

    title = arguments.get("title", "Report")
    subtitle = arguments.get("subtitle", "")
    sections_raw = arguments.get("sections", "[]")
    output_filename = arguments.get("output_filename", "report.pdf")
    if not output_filename.lower().endswith(".pdf"):
        output_filename += ".pdf"

    try:
        sections = json.loads(sections_raw)
    except json.JSONDecodeError as exc:
        return [types.TextContent(type="text", text=f"Invalid sections JSON: {exc}")]

    if not isinstance(sections, list):
        return [types.TextContent(type="text", text="sections must be a JSON array.")]

    try:
        pdf_bytes = _build_pdf(title, subtitle, sections)
    except Exception as exc:
        return [types.TextContent(type="text", text=f"PDF generation failed: {exc}")]

    b64 = base64.b64encode(pdf_bytes).decode()
    return [
        types.TextContent(
            type="text",
            text=json.dumps({
                "status": "ok",
                "filename": output_filename,
                "size_bytes": len(pdf_bytes),
                "data_base64": b64,
                "mime_type": "application/pdf",
            }),
        )
    ]


async def _main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(_main())
