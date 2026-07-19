/* TTB Label Checker — unified worksheet flow (WP5 + WP7 format-agnostic form).
   One flow: drop 1..N label photos + an optional submittal form (CSV, TSV,
   Excel, PDF, or a photo of the form), press Run. Non-CSV forms are read
   through POST /api/ingest-form into normalized rows the agent can preview
   BEFORE scanning; at submit those rows are serialized back to the canonical
   CSV and ride the frozen /api/verify-batch `manifest` field, so the batch
   contract is untouched. Plain CSVs keep the legacy raw-file path (the
   server's manifest parser stays the authority).

   The client chunks the photos through POST /api/verify-batch (CHUNK_SIZE per
   request) so progress reflects real server completion, and builds a
   worksheet: one row per label with a serial number, scan timestamp,
   thumbnail, the extracted value per field with a compact status mark, a
   score, and a PASS / FAIL / REVIEW result. FAIL and REVIEW rows are flagged
   for human review; clicking a row (or its Review button) opens a drill-down
   panel with the label photo large and the field-by-field comparison.

   Serial numbers, timestamps, scores, pass/fail, and the required-elements
   check are all CLIENT-derived from the server's per-field verdicts — the API
   response shapes are frozen.

   SECURITY: every dynamic string (extracted values, file names, reasons,
   error messages) is rendered with textContent or set as an attribute string
   — never innerHTML. Extraction output and file names are hostile until
   proven otherwise. Images render from client-side object URLs of the kept
   File objects — nothing is stored server-side (R8). */

"use strict";

