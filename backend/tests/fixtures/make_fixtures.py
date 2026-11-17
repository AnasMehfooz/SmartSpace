"""Generate the fixture workspace used by the integration tests and the search eval (SDD 15.1/15.2).

German + English documents: 5 PDFs (1 scanned, OCR only), 2 DOCX, 1 PPTX, 2 Markdown, 1 text,
3 images with text, plus edge cases (unsupported, empty, hidden, excluded).

    python -m tests.fixtures.make_fixtures <dest_folder>
"""

from __future__ import annotations

import sys
from pathlib import Path

DOCS: dict[str, str] = {
    "Housing/Mietvertrag_2024.pdf": """Mietvertrag für Wohnraum

Zwischen Herrn Thomas Becker (Vermieter), Lindenstraße 12, 10969 Berlin, und Frau Anna Schmidt (Mieterin) wird folgender Mietvertrag geschlossen.

§1 Mietsache: Vermietet wird die Wohnung im 3. Obergeschoss, Lindenstraße 12, 10969 Berlin, bestehend aus zwei Zimmern, Küche und Bad mit insgesamt 58 Quadratmetern.

§2 Mietzeit: Das Mietverhältnis beginnt am 1. März 2024 und läuft auf unbestimmte Zeit. Die Kündigungsfrist beträgt drei Monate zum Monatsende.

§3 Miete: Die monatliche Kaltmiete beträgt 950,00 Euro. Zusätzlich ist eine Vorauszahlung für Betriebskosten und Heizung von 180,00 Euro zu leisten. Die Miete ist bis zum dritten Werktag eines Monats auf das Konto des Vermieters zu überweisen.

§4 Kaution: Die Mieterin leistet eine Mietkaution in Höhe von 2.850,00 Euro, zahlbar in drei Raten.

§5 Haustiere: Die Haltung von Kleintieren ist erlaubt. Hunde und Katzen nur mit schriftlicher Zustimmung.""",
    "Finance/Steuererklaerung_2023.pdf": """Einkommensteuererklärung 2023 – Zusammenfassung

Steuerpflichtige: Anna Schmidt, Steuer-ID 12 345 678 901, zuständiges Finanzamt Berlin-Mitte.

Einkünfte aus nichtselbständiger Arbeit: Bruttoarbeitslohn 48.200 Euro. Einbehaltene Lohnsteuer 7.910 Euro.

Werbungskosten: Arbeitsmittel (Laptop) 1.299 Euro, Fahrtkosten zur Arbeitsstätte 1.120 Euro, Fortbildung 640 Euro.
Sonderausgaben: Krankenversicherung und Pflegeversicherung 4.300 Euro.

Ergebnis laut Steuerbescheid vom 14. Juni 2024: Die Steuererstattung beträgt 1.240 Euro und wird auf das angegebene Konto überwiesen.""",
    "Finance/Invoice_Laptop_2024.pdf": """INVOICE  INV-20417

Seller: TechStore GmbH, Friedrichstraße 88, 10117 Berlin
Customer: Anna Schmidt
Invoice date: 12 May 2024    Payment terms: 14 days

Item: Dell XPS 15 laptop, 32 GB RAM, 1 TB SSD     Quantity: 1     Unit price: 1,091.60 EUR
VAT 19%: 207.40 EUR
Total amount due: 1,299.00 EUR

Warranty: 24 months manufacturer warranty. Thank you for your purchase.""",
    "Work/Project_Report_Q3.pdf": """Project Report Q3 2024 – Smart Space Prototype

Summary: The team delivered the first working prototype of the local-first file assistant. Hybrid search and the chat with citations are working end to end on the test machine.

Milestones: hardware profiler finished in July, model download manager finished in August, indexing pipeline and semantic search finished in September.

Risks: packaging the Python backend with PyInstaller may fail because of ONNX dependencies; GPU memory on 4 GB cards is tight for the 7B model.

Budget: 12,000 EUR planned, 9,450 EUR spent. The remaining budget covers user testing in October.""",
    "University/Lecture_Notes_Databases.pdf": """Lecture 7: Database Systems – Normalization, Indexes and Transactions

First normal form requires atomic attribute values. Second normal form removes partial dependencies on a composite key. Third normal form removes transitive dependencies.

B-tree indexes keep keys sorted in balanced pages, so lookups, range scans and inserts run in logarithmic time. Hash indexes only support equality lookups.

Transactions follow the ACID properties: atomicity, consistency, isolation and durability. Write-ahead logging (WAL) makes commits durable; isolation levels trade correctness for concurrency.

Exam tip: practise decomposing relations into BCNF and explain lost update anomalies.""",
}

