"""Generate the synthetic order PDFs in this directory and their expected results.

Run `uv run python tests/fixtures/generate_fixtures.py` to rebuild. All people, NPIs and
practices are invented. Scanned faxes are image-only (no text layer); the others are text PDFs.
Output is deterministic for a given Pillow version and font.
"""

import io
import json
import random
from pathlib import Path
from typing import Any

import pypdf
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from app.validation import npi_check_digit_ok

HERE = Path(__file__).parent
OFFICIAL_SAMPLE = HERE.parents[1] / "data" / "DME Patient Demo Document CPAP.fax.pdf"
FONT_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
]
DPI = 150
PAGE = (int(8.5 * DPI), 11 * DPI)


def npi(first_nine: str) -> str:
    """A 10-digit NPI with a correct check digit."""
    for digit in "0123456789":
        if npi_check_digit_ok(first_nine + digit):
            return first_nine + digit
    raise ValueError(first_nine)


def bad_npi(first_nine: str) -> str:
    """A 10-digit NPI whose check digit is wrong, like the official sample's."""
    good = npi(first_nine)
    return good[:9] + str((int(good[9]) + 3) % 10)


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in FONT_PATHS:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size)


def scanned_page(lines: list[str], rng: random.Random, angle: float, header: str) -> Image.Image:
    """Render lines as a tilted, speckled, bilevel fax page."""
    page = Image.new("L", PAGE, 255)
    draw = ImageDraw.Draw(page)
    draw.text((60, 25), header, font=_font(20), fill=0)
    y = 130
    for line in lines:
        size = 34 if line.isupper() and len(line) < 40 else 26
        draw.text((110, y), line, font=_font(size), fill=0)
        y += size + 18
    page = page.rotate(angle, resample=Image.Resampling.BICUBIC, fillcolor=255, expand=False)
    page = page.filter(ImageFilter.GaussianBlur(0.8))
    pixels = page.load()
    assert pixels is not None
    for _ in range(9000):
        x, y = rng.randrange(PAGE[0]), rng.randrange(PAGE[1])
        pixels[x, y] = 0 if rng.random() < 0.7 else 255
    return page.point(lambda v: 255 if v > 150 else 0).convert("1")


def degrade(page: Image.Image, rng: random.Random, angle: float) -> Image.Image:
    """Downscale to fax resolution, rotate, speckle and threshold an existing page image."""
    page = page.convert("L").resize(PAGE, Image.Resampling.LANCZOS)
    page = page.rotate(angle, resample=Image.Resampling.BICUBIC, fillcolor=255)
    pixels = page.load()
    assert pixels is not None
    for _ in range(12000):
        x, y = rng.randrange(PAGE[0]), rng.randrange(PAGE[1])
        pixels[x, y] = 0 if rng.random() < 0.7 else 255
    return page.point(lambda v: 255 if v > 160 else 0).convert("1")


def official_pages() -> list[Image.Image]:
    """The official sample's three non-blank page images (it is image-only)."""
    reader = pypdf.PdfReader(OFFICIAL_SAMPLE)
    images = [page.images[0].image for page in reader.pages[:3]]
    return [image.convert("L") for image in images if image is not None]


