/* TTB Label Checker — unified worksheet flow (WP5).
   One flow: drop 1..N label photos + an optional submittal-form CSV, click
   Scan Labels. The client chunks the photos through POST /api/verify-batch
   (CHUNK_SIZE per request) so progress reflects real server completion, and
   builds a worksheet: one row per label with a serial number, scan timestamp,
   thumbnail, the extracted value per field with a compact status mark, a
   score, and a PASS / FAIL / REVIEW result. FAIL and REVIEW rows are flagged
   for human review; clicking a row (or its Review button) opens a drill-down
   panel with the label photo large and the field-by-field comparison.

   Serial numbers, timestamps, scores, and pass/fail are all CLIENT-derived
   from the server's per-field verdicts — the API response shapes are frozen.

   SECURITY: every dynamic string (extracted values, file names, reasons,
   error messages) is rendered with textContent or set as an attribute string
   — never innerHTML. Extraction output and file names are hostile until
   proven otherwise. Images render from client-side object URLs of the kept
   File objects — nothing is stored server-side (R8). */

"use strict";

(function () {
  var CHUNK_SIZE = 10;   // labels per request: real progress ticks, bounded requests
  var MAX_FILES = 300;   // mirrors the server's batch cap
  var COLUMN_COUNT = 12; // serial, time, photo, 7 fields, score, result (with Review button)

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
    nodata: { icon: "⚠", text: "No submittal data — needs review", cls: "mark-review" }
  };

  var STATUSES = {
    pass: { badge: "PASS", cls: "status-pass", flagged: false },
    fail: { badge: "FAIL", cls: "status-fail", flagged: true },
    review: { badge: "REVIEW", cls: "status-review", flagged: true },
    error: { badge: "ERROR", cls: "status-error", flagged: true }
  };

  var NO_DATA_TEXT = "No submittal data — needs review";

  /* Fields verified against the submittal form. The health warning is checked
     against the statutory text (27 CFR 16.21), not the submittal form, so its
     verdict stays meaningful even without a CSV. */
  var SUBMITTAL_FIELDS = [
    "brand", "class_type", "abv", "net_contents", "producer", "origin_country"
  ];

  /* ---------- elements ---------- */

  var form = document.getElementById("scan-form");
  var dropzone = document.getElementById("dropzone");
  var fileInput = document.getElementById("file-input");
  var dropzoneEmpty = document.getElementById("dropzone-empty");
  var dropzoneSelected = document.getElementById("dropzone-selected");
  var fileSummary = document.getElementById("file-summary");
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

  var selectedFiles = [];
  var csvFile = null;
  var rows = [];          // one record per scanned label, in serial order
  var openDetail = null;  // { row, detailTr, button } — at most one open panel

  /* ---------- file selection ---------- */

  function clearWorksheet() {
    closeDetailPanel(false);
    rows.forEach(function (row) {
      if (row.url) { URL.revokeObjectURL(row.url); }
    });
    rows = [];
    worksheetBody.textContent = "";
    resultsSection.hidden = true;
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

  /* ---------- submittal CSV selection ---------- */

  function syncCsv() {
    csvFile = (csvInput.files && csvInput.files.length > 0) ? csvInput.files[0] : null;
    var hasCsv = csvFile !== null;
    csvStatus.hidden = !hasCsv;
    csvClear.hidden = !hasCsv;
    if (hasCsv) {
      csvStatus.textContent = "Using “" + csvFile.name +
        "” — each photo will be checked against its row.";
    }
  }

  csvInput.addEventListener("change", syncCsv);
  csvClear.addEventListener("click", function () {
    csvInput.value = "";
    syncCsv();
  });
  syncCsv();

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

  function setBusy(busy) {
    scanButton.disabled = busy;
    scanButton.textContent = busy ? "Scanning…" : "Scan Labels";
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

  /* ---------- row scoring (client-derived; API shapes untouched) ---------- */

  function pad3(n) {
    if (n < 10) { return "00" + n; }
    if (n < 100) { return "0" + n; }
    return String(n);
  }

  function markFor(row, key) {
    if (!row.hasSubmittal && SUBMITTAL_FIELDS.indexOf(key) !== -1) {
      return MARKS.nodata;
    }
    var field = row.entry.fields[key];
    return (field && MARKS[field.verdict]) || MARKS.review;
  }

  /* Score + pass/fail for one row.
     - error entry            -> ERROR (flagged)
     - with CSV: any mismatch -> FAIL; any review -> REVIEW; else PASS,
       score = matches / applicable (applicable = verdict !== "na")
     - without CSV: never a silent pass — the six submittal-checked fields
       have nothing to compare against, so the row is at best REVIEW; the
       statutory health-warning check still runs, so a warning mismatch
       still makes the row FAIL. */
  function computeOutcome(row) {
    if (row.entry.error) {
      return { status: "error", scoreText: "—" };
    }
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
    if (status.flagged) {
      var sr = document.createElement("span");
      sr.className = "visually-hidden";
      sr.textContent = " — flagged for human review";
      td.appendChild(sr);
    }
    td.appendChild(reviewButton);
    return td;
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

    var photoTd = document.createElement("td");
    photoTd.className = "photo-cell";
    photoTd.setAttribute("data-label", "Photo");
    var thumb = document.createElement("img");
    thumb.className = "thumb";
    thumb.alt = "";
    thumb.src = row.url;
    photoTd.appendChild(thumb);
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
      errorTd.textContent = "Couldn't scan — " + row.entry.error.message;
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
    }

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

  function comparisonTable(row) {
    var table = document.createElement("table");
    table.className = "detail-table";
    var head = document.createElement("tr");
    ["What we checked", "Submittal form says", "Scan found", "Result", "Explanation"]
      .forEach(function (title) {
        var th = document.createElement("th");
        th.scope = "col";
        th.textContent = title;
        head.appendChild(th);
      });
    table.appendChild(head);

    var CELL_LABELS = ["", "Submittal form says", "Scan found", "Result", "Explanation"];
    FIELD_ORDER.forEach(function (key) {
      var field = row.entry.fields[key];
      if (!field) { return; }
      var noData = !row.hasSubmittal && SUBMITTAL_FIELDS.indexOf(key) !== -1;
      var mark = markFor(row, key);
      var cells = [
        FIELD_NAMES[key] || key,
        noData ? "— (no submittal data)" : comparisonValue(field.expected),
        comparisonValue(field.extracted),
        mark.icon + " " + mark.text,
        noData ? NO_DATA_TEXT + "." : (field.reason || "")
      ];
      var trEl = document.createElement("tr");
      cells.forEach(function (value, index) {
        var td = document.createElement("td");
        td.textContent = value;
        if (index === 0) { td.className = "field-name"; }
        if (index === 3) { td.className = "verdict " + mark.cls; }
        if (CELL_LABELS[index]) { td.setAttribute("data-label", CELL_LABELS[index]); }
        trEl.appendChild(td);
      });
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
        diffTd.colSpan = 5;
        diffTd.appendChild(clauseDiffBlock(field.detail.clause_diff));
        diffTr.appendChild(diffTd);
        table.appendChild(diffTr);
      }
    });
    return table;
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
    stamp.textContent = "Scanned " + displayStamp(row.scannedAt);
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
    figure.appendChild(img);
    var caption = document.createElement("figcaption");
    caption.textContent = "The photo we scanned";
    figure.appendChild(caption);
    layout.appendChild(figure);

    var body = document.createElement("div");
    body.className = "detail-body";
    if (row.entry.error) {
      var callout = document.createElement("p");
      callout.className = "detail-error";
      callout.textContent = "Couldn't scan this photo — " + row.entry.error.message;
      body.appendChild(callout);
    } else {
      body.appendChild(comparisonTable(row));
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

  function renderSummary(totalTimeMs) {
    var counts = { pass: 0, fail: 0, review: 0, error: 0 };
    rows.forEach(function (row) { counts[row.status] += 1; });

    var overall = "match";
    if (counts.fail > 0) { overall = "mismatch"; }
    else if (counts.review > 0 || counts.error > 0) { overall = "review"; }

    var icons = { match: "✅", review: "⚠️", mismatch: "❌" };
    banner.className = "banner banner-" + overall;
    bannerIcon.textContent = icons[overall];

    var parts = [counts.pass + " passed"];
    if (counts.fail > 0) { parts.push(counts.fail + " failed"); }
    if (counts.review > 0) {
      parts.push(counts.review + (counts.review === 1 ? " needs review" : " need review"));
    }
    if (counts.error > 0) { parts.push(counts.error + " couldn't be scanned"); }

    bannerText.textContent = (rows.length === 1 ? "1 label scanned — " : rows.length + " labels scanned — ")
      + parts.join(", ");
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

  function buildCsv(exportRows) {
    var header = ["serial", "filename", "scan_timestamp", "pass_fail", "score"];
    FIELD_ORDER.forEach(function (key) {
      header.push(key + "_verdict");
      header.push(key + "_reason");
    });
    header.push("error");

    var lines = [header.map(csvCell).join(",")];
    exportRows.forEach(function (row) {
      var cells = [
        row.serial,
        row.filename,
        isoStamp(row.scannedAt),
        STATUSES[row.status].badge,
        csvScore(row)
      ];
      FIELD_ORDER.forEach(function (key) {
        var field = row.entry.fields && row.entry.fields[key];
        if (field && !row.hasSubmittal && SUBMITTAL_FIELDS.indexOf(key) !== -1) {
          cells.push("no_submittal_data");
          cells.push(NO_DATA_TEXT + ".");
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

  downloadButton.addEventListener("click", function () {
    var blob = new Blob([buildCsv(rows)], { type: "text/csv;charset=utf-8" });
    var link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = "label-scan-worksheet.csv";
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    URL.revokeObjectURL(link.href);
  });

  /* ---------- submit: chunked requests for real progress ---------- */

  function sendChunk(chunk) {
    var formData = new FormData();
    chunk.forEach(function (file) { formData.append("files", file, file.name); });
    if (csvFile) {
      // The server ignores manifest rows for files not in this chunk, so the
      // full spreadsheet can ride along with every sub-batch.
      formData.append("manifest", csvFile, csvFile.name);
    } else {
      // No submittal form. The endpoint requires a brand to compare against,
      // so send a placeholder — the server still extracts every field, and
      // the client DISCARDS all submittal-dependent verdicts for these rows,
      // rendering "No submittal data — needs review" instead (never a
      // silent pass). Only the statutory health-warning verdict is kept.
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
    var hasSubmittal = csvFile !== null;
    var total = files.length;
    var chunks = [];
    for (var i = 0; i < total; i += CHUNK_SIZE) {
      chunks.push(files.slice(i, i + CHUNK_SIZE));
    }

    clearWorksheet();
    setBusy(true);
    progressBlock.hidden = false;
    updateProgress(0, total);
    var startedAt = Date.now();
    var nextSerial = 1;

    function addRow(file, entry) {
      var row = {
        serial: pad3(nextSerial++),
        filename: file.name,
        file: file,
        url: URL.createObjectURL(file),
        scannedAt: new Date(),   // the moment this label's result landed
        hasSubmittal: hasSubmittal,
        entry: entry
      };
      var outcome = computeOutcome(row);
      row.status = outcome.status;
      row.scoreText = outcome.scoreText;
      rows.push(row);
      appendRow(row);
    }

    var sequence = Promise.resolve();
    chunks.forEach(function (chunk) {
      sequence = sequence.then(function () {
        return sendChunk(chunk).catch(function (error) {
          // A failed sub-batch becomes error rows; the scan continues.
          var message = error instanceof TypeError
            ? "We couldn't reach the scanning service for these photos."
            : error.message;
          return chunk.map(function (file) {
            return { filename: file.name, error: { code: "request_failed", message: message } };
          });
        }).then(function (chunkResults) {
          // Results come back in upload order; pair them with the chunk's
          // File objects by index so thumbnails stay attached to the right
          // row even with duplicate file names.
          chunkResults.forEach(function (entry, index) {
            addRow(chunk[index] || { name: entry.filename }, entry);
          });
          updateProgress(rows.length, total);
          resultsSection.hidden = false;
        });
      });
    });

    sequence.then(function () {
      renderSummary(Date.now() - startedAt);
      progressBlock.hidden = true;
      setBusy(false);
    });
  });
})();