(function () {
  var CHUNK_SIZE = 10;   // labels per request: real progress ticks, bounded requests
  var MAX_FILES = 300;   // mirrors the server's batch cap
  var COLUMN_COUNT = 13; // serial, scanned-at, elapsed time, photo, 7 fields, score, result (with Review button)

  var FIELD_ORDER = [
    "brand", "class_type", "abv", "net_contents",
    "producer", "origin_country", "government_warning"
  ];

  var FIELD_NAMES = {
    brand: "Brand name",
    class_type: "Kind of drink",
    abv: "Alcohol content",
    net_contents: "Amount in bottle",
    producer: "Producer",
    origin_country: "Country of origin",
    government_warning: "Health warning"
  };

  /* Compact per-cell marks: icon + accessible text (title + visually hidden
     span) — never color alone. */
  var MARKS = {
    match: { icon: "✓", text: "Matches", cls: "mark-match" },
    review: { icon: "⚠", text: "Needs review", cls: "mark-review" },
    mismatch: { icon: "✗", text: "Doesn't match", cls: "mark-mismatch" },
    na: { icon: "—", text: "Not checked", cls: "mark-na" },
    nodata: { icon: "⚠", text: "No submittal data — needs review", cls: "mark-review" },
    required: { icon: "⚠", text: "Required element — not found in this photo", cls: "mark-review" }
  };

  var STATUSES = {
    pass: { badge: "PASS", cls: "status-pass", flagged: false },
    fail: { badge: "FAIL", cls: "status-fail", flagged: true },
    review: { badge: "REVIEW", cls: "status-review", flagged: true },
    error: { badge: "ERROR", cls: "status-error", flagged: true },
    // Form row with no uploaded photo (fewer images than submittal items).
    missing: { badge: "MISSING", cls: "status-missing", flagged: true },
    // Human reviewer decisions (client-only; the scan verdict stays in row.status).
    approved: { badge: "APPROVED", cls: "status-pass", flagged: false },
    denied: { badge: "DENIED", cls: "status-fail", flagged: false }
  };

  /* The status a row displays: the reviewer's decision wins over the scan. */
  function effectiveStatus(row) {
    if (row.review === "approved") { return "approved"; }
    if (row.review === "denied") { return "denied"; }
    return row.status;
  }

  var NO_DATA_TEXT = "No submittal data — needs review";

  /* Fields verified against the submittal form. The health warning is checked
     against the statutory text (27 CFR 16.21), not the submittal form, so its
     verdict stays meaningful even without a CSV. */
  var SUBMITTAL_FIELDS = [
    "brand", "class_type", "abv", "net_contents", "producer", "origin_country"
  ];

  /* ---------- required-elements check (deterministic, zero model calls) ----

     Grounded in TTB's mandatory-label-information guidance, per class family:
       - Malt beverages: 27 CFR part 7 subpart E (brand 7.64, class/type 7.65,
         producer name+address 7.66-7.68, net contents 7.70) + TTB G 2023-12.
         Alcohol content is deliberately NOT required here: for malt beverages
         it is mandatory only when the alcohol derives from added flavors or
         other added non-beverage ingredients (27 CFR 7.63(a)(3)).
       - Distilled spirits: 27 CFR 5.61 / 5.64 / 5.65 / 5.141 + TTB G 2021-2.
         Alcohol content IS required (5.65), and 5.61 requires it in the same
         field of vision as the brand and class/type — hence the specially
         worded reason when the photo shows both but no alcohol content.
       - Wine: 27 CFR 4.32-4.36 + TTB G 2019-8. Alcohol content required.
         (Wines under 7% ABV fall under FDA labeling rules, and the
         appellation-of-origin rules (4.23 / 4.27) are out of scope — both are
         documented growth path, see APPROACH.md.)
     Common to all: the statutory health warning (27 CFR part 16 / 16.21).
     Origin is required only for imports (malt 27 CFR 7.69 — CBP rules); the
     submittal's is_import flag drives that check deterministically
     server-side (app/rules/origin.py renders MISMATCH for an import with no
     origin statement), so the client adds nothing for it here.

     SEVERITY: a required element not found in the photo flags the row at
     REVIEW, not FAIL — net contents and the producer statement may be blown,
     embossed, or molded into the container (7.70, 7.66-7.68), and the health
     warning may sit on a front, back, or side label (27 CFR part 16), so a
     single photo legitimately may not show them. A row that already FAILs on
     a real mismatch stays FAIL (worst-issue logic unchanged). */

  var REQUIRED_MISSING_TEXT =
    "Not found in this photo — it may appear on another label or be embossed " +
    "on the container. Verify before approving.";
  var SPIRITS_ABV_FOV_TEXT =
    "Distilled spirits must show alcohol content in the same field of vision " +
    "as the brand and class (27 CFR 5.61) — not found in this photo. " +
    "Verify before approving.";

  var CORE_REQUIRED = [
    { key: "brand", name: "Brand name" },
    { key: "class_type", name: "Kind of drink" },
    { key: "net_contents", name: "Amount in bottle" },
    { key: "producer", name: "Producer" },
    { key: "government_warning", name: "Health warning" }
  ];
  var ABV_REQUIRED = { key: "abv", name: "Alcohol content" };

  /* Class-family inference from the declared (or, without a submittal, the
     extracted) class/type. Keyword order matters: spirits before malt so
     "Single Malt Whisky" reads as spirits, wine's "port" is boundary-matched
     so "Porter" stays malt. Unknown/absent class -> the generic core set. */
  var FAMILY_MATCHERS = [
    { family: "spirits", re: /(^|[^a-z0-9])(whiskey|whisky|bourbon|rye|vodka|gin|rum|tequila|mezcal|brandy|cognac|liqueur|schnapps|spirits?)([^a-z0-9]|$)/ },
    { family: "wine", re: /(^|[^a-z0-9])(wine|champagne|sparkling|vermouth|port|sherry|riesling|chardonnay|cabernet|zinfandel|merlot|rosé)([^a-z0-9]|$)/ },
    { family: "malt", re: /(^|[^a-z0-9])(beer|ale|lager|stout|porter|ipa|pilsner|malt)([^a-z0-9]|$)/ }
  ];

  function classFamily(classTypeText) {
    if (!classTypeText) { return null; }
    var text = String(classTypeText).toLowerCase();
    for (var i = 0; i < FAMILY_MATCHERS.length; i++) {
      if (FAMILY_MATCHERS[i].re.test(text)) { return FAMILY_MATCHERS[i].family; }
    }
    return null;
  }

  function fieldValuePresent(entry, key) {
    var field = entry.fields[key];
    var value = field ? field.extracted : null;
    return !(value === null || value === undefined || String(value).trim() === "");
  }

  /* -> [{key, name, reason}] for the required elements NOT found on the label
     in this photo. Empty for error entries.
     Prefer the server's required_elements payload (QA P1-4) so API and UI agree;
     fall back to the local check for older responses. */
  function missingRequired(entry) {
    if (entry.error) { return []; }
    if (entry.required_elements && Array.isArray(entry.required_elements.missing)) {
      return entry.required_elements.missing.map(function (element) {
        return {
          key: element.key,
          name: element.name,
          reason: element.reason || REQUIRED_MISSING_TEXT
        };
      }).filter(function (element) {
        // Client UI only marks the seven worksheet fields; disclosure hints
        // (sulfites, etc.) stay out of per-cell marks.
        return FIELD_ORDER.indexOf(element.key) !== -1;
      });
    }
    var classField = entry.fields.class_type || {};
    var family = classFamily(classField.expected || classField.extracted);
    var required = CORE_REQUIRED.slice();
    if (family === "spirits" || family === "wine") { required.push(ABV_REQUIRED); }
    var missing = [];
    required.forEach(function (element) {
      if (fieldValuePresent(entry, element.key)) { return; }
      var reason = REQUIRED_MISSING_TEXT;
      if (
        family === "spirits" && element.key === "abv" &&
        fieldValuePresent(entry, "brand") && fieldValuePresent(entry, "class_type")
      ) {
        reason = SPIRITS_ABV_FOV_TEXT;  // 27 CFR 5.61 field-of-vision rule
      }
      missing.push({ key: element.key, name: element.name, reason: reason });
    });
    return missing;
  }

  function isRequiredMissing(row, key) {
    if (!row.requiredMissing) { return false; }
    return row.requiredMissing.some(function (element) { return element.key === key; });
  }

  /* ---------- elements ---------- */

  var form = document.getElementById("scan-form");
  var dropzone = document.getElementById("dropzone");
  var fileInput = document.getElementById("file-input");
  var dropzoneEmpty = document.getElementById("dropzone-empty");
  var dropzoneSelected = document.getElementById("dropzone-selected");
  var fileSummary = document.getElementById("file-summary");
  var csvDropzone = document.getElementById("csv-dropzone");
  var csvDropzoneEmpty = document.getElementById("csv-dropzone-empty");
  var csvDropzoneSelected = document.getElementById("csv-dropzone-selected");
  var csvInput = document.getElementById("csv-input");
  var csvStatus = document.getElementById("csv-status");
  var csvClear = document.getElementById("csv-clear");
  var scanButton = document.getElementById("scan-button");
  var progressBlock = document.getElementById("progress-block");
  var progressText = document.getElementById("progress-text");
  var progressBar = document.getElementById("progress-bar");
  var errorCallout = document.getElementById("error-callout");
  var errorMessage = document.getElementById("error-message");
  var resultsSection = document.getElementById("results");
  var banner = document.getElementById("banner");
  var bannerIcon = document.getElementById("banner-icon");
  var bannerText = document.getElementById("banner-text");
  var timing = document.getElementById("timing");
  var worksheetBody = document.getElementById("worksheet-body");
  var downloadButton = document.getElementById("download-csv");
  var formWarnings = document.getElementById("form-warnings");
  var ingestPreview = document.getElementById("ingest-preview");
  var ingestPreviewBody = document.getElementById("ingest-preview-table").querySelector("tbody");
  var matchNotice = document.getElementById("match-notice");
  var recapBar = document.getElementById("recap-bar");
  var recapText = document.getElementById("recap-text");
  var newScanButton = document.getElementById("new-scan");
  var uploadHeading = document.getElementById("upload-heading");

  var selectedFiles = [];
  var csvFile = null;
  var rows = [];          // one record per scanned label, in serial order
  var openDetail = null;  // { row, detailTr, button } — at most one open panel

  /* ---------- worksheet filter (client-only, over effective status) ---------- */

  var filterGroup = document.getElementById("filter-group");
  var FILTER_BUCKETS = {
    passed: { pass: true, approved: true },
    failed: { fail: true, denied: true }
  };
  var activeFilter = "all";

  function applyFilter() {
    var bucket = FILTER_BUCKETS[activeFilter];
    rows.forEach(function (row) {
      row.tr.hidden = !!(bucket && !bucket[effectiveStatus(row)]);
    });
    if (openDetail && openDetail.row.tr.hidden) { closeDetailPanel(false); }
  }

  function setFilter(name) {
    activeFilter = name;
    Array.prototype.forEach.call(
      filterGroup.querySelectorAll("button[data-filter]"),
      function (button) {
        var on = button.getAttribute("data-filter") === name;
        button.classList.toggle("is-active", on);
        button.setAttribute("aria-pressed", String(on));
      }
    );
    applyFilter();
  }

  filterGroup.addEventListener("click", function (event) {
    var button = event.target.closest("button[data-filter]");
    if (button) { setFilter(button.getAttribute("data-filter")); }
  });

  /* ---------- file selection ---------- */

  function clearWorksheet() {
    closeDetailPanel(false);
    rows.forEach(function (row) {
      if (row.url) { URL.revokeObjectURL(row.url); }
    });
    rows = [];
    worksheetBody.textContent = "";
    resultsSection.hidden = true;
    setFilter("all");
    // Leaving review mode (WP8): whatever cleared the worksheet, the upload
    // form is the page again.
    recapBar.hidden = true;
    form.hidden = false;
  }

  /* Empty step-1 / step-2 dropzones (used by "Start a new scan"). */
  function clearPhotoSelection() {
    selectedFiles = [];
    fileInput.value = "";
    fileSummary.textContent = "";
    dropzoneEmpty.hidden = false;
    dropzoneSelected.hidden = true;
  }

  function clearFormSelection() {
    csvInput.value = "";
    csvFile = null;
    csvDropzoneEmpty.hidden = false;
    csvDropzoneSelected.hidden = true;
    csvStatus.textContent = "";
    resetIngest();
  }

  function setFiles(fileList) {
    var files = [];
    for (var i = 0; i < fileList.length; i++) { files.push(fileList[i]); }
    if (files.length === 0) { return; }
    selectedFiles = files;
    fileSummary.textContent = files.length === 1
      ? "1 photo selected (" + files[0].name + ")"
      : files.length + " photos selected";
    dropzoneEmpty.hidden = true;
    dropzoneSelected.hidden = false;
    // New photos start a new scan: never leave a stale worksheet on screen.
    clearWorksheet();
    hideError();
  }

  fileInput.addEventListener("change", function () {
    if (fileInput.files) { setFiles(fileInput.files); }
  });

  ["dragenter", "dragover"].forEach(function (name) {
    dropzone.addEventListener(name, function (event) {
      event.preventDefault();
      dropzone.classList.add("dragover");
    });
  });

  ["dragleave", "drop"].forEach(function (name) {
    dropzone.addEventListener(name, function (event) {
      event.preventDefault();
      dropzone.classList.remove("dragover");
    });
  });

  dropzone.addEventListener("drop", function (event) {
    var files = event.dataTransfer && event.dataTransfer.files;
    if (files && files.length > 0) { setFiles(files); }
  });

  // Clicking anywhere in the dropzone opens the file picker (the labels
  // inside already do this natively; don't double-open).
  dropzone.addEventListener("click", function (event) {
    if (event.target.closest("label") || event.target === fileInput) { return; }
    fileInput.click();
  });

  /* ---------- submittal form selection (a dropzone, sibling to step 1) ------
     The form can be a CSV/TSV, an Excel sheet, a PDF, or a photo. Selecting
     one immediately POSTs it to /api/ingest-form so the agent can eyeball the
     parsed rows BEFORE scanning. Plain CSVs stay usable even if the preview
     read fails — the raw file goes to the server at scan time, which remains
     the authority on manifest errors. */

  var ingestToken = 0;   // bump to invalidate any in-flight ingest response
  var ingest = { status: "none", file: null, rows: null, sourceKind: null, warnings: [] };

  var PREVIEW_ROW_CAP = 50;
  var PREVIEW_COLUMNS = [
    "filename", "brand", "class_type", "abv",
    "net_contents", "producer", "origin_country", "is_import"
  ];

  function resetIngest() {
    ingestToken += 1;
    ingest = { status: "none", file: null, rows: null, sourceKind: null, warnings: [] };
    formWarnings.textContent = "";
    formWarnings.hidden = true;
    ingestPreview.hidden = true;
    ingestPreview.open = false;
    ingestPreviewBody.textContent = "";
    matchNotice.hidden = true;
    matchNotice.textContent = "";
  }

  function renderIngestPreview(previewRows) {
    ingestPreviewBody.textContent = "";
    previewRows.slice(0, PREVIEW_ROW_CAP).forEach(function (row, index) {
      var tr = document.createElement("tr");
      var num = document.createElement("td");
      num.textContent = String(index + 1);
      tr.appendChild(num);
      PREVIEW_COLUMNS.forEach(function (key) {
        var td = document.createElement("td");
        var value = key === "is_import" ? (row.is_import ? "yes" : "no") : row[key];
        // LLM-parsed rows are hostile until proven otherwise: textContent only.
        td.textContent = (value === null || value === undefined || value === "")
          ? "—" : String(value);
        tr.appendChild(td);
      });
      ingestPreviewBody.appendChild(tr);
    });
    if (previewRows.length > PREVIEW_ROW_CAP) {
      var more = document.createElement("tr");
      var td = document.createElement("td");
      td.colSpan = PREVIEW_COLUMNS.length + 1;
      td.textContent = "…and " + (previewRows.length - PREVIEW_ROW_CAP) + " more rows.";
      more.appendChild(td);
      ingestPreviewBody.appendChild(more);
    }
  }

  function ingestReady(file, body) {
    ingest = {
      status: "ready", file: file, rows: body.rows,
      sourceKind: body.source_kind, warnings: body.warnings || []
    };
    csvStatus.textContent = "Read " + body.rows.length +
      (body.rows.length === 1 ? " row" : " rows") + " from “" + file.name + "”.";
    formWarnings.textContent = "";
    ingest.warnings.forEach(function (warning) {
      var item = document.createElement("li");
      item.textContent = warning;
      formWarnings.appendChild(item);
    });
    formWarnings.hidden = ingest.warnings.length === 0;
    renderIngestPreview(body.rows);
    ingestPreview.hidden = false;
  }

  function ingestFailed(file, message) {
    if (looksLikeCsv(file)) {
      // CSVs keep working exactly as before: the raw file is sent at scan
      // time and the server's manifest parser reports any problem then.
      ingest = { status: "failed", file: file, rows: null, sourceKind: null, warnings: [] };
      csvStatus.textContent = "Using “" + file.name +
        "” — each photo will be checked against its row.";
      return;
    }
    // Non-CSV forms can't be scanned without a successful read: friendly
    // callout, nothing left selected.
    csvInput.value = "";
    syncCsv();
    showError(message);
  }

  function startIngest(file) {
    var token = ++ingestToken;
    ingest = { status: "loading", file: file, rows: null, sourceKind: null, warnings: [] };
    var formData = new FormData();
    formData.append("file", file, file.name);
    fetch("/api/ingest-form", { method: "POST", body: formData })
      .then(function (response) {
        return response.json().catch(function () { return null; }).then(function (body) {
          if (token !== ingestToken) { return; }  // a newer selection won
          if (!response.ok || !body || !body.rows) {
            var message = (body && body.error && body.error.message) ||
              "We couldn't read that form. Please try again.";
            ingestFailed(file, message);
            return;
          }
          ingestReady(file, body);
        });
      })
      .catch(function () {
        if (token !== ingestToken) { return; }
        ingestFailed(file,
          "We couldn't reach the form reading service. Check the connection and re-add the form.");
      });
  }

  function syncCsv() {
    csvFile = (csvInput.files && csvInput.files.length > 0) ? csvInput.files[0] : null;
    var hasCsv = csvFile !== null;
    csvDropzoneEmpty.hidden = hasCsv;
    csvDropzoneSelected.hidden = !hasCsv;
    resetIngest();
    if (hasCsv) {
      // Synchronous status line first; the async ingest below refines it to
      // "Read N rows from …" once the parse lands.
      csvStatus.textContent = looksLikeCsv(csvFile)
        ? "Using “" + csvFile.name + "” — each photo will be checked against its row."
        : "Reading “" + csvFile.name + "”…";
      startIngest(csvFile);
    }
    // Results on screen were scored against the previous form — clear them,
    // same rule as choosing new photos. Mid-scan the worksheet is left
    // alone: the running scan uses the rows/file snapshotted at submit.
    if (!scanning) {
      clearWorksheet();
    }
  }

  csvInput.addEventListener("change", syncCsv);
  csvClear.addEventListener("click", function () {
    csvInput.value = "";
    syncCsv();
  });
  syncCsv();

  // Real, keyboard-operable buttons open the picker for the hidden input
  // (both the empty-state and the "choose a different form" button).
  Array.prototype.forEach.call(
    document.querySelectorAll(".csv-choose"),
    function (button) {
      button.addEventListener("click", function () { csvInput.click(); });
    }
  );

  ["dragenter", "dragover"].forEach(function (name) {
    csvDropzone.addEventListener(name, function (event) {
      event.preventDefault();
      csvDropzone.classList.add("dragover");
    });
  });

  ["dragleave", "drop"].forEach(function (name) {
    csvDropzone.addEventListener(name, function (event) {
      event.preventDefault();
      csvDropzone.classList.remove("dragover");
    });
  });

  function looksLikeCsv(file) {
    return /\.csv$/i.test(file.name) || file.type === "text/csv";
  }

  function looksLikeForm(file) {
    return /\.(csv|tsv|txt|xlsx|pdf|png|jpe?g|webp)$/i.test(file.name) ||
      file.type === "text/csv" || file.type === "application/pdf" ||
      /^image\//.test(file.type);
  }

  csvDropzone.addEventListener("drop", function (event) {
    var files = event.dataTransfer && event.dataTransfer.files;
    if (!files || files.length === 0) { return; }
    if (files.length > 1 || !looksLikeForm(files[0])) {
      showError("That doesn't look like a submittal form — drop one CSV, Excel " +
        "(.xlsx), PDF, or photo of the form here, or use " +
        "“Choose form from your computer”.");
      return;
    }
    // The hidden input stays the source of truth: assign the dropped file to
    // it, then run the same change path as the picker (replace semantics,
    // worksheet clearing, status line).
    csvInput.files = files;
    syncCsv();
    hideError();
  });

  // Clicking anywhere else in the form dropzone opens the picker (the buttons
  // inside already have their own jobs, and the warnings list + "Show what
  // was read" preview are their own interactive surfaces; don't double-open).
  csvDropzone.addEventListener("click", function (event) {
    if (event.target.closest("button, label, details, ul") || event.target === csvInput) { return; }
    csvInput.click();
  });

  /* ---------- status helpers ---------- */

  function showError(message) {
    errorMessage.textContent = message;
    errorCallout.hidden = false;
    // Focus the callout so screen readers announce it (role=alert is backup).
    errorCallout.focus({ preventScroll: false });
  }

  function hideError() {
    errorCallout.hidden = true;
  }

  var scanning = false;

  function setBusy(busy) {
    scanning = busy;
    scanButton.disabled = busy;
    scanButton.textContent = busy ? "Running…" : "Run";
  }

  function updateProgress(done, total) {
    progressText.textContent = "Scanned " + done + " of " + total + "…";
    progressBar.max = total;
    progressBar.value = done;
  }

  /* ---------- timestamps (addendum: per-row scan date-time stamp) ---------- */

  function pad2(n) { return (n < 10 ? "0" : "") + n; }

  function stampParts(date) {
    return {
      date: date.getFullYear() + "-" + pad2(date.getMonth() + 1) + "-" + pad2(date.getDate()),
      time: pad2(date.getHours()) + ":" + pad2(date.getMinutes()) + ":" + pad2(date.getSeconds())
    };
  }

  function displayStamp(date) {  // "2026-07-18 19:42:07" — local, 24h
    var p = stampParts(date);
    return p.date + " " + p.time;
  }

  function isoStamp(date) {      // "2026-07-18T19:42:07" — ISO 8601 local
    var p = stampParts(date);
    return p.date + "T" + p.time;
  }

  /* Per-label elapsed time (R2: the elapsed time is displayed on every
     result). Sourced from the server's per-label processing_time_ms — the
     API shapes already carry it; error entries have none. */

  function elapsedSecondsValue(entry) {  // "4.9" (one decimal) or null
    if (!entry || typeof entry.processing_time_ms !== "number") { return null; }
    return (entry.processing_time_ms / 1000).toFixed(1);
  }

  function elapsedSecondsText(entry) {   // "4.9s" or "6.2s (over 5s budget)" or null
    var value = elapsedSecondsValue(entry);
    if (value === null) { return null; }
    var over = entry && typeof entry.processing_time_ms === "number" &&
      entry.processing_time_ms > 5000;
    return over ? value + "s (over 5s budget)" : value + "s";
  }

  function isOverBudget(entry) {
    return !!(entry && typeof entry.processing_time_ms === "number" &&
      entry.processing_time_ms > 5000);
  }

  /* ---------- row scoring (client-derived; API shapes untouched) ---------- */

  function pad3(n) {
    if (n < 10) { return "00" + n; }
    if (n < 100) { return "0" + n; }
    return String(n);
  }

  function markFor(row, key) {
    if (!row.hasSubmittal && SUBMITTAL_FIELDS.indexOf(key) !== -1) {
      return isRequiredMissing(row, key) ? MARKS.required : MARKS.nodata;
    }
    var field = row.entry.fields[key];
    var mark = (field && MARKS[field.verdict]) || MARKS.review;
    // A required element the submittal didn't ask about ("Not checked") still
    // has to be printed on the label — surface the miss instead of a dash.
    // A real comparison verdict (match/mismatch/review) is more informative
    // and keeps precedence.
    if (mark === MARKS.na && isRequiredMissing(row, key)) { return MARKS.required; }
    return mark;
  }

  /* Score + pass/fail for one row.
     - error entry            -> ERROR (flagged)
     - with a form: any mismatch -> FAIL; any review -> REVIEW; else PASS,
       score = matches / applicable (applicable = verdict !== "na")
     - without a form: never a silent pass — the six submittal-checked fields
       have nothing to compare against, so the row is at best REVIEW; the
       statutory health-warning check still runs, so a warning mismatch
       still makes the row FAIL.
     - a required label element not found in the photo flags the row at
       REVIEW level (it may sit on another label of the set or be embossed on
       the container — see the required-elements block above); it never
       downgrades an existing FAIL and never upgrades one to FAIL. */
  function computeOutcome(row) {
    if (row.entry.error) {
      return { status: "error", scoreText: "—" };
    }
    var requiredMiss = (row.requiredMissing || []).length > 0;
    if (!row.hasSubmittal) {
      var warning = row.entry.fields.government_warning;
      var status = warning && warning.verdict === "mismatch" ? "fail" : "review";
      return { status: status, scoreText: NO_DATA_TEXT };
    }
    var matches = 0;
    var applicable = 0;
    var worst = "pass";
    FIELD_ORDER.forEach(function (key) {
      var field = row.entry.fields[key];
      if (!field || field.verdict === "na") { return; }
      applicable += 1;
      if (field.verdict === "match") { matches += 1; }
      if (field.verdict === "mismatch") { worst = "fail"; }
      else if (field.verdict === "review" && worst !== "fail") { worst = "review"; }
    });
    if (requiredMiss && worst === "pass") { worst = "review"; }
    return {
      status: worst,
      scoreText: matches + "/" + applicable + " fields match"
    };
  }

  /* ---------- worksheet rendering ---------- */

  function shorten(text, max) {
    if (text.length <= max) { return text; }
    return text.slice(0, max - 1) + "…";
  }

  function markSpan(mark) {
    var span = document.createElement("span");
    span.className = "mark " + mark.cls;
    span.title = mark.text;
    var icon = document.createElement("span");
    icon.setAttribute("aria-hidden", "true");
    icon.textContent = mark.icon;
    span.appendChild(icon);
    var sr = document.createElement("span");
    sr.className = "visually-hidden";
    sr.textContent = mark.text;
    span.appendChild(sr);
    return span;
  }

  function fieldCell(row, key) {
    var td = document.createElement("td");
    td.className = "field-cell";
    td.setAttribute("data-label", FIELD_NAMES[key]);
    td.appendChild(markSpan(markFor(row, key)));
    var field = row.entry.fields[key];
    var value = field ? field.extracted : null;
    var span = document.createElement("span");
    span.className = "cell-value";
    if (value === null || value === undefined || value === "") {
      span.textContent = "—";
      span.className += " muted";
    } else {
      var text = String(value);
      span.textContent = shorten(text, 60);
      if (text.length > 60) { td.title = text; }
    }
    td.appendChild(span);
    return td;
  }

  function statusCell(row, reviewButton) {
    var td = document.createElement("td");
    td.className = "status-cell";
    td.setAttribute("data-label", "Result");
    var status = STATUSES[row.status];
    var badge = document.createElement("span");
    badge.className = "status-badge " + status.cls;
    badge.textContent = status.badge;
    td.appendChild(badge);
    row.badgeEl = badge;
    var sr = document.createElement("span");
    sr.className = "visually-hidden";
    sr.textContent = status.flagged ? " — flagged for human review" : "";
    td.appendChild(sr);
    row.flagSrEl = sr;
    td.appendChild(reviewButton);
    return td;
  }

  /* Re-render one row's status after a reviewer decision (badge, row tint,
     flag, screen-reader text), then re-apply the active filter and banner. */
  function refreshRowStatus(row) {
    var key = effectiveStatus(row);
    var status = STATUSES[key];
    row.badgeEl.className = "status-badge " + status.cls;
    row.badgeEl.textContent = status.badge;
    row.tr.className = "worksheet-row" + (status.flagged ? " row-" + key : "");
    row.flagSrEl.textContent = status.flagged ? " — flagged for human review" : "";
    if (row.flagEl) { row.flagEl.hidden = !status.flagged; }
    applyFilter();
    renderBanner();
  }

  function appendRow(row) {
    var tr = document.createElement("tr");
    tr.className = "worksheet-row";

    var serialTd = document.createElement("td");
    serialTd.className = "serial-cell";
    serialTd.setAttribute("data-label", "Serial");
    serialTd.textContent = row.serial;
    tr.appendChild(serialTd);

    var timeTd = document.createElement("td");
    timeTd.className = "time-cell";
    timeTd.setAttribute("data-label", "Scanned at");
    timeTd.textContent = displayStamp(row.scannedAt);
    tr.appendChild(timeTd);

    var elapsedTd = document.createElement("td");
    elapsedTd.className = "elapsed-cell" + (isOverBudget(row.entry) ? " over-budget" : "");
    elapsedTd.setAttribute("data-label", "Time");
    elapsedTd.textContent = elapsedSecondsText(row.entry) || "—";
    if (isOverBudget(row.entry)) {
      elapsedTd.title = "This label took longer than the 5 second target (R2).";
    }
    tr.appendChild(elapsedTd);

    var photoTd = document.createElement("td");
    photoTd.className = "photo-cell";
    photoTd.setAttribute("data-label", "Photo");
    if (row.url) {
      var thumb = document.createElement("img");
      thumb.className = "thumb";
      thumb.alt = "";
      thumb.src = row.url;
      photoTd.appendChild(thumb);
    } else {
      var noPhoto = document.createElement("span");
      noPhoto.className = "photo-name muted";
      noPhoto.textContent = "No photo";
      photoTd.appendChild(noPhoto);
    }
    var name = document.createElement("span");
    name.className = "photo-name";
    name.textContent = row.filename;
    photoTd.appendChild(name);
    tr.appendChild(photoTd);

    if (row.entry.error) {
      var errorTd = document.createElement("td");
      errorTd.className = "error-cell";
      errorTd.colSpan = FIELD_ORDER.length;
      errorTd.setAttribute("data-label", "Problem");
      var prefix = row.status === "missing" ? "" : "Couldn't scan — ";
      errorTd.textContent = prefix + row.entry.error.message;
      tr.appendChild(errorTd);
    } else {
      FIELD_ORDER.forEach(function (key) {
        tr.appendChild(fieldCell(row, key));
      });
    }

    var scoreTd = document.createElement("td");
    scoreTd.className = "score-cell";
    scoreTd.setAttribute("data-label", "Score");
    scoreTd.textContent = row.scoreText;
    tr.appendChild(scoreTd);

    var button = document.createElement("button");
    button.type = "button";
    button.className = "button button-secondary review-button";
    button.textContent = "Review";
    button.setAttribute("aria-expanded", "false");
    tr.appendChild(statusCell(row, button));

    if (STATUSES[row.status].flagged) {
      tr.classList.add("row-" + row.status);
      var flag = document.createElement("span");
      flag.className = "flag";
      flag.setAttribute("aria-hidden", "true");
      flag.textContent = " ⚑";
      serialTd.appendChild(flag);
      row.flagEl = flag;
    }
    row.tr = tr;
    var bucket = FILTER_BUCKETS[activeFilter];
    tr.hidden = !!(bucket && !bucket[effectiveStatus(row)]);

    button.addEventListener("click", function () {
      toggleDetail(row, tr, button);
    });
    // The whole row is a click target too (the real, keyboard-accessible
    // control is the Review button).
    tr.addEventListener("click", function (event) {
      if (event.target.closest("button")) { return; }
      toggleDetail(row, tr, button);
    });

    worksheetBody.appendChild(tr);
  }

  /* ---------- review drill-down panel ---------- */

  /* Click-to-enlarge lightbox for the label photo: one native <dialog>,
     reused. Esc and any click close it. */
  var lightbox = document.createElement("dialog");
  lightbox.className = "lightbox";
  var lightboxClose = document.createElement("button");
  lightboxClose.type = "button";
  lightboxClose.className = "lightbox-close";
  lightboxClose.setAttribute("aria-label", "Close the enlarged photo");
  lightboxClose.textContent = "✕";
  lightbox.appendChild(lightboxClose);
  var lightboxImg = document.createElement("img");
  lightboxImg.alt = "";
  lightbox.appendChild(lightboxImg);
  lightbox.addEventListener("click", function () { lightbox.close(); });
  document.body.appendChild(lightbox);

  function openLightbox(src, alt) {
    lightboxImg.src = src;
    lightboxImg.alt = alt || "";
    lightbox.showModal();
  }

  function closeDetailPanel(refocus) {
    if (!openDetail) { return; }
    var closing = openDetail;
    openDetail = null;
    closing.detailTr.remove();
    closing.button.setAttribute("aria-expanded", "false");
    if (refocus) { closing.button.focus(); }
  }

  function comparisonValue(value) {
    if (value === null || value === undefined || value === "") { return "—"; }
    return String(value);
  }

  function clauseName(key) {
    if (key === "prefix") { return "The opening words"; }
    return "Part " + key;
  }

  function clauseDiffBlock(diff) {
    var block = document.createElement("div");
    block.className = "clause-diff";
    var heading = document.createElement("h4");
    heading.textContent = "Where the warning text differs:";
    block.appendChild(heading);

    diff.forEach(function (entry) {
      var clause = document.createElement("div");
      clause.className = "clause";

      var label = document.createElement("p");
      label.className = "clause-label";
      label.textContent = clauseName(entry.clause);
      clause.appendChild(label);

      var expected = document.createElement("p");
      expected.textContent = "Should say: “" + entry.expected + "”";
      clause.appendChild(expected);

      var found = document.createElement("p");
      found.textContent = entry.found === null
        ? "The label is missing this part."
        : "Label says: “" + entry.found + "”";
      clause.appendChild(found);

      if (entry.differences && entry.differences.length > 0 && entry.found !== null) {
        var detail = document.createElement("p");
        detail.className = "muted";
        detail.textContent = "Difference: " + entry.differences.join("; ");
        clause.appendChild(detail);
      }

      block.appendChild(clause);
    });
    return block;
  }

  /* ---------- per-field reviewer checks (flagged rows only) ----------
     The human confirms the photo against the submittal form and the model's
     reading, field by field: "Confirm" = the reading is right as scanned;
     "Update" = the model misread the label, with the corrected reading typed
     in. Checks are evidence for the audit trail (they ride the CSV) — the
     row's outcome stays the human's Approve / Deny call above. */

  function fieldReviewState(row, key) {
    if (!row.fieldReview) { row.fieldReview = {}; }
    if (!row.fieldReview[key]) { row.fieldReview[key] = { mode: null, corrected: "" }; }
    return row.fieldReview[key];
  }

  function checkCell(row, key) {
    var td = document.createElement("td");
    td.className = "check-cell";
    td.setAttribute("data-label", "Your check");
    var state = fieldReviewState(row, key);

    function checkboxOption(labelText) {
      var label = document.createElement("label");
      label.className = "check-option";
      var box = document.createElement("input");
      box.type = "checkbox";
      label.appendChild(box);
      label.appendChild(document.createTextNode(" " + labelText));
      td.appendChild(label);
      return box;
    }

    var confirmBox = checkboxOption("Confirm reading");
    var updateBox = checkboxOption("Update reading");
    var input = document.createElement("input");
    input.type = "text";
    input.className = "check-input";
    input.setAttribute("aria-label", "Corrected reading for " + FIELD_NAMES[key]);
    td.appendChild(input);

    function sync() {
      confirmBox.checked = state.mode === "confirm";
      updateBox.checked = state.mode === "update";
      input.hidden = state.mode !== "update";
      input.value = state.corrected;
    }
    confirmBox.addEventListener("change", function () {
      state.mode = confirmBox.checked ? "confirm" : null;
      sync();
    });
    updateBox.addEventListener("change", function () {
      state.mode = updateBox.checked ? "update" : null;
      if (state.mode === "update" && !state.corrected) {
        var field = row.entry.fields[key];
        var extracted = field ? field.extracted : null;
        state.corrected = (extracted === null || extracted === undefined) ? "" : String(extracted);
      }
      sync();
      if (state.mode === "update") { input.focus(); }
    });
    input.addEventListener("input", function () { state.corrected = input.value; });
    sync();
    return td;
  }

  function comparisonTable(row) {
    var flagged = STATUSES[row.status].flagged;
    var columnCount = flagged ? 5 : 4;
    var table = document.createElement("table");
    table.className = "detail-table";
    var head = document.createElement("tr");
    var headers = ["What we checked", "Submittal form says", "Scan found", "Result"];
    if (flagged) { headers.push("Your check"); }
    headers.forEach(function (title) {
        var th = document.createElement("th");
        th.scope = "col";
        th.textContent = title;
        head.appendChild(th);
      });
    table.appendChild(head);

    var CELL_LABELS = ["", "Submittal form says", "Scan found"];
    FIELD_ORDER.forEach(function (key) {
      var field = row.entry.fields[key];
      if (!field) { return; }
      var noData = !row.hasSubmittal && SUBMITTAL_FIELDS.indexOf(key) !== -1;
      var mark = markFor(row, key);
      var cells = [
        FIELD_NAMES[key] || key,
        noData ? "— (no submittal data)" : comparisonValue(field.expected),
        comparisonValue(field.extracted)
      ];
      var trEl = document.createElement("tr");
      cells.forEach(function (value, index) {
        var td = document.createElement("td");
        td.textContent = value;
        if (index === 0) { td.className = "field-name"; }
        if (CELL_LABELS[index]) { td.setAttribute("data-label", CELL_LABELS[index]); }
        trEl.appendChild(td);
      });
      // Result cell: the verdict, with the explanation folded in underneath.
      var resultTd = document.createElement("td");
      resultTd.className = "verdict " + mark.cls;
      resultTd.setAttribute("data-label", "Result");
      resultTd.textContent = mark.icon + " " + mark.text;
      var reasonText = noData ? NO_DATA_TEXT + "." : (field.reason || "");
      if (reasonText) {
        var reasonP = document.createElement("p");
        reasonP.className = "verdict-reason muted";
        reasonP.textContent = reasonText;
        resultTd.appendChild(reasonP);
      }
      trEl.appendChild(resultTd);
      if (flagged) { trEl.appendChild(checkCell(row, key)); }
      table.appendChild(trEl);

      // The warning clause-by-clause diff, as prose, under the warning row.
      if (
        key === "government_warning" &&
        field.detail &&
        Array.isArray(field.detail.clause_diff) &&
        field.detail.clause_diff.length > 0
      ) {
        var diffTr = document.createElement("tr");
        var diffTd = document.createElement("td");
        diffTd.colSpan = columnCount;
        diffTd.appendChild(clauseDiffBlock(field.detail.clause_diff));
        diffTr.appendChild(diffTd);
        table.appendChild(diffTr);
      }
    });
    return table;
  }

  /* "Required on every label" mini-section: the required elements the scan
     did not find in this photo, each with its (TTB-grounded) reason. */
  function requiredMissingBlock(row) {
    var block = document.createElement("div");
    block.className = "required-missing";
    var heading = document.createElement("h4");
    heading.textContent = "Required on every label";
    block.appendChild(heading);
    var list = document.createElement("ul");
    row.requiredMissing.forEach(function (element) {
      var item = document.createElement("li");
      item.textContent = element.name + " — " + element.reason;
      list.appendChild(item);
    });
    block.appendChild(list);
    return block;
  }

  /* Approve / Deny controls for flagged rows. The decision lives on the row
     (client-only, R8: nothing stored server-side), overrides the displayed
     status, and rides the CSV export's pass_fail + reviewer_note columns. */
  function decisionBar(row) {
    var wrapper = document.createElement("div");
    wrapper.className = "decision-block";
    var bar = document.createElement("div");
    bar.className = "decision-bar";
    wrapper.appendChild(bar);

    // Feedback to the company: WHY the label failed. Rides the CSV export's
    // reviewer_comment column so the notice can be sent from the record.
    var commentLabel = document.createElement("label");
    commentLabel.className = "decision-comment-label";
    commentLabel.textContent =
      "Feedback for the company — why this label failed (goes in the results CSV):";
    var comment = document.createElement("textarea");
    comment.className = "decision-comment";
    comment.rows = 3;
    comment.placeholder =
      "e.g. The alcohol content printed on the label (14.2%) does not match the application (13.5%).";
    comment.value = row.comment || "";
    comment.addEventListener("input", function () { row.comment = comment.value; });
    commentLabel.appendChild(comment);
    wrapper.appendChild(commentLabel);
    var note = document.createElement("p");
    note.className = "decision-note";
    bar.appendChild(note);
    var approve = document.createElement("button");
    approve.type = "button";
    approve.className = "button button-secondary decision-approve";
    approve.textContent = "Approve";
    bar.appendChild(approve);
    var deny = document.createElement("button");
    deny.type = "button";
    deny.className = "button button-secondary decision-deny";
    deny.textContent = "Deny";
    bar.appendChild(deny);
    var undo = document.createElement("button");
    undo.type = "button";
    undo.className = "button button-secondary";
    undo.textContent = "Undo decision";
    bar.appendChild(undo);

    function sync() {
      var decided = !!row.review;
      note.textContent = decided
        ? "Reviewer decision: " + (row.review === "approved" ? "approved." : "denied.")
        : "Your call — approve or deny this label:";
      approve.hidden = decided;
      deny.hidden = decided;
      undo.hidden = !decided;
    }

    function decide(decision) {
      row.review = decision;
      refreshRowStatus(row);
      sync();
    }

    approve.addEventListener("click", function () { decide("approved"); });
    deny.addEventListener("click", function () { decide("denied"); });
    undo.addEventListener("click", function () { decide(null); });
    sync();
    return wrapper;
  }

  function buildDetailPanel(row) {
    var panel = document.createElement("div");
    panel.className = "detail-panel";
    panel.tabIndex = -1;
    panel.setAttribute("role", "group");
    panel.setAttribute("aria-label", "Details for " + row.filename);

    var header = document.createElement("div");
    header.className = "detail-header";
    var title = document.createElement("h3");
    title.textContent = row.serial + " — " + row.filename;
    header.appendChild(title);
    var stamp = document.createElement("p");
    stamp.className = "detail-stamp";
    var elapsed = elapsedSecondsText(row.entry);
    stamp.textContent = "Scanned " + displayStamp(row.scannedAt) +
      (elapsed ? " · " + elapsed : "");
    header.appendChild(stamp);
    var close = document.createElement("button");
    close.type = "button";
    close.className = "button button-secondary detail-close";
    close.textContent = "Close (Esc)";
    close.addEventListener("click", function () { closeDetailPanel(true); });
    header.appendChild(close);
    panel.appendChild(header);

    var layout = document.createElement("div");
    layout.className = "detail-layout";

    var figure = document.createElement("figure");
    figure.className = "detail-figure";
    var img = document.createElement("img");
    img.src = row.url;
    img.alt = "The label photo for row " + row.serial;
    if (row.url) {
      var zoom = document.createElement("button");
      zoom.type = "button";
      zoom.className = "photo-zoom";
      zoom.title = "Click to enlarge";
      zoom.setAttribute("aria-label", "Enlarge the label photo for row " + row.serial);
      zoom.appendChild(img);
      zoom.addEventListener("click", function () { openLightbox(row.url, img.alt); });
      figure.appendChild(zoom);
    } else {
      figure.appendChild(img);
    }
    var caption = document.createElement("figcaption");
    caption.textContent = row.url ? "The photo we scanned — click to enlarge" : "The photo we scanned";
    figure.appendChild(caption);
    layout.appendChild(figure);

    var body = document.createElement("div");
    body.className = "detail-body";
    if (STATUSES[row.status].flagged) { body.appendChild(decisionBar(row)); }
    if (row.entry.error) {
      var callout = document.createElement("p");
      callout.className = "detail-error";
      callout.textContent = "Couldn't scan this photo — " + row.entry.error.message;
      body.appendChild(callout);
    } else {
      // The comparison scrolls freely in its own box (both axes) so no cell
      // is ever clipped; the photo column stays put alongside.
      var scroll = document.createElement("div");
      scroll.className = "detail-scroll";
      scroll.appendChild(comparisonTable(row));
      if (row.requiredMissing && row.requiredMissing.length > 0) {
        scroll.appendChild(requiredMissingBlock(row));
      }
      body.appendChild(scroll);
    }
    layout.appendChild(body);
    panel.appendChild(layout);

    panel.addEventListener("keydown", function (event) {
      if (event.key === "Escape") {
        event.preventDefault();
        closeDetailPanel(true);
      }
    });
    return panel;
  }

  function toggleDetail(row, tr, button) {
    if (openDetail && openDetail.row === row) {
      closeDetailPanel(true);
      return;
    }
    closeDetailPanel(false);
    var detailTr = document.createElement("tr");
    detailTr.className = "detail-row";
    var td = document.createElement("td");
    td.colSpan = COLUMN_COUNT;
    var panel = buildDetailPanel(row);
    td.appendChild(panel);
    detailTr.appendChild(td);
    tr.after(detailTr);
    button.setAttribute("aria-expanded", "true");
    openDetail = { row: row, detailTr: detailTr, button: button };
    panel.focus({ preventScroll: false });
    panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  /* ---------- summary banner ---------- */

  function renderBanner() {
    var counts = { pass: 0, fail: 0, review: 0, error: 0, missing: 0 };
    var decided = 0;
    rows.forEach(function (row) {
      var status = effectiveStatus(row);
      if (status === "approved") { counts.pass += 1; decided += 1; }
      else if (status === "denied") { counts.fail += 1; decided += 1; }
      else { counts[status] += 1; }
    });

    var overall = "match";
    if (counts.fail > 0) { overall = "mismatch"; }
    else if (counts.review > 0 || counts.error > 0 || counts.missing > 0) {
      overall = "review";
    }

    var icons = { match: "✅", review: "⚠️", mismatch: "❌" };
    banner.className = "banner banner-" + overall;
    bannerIcon.textContent = icons[overall];

    var parts = [counts.pass + " passed"];
    if (counts.fail > 0) { parts.push(counts.fail + " failed"); }
    if (counts.review > 0) {
      parts.push(counts.review + (counts.review === 1 ? " needs review" : " need review"));
    }
    if (counts.error > 0) { parts.push(counts.error + " couldn't be scanned"); }
    if (counts.missing > 0) {
      parts.push(
        counts.missing === 1
          ? "1 form row had no photo"
          : counts.missing + " form rows had no photo"
      );
    }
    if (decided > 0) { parts.push(decided + " decided by reviewer"); }

    var scanned = counts.pass + counts.fail + counts.review + counts.error;
    var labelWord = scanned === 1 ? "1 label scanned" : scanned + " labels scanned";
    if (counts.missing > 0 && scanned === 0) {
      bannerText.textContent = parts.join(", ");
    } else {
      bannerText.textContent = labelWord + " — " + parts.join(", ");
    }
  }

  function renderSummary(totalTimeMs) {
    renderBanner();
    timing.textContent = "Finished in " + (totalTimeMs / 1000).toFixed(1) + " seconds.";
    // Scan is done: move focus to the summary banner so screen readers
    // announce completion (aria-live on the section is the backup).
    banner.focus({ preventScroll: false });
  }

  /* ---------- CSV export (client-side, Excel-safe) ---------- */

  function csvCell(value) {
    var text = value == null ? "" : String(value);
    // Guard against spreadsheet formula injection on open-in-Excel.
    if (/^[=+\-@]/.test(text)) { text = "'" + text; }
    return '"' + text.replace(/"/g, '""') + '"';
  }

  function csvScore(row) {
    if (row.entry.error) { return ""; }
    if (!row.hasSubmittal) { return ""; }
    return row.scoreText.replace(" fields match", "");
  }

  /* "Brand name: reading confirmed; Alcohol content: reading updated to
     “13.5%”" — the reviewer's per-field checks, for the audit trail. */
  function fieldChecksSummary(row) {
    if (!row.fieldReview) { return ""; }
    return FIELD_ORDER.filter(function (key) {
      var state = row.fieldReview[key];
      return state && state.mode;
    }).map(function (key) {
      var state = row.fieldReview[key];
      return FIELD_NAMES[key] + ": " + (state.mode === "confirm"
        ? "reading confirmed"
        : "reading updated to “" + state.corrected + "”");
    }).join("; ");
  }

  function buildCsv(exportRows) {
    var header = [
      "serial", "filename", "scan_timestamp", "processing_seconds",
      "over_5s_budget", "pass_fail", "score", "required_missing", "reviewer_note",
      "reviewer_comment", "field_checks"
    ];
    FIELD_ORDER.forEach(function (key) {
      header.push(key + "_verdict");
      header.push(key + "_reason");
    });
    header.push("error");

    var lines = [header.map(csvCell).join(",")];
    exportRows.forEach(function (row) {
      var missingKeys = (row.requiredMissing || []).map(function (e) { return e.key; }).join(";");
      var cells = [
        row.serial,
        row.filename,
        isoStamp(row.scannedAt),
        elapsedSecondsValue(row.entry) || "",
        isOverBudget(row.entry) ? "yes" : "no",
        STATUSES[effectiveStatus(row)].badge,
        csvScore(row),
        missingKeys,
        // reviewer_note: the in-app decision if one was made; otherwise blank
        // to fill in spreadsheet (R8: nothing stored server-side).
        row.review
          ? (row.review === "approved" ? "Approved" : "Denied") +
            " by reviewer (was " + STATUSES[row.status].badge + ")"
          : "",
        row.comment || "",
        fieldChecksSummary(row)
      ];
      FIELD_ORDER.forEach(function (key) {
        // Required-elements misses ride the EXISTING verdict/reason columns
        // (the export column set is contract-locked): a field with no
        // submittal comparison exports verdict "missing_required"; a field
        // that also has a server verdict keeps it and appends the reason.
        var field = row.entry.fields && row.entry.fields[key];
        var requiredMiss = field && isRequiredMissing(row, key);
        var element = requiredMiss
          ? row.requiredMissing.filter(function (e) { return e.key === key; })[0]
          : null;
        if (field && !row.hasSubmittal && SUBMITTAL_FIELDS.indexOf(key) !== -1) {
          cells.push(requiredMiss ? "missing_required" : "no_submittal_data");
          cells.push(requiredMiss ? element.reason : NO_DATA_TEXT + ".");
        } else if (field && requiredMiss) {
          cells.push(field.verdict === "na" ? "missing_required" : field.verdict);
          cells.push(field.reason ? field.reason + " " + element.reason : element.reason);
        } else {
          cells.push(field ? field.verdict : "");
          cells.push(field ? field.reason : "");
        }
      });
      cells.push(row.entry.error ? row.entry.error.message : "");
      lines.push(cells.map(csvCell).join(","));
    });
    // UTF-8 BOM + CRLF so Excel opens it as UTF-8 with proper rows.
    return "﻿" + lines.join("\r\n") + "\r\n";
  }

  function downloadCsvText(text, filename) {
    var blob = new Blob([text], { type: "text/csv;charset=utf-8" });
    var link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    URL.revokeObjectURL(link.href);
  }

  downloadButton.addEventListener("click", function () {
    downloadCsvText(buildCsv(rows), "label-scan-worksheet.csv");
  });

  /* ---------- review mode (WP8): after a scan the upload form gives way to a
     slim recap bar so the worksheet is the page; "Start a new scan" resets
     everything back to the empty step-1 state (photos, form, results). ---------- */

  function enterReviewMode(total, formName) {
    recapText.textContent = plural(total, "photo") + " scanned" +
      (formName ? " against “" + formName + "”." : " — no submittal form.");
    form.hidden = true;
    recapBar.hidden = false;
  }

  newScanButton.addEventListener("click", function () {
    clearWorksheet();      // restores the form / hides the recap bar
    clearPhotoSelection(); // empty dropzone — no leftover photos
    clearFormSelection();  // empty form slot — no leftover submittal
    matchNotice.hidden = true;
    matchNotice.textContent = "";
    hideError();
    progressBlock.hidden = true;
    uploadHeading.focus({ preventScroll: false });
    uploadHeading.scrollIntoView({ behavior: "smooth", block: "start" });
  });

  /* ---------- submit: matching plan + chunked requests for real progress ---- */

  function sendChunk(chunk, manifestSource) {
    var formData = new FormData();
    chunk.forEach(function (file) { formData.append("files", file, file.name); });
    if (manifestSource) {
      // manifestSource is the raw CSV File, or a Blob serialized from the
      // ingested rows. The server ignores manifest rows for files not in this
      // chunk, so the full form can ride along with every sub-batch.
      formData.append("manifest", manifestSource, manifestSource.name || "ingested-form.csv");
    } else {
      // No submittal data for these photos. The endpoint requires a brand to
      // compare against, so send a placeholder — the server still extracts
      // every field, and the client DISCARDS all submittal-dependent verdicts
      // for these rows, rendering "No submittal data — needs review" instead
      // (never a silent pass). Only the statutory health-warning verdict and
      // the required-elements check are kept.
      formData.append("brand", "-");
    }
    return fetch("/api/verify-batch", { method: "POST", body: formData })
      .then(function (response) {
        return response.json().catch(function () { return null; }).then(function (body) {
          if (!response.ok || !body || !body.results) {
            var message = (body && body.error && body.error.message) ||
              "Something went wrong on our end. Please try again.";
            throw new Error(message);
          }
          return body.results;
        });
      });
  }

  /* ---------- matching: form rows -> photos (client-side, deterministic) ----
     Rules, in order:
       1. Rows that name a photo file match by (normalized) file name — the
          existing server behavior, untouched.
       2. If NO row names a file: equal counts -> pair rows to photos in
          selection order (with a persistent notice); unequal counts -> block
          the scan with an explanation (never a silent mispairing).
       3. Mixed: named rows match by name; the leftover rows pair with the
          leftover photos by order only when the counts make it unambiguous,
          otherwise the unnamed rows are set aside with a notice and the
          unmatched photos scan without submittal data (flagged rows). */

  var MANIFEST_COLUMNS = [
    "filename", "brand", "class_type", "abv",
    "net_contents", "producer", "origin_country", "is_import"
  ];

  function csvFieldQuote(value) {
    var text = value === null || value === undefined ? "" : String(value);
    return '"' + text.replace(/"/g, '""') + '"';
  }

  function serializeManifest(manifestRows) {
    var lines = [MANIFEST_COLUMNS.join(",")];
    manifestRows.forEach(function (row) {
      lines.push(MANIFEST_COLUMNS.map(function (key) {
        if (key === "is_import") { return row.is_import ? "true" : "false"; }
        return csvFieldQuote(row[key]);
      }).join(","));
    });
    return lines.join("\r\n") + "\r\n";
  }

  /* Browser percent-encodes " in multipart Content-Disposition filenames
     (a"b.png -> a%22b.png). Manifest keys must match the wire name (QA5-F1). */
  function wireSafeFilename(name) {
    return String(name || "").replace(/"/g, "%22");
  }

  function normalizeName(name) {  // mirror of the server's normalize_filename + wire-safe
    var base = String(name || "").trim().replace(/\\/g, "/").split("/").pop();
    return wireSafeFilename(base).toLowerCase();
  }

  function plural(count, word) {
    return count + " " + word + (count === 1 ? "" : "s");
  }

  function photoKeySet(fileList) {
    var keys = {};
    fileList.forEach(function (file) { keys[normalizeName(file.name)] = true; });
    return keys;
  }

  function rowsMissingPhotos(formRows, fileList) {
    var keys = photoKeySet(fileList);
    return formRows.filter(function (row) {
      return row.filename && !keys[normalizeName(row.filename)];
    });
  }

  function missingFormNotice(missingFormRows) {
    if (!missingFormRows || missingFormRows.length === 0) { return null; }
    return plural(missingFormRows.length, "form row") +
      " had no matching photo — listed as MISSING in the worksheet.";
  }

  /* Decide how the photos and the (already-ingested) form rows pair up.
     Returns { error: message } to block the scan, or
     { partitions, notice, missingFormRows }.
     missingFormRows: submittal rows with no uploaded photo (fewer images
     than form items, or named files not among the selected photos). */
  function buildScanPlan(files, formFile) {
    if (!formFile) {
      return {
        partitions: [{ files: files, manifest: null, hasSubmittal: false }],
        notice: null,
        missingFormRows: []
      };
    }
    var haveRows = ingest.status === "ready" && ingest.file === formFile && ingest.rows;
    var planRows = haveRows ? ingest.rows : null;

    if (looksLikeCsv(formFile)) {
      // Raw-CSV path whenever possible — the server's manifest parser stays
      // the authority (and the QA-locked semantics stay byte-identical). Only
      // a CSV whose rows lack file names needs the serialized path below.
      if (!haveRows || planRows.every(function (row) { return !!row.filename; })) {
        var earlyMissing = haveRows ? rowsMissingPhotos(planRows, files) : [];
        return {
          partitions: [{ files: files, manifest: formFile, hasSubmittal: true }],
          notice: missingFormNotice(earlyMissing),
          missingFormRows: earlyMissing
        };
      }
    } else if (ingest.status === "loading" && ingest.file === formFile) {
      return { error: "Still reading the submittal form — give it a second, then press Run again." };
    } else if (!haveRows) {
      return { error: "We couldn't read that submittal form. Re-add it, or export it as CSV and try again." };
    }

    // Serialized path: snapshot the INGESTED ROWS now — swapping or removing
    // the form mid-scan must not change what later chunks are checked against.
    planRows = planRows.map(function (row) { return Object.assign({}, row); });

    for (var r = 0; r < planRows.length; r++) {
      if (!planRows[r].brand || !String(planRows[r].brand).trim()) {
        return {
          error: "Row " + (r + 1) + " of the form has no brand name — every " +
            "application needs one. Fix the form and add it again."
        };
      }
    }

    var named = planRows.filter(function (row) { return !!row.filename; });
    var unnamed = planRows.filter(function (row) { return !row.filename; });
    var notice = null;
    var noSubmittalPhotos = [];
    var missingFormRows = [];

    var claimed = {};
    named.forEach(function (row) { claimed[normalizeName(row.filename)] = true; });
    var leftoverPhotos = files.filter(function (file) {
      return !claimed[normalizeName(file.name)];
    });

    // Named form rows whose file was not uploaded → MISSING in the report.
    missingFormRows = rowsMissingPhotos(named, files);

    if (unnamed.length > 0) {
      if (named.length === 0) {
        // Pure order-matching: pair what we can; leftover form rows are MISSING
        // when there are fewer photos than form items (never a silent drop).
        if (planRows.length > files.length) {
          var pairCount = files.length;
          var toPair = planRows.slice(0, pairCount);
          missingFormRows = planRows.slice(pairCount).map(function (row) {
            return Object.assign({}, row);
          });
          var seenAll = {};
          for (var p0 = 0; p0 < files.length; p0++) {
            var k0 = normalizeName(files[p0].name);
            if (seenAll[k0]) {
              return {
                error: "Two of your photos share the file name “" + files[p0].name +
                  "” — rename one so each form row can be matched to the right photo."
              };
            }
            seenAll[k0] = true;
          }
          toPair.forEach(function (row, index) {
            row.filename = wireSafeFilename(files[index].name);
          });
          planRows = toPair;
          notice = "Matched " + plural(pairCount, "photo") + " to the first " +
            plural(pairCount, "form row") + " by order; " +
            missingFormNotice(missingFormRows);
          leftoverPhotos = [];
        } else if (planRows.length < files.length) {
          // More photos than form rows: pair by order; leftover photos scan
          // without submittal data.
          var seenFewer = {};
          for (var p1 = 0; p1 < planRows.length; p1++) {
            var k1 = normalizeName(files[p1].name);
            if (seenFewer[k1]) {
              return {
                error: "Two of your photos share the file name “" + files[p1].name +
                  "” — rename one so each form row can be matched to the right photo."
              };
            }
            seenFewer[k1] = true;
            planRows[p1].filename = wireSafeFilename(files[p1].name);
            claimed[normalizeName(files[p1].name)] = true;
          }
          noSubmittalPhotos = files.slice(planRows.length);
          leftoverPhotos = [];
          notice = "Matched " + plural(planRows.length, "form row") + " to the first " +
            plural(planRows.length, "photo") + " by order; " +
            plural(noSubmittalPhotos.length, "extra photo") +
            " scanned without submittal data.";
        } else {
          // Equal counts — order-match everything.
          var seenEq = {};
          for (var p2 = 0; p2 < files.length; p2++) {
            var k2 = normalizeName(files[p2].name);
            if (seenEq[k2]) {
              return {
                error: "Two of your photos share the file name “" + files[p2].name +
                  "” — rename one so each form row can be matched to the right photo."
              };
            }
            seenEq[k2] = true;
            planRows[p2].filename = wireSafeFilename(files[p2].name);
          }
          notice = "Matched " + plural(planRows.length, "row") + " to " +
            plural(planRows.length, "photo") + " by order — check the pairings in the worksheet.";
          leftoverPhotos = [];
        }
      } else if (unnamed.length === leftoverPhotos.length) {
        // Mixed: unambiguous order-pair for leftovers.
        var seen = {};
        for (var p = 0; p < leftoverPhotos.length; p++) {
          var key = normalizeName(leftoverPhotos[p].name);
          if (seen[key]) {
            return {
              error: "Two of your photos share the file name “" + leftoverPhotos[p].name +
                "” — rename one so each form row can be matched to the right photo."
            };
          }
          seen[key] = true;
        }
        unnamed.forEach(function (row, index) {
          row.filename = wireSafeFilename(leftoverPhotos[index].name);
          claimed[normalizeName(leftoverPhotos[index].name)] = true;
        });
        notice = "Matched " + plural(unnamed.length, "row") + " to " +
          plural(unnamed.length, "photo") + " by order — check the pairings in the worksheet.";
        if (missingFormRows.length > 0) {
          notice += " " + missingFormNotice(missingFormRows);
        }
        leftoverPhotos = [];
      } else if (unnamed.length > leftoverPhotos.length && leftoverPhotos.length > 0) {
        // Fewer leftover photos than unnamed form rows: pair what we can.
        var seenMix = {};
        for (var pm = 0; pm < leftoverPhotos.length; pm++) {
          var km = normalizeName(leftoverPhotos[pm].name);
          if (seenMix[km]) {
            return {
              error: "Two of your photos share the file name “" + leftoverPhotos[pm].name +
                "” — rename one so each form row can be matched to the right photo."
            };
          }
          seenMix[km] = true;
        }
        var pairedUnnamed = unnamed.slice(0, leftoverPhotos.length);
        var unpairedUnnamed = unnamed.slice(leftoverPhotos.length);
        pairedUnnamed.forEach(function (row, index) {
          row.filename = wireSafeFilename(leftoverPhotos[index].name);
          claimed[normalizeName(leftoverPhotos[index].name)] = true;
        });
        missingFormRows = missingFormRows.concat(unpairedUnnamed);
        planRows = named.concat(pairedUnnamed);
        leftoverPhotos = [];
        notice = missingFormNotice(missingFormRows);
      } else {
        // Ambiguous or no leftover photos for unnamed rows: unnamed → MISSING;
        // unclaimed photos scan without submittal data.
        planRows = named;
        noSubmittalPhotos = leftoverPhotos;
        missingFormRows = missingFormRows.concat(unnamed);
        leftoverPhotos = [];
        notice = missingFormNotice(missingFormRows);
        if (noSubmittalPhotos.length > 0) {
          notice = (notice ? notice + " " : "") +
            plural(noSubmittalPhotos.length, "photo") +
            " had no form row and were scanned without submittal data.";
        }
      }
    } else if (missingFormRows.length > 0) {
      notice = missingFormNotice(missingFormRows);
    }

    var matchedFiles = files;
    if (noSubmittalPhotos.length > 0) {
      matchedFiles = files.filter(function (file) { return claimed[normalizeName(file.name)]; });
    }
    // Photos the form doesn't mention still ride with the manifest — the
    // server gives them the standard "no row for this photo" error entry.
    var manifestBlob = new Blob([serializeManifest(planRows)], { type: "text/csv" });
    var partitions = [];
    if (matchedFiles.length > 0) {
      partitions.push({ files: matchedFiles, manifest: manifestBlob, hasSubmittal: true });
    }
    if (noSubmittalPhotos.length > 0) {
      partitions.push({ files: noSubmittalPhotos, manifest: null, hasSubmittal: false });
    }
    return {
      partitions: partitions,
      notice: notice,
      missingFormRows: missingFormRows
    };
  }

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    hideError();

    if (selectedFiles.length === 0) {
      showError("Please add the label photos first — drag them into the box above or use “Choose photos”.");
      return;
    }
    if (selectedFiles.length > MAX_FILES) {
      showError("That's " + selectedFiles.length + " photos — we can scan up to " +
        MAX_FILES + " at a time. Please split them into smaller batches.");
      return;
    }

    var files = selectedFiles.slice();
    // The plan snapshots the form state now (the ingested ROWS, or the raw
    // CSV File): removing or swapping the form mid-scan must not change what
    // later chunks are checked against.
    var plan = buildScanPlan(files, csvFile);
    if (plan.error) {
      showError(plan.error);
      return;
    }
    matchNotice.textContent = plan.notice || "";
    matchNotice.hidden = !plan.notice;

    var total = files.length;
    var missingFormRows = plan.missingFormRows || [];
    var jobs = [];  // one entry per sub-batch: { chunk, manifest, hasSubmittal }
    plan.partitions.forEach(function (partition) {
      for (var i = 0; i < partition.files.length; i += CHUNK_SIZE) {
        jobs.push({
          chunk: partition.files.slice(i, i + CHUNK_SIZE),
          manifest: partition.manifest,
          hasSubmittal: partition.hasSubmittal
        });
      }
    });

    clearWorksheet();
    setBusy(true);
    progressBlock.hidden = false;
    updateProgress(0, total);
    var startedAt = Date.now();
    var nextSerial = 1;

    function addRow(file, entry, hasSubmittal) {
      var row = {
        serial: pad3(nextSerial++),
        filename: file.name,
        file: file,
        url: URL.createObjectURL(file),
        scannedAt: new Date(),   // the moment this label's result landed
        hasSubmittal: hasSubmittal,
        entry: entry
      };
      row.requiredMissing = missingRequired(entry);
      var outcome = computeOutcome(row);
      row.status = outcome.status;
      row.scoreText = outcome.scoreText;
      rows.push(row);
      appendRow(row);
    }

    function addMissingFormRow(formRow) {
      var expectedFile = formRow.filename ? String(formRow.filename) : "";
      var brandBit = formRow.brand ? "brand “" + formRow.brand + "”" : "this application";
      var message = expectedFile
        ? "No photo was uploaded for " + brandBit + " (expected file “" + expectedFile + "”)."
        : "No photo was uploaded for " + brandBit + " — fewer photos than form rows.";
      var row = {
        serial: pad3(nextSerial++),
        filename: expectedFile || "(no file name on form)",
        file: null,
        url: null,
        scannedAt: new Date(),
        hasSubmittal: true,
        entry: {
          error: { code: "missing_photo", message: message },
          missing_form_row: {
            brand: formRow.brand || null,
            class_type: formRow.class_type || null,
            filename: formRow.filename || null
          }
        },
        requiredMissing: [],
        status: "missing",
        scoreText: "—"
      };
      rows.push(row);
      appendRow(row);
    }

    var sequence = Promise.resolve();
    jobs.forEach(function (job) {
      sequence = sequence.then(function () {
        return sendChunk(job.chunk, job.manifest).catch(function (error) {
          // A failed sub-batch becomes error rows; the scan continues.
          var message = error instanceof TypeError
            ? "We couldn't reach the scanning service for these photos."
            : error.message;
          return job.chunk.map(function (file) {
            return { filename: file.name, error: { code: "request_failed", message: message } };
          });
        }).then(function (chunkResults) {
          // Results come back in upload order; pair them with the chunk's
          // File objects by index so thumbnails stay attached to the right
          // row even with duplicate file names.
          chunkResults.forEach(function (entry, index) {
            addRow(job.chunk[index] || { name: entry.filename }, entry, job.hasSubmittal);
          });
          updateProgress(Math.min(rows.length, total), total);
          resultsSection.hidden = false;
        });
      });
    });

    var formName = csvFile ? csvFile.name : null;  // snapshot for the recap
    sequence.then(function () {
      // Append form rows that had no photo so the report is complete.
      missingFormRows.forEach(function (formRow) {
        addMissingFormRow(formRow);
      });
      if (missingFormRows.length > 0) {
        resultsSection.hidden = false;
        if (!matchNotice.textContent) {
          matchNotice.textContent = missingFormNotice(missingFormRows);
          matchNotice.hidden = false;
        }
      }
      renderSummary(Date.now() - startedAt);
      progressBlock.hidden = true;
      setBusy(false);
      enterReviewMode(total, formName);
    });
  });
})();
