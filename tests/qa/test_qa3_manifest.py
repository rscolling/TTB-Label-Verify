"""QA gate 3 — adversarial CSV manifest attacks on POST /api/verify-batch (L2).

Independent of the build agent's tests/test_batch_api.py. Every test judges the
response against two bars: (a) correctness of the parse/match semantics, and
(b) SPEC.md's error-handling criterion — structural CSV problems must come back
as a friendly 400 *before* any extraction spend.

Attack surfaces: duplicate filenames differing only by case, path-traversal
filenames, unicode filenames (accents, full-width digits), line-ending and BOM
variants, quoted fields with embedded commas/newlines, is_import truthiness,
unknown columns, a 10,000-row manifest, an empty CSV, a PNG renamed to .csv,
and a header-only CSV.
"""

from __future__ import annotations

from tests.conftest import FakeExtractor

MANIFEST_HEADER = "filename,brand,class_type,abv,net_contents,producer,origin_country,is_import"


def post_batch(client, files, form=None, manifest: bytes | str | None = None):
    upload = [("files", (name, data, "image/png")) for name, data in files]
    if manifest is not None:
        raw = manifest.encode("utf-8") if isinstance(manifest, str) else manifest
        upload.append(("manifest", ("manifest.csv", raw, "text/csv")))
    return client.post("/api/verify-batch", files=upload, data=form or {})


def manifest_for(rows: list[str]) -> str:
    return "\n".join([MANIFEST_HEADER, *rows])


class TestQa3DuplicateAndTraversalFilenames:
    def test_qa3_duplicate_rows_differing_only_by_case_are_rejected_before_spend(
        self, make_client, png_bytes
    ):
        """'A.PNG' and 'a.png' address the same upload (matching is
        case-insensitive), so they are duplicates and must 400 pre-extraction."""
        extractor = FakeExtractor()
        client = make_client(extractor)
        manifest = manifest_for(["A.PNG,Brand One,,,,,,", "a.png,Brand Two,,,,,,"])
        response = post_batch(client, [("a.png", png_bytes)], manifest=manifest)
        assert response.status_code == 400
        error = response.json()["error"]
        assert error["code"] == "bad_manifest"
        assert "more than once" in error["message"]
        assert extractor.calls == 0

    def test_qa3_windows_traversal_row_matches_by_basename_only(
        self, make_client, png_bytes, good_extraction
    ):
        r"""A manifest row of '..\evil.png' must address the uploaded basename
        'evil.png' (documented basename matching), never a filesystem path."""
        client = make_client(FakeExtractor(good_extraction))
        manifest = manifest_for([r"..\evil.png,Stone's Throw,,,,,,"])
        body = post_batch(client, [("evil.png", png_bytes)], manifest=manifest).json()
        assert body["results"][0]["overall_status"] == "match"

    def test_qa3_posix_subdir_row_matches_uploaded_basename(
        self, make_client, png_bytes, good_extraction
    ):
        client = make_client(FakeExtractor(good_extraction))
        manifest = manifest_for(["dir/01.png,Stone's Throw,,,,,,"])
        body = post_batch(client, [("01.png", png_bytes)], manifest=manifest).json()
        assert body["results"][0]["overall_status"] == "match"

    def test_qa3_uploaded_directory_style_name_matches_plain_row(
        self, make_client, png_bytes, good_extraction
    ):
        """Folder uploads send 'shoot1/01.png'; the row says '01.png'. Both
        sides must normalize."""
        client = make_client(FakeExtractor(good_extraction))
        manifest = manifest_for(["01.png,Stone's Throw,,,,,,"])
        body = post_batch(client, [("shoot1/01.png", png_bytes)], manifest=manifest).json()
        assert body["results"][0]["overall_status"] == "match"

    def test_qa3_hostile_traversal_upload_name_is_echoed_verbatim_and_writes_nothing(
        self, make_client, png_bytes, good_extraction, good_application, tmp_path, monkeypatch
    ):
        r"""An uploaded filename of '..\..\evil.png' is data, not a path: the
        response echoes it verbatim and nothing appears on disk (R8)."""
        monkeypatch.chdir(tmp_path)  # if anything writes relative paths, catch it here
        client = make_client(FakeExtractor(good_extraction))
        hostile = r"..\..\evil.png"
        body = post_batch(client, [(hostile, png_bytes)], form=good_application).json()
        assert body["results"][0]["filename"] == hostile
        assert list(tmp_path.iterdir()) == []