DOCX = {
    "Personal/Lebenslauf_Anna_Schmidt.docx": {
        "title": "Lebenslauf – Anna Schmidt",
        "paras": [
            "Persönliche Daten: geboren 1998 in Hamburg, wohnhaft in Berlin, E-Mail anna.schmidt@example.com.",
            "Berufserfahrung: Seit 2022 Softwareentwicklerin bei der DataWorks AG in Berlin. Entwicklung von "
            "Datenpipelines mit Python und SQL, Aufbau von Dashboards und Betreuung von Werkstudenten.",
            "Ausbildung: Master of Science Informatik an der Technischen Universität Berlin (2020–2022), "
            "Bachelor Wirtschaftsinformatik an der Universität Hamburg (2016–2020).",
            "Kenntnisse: Python, SQL, PostgreSQL, Docker, Git, maschinelles Lernen. Sprachen: Deutsch "
            "(Muttersprache), Englisch (fließend), Spanisch (Grundkenntnisse).",
        ],
        "table": None,
    },
    "Work/Meeting_Notes_Kickoff.docx": {
        "title": "Kickoff Meeting Notes – Smart Space",
        "paras": [
            "Date: 4 November 2026. Participants: Developer A (frontend and desktop shell), Developer B "
            "(AI and backend), supervisor Prof. Weber.",
            "Decisions: Tauri for the desktop shell, FastAPI sidecar for the backend, llama.cpp for the language "
            "model, LanceDB for vectors. Everything must run offline after the model download.",
        ],
        "table": [
            ["Action item", "Owner", "Due"],
            ["Agree the OpenAPI contract", "Developer A and B", "Week 1"],
            ["Implement the search API", "Developer B", "Week 4"],
            ["Build the onboarding wizard", "Developer A", "Week 2"],
        ],
    },
}

PPTX = {
    "University/Machine_Learning_Intro.pptx": [
        ("Introduction to Machine Learning", "Supervised, unsupervised and reinforcement learning",
         "Welcome slide. Mention the course schedule."),
        ("Supervised learning", "Learn a mapping from labelled examples\nRegression predicts numbers\n"
         "Classification predicts categories", "Example: predicting house prices with linear regression."),
        ("Overfitting", "A model memorises the training data and fails on new data\nUse validation sets, "
         "regularisation and cross-validation", "Show the bias-variance trade-off plot."),
    ]
}

TEXTS = {
    "Notes/Pancake_Recipe.md": """# Fluffy Pancakes

Ingredients: 200 g flour, 2 eggs, 300 ml milk, 1 tbsp sugar, 1 tsp baking powder, a pinch of salt, butter for the pan.

1. Whisk the eggs with milk and sugar.
2. Fold in flour, baking powder and salt until smooth; rest the batter for 10 minutes.
3. Fry small pancakes in butter over medium heat until golden on both sides.

Serve with maple syrup and blueberries.
""",
    "Notes/Urlaubsplanung.md": """# Urlaubsplanung Sommer 2025

Reiseziel: Italien – eine Woche Florenz und drei Tage in Bologna.

- Zugtickets Berlin–München–Florenz mit dem Nachtzug buchen (ca. 180 Euro pro Person).
- Hotel in Florenz in der Nähe des Doms reservieren, Budget 120 Euro pro Nacht.
- Uffizien-Tickets vorab online kaufen.
- Packliste: Reisepass, Sonnencreme, Wanderschuhe.
""",
    "Notes/todo.txt": """Things to do this week
- renew library books
- call the insurance about the bike
- prepare slides for the databases study group
""",
}

IMAGES = {
    "Images/receipt_coffee.png": ["COFFEE CORNER", "Cappuccino 3.50 EUR", "Croissant 2.40 EUR",
                                  "Orange juice 2.00 EUR", "TOTAL 7.90 EUR", "Thank you!"],
    "Images/whiteboard_sprint.png": ["Sprint 5 goals", "search API with filters", "chat streaming",
                                     "purge safety tests"],
    "Images/Hinweisschild.jpg": ["Parkplatz nur fuer Mieter", "Unberechtigt parkende Fahrzeuge",
                                 "werden kostenpflichtig abgeschleppt"],
}

