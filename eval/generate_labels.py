#!/usr/bin/env python3
"""Synthetic TTB test-label generator — eval harness ground truth.

Renders 16 fictional bottle labels (14 direct + 2 degraded variants) as HTML/CSS -> PNG (Playwright/Chromium),
derives 2 photo-degraded variants (Pillow/numpy, fixed seed), and writes
eval/manifest.json declaring, per label, the submitted application data and
the expected per-field verdicts.

Run:  python eval/generate_labels.py
Deps: playwright (chromium installed), pillow, numpy — see eval/.venv-labels.

All brand/producer names are FICTIONAL. The statutory government-warning text
is reproduced exactly from docs/SPEC.md (27 CFR 16.21) on compliant labels.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter

EVAL_DIR = Path(__file__).resolve().parent
LABELS_DIR = EVAL_DIR / "labels"
TEMPLATE = (EVAL_DIR / "templates" / "label_base.html").read_text(encoding="utf-8")

SEED = 20260717  # fixed seed — regeneration must be verdict-stable

# ---------------------------------------------------------------------------
# Statutory warning (27 CFR 16.21) — must match docs/SPEC.md exactly.
# ---------------------------------------------------------------------------
WARNING_CLAUSE_1 = (
    "(1) According to the Surgeon General, women should not drink "
    "alcoholic beverages during pregnancy because of the risk of birth defects."
)
WARNING_CLAUSE_2 = (
    "(2) Consumption of alcoholic beverages impairs your ability to drive a car or "
    "operate machinery, and may cause health problems."
)
STATUTORY_WARNING = f"GOVERNMENT WARNING: {WARNING_CLAUSE_1} {WARNING_CLAUSE_2}"


def warning_html(variant: str) -> str:
    """Return the warning block HTML for a given trap variant."""
    if variant == "standard":
        return (
            f'<p class="warning"><b>GOVERNMENT WARNING:</b> '
            f"{WARNING_CLAUSE_1} {WARNING_CLAUSE_2}</p>"
        )
    if variant == "titlecase":  # trap 2 — prefix not ALL CAPS -> F7 fail
        return (
            f'<p class="warning"><b>Government Warning:</b> '
            f"{WARNING_CLAUSE_1} {WARNING_CLAUSE_2}</p>"
        )
    if variant == "wordswap":  # trap 3 — one word changed -> F7 fail w/ clause diff
        swapped = WARNING_CLAUSE_2.replace(
            "may cause health problems", "might cause health problems"
        )
        assert swapped != WARNING_CLAUSE_2
        return (
            f'<p class="warning"><b>GOVERNMENT WARNING:</b> '
            f"{WARNING_CLAUSE_1} {swapped}</p>"
        )
    if variant == "cosmetic":  # trap 4 — whitespace-only deviations -> F7 pass
        # pre-wrap block: literal double spaces + hard line breaks mid-clause.
        # Text content is the statutory text verbatim; only whitespace differs.
        return (
            '<p class="warning pre"><b>GOVERNMENT WARNING:</b>  '
            "(1) According to the Surgeon General, women should not\n"
            "drink alcoholic beverages during pregnancy  because of the risk of\n"
            "birth defects.  (2) Consumption of alcoholic beverages impairs your\n"
            "ability to drive a car or  operate machinery, and may cause health\n"
            "problems.</p>"
        )
    raise ValueError(f"unknown warning variant: {variant}")


# ---------------------------------------------------------------------------
# Visual styles — varied palettes/fonts so labels look like bottle labels.
# ---------------------------------------------------------------------------
STYLES = {
    "bourbon": dict(
        paper="#f3ead7", ink="#2b1d12", accent="#8c5a2b",
        body_font="'Palatino Linotype', 'Book Antiqua', serif",
        brand_font="'Georgia', serif", brand_size="72px", brand_weight="bold",
        brand_spacing="6px", brand_color="#4a2c14",
        frame_border="3px double #8c5a2b", frame_outline="1px solid #8c5a2b",
        outline_offset="5px", band_text="#f3ead7",
    ),
    "wine": dict(
        paper="#faf6ee", ink="#2e2a26", accent="#6b1f2a",
        body_font="'Garamond', 'Times New Roman', serif",
        brand_font="'Constantia', 'Cambria', serif", brand_size="66px",
        brand_weight="normal", brand_spacing="8px", brand_color="#6b1f2a",
        frame_border="1px solid #6b1f2a", frame_outline="none",
        outline_offset="0", band_text="#faf6ee",
    ),
    "beer": dict(
        paper="#fdf1dc", ink="#1e2a32", accent="#d96c2f",
        body_font="'Trebuchet MS', 'Verdana', sans-serif",
        brand_font="'Impact', 'Arial Black', sans-serif", brand_size="78px",
        brand_weight="normal", brand_spacing="3px", brand_color="#1e2a32",
        frame_border="4px solid #1e2a32", frame_outline="none",
        outline_offset="0", band_text="#fdf1dc",
    ),
    "gin": dict(
        paper="#eef3f1", ink="#12343b", accent="#12656b",
        body_font="'Candara', 'Corbel', sans-serif",
        brand_font="'Copperplate Gothic Bold', 'Cambria', serif",
        brand_size="64px", brand_weight="bold", brand_spacing="10px",
        brand_color="#12343b", frame_border="2px solid #12656b",
        frame_outline="1px solid #12656b", outline_offset="4px",
        band_text="#eef3f1",
    ),
    "dark": dict(
        paper="#221d19", ink="#e8ddc8", accent="#c9a24b",
        body_font="'Cambria', serif",
        brand_font="'Georgia', serif", brand_size="70px", brand_weight="bold",
        brand_spacing="7px", brand_color="#c9a24b",
        frame_border="2px solid #c9a24b", frame_outline="1px solid #c9a24b",
        outline_offset="5px", band_text="#221d19",
    ),
    "vodka": dict(
        paper="#f6f8fa", ink="#26313d", accent="#4a6fa5",
        body_font="'Segoe UI', 'Arial', sans-serif",
        brand_font="'Franklin Gothic Medium', 'Arial Black', sans-serif",
        brand_size="74px", brand_weight="normal", brand_spacing="12px",
        brand_color="#26313d", frame_border="1px solid #4a6fa5",
        frame_outline="none", outline_offset="0", band_text="#f6f8fa",
    ),
}


def build_content(spec: dict) -> str:
    """Assemble the inner label HTML from a label spec."""
    parts: list[str] = []
    layout = spec.get("layout", "classic")

    if layout == "band":
        parts.append(
            f'<div class="band"><div class="brand">{spec["brand_html"]}</div>'
            f'<div class="topline" style="color:inherit;margin:8px 0 0 0;">'
            f'{spec.get("topline", "")}</div></div>'
        )
    else:
        if spec.get("topline"):
            parts.append(f'<div class="topline">{spec["topline"]}</div>')
        parts.append(f'<div class="brand">{spec["brand_html"]}</div>')

    if spec.get("tagline"):
        parts.append(f'<div class="tagline">{spec["tagline"]}</div>')
    parts.append('<hr class="rule">')
    parts.append(f'<div class="classtype">{spec["class_type_html"]}</div>')
    parts.append(f'<div class="stats">{spec["abv_html"]}'
                 f'<span class="sep">&bull;</span>{spec["net_html"]}</div>')
    if spec.get("extras"):
        parts.append(f'<div class="extras">{spec["extras"]}</div>')
    parts.append('<hr class="rule thin">')
    producer_html = "<br>".join(spec["producer_lines"])
    parts.append(f'<div class="producer">{producer_html}</div>')
    if spec.get("origin_html"):
        parts.append(f'<div class="origin">{spec["origin_html"]}</div>')
    parts.append(warning_html(spec["warning_variant"]))
    return "\n".join(parts)


def render_html(spec: dict) -> str:
    style = dict(STYLES[spec["style"]])
    return TEMPLATE.format(content=build_content(spec), **style)


# ---------------------------------------------------------------------------
# Label definitions: visual spec + application data + expected verdicts.
# expected values are LISTS of acceptable verdicts (match/review/mismatch/na).
# ---------------------------------------------------------------------------
ALL_MATCH = {
    "brand": ["match"], "class_type": ["match"], "abv": ["match"],
    "net_contents": ["match"], "producer": ["match"],
    "origin_country": ["na"], "warning": ["match"],
}


def expected(**overrides) -> dict:
    out = {k: list(v) for k, v in ALL_MATCH.items()}
    for k, v in overrides.items():
        out[k] = v
    return out


LABELS: list[dict] = [
    # ---- clean baselines -------------------------------------------------
    dict(
        id="01-bourbon-clean", trap_case=None,
        description="Clean baseline: Kentucky straight bourbon, all 7 fields present and matching.",
        spec=dict(
            style="bourbon", layout="classic",
            topline="Est. 1987 &mdash; Small Batch",
            brand_html="COPPER HOLLOW",
            tagline="Aged Four Years in New Charred Oak",
            class_type_html="Kentucky Straight Bourbon Whiskey",
            abv_html="45% Alc./Vol. (90 Proof)", net_html="750 mL",
            extras="Batch No. 12 &mdash; Bottled in Bardstown",
            producer_lines=[
                "Distilled and Bottled by Copper Hollow Distilling Co.",
                "412 Millrace Road, Bardstown, Kentucky 40004",
            ],
            warning_variant="standard",
        ),
        application=dict(
            brand="Copper Hollow",
            class_type="Kentucky Straight Bourbon Whiskey",
            abv=45.0, net_contents="750 mL",
            producer="Copper Hollow Distilling Co., 412 Millrace Road, Bardstown, Kentucky 40004",
            origin_country=None, is_import=False,
        ),
        expected=expected(),
    ),
    dict(
        id="02-wine-clean", trap_case=8,
        description="Clean baseline: domestic red wine. Embodies trap 8 — domestic product with no country of origin on label -> origin N/A, not fail.",
        spec=dict(
            style="wine", layout="classic",
            topline="Columbia Valley",
            brand_html="SILVERBROOK CELLARS",
            tagline="Estate Grown &amp; Bottled",
            class_type_html="Red Wine",
            abv_html="Alc. 13.5% by Vol.", net_html="750 mL",
            extras="Vintage 2023",
            producer_lines=[
                "Produced and Bottled by Silverbrook Cellars",
                "88 Orchard Bench Lane, Walla Walla, Washington 99362",
            ],
            warning_variant="standard",
        ),
        application=dict(
            brand="Silverbrook Cellars", class_type="Red Wine",
            abv=13.5, net_contents="750 mL",
            producer="Silverbrook Cellars, 88 Orchard Bench Lane, Walla Walla, Washington 99362",
            origin_country=None, is_import=False,
        ),
        expected=expected(),
    ),
    dict(
        id="03-beer-clean", trap_case=None,
        description="Clean baseline: domestic IPA in fl oz.",
        spec=dict(
            style="beer", layout="band",
            topline="Hop-Forward &mdash; Cold Conditioned",
            brand_html="GRANITE LEDGE",
            class_type_html="India Pale Ale",
            abv_html="6.5% ALC/VOL", net_html="12 FL OZ",
            producer_lines=[
                "Brewed and Canned by Granite Ledge Brewing Co.",
                "27 Switchback Trail, Missoula, Montana 59802",
            ],
            warning_variant="standard",
        ),
        application=dict(
            brand="Granite Ledge", class_type="India Pale Ale",
            abv=6.5, net_contents="12 fl oz",
            producer="Granite Ledge Brewing Co., 27 Switchback Trail, Missoula, Montana 59802",
            origin_country=None, is_import=False,
        ),
        expected=expected(),
    ),
    dict(
        id="04-gin-import-clean", trap_case=None,
        description="Clean baseline: imported London dry gin with country-of-origin statement.",
        spec=dict(
            style="gin", layout="classic",
            topline="Distilled with Nine Botanicals",
            brand_html="THORNGATE",
            tagline="London Dry",
            class_type_html="London Dry Gin",
            abv_html="47% Alc./Vol.", net_html="700 mL",
            producer_lines=[
                "Distilled by Thorngate Distillery Ltd., Marsh Lane, London, England",
                "Imported by Harbor &amp; Main Imports, Baltimore, Maryland",
            ],
            origin_html="Product of England",
            warning_variant="standard",
        ),
        application=dict(
            brand="Thorngate", class_type="London Dry Gin",
            abv=47.0, net_contents="700 mL",
            producer="Thorngate Distillery Ltd., Marsh Lane, London, England",
            origin_country="England", is_import=True,
        ),
        expected=expected(origin_country=["match"]),
    ),
    # ---- trap cases ------------------------------------------------------
    dict(
        id="05-brand-case-fuzzy", trap_case=1,
        description="Trap 1: label brand STONE'S THROW (all caps, curly apostrophe) vs application 'Stone's Throw' -> brand match via case-insensitive fuzzy.",
        spec=dict(
            style="dark", layout="classic",
            topline="Single Barrel Selection",
            brand_html="STONE’S THROW",
            tagline="Hand Numbered &mdash; Barrel Proofed Down",
            class_type_html="Straight Bourbon Whiskey",
            abv_html="45% Alc./Vol.", net_html="750 mL",
            producer_lines=[
                "Distilled and Bottled by Stone's Throw Distillers",
                "1 Quarry Bend Road, Frankfort, Kentucky 40601",
            ],
            warning_variant="standard",
        ),
        application=dict(
            brand="Stone's Throw", class_type="Straight Bourbon Whiskey",
            abv=45.0, net_contents="750 mL",
            producer="Stone's Throw Distillers, 1 Quarry Bend Road, Frankfort, Kentucky 40601",
            origin_country=None, is_import=False,
        ),
        expected=expected(),
    ),
    dict(
        id="06-warning-titlecase", trap_case=2,
        description="Trap 2: warning prefix rendered 'Government Warning:' (title case) -> warning fail on the ALL-CAPS rule; text otherwise verbatim.",
        spec=dict(
            style="bourbon", layout="classic",
            topline="Barrel House Reserve",
            brand_html="BLACKPINE RESERVE",
            tagline="Matured in Toasted Oak",
            class_type_html="Straight Bourbon Whiskey",
            abv_html="46% Alc./Vol. (92 Proof)", net_html="750 mL",
            producer_lines=[
                "Distilled and Bottled by Blackpine Reserve Distilling",
                "310 Kilnhouse Avenue, Lexington, Kentucky 40507",
            ],
            warning_variant="titlecase",
        ),
        application=dict(
            brand="Blackpine Reserve", class_type="Straight Bourbon Whiskey",
            abv=46.0, net_contents="750 mL",
            producer="Blackpine Reserve Distilling, 310 Kilnhouse Avenue, Lexington, Kentucky 40507",
            origin_country=None, is_import=False,
        ),
        expected=expected(warning=["mismatch"]),
    ),
    dict(
        id="07-warning-word-swap", trap_case=3,
        description="Trap 3: warning clause 2 says 'might cause health problems' instead of 'may cause health problems' -> warning fail with clause diff.",
        spec=dict(
            style="wine", layout="classic",
            topline="Dry Creek Bench",
            brand_html="REDLANDS RANCH",
            tagline="Old Vine &mdash; Unfiltered",
            class_type_html="Zinfandel",
            abv_html="14.2% Alc./Vol.", net_html="750 mL",
            extras="Vintage 2022",
            producer_lines=[
                "Produced and Bottled by Redlands Ranch Winery",
                "740 Terrace Loop, Healdsburg, California 95448",
            ],
            warning_variant="wordswap",
        ),
        application=dict(
            brand="Redlands Ranch", class_type="Zinfandel",
            abv=14.2, net_contents="750 mL",
            producer="Redlands Ranch Winery, 740 Terrace Loop, Healdsburg, California 95448",
            origin_country=None, is_import=False,
        ),
        expected=expected(warning=["mismatch"]),
    ),
    dict(
        id="08-warning-cosmetic", trap_case=4,
        description="Trap 4: statutory warning verbatim but with double spaces and hard line breaks mid-clause; producer name uses a curly apostrophe vs straight in application -> warning match after whitespace/quote normalization.",
        spec=dict(
            style="dark", layout="classic",
            topline="Cask Strength &mdash; Pot Still",
            brand_html="MARINER’S COVE",
            tagline="Spiced Rum",
            class_type_html="Rum with Natural Spices",
            abv_html="52% Alc./Vol. (104 Proof)", net_html="750 mL",
            producer_lines=[
                "Distilled and Bottled by Mariner’s Cove Distilling Co.",
                "19 Drydock Street, Charleston, South Carolina 29401",
            ],
            warning_variant="cosmetic",
        ),
        application=dict(
            brand="Mariner's Cove", class_type="Rum with Natural Spices",
            abv=52.0, net_contents="750 mL",
            producer="Mariner's Cove Distilling Co., 19 Drydock Street, Charleston, South Carolina 29401",
            origin_country=None, is_import=False,
        ),
        expected=expected(),
    ),
    dict(
        id="09-proof-only", trap_case=5,
        description="Trap 5: label states alcohol content only as '90 Proof'; application says 45% -> abv match via proof = 2 x ABV conversion.",
        spec=dict(
            style="bourbon", layout="classic",
            topline="Sour Mash &mdash; Bottled in Bond Style",
            brand_html="OLD KESTREL",
            tagline="A Whiskey of Patience",
            class_type_html="Straight Bourbon Whiskey",
            abv_html="90 PROOF", net_html="750 mL",
            producer_lines=[
                "Distilled and Bottled by Old Kestrel Distillery",
                "66 Creekstone Pike, Loretto, Kentucky 40037",
            ],
            warning_variant="standard",
        ),
        application=dict(
            brand="Old Kestrel", class_type="Straight Bourbon Whiskey",
            abv=45.0, net_contents="750 mL",
            producer="Old Kestrel Distillery, 66 Creekstone Pike, Loretto, Kentucky 40037",
            origin_country=None, is_import=False,
        ),
        expected=expected(),
    ),
    dict(
        id="10-abv-wrong", trap_case=6,
        description="Trap 6: label says 40% Alc./Vol.; application says 45% -> abv mismatch.",
        spec=dict(
            style="vodka", layout="band",
            topline="Distilled Six Times",
            brand_html="FALLING WATER",
            class_type_html="Vodka",
            abv_html="40% Alc./Vol.", net_html="750 mL",
            producer_lines=[
                "Distilled and Bottled by Falling Water Spirits LLC",
                "5150 Coldspring Parkway, Rochester, New York 14604",
            ],
            warning_variant="standard",
        ),
        application=dict(
            brand="Falling Water", class_type="Vodka",
            abv=45.0, net_contents="750 mL",
            producer="Falling Water Spirits LLC, 5150 Coldspring Parkway, Rochester, New York 14604",
            origin_country=None, is_import=False,
        ),
        expected=expected(abv=["mismatch"]),
    ),
    dict(
        id="11-netcontents-cl", trap_case=7,
        description="Trap 7a: label net contents '75 cL' vs application '750 mL' -> net contents match via unit normalization. Imported sparkling wine with matching origin.",
        spec=dict(
            style="wine", layout="classic",
            topline="M&eacute;thode Traditionnelle",
            brand_html="VELVET ANTLER",
            tagline="Brut &mdash; C&ocirc;te des Aubes",
            class_type_html="Sparkling Wine",
            abv_html="12% Alc./Vol.", net_html="75 cL",
            producer_lines=[
                "Produced by Maison Velvet Antler, &Eacute;pernay Region, France",
                "Imported by Trellis &amp; Vine Imports, Portland, Oregon",
            ],
            origin_html="Product of France",
            warning_variant="standard",
        ),
        application=dict(
            brand="Velvet Antler", class_type="Sparkling Wine",
            abv=12.0, net_contents="750 mL",
            producer="Maison Velvet Antler, Epernay Region, France",
            origin_country="France", is_import=True,
        ),
        expected=expected(origin_country=["match"]),
    ),
    dict(
        id="12-netcontents-wrong", trap_case=7,
        description="Trap 7b: label net contents 700 mL vs application 750 mL -> net contents mismatch (outside 1% tolerance).",
        spec=dict(
            style="gin", layout="band",
            topline="Juniper &mdash; Coriander &mdash; Bitter Orange",
            brand_html="JUNIPER FLATS",
            class_type_html="Dry Gin",
            abv_html="44% Alc./Vol.", net_html="700 mL",
            producer_lines=[
                "Distilled and Bottled by Juniper Flats Botanical Works",
                "902 Alkali Basin Road, Bend, Oregon 97701",
            ],
            warning_variant="standard",
        ),
        application=dict(
            brand="Juniper Flats", class_type="Dry Gin",
            abv=44.0, net_contents="750 mL",
            producer="Juniper Flats Botanical Works, 902 Alkali Basin Road, Bend, Oregon 97701",
            origin_country=None, is_import=False,
        ),
        expected=expected(net_contents=["mismatch"]),
    ),
    dict(
        id="13-import-missing-origin", trap_case=9,
        description="Trap 9: application marks the product as an import from Scotland but the label carries no country-of-origin statement -> origin mismatch.",
        spec=dict(
            style="dark", layout="classic",
            topline="Aged Twelve Years",
            brand_html="PEATSMOKE HOLLOW",
            tagline="Non Chill-Filtered",
            class_type_html="Single Malt Whisky",
            abv_html="43% Alc./Vol.", net_html="750 mL",
            producer_lines=[
                "Distilled and Bottled by Peatsmoke Hollow Distillery",
                "Imported by Firth &amp; Fog Selections, Boston, Massachusetts",
            ],
            warning_variant="standard",
        ),
        application=dict(
            brand="Peatsmoke Hollow", class_type="Single Malt Whisky",
            abv=43.0, net_contents="750 mL",
            producer="Peatsmoke Hollow Distillery",
            origin_country="Scotland", is_import=True,
        ),
        expected=expected(origin_country=["mismatch"]),
    ),
    dict(
        id="14-classtype-wrong", trap_case=None,
        description="Class/type mismatch: label says Kentucky Straight Bourbon Whiskey; application says Straight Rye Whiskey -> class_type mismatch (F2 fuzzy fail path).",
        spec=dict(
            style="bourbon", layout="classic",
            topline="Twin Copper Pot Stills",
            brand_html="SIX BRIDGES",
            tagline="Charred Oak &mdash; Limestone Water",
            class_type_html="Kentucky Straight Bourbon Whiskey",
            abv_html="47.5% Alc./Vol. (95 Proof)", net_html="750 mL",
            producer_lines=[
                "Distilled and Bottled by Six Bridges Distilling Co.",
                "48 Ferry Landing Road, Louisville, Kentucky 40202",
            ],
            warning_variant="standard",
        ),
        application=dict(
            brand="Six Bridges", class_type="Straight Rye Whiskey",
            abv=47.5, net_contents="750 mL",
            producer="Six Bridges Distilling Co., 48 Ferry Landing Road, Louisville, Kentucky 40202",
            origin_country=None, is_import=False,
        ),
        expected=expected(class_type=["mismatch"]),
    ),
]

# Degraded variants of label 01 (trap 10 / R6 robustness). A degraded image may
# legitimately fall to needs-review but must NEVER yield a wrong definitive verdict.
DEGRADED_EXPECTED = {
    "brand": ["match", "review"], "class_type": ["match", "review"],
    "abv": ["match", "review"], "net_contents": ["match", "review"],
    "producer": ["match", "review"], "origin_country": ["na", "review"],
    "warning": ["match", "review"],
}

DEGRADED: list[dict] = [
    dict(
        id="15-bourbon-angled", trap_case=10, source="01-bourbon-clean",
        description="Trap 10 / robustness: label 01 photographed at an angle (rotation + perspective skew). Fields may fall to review; never a wrong definitive verdict.",
        transform="angled",
    ),
    dict(
        id="16-bourbon-glare", trap_case=10, source="01-bourbon-clean",
        description="Trap 10 / robustness: label 01 with a glare hotspot and slight blur. Fields may fall to review; never a wrong definitive verdict.",
        transform="glare",
    ),
]


# ---------------------------------------------------------------------------
# Photo degradation (Pillow/numpy, deterministic).
# ---------------------------------------------------------------------------
def _find_coeffs(source_quad, target_quad):
    """Coefficients for PIL PERSPECTIVE mapping target -> source."""
    matrix = []
    for (sx, sy), (tx, ty) in zip(source_quad, target_quad):
        matrix.append([tx, ty, 1, 0, 0, 0, -sx * tx, -sx * ty])
        matrix.append([0, 0, 0, tx, ty, 1, -sy * tx, -sy * ty])
    a = np.array(matrix, dtype=np.float64)
    b = np.array(source_quad, dtype=np.float64).reshape(8)
    return np.linalg.solve(a, b)


def degrade_angled(img: Image.Image) -> Image.Image:
    img = img.convert("RGB")
    backdrop = (188, 182, 170)
    rot = img.rotate(-6.5, expand=True, resample=Image.BICUBIC, fillcolor=backdrop)
    w, h = rot.size
    # mild keystone: top edge pinched, bottom pulled
    target = [(w * 0.045, h * 0.025), (w * 0.965, 0), (w, h * 0.985), (0.0, h)]
    source = [(0, 0), (w, 0), (w, h), (0, h)]
    coeffs = _find_coeffs(source, target)
    out = rot.transform((w, h), Image.PERSPECTIVE, coeffs, Image.BICUBIC,
                        fillcolor=backdrop)
    # slight dimming, as if photographed indoors
    arr = np.asarray(out, dtype=np.float32) * 0.92
    return Image.fromarray(arr.astype(np.uint8))


def degrade_glare(img: Image.Image) -> Image.Image:
    img = img.convert("RGB")
    w, h = img.size
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    cx, cy, radius = w * 0.68, h * 0.20, w * 0.55
    dist = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2)
    alpha = np.clip(1.0 - dist / radius, 0.0, 1.0) ** 2 * 0.78
    arr = np.asarray(img, dtype=np.float32)
    out = arr + (255.0 - arr) * alpha[..., None]
    glared = Image.fromarray(out.astype(np.uint8))
    return glared.filter(ImageFilter.GaussianBlur(1.15))


TRANSFORMS = {"angled": degrade_angled, "glare": degrade_glare}


# ---------------------------------------------------------------------------
# Rendering + manifest
# ---------------------------------------------------------------------------
def render_all() -> None:
    from playwright.sync_api import sync_playwright

    LABELS_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1200, "height": 1800},
                                device_scale_factor=1)
        for label in LABELS:
            html = render_html(label["spec"])
            page.set_content(html, wait_until="load")
            target = LABELS_DIR / f"{label['id']}.png"
            page.locator("#label").screenshot(path=str(target))
            print(f"rendered {target.name}")
        browser.close()

    random.seed(SEED)
    np.random.seed(SEED % (2 ** 32))
    for dg in DEGRADED:
        src = Image.open(LABELS_DIR / f"{dg['source']}.png")
        out = TRANSFORMS[dg["transform"]](src)
        target = LABELS_DIR / f"{dg['id']}.png"
        out.save(target)
        print(f"degraded {target.name}")


def build_manifest() -> dict:
    entries = []
    for label in LABELS:
        entries.append(dict(
            file=f"labels/{label['id']}.png",
            trap_case=label["trap_case"],
            description=label["description"],
            application=label["application"],
            expected=label["expected"],
        ))
    src_by_id = {l["id"]: l for l in LABELS}
    for dg in DEGRADED:
        src = src_by_id[dg["source"]]
        entries.append(dict(
            file=f"labels/{dg['id']}.png",
            trap_case=dg["trap_case"],
            description=dg["description"],
            application=src["application"],
            expected={k: list(v) for k, v in DEGRADED_EXPECTED.items()},
        ))
    return {"labels": entries}


VERDICTS = {"match", "review", "mismatch", "na"}
FIELDS = ["brand", "class_type", "abv", "net_contents", "producer",
          "origin_country", "warning"]


def self_check(manifest: dict) -> None:
    assert STATUTORY_WARNING.startswith("GOVERNMENT WARNING: (1) According to")
    # compliant warning HTML must contain the statutory text verbatim
    import re
    flat = re.sub(r"<[^>]+>", "", warning_html("standard"))
    assert flat == STATUTORY_WARNING, "standard warning drifted from statutory text"
    # cosmetic variant must equal statutory text after whitespace collapse
    flat_cos = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", warning_html("cosmetic")))
    assert flat_cos == STATUTORY_WARNING, "cosmetic warning is not whitespace-only deviation"

    entries = manifest["labels"]
    assert len(entries) == 16, f"expected 16 labels, got {len(entries)}"
    covered = set()
    for e in entries:
        png = EVAL_DIR / e["file"]
        assert png.exists(), f"missing image {e['file']}"
        img = Image.open(png)
        assert img.size[0] >= 950, f"{e['file']} narrower than ~1000px: {img.size}"
        for field in FIELDS:
            verdicts = e["expected"][field]
            assert verdicts and all(v in VERDICTS for v in verdicts), \
                f"bad verdicts for {field} in {e['file']}: {verdicts}"
        for key in ("brand", "class_type", "abv", "net_contents", "producer",
                    "origin_country", "is_import"):
            assert key in e["application"], f"application missing {key} in {e['file']}"
        if e["trap_case"] is not None:
            covered.add(e["trap_case"])
    missing = set(range(1, 11)) - covered
    assert not missing, f"canonical traps not covered: {missing}"
    print(f"self-check OK: 16 labels, traps covered: {sorted(covered)}")


def main() -> None:
    render_all()
    manifest = build_manifest()
    manifest_path = EVAL_DIR / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
                             encoding="utf-8")
    print(f"wrote {manifest_path}")
    self_check(manifest)


if __name__ == "__main__":
    main()