class TestQa3UnicodeFilenames:
    def test_qa3_accented_filename_matches_same_form(self, make_client, png_bytes, good_extraction):
        client = make_client(FakeExtractor(good_extraction))
        manifest = manifest_for(["café.png,Stone's Throw,,,,,,"])
        body = post_batch(client, [("café.png", png_bytes)], manifest=manifest).json()
        assert body["results"][0]["overall_status"] == "match"

    def test_qa3_fullwidth_digit_filename_mismatch_degrades_to_error_entry_not_crash(
        self, make_client, png_bytes, good_extraction
    ):
        """Full-width '０１.png' is NOT folded to ASCII '01.png' (documented
        limitation: matching is case-insensitive, not width-insensitive).
        The file must degrade to a per-label no_application entry, not a 500."""
        client = make_client(FakeExtractor(good_extraction))
        manifest = manifest_for(["01.png,Stone's Throw,,,,,,"])
        body = post_batch(client, [("０１.png", png_bytes)], manifest=manifest).json()
        entry = body["results"][0]
        assert entry["error"]["code"] == "no_application"
        assert "０１.png" in entry["error"]["message"]


class TestQa3EncodingAndLineEndings:
    def test_qa3_crlf_lf_and_no_final_newline_all_parse(
        self, make_client, png_bytes, good_extraction
    ):
        client = make_client(FakeExtractor(good_extraction))
        row = "a.png,Stone's Throw,,,,,,"
        for text in (
            MANIFEST_HEADER + "\r\n" + row + "\r\n",
            MANIFEST_HEADER + "\n" + row + "\n",
            MANIFEST_HEADER + "\n" + row,  # no final newline
        ):
            body = post_batch(client, [("a.png", png_bytes)], manifest=text).json()
            assert body["summary"]["match"] == 1, f"failed for {text!r}"

    def test_qa3_utf8_bom_in_manifest_does_not_break_first_header(
        self, make_client, png_bytes, good_extraction
    ):
        """Excel writes a BOM; if it isn't stripped, the first column reads
        '﻿filename' and the whole manifest 400s."""
        client = make_client(FakeExtractor(good_extraction))
        raw = ("﻿" + manifest_for(["a.png,Stone's Throw,,,,,,"])).encode("utf-8")
        body = post_batch(client, [("a.png", png_bytes)], manifest=raw).json()
        assert body["summary"]["match"] == 1

    def test_qa3_quoted_field_with_comma_and_embedded_newline(
        self, make_client, png_bytes, good_extraction
    ):
        client = make_client(FakeExtractor(good_extraction))
        manifest = 'filename,brand\r\n"a.png","Br,and\nName"\r\n'
        body = post_batch(client, [("a.png", png_bytes)], manifest=manifest).json()
        entry = body["results"][0]
        assert entry["fields"]["brand"]["expected"] == "Br,and\nName"
        # And the verdict engine ran against that exact string (mismatch, not crash).
        assert entry["fields"]["brand"]["verdict"] == "mismatch"

    def test_qa3_png_renamed_to_csv_is_friendly_400_before_spend(self, make_client, png_bytes):
        extractor = FakeExtractor()
        client = make_client(extractor)
        response = post_batch(client, [("a.png", png_bytes)], manifest=png_bytes)
        assert response.status_code == 400
        error = response.json()["error"]
        assert error["code"] == "bad_manifest"
        assert "CSV" in error["message"]  # friendly, actionable
        assert extractor.calls == 0