SCANNED = {
    "Scans/Scanned_Insurance_Letter.pdf": ["Haftpflichtversicherung", "Versicherungsnummer 4711-2024",
                                           "Jahresbeitrag 89,50 Euro", "Versicherte Person: Anna Schmidt",
                                           "Beginn der Versicherung: 1. Januar 2025"],
}


def _font(size: int):
    from PIL import ImageFont

    for name in ("arial.ttf", "DejaVuSans.ttf", "Arial.ttf"):
        for base in ("C:/Windows/Fonts/", "/usr/share/fonts/truetype/dejavu/", "/Library/Fonts/", ""):
            try:
                return ImageFont.truetype(base + name, size)
            except OSError:
                continue
    return ImageFont.load_default(size=size)


def _text_image(lines: list[str], width: int = 1400, size: int = 56):
    from PIL import Image, ImageDraw

    font = _font(size)
    height = 80 + len(lines) * int(size * 1.6)
    img = Image.new("RGB", (width, height), "white")
    d = ImageDraw.Draw(img)
    y = 40
    for line in lines:
        d.text((60, y), line, fill="black", font=font)
        y += int(size * 1.6)
    return img


def _pdf(path: Path, text: str) -> None:
    import pymupdf

    doc = pymupdf.open()
    paras = text.split("\n\n")
    page = doc.new_page()
    rect = pymupdf.Rect(56, 56, 540, 790)
    y = rect.y0
    for para in paras:
        h = 16 + 14 * (len(para) // 85 + para.count("\n") + 1)
        if y + h > rect.y1:
            page = doc.new_page()
            y = rect.y0
        page.insert_textbox(pymupdf.Rect(rect.x0, y, rect.x1, y + h), para, fontsize=11, fontname="helv")
        y += h
    doc.save(str(path))
    doc.close()


def _scanned_pdf(path: Path, lines: list[str]) -> None:
    import io

    import pymupdf

    img = _text_image(lines, width=1240, size=44)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_image(pymupdf.Rect(40, 60, 555, 60 + 515 * img.height / img.width), stream=buf.getvalue())
    doc.save(str(path))
    doc.close()


def _docx(path: Path, spec: dict) -> None:
    import docx

    d = docx.Document()
    d.add_heading(spec["title"], level=1)
    for p in spec["paras"]:
        d.add_paragraph(p)
    if spec.get("table"):
        rows = spec["table"]
        t = d.add_table(rows=len(rows), cols=len(rows[0]))
        for i, row in enumerate(rows):
            for j, cell in enumerate(row):
                t.cell(i, j).text = cell
    d.save(str(path))


def _pptx(path: Path, slides: list[tuple[str, str, str]]) -> None:
    from pptx import Presentation

    prs = Presentation()
    for title, body, notes in slides:
        s = prs.slides.add_slide(prs.slide_layouts[1])
        s.shapes.title.text = title
        s.placeholders[1].text = body
        s.notes_slide.notes_text_frame.text = notes
    prs.save(str(path))


def build_fixture_folder(dest: Path) -> Path:
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)

    def p(rel: str) -> Path:
        out = dest / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        return out

    for rel, text in DOCS.items():
        _pdf(p(rel), text)
    for rel, lines in SCANNED.items():
        _scanned_pdf(p(rel), lines)
    for rel, spec in DOCX.items():
        _docx(p(rel), spec)
    for rel, slides in PPTX.items():
        _pptx(p(rel), slides)
    for rel, text in TEXTS.items():
        p(rel).write_text(text, encoding="utf-8")
    for rel, lines in IMAGES.items():
        img = _text_image(lines)
        img.save(p(rel), quality=92) if rel.endswith(".jpg") else img.save(p(rel))
    # edge cases
    p("Misc/song.mp3").write_bytes(b"ID3" + b"\x00" * 512)            # unsupported
    p("Misc/empty.txt").write_bytes(b"")                                # empty
    p("node_modules/pkg/readme.txt").write_text("excluded by built-in pattern", encoding="utf-8")
    p(".hidden/secret.txt").write_text("hidden folder, never indexed", encoding="utf-8")
    p("Misc/~$lockfile.docx").write_bytes(b"office lock file")         # excluded (~$*)
    return dest


if __name__ == "__main__":
    out = build_fixture_folder(Path(sys.argv[1] if len(sys.argv) > 1 else "fixture_workspace"))
    print(f"fixture workspace written to {out}")