def image_pdf(pages: list[Image.Image]) -> bytes:
    out = io.BytesIO()
    pages[0].save(out, "PDF", save_all=True, append_images=pages[1:], resolution=DPI)
    return out.getvalue()


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def text_pdf(pages: list[list[str]]) -> bytes:
    """A minimal PDF with a Helvetica text layer, one list of lines per page."""
    objects: list[bytes] = []
    page_ids = [4 + 2 * i for i in range(len(pages))]
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode())
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for index, lines in enumerate(pages):
        content = "BT /F1 11 Tf 15 TL 60 740 Td " + " ".join(
            f"({_escape(line)}) Tj T*" for line in lines
        )
        stream = (content + " ET").encode("latin-1")
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 3 0 R >> >> "
            f"/Contents {page_ids[index] + 1} 0 R >>".encode()
        )
        objects.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % number + body + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1))
    for offset in offsets:
        out.write(b"%010d 00000 n \n" % offset)
    out.write(
        b"trailer << /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    )
    return out.getvalue()


def expect(
    first: str | None,
    last: str | None,
    dob: str | None,
    provider: str | None,
    provider_npi: str | None,
    npi_status: str = "valid",
) -> dict[str, dict[str, str | None]]:
    def field(value: str | None, status: str = "valid") -> dict[str, str | None]:
        return {"value": value, "status": status if value else "missing"}

    return {
        "patient_first_name": field(first),
        "patient_last_name": field(last),
        "patient_date_of_birth": field(dob),
        "provider_name": field(provider),
        "provider_npi": field(provider_npi, npi_status),
    }


def build() -> dict[str, Any]:
    # Seeded noise for reproducible fixtures, not cryptography.
    rng = random.Random(20261106)  # noqa: S311
    expected: dict[str, Any] = {}
    fax = "FROM: 555-010-0199   02/03/2026 09:14AM   PAGE {} OF {}"

    # 1. Image-only scanned fax: tilted, noisy, with a cover page.
    ordering = npi("142856719")
    cover = [
        "FAX COVER SHEET",
        "To: Northwind Medical Supply - Intake",
        "From: Lakeside Pulmonary Associates",
        "Re: New CPAP order",
        "Pages: 2 including cover",
        "",
        "CONFIDENTIAL: intended only for the recipient named above.",
    ]
    order = [
        "DETAILED WRITTEN ORDER",
        "Patient: Rosalind Franklin",
        "Date of Birth: 07/25/1960",
        "Address: 14 Helix Lane, Springfield, IL 62704",
        "",
        "Item: E0601 CPAP device, A7030 full face mask",
        "Diagnosis: G47.33 Obstructive sleep apnea",
        "Length of need: 99 months",
        "",
        "Ordering Provider: Linus Pauling, MD",
        f"NPI: {ordering}",
        "Signature: ____L. Pauling____   Date: 01/28/2026",
    ]
    (HERE / "scanned_fax_tilted.pdf").write_bytes(
        image_pdf(
            [
                scanned_page(cover, rng, 1.5, fax.format(1, 2)),
                scanned_page(order, rng, -2.5, fax.format(2, 2)),
            ]
        )
    )
    expected["scanned_fax_tilted.pdf"] = expect(
        "Rosalind", "Franklin", "1960-07-25", "Linus Pauling", ordering
    )

    # 2. Multi-page: patient on page 1, notes on page 2, prescriber on page 3.
    prescriber = npi("161803398")
    (HERE / "multipage_split.pdf").write_bytes(
        text_pdf(
            [
                [
                    "Riverbend Family Clinic - Patient Demographics",
                    "",
                    "Patient Name: Alan Turing",
                    "DOB: 06/23/1952",
                    "Sex: M",
                    "Insurance: Medicare Part B, MBI 1EG4-TE5-MK73",
                    "Phone: 555-0142",
                ],
                [
                    "Progress Note",
                    "",
                    "Chief complaint: difficulty walking more than 50 feet.",
                    "Assessment: severe osteoarthritis of both knees.",
                    "Plan: standard walker, follow up in 6 weeks.",
                    "Note written by: Rachel Carson, RN",
                ],
                [
                    "Standard Written Order",
                    "",
                    "Item: E0130 walker, rigid, adjustable",
                    "Quantity: 1   Length of need: lifetime",
                    "",
                    "Prescriber: Barbara McClintock, DO",
                    f"Prescriber NPI: {prescriber}",
                    "Prescriber signature: B. McClintock   Date: 01/30/2026",
                ],
            ]
        )
    )
    expected["multipage_split.pdf"] = expect(
        "Alan", "Turing", "1952-06-23", "Barbara McClintock", prescriber
    )

    # 3. Several providers on one form; only the ordering one counts.
    referring, pcp, ordering3 = npi("173205080"), npi("141421356"), npi("122360679")
    (HERE / "multiple_providers.pdf").write_bytes(
        text_pdf(
            [
                [
                    "Home Oxygen Certificate of Medical Necessity",
                    "",
                    "Patient: Carl Sagan        Date of birth: November 9, 1954",
                    "",
                    f"Referring Physician: Edwin Hubble, MD     NPI: {referring}",
                    f"Primary Care Physician: Henrietta Leavitt, MD   NPI: {pcp}",
                    "",
                    "Item: E1390 oxygen concentrator, 2 L/min continuous",
                    "Qualifying SpO2: 86% at rest on room air",
                    "",
                    "ORDERING PHYSICIAN (signs below)",
                    "Name: Vera Rubin, MD",
                    f"NPI: {ordering3}",
                    "Signature: V. Rubin   Date: 02/01/2026",
                ]
            ]
        )
    )
    expected["multiple_providers.pdf"] = expect(
        "Carl", "Sagan", "1954-11-09", "Vera Rubin", ordering3
    )

    # 4. No date of birth anywhere: it must come back null.
    no_dob = npi("193649122")
    (HERE / "missing_dob.pdf").write_bytes(
        image_pdf(
            [
                scanned_page(
                    [
                        "PRESCRIPTION",
                        "Patient: Hedy Lamarr",
                        "Address: 9 Frequency Way, Vienna, VA 22180",
                        "Phone: 555-0177",
                        "",
                        "Rx: K0001 standard wheelchair",
                        "Dx: M62.81 muscle weakness",
                        "",
                        "Prescribing physician: Jonas Salk, MD",
                        f"NPI {no_dob}",
                        "Date: 01/15/2026",
                    ],
                    rng,
                    0.8,
                    fax.format(1, 1),
                )
            ]
        )
    )
    expected["missing_dob.pdf"] = expect("Hedy", "Lamarr", None, "Jonas Salk", no_dob)

    # 5. Unusual labels, last-name-first patient name.
    unusual = npi("127182818")
    (HERE / "unusual_labels.pdf").write_bytes(
        text_pdf(
            [
                [
                    "DME REQUEST FORM",
                    "",
                    "Pt Name: LOVELACE, ADA",
                    "D.O.B.: 12-10-1915",
                    "MRN: 00418",
                    "",
                    "Equip: E0260 hospital bed, semi-electric",
                    "",
                    "Ordering Physician: Charles Babbage M.D.",
                    f"NPI#: {unusual}",
                    "Phys. Sig: C. Babbage",
                ]
            ]
        )
    )
    expected["unusual_labels.pdf"] = expect(
        "Ada", "Lovelace", "1915-12-10", "Charles Babbage", unusual
    )

    # 6. Scanned order whose NPI fails the check digit (like the official sample).
    fake = bad_npi("198765432")
    (HERE / "scanned_fake_npi.pdf").write_bytes(
        image_pdf(
            [
                scanned_page(
                    [
                        "STANDARD WRITTEN ORDER",
                        "Patient Name: Katherine Johnson",
                        "Patient DOB: 08/26/1918",
                        "",
                        "Item: E0114 crutches, underarm, pair",
                        "",
                        "Prescriber: Dorothy Vaughan, NP",
                        f"Prescriber NPI: {fake}",
                        "Date: 01/22/2026",
                    ],
                    rng,
                    -1.2,
                    fax.format(1, 1),
                )
            ]
        )
    )
    expected["scanned_fake_npi.pdf"] = expect(
        "Katherine", "Johnson", "1918-08-26", "Dorothy Vaughan", fake, "invalid_checksum"
    )
    # 7-8. The official sample, degraded; then with the date of birth blacked out.
    if OFFICIAL_SAMPLE.exists():
        pages = official_pages()
        (HERE / "official_degraded.pdf").write_bytes(
            image_pdf([degrade(pg, rng, a) for pg, a in zip(pages, (3.0, -2.5, 3.2), strict=True)])
        )
        sample = expect(
            "Marie", "Curie", "1900-12-05", "Arjun Raj", "9182734556", "invalid_checksum"
        )
        expected["official_degraded.pdf"] = sample
        redacted = [pg.copy() for pg in pages]
        # Page 1 "Patient Date of Birth" value and page 2 "DOB:" value, in source pixels.
        ImageDraw.Draw(redacted[0]).rectangle((2150, 762, 2600, 850), fill=0)
        ImageDraw.Draw(redacted[1]).rectangle((480, 955, 880, 1050), fill=0)
        (HERE / "official_dob_redacted.pdf").write_bytes(
            image_pdf([degrade(pg, rng, 1.0) for pg in redacted])
        )
        expected["official_dob_redacted.pdf"] = {
            **sample,
            "patient_date_of_birth": {"value": None, "status": "missing"},
        }

    # 9. Five-page messy fax with distractor people on every page.
    referring9, ordering9 = npi("135792468"), npi("108642097")
    five = [
        [
            "FAX COVER SHEET",
            "From: Harborview Orthopedics",
            "Phone: 555-0110   Fax: 555-0111",
            "Contact: Nancy Drew, Intake Coordinator",
            "To: Northwind Medical Supply",
            "Pages: 5 including cover",
        ],
        [
            "INSURANCE INFORMATION",
            "Primary: Medicare Part B   MBI: 7AB2-CD3-EF45",
            "Secondary: Blue Shield PPO   Member ID: BSX448812",
            "Subscriber: self",
            "",
            "Emergency contact: James Hamilton (spouse)",
            "Emergency contact phone: 555-0123",
        ],
        [
            "PATIENT DEMOGRAPHICS",
            "Name: Margaret Hamilton",
            "Date of birth: Dec 5, 1900",
            "Sex: F",
            "Address: 42 Apollo Road, Boston, MA 02110",
        ],
        [
            "ORDER DETAILS",
            "Item: E0143 walker, folding, wheeled",
            "Diagnosis: M17.0 bilateral primary osteoarthritis of knee",
            "Length of need: 99 months",
            f"Referring physician: Henry Gray, MD   NPI: {referring9}",
        ],
        [
            "PHYSICIAN ATTESTATION",
            "I certify this equipment is medically necessary.",
            "",
            "Ordering physician: Elizabeth Blackwell, MD",
            f"NPI: {ordering9}",
            "Signature: E. Blackwell   Date: 02/02/2026",
        ],
    ]
    (HERE / "messy_five_page.pdf").write_bytes(
        image_pdf(
            [
                scanned_page(lines, rng, angle, fax.format(i + 1, 5))
                for i, (lines, angle) in enumerate(
                    zip(five, (1.2, -1.0, 1.8, -1.5, 1.1), strict=True)
                )
            ]
        )
    )
    five_expected = expect("Margaret", "Hamilton", "1900-12-05", "Elizabeth Blackwell", ordering9)
    for field, page in {
        "patient_first_name": 3,
        "patient_last_name": 3,
        "patient_date_of_birth": 3,
        "provider_name": 5,
        "provider_npi": 5,
    }.items():
        five_expected[field]["page"] = str(page)
    expected["messy_five_page.pdf"] = five_expected

    # 10. Two complete orders for two patients in one packet.
    npi_a, npi_b = npi("111222333"), npi("144455566")
    (HERE / "two_patient_packet.pdf").write_bytes(
        text_pdf(
            [
                [
                    "STANDARD WRITTEN ORDER (1 of 2)",
                    "Patient: Chien-Shiung Wu   DOB: 05/31/1912",
                    "Item: E0601 CPAP device",
                    "Ordering physician: Lise Meitner, MD   NPI: " + npi_a,
                ],
                [
                    "STANDARD WRITTEN ORDER (2 of 2)",
                    "Patient: Emmy Noether   DOB: 03/23/1922",
                    "Item: E0100 cane",
                    "Ordering physician: Max Born, MD   NPI: " + npi_b,
                ],
            ]
        )
    )
    # Either complete order is acceptable (or nulls); fields from both patients never are.
    expected["two_patient_packet.pdf"] = {
        "any_of": [
            expect("Chien-Shiung", "Wu", "1912-05-31", "Lise Meitner", npi_a),
            expect("Emmy", "Noether", "1922-03-23", "Max Born", npi_b),
        ]
    }
    return expected


if __name__ == "__main__":
    results = build()
    (HERE / "expected.json").write_text(json.dumps(results, indent=2) + "\n")
    for path in sorted(HERE.glob("*.pdf")):
        print(f"{path.name}: {path.stat().st_size // 1024} KiB")