class TestQa3IsImportTruthiness:
    def test_qa3_true_variants_activate_the_import_origin_check(
        self, make_client, png_bytes, good_extraction
    ):
        """TRUE / Yes / 1 must all mean import. good_extraction has no origin
        statement, so an import application with a country must MISMATCH F6."""
        client = make_client(FakeExtractor(good_extraction))
        for raw in ("TRUE", "Yes", "1", "y"):
            manifest = manifest_for([f"a.png,Stone's Throw,,,,,France,{raw}"])
            body = post_batch(client, [("a.png", png_bytes)], manifest=manifest).json()
            verdict = body["results"][0]["fields"]["origin_country"]["verdict"]
            assert verdict == "mismatch", f"is_import={raw!r} did not activate import checking"

    def test_qa3_false_variants_and_blank_stay_domestic(
        self, make_client, png_bytes, good_extraction
    ):
        client = make_client(FakeExtractor(good_extraction))
        for raw in ("FALSE", "No", "0", "n", ""):
            manifest = manifest_for([f"a.png,Stone's Throw,,,,,,{raw}"])
            body = post_batch(client, [("a.png", png_bytes)], manifest=manifest).json()
            verdict = body["results"][0]["fields"]["origin_country"]["verdict"]
            assert verdict == "na", f"is_import={raw!r} should be domestic (na)"

    def test_qa3_garbage_is_import_is_friendly_400_naming_the_row(
        self, make_client, png_bytes
    ):
        extractor = FakeExtractor()
        client = make_client(extractor)
        manifest = manifest_for(["a.png,Brand,,,,,,oui"])
        response = post_batch(client, [("a.png", png_bytes)], manifest=manifest)
        assert response.status_code == 400
        message = response.json()["error"]["message"]
        assert "is_import" in message
        assert "Row 2" in message  # actionable: tells the agent WHERE
        assert "true or false" in message
        assert extractor.calls == 0


class TestQa3StructureVariants:
    def test_qa3_extra_unknown_columns_are_tolerated(
        self, make_client, png_bytes, good_extraction
    ):
        client = make_client(FakeExtractor(good_extraction))
        manifest = (
            "reviewer,filename,brand,shelf_position\n"
            "Pat,a.png,Stone's Throw,7\n"
        )
        body = post_batch(client, [("a.png", png_bytes)], manifest=manifest).json()
        assert body["summary"]["match"] == 1

    def test_qa3_ten_thousand_row_manifest_with_three_files(
        self, make_client, png_bytes, good_extraction
    ):
        """The chunked UI re-sends the whole spreadsheet with every sub-batch,
        so a huge manifest with few files is the NORMAL case, not an edge."""
        extractor = FakeExtractor(good_extraction)
        client = make_client(extractor)
        rows = [f"f{i}.png,Stone's Throw,,,,,," for i in range(10_000)]
        manifest = manifest_for(rows)
        files = [(f"f{i}.png", png_bytes) for i in range(3)]
        body = post_batch(client, files, manifest=manifest).json()
        assert body["summary"] == {
            "total": 3,
            "match": 3,
            "review": 0,
            "mismatch": 0,
            "error": 0,
            "total_time_ms": body["summary"]["total_time_ms"],
        }
        assert extractor.calls == 3  # 9,997 file-less rows ignored, zero extra spend

    def test_qa3_empty_csv_is_friendly_400(self, make_client, png_bytes):
        client = make_client(FakeExtractor())
        response = post_batch(client, [("a.png", png_bytes)], manifest="")
        assert response.status_code == 400
        message = response.json()["error"]["message"]
        assert "empty" in message.lower()
        assert "filename" in message  # tells the user the expected columns

    def test_qa3_header_only_csv_is_friendly_400(self, make_client, png_bytes):
        client = make_client(FakeExtractor())
        response = post_batch(client, [("a.png", png_bytes)], manifest=MANIFEST_HEADER + "\n")
        assert response.status_code == 400
        assert "no rows" in response.json()["error"]["message"]

    def test_qa3_blank_lines_between_rows_are_tolerated(
        self, make_client, png_bytes, good_extraction
    ):
        client = make_client(FakeExtractor(good_extraction))
        manifest = MANIFEST_HEADER + "\n\na.png,Stone's Throw,,,,,,\n\n\n"
        body = post_batch(client, [("a.png", png_bytes)], manifest=manifest).json()
        assert body["summary"]["match"] == 1

    def test_qa3_reordered_header_columns_still_map_by_name(
        self, make_client, png_bytes, good_extraction
    ):
        client = make_client(FakeExtractor(good_extraction))
        manifest = "brand,filename,is_import\nStone's Throw,a.png,false\n"
        body = post_batch(client, [("a.png", png_bytes)], manifest=manifest).json()
        assert body["summary"]["match"] == 1
