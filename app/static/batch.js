/* TTB Label Checker — batch ("Check many labels") flow.
   Talks to POST /api/verify-batch in sub-batches of CHUNK_SIZE so the
   progress indicator reflects real server-side completion (no websockets).
   All dynamic text is inserted with textContent (never innerHTML). */

"use strict";

(function () {
  var CHUNK_SIZE = 10;      // labels per request: real progress ticks, bounded requests
  var MAX_FILES = 300;      // mirrors the server's batch cap

  var FIELD_ORDER = [
    "brand", "class_type", "abv", "net_contents",
    "producer", "origin_country", "government_warning"
  ];

  var FIELD_NAMES = {
    brand: "Brand name",
    class_type: "Kind of drink",
    abv: "Alcohol content",
    net_contents: "Amount in the bottle",
    producer: "Producer name and address",
    origin_country: "Country of origin",
    government_warning: "Government health warning"
  };

  var VERDICTS = {
    match: { icon: "✅", text: "Matches" },
    review: { icon: "⚠️", text: "Needs review" },
    mismatch: { icon: "❌", text: "Doesn't match" },
    na: { icon: "—", text: "Not checked" }
  };

  /* ---------- tabs ---------- */

  var tabSingle = document.getElementById("tab-single");
  var tabBatch = document.getElementById("tab-batch");
  var singlePanel = document.getElementById("single-panel");
  var batchPanel = document.getElementById("batch-panel");

  function selectTab(which) {
    var batch = which === "batch";
    singlePanel.hidden = batch;
    batchPanel.hidden = !batch;
    tabSingle.classList.toggle("active", !batch);
    tabBatch.classList.toggle("active", batch);
    tabSingle.setAttribute("aria-selected", String(!batch));
    tabBatch.setAttribute("aria-selected", String(batch));
  }
  tabSingle.addEventListener("click", function () { selectTab("single"); });
  tabBatch.addEventListener("click", function () { selectTab("batch"); });

  // Arrow keys move between the two tabs (standard tablist keyboard pattern).
  [tabSingle, tabBatch].forEach(function (tab) {
    tab.addEventListener("keydown", function (event) {
      if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") { return; }
      event.preventDefault();
      var other = tab === tabSingle ? tabBatch : tabSingle;
      other.focus();
      other.click();
    });
  });

  /* ---------- elements ---------- */

  var form = document.getElementById("batch-form");
  var dropzone = document.getElementById("batch-dropzone");
  var fileInput = document.getElementById("batch-file-input");
  var dropzoneEmpty = document.getElementById("batch-dropzone-empty");
  var dropzoneSelected = document.getElementById("batch-dropzone-selected");
  var fileSummary = document.getElementById("batch-file-summary");
  var manifestInput = document.getElementById("manifest-input");
  var manifestStatus = document.getElementById("manifest-status");
  var manifestClear = document.getElementById("manifest-clear");
  var sharedFields = document.getElementById("batch-shared-fields");
  var importCheckbox = document.getElementById("batch_is_import");
  var originField = document.getElementById("batch-origin-field");
  var checkButton = document.getElementById("batch-check-button");
  var progressBlock = document.getElementById("batch-progress");
  var progressText = document.getElementById("batch-progress-text");
  var progressBar = document.getElementById("batch-progress-bar");
  var errorCallout = document.getElementById("batch-error");
  var errorMessage = document.getElementById("batch-error-message");
  var resultsSection = document.getElementById("batch-results");
  var banner = document.getElementById("batch-banner");
  var bannerIcon = document.getElementById("batch-banner-icon");
  var bannerText = document.getElementById("batch-banner-text");
  var timing = document.getElementById("batch-timing");
  var resultsBody = document.getElementById("batch-results-body");
  var downloadButton = document.getElementById("batch-download");

  var selectedFiles = [];
  var manifestFile = null;
  var allResults = [];

  /* ---------- file selection ---------- */

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

  dropzone.addEventListener("click", function (event) {
    if (event.target.closest("label") || event.target === fileInput) { return; }
    fileInput.click();
  });

  /* ---------- manifest selection ---------- */

  function syncManifest() {
    manifestFile = (manifestInput.files && manifestInput.files.length > 0)
      ? manifestInput.files[0] : null;
    var hasManifest = manifestFile !== null;
    manifestStatus.hidden = !hasManifest;
    manifestClear.hidden = !hasManifest;
    if (hasManifest) {
      manifestStatus.textContent = "Using “" + manifestFile.name +
        "” — the details typed below will be ignored.";
    }
    sharedFields.disabled = hasManifest;
  }

  manifestInput.addEventListener("change", syncManifest);
  manifestClear.addEventListener("click", function () {
    manifestInput.value = "";
    syncManifest();
  });
  syncManifest();

  function syncOriginField() {
    originField.hidden = !importCheckbox.checked;
  }
  importCheckbox.addEventListener("change", syncOriginField);
  syncOriginField();

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
    checkButton.disabled = busy;
    checkButton.textContent = busy ? "Checking…" : "Check All Labels";
  }

  function updateProgress(done, total) {
    progressText.textContent = "Checked " + done + " of " + total + "…";
    progressBar.max = total;
    progressBar.value = done;
  }

  /* ---------- results rendering ---------- */

  function shorten(text, max) {
    if (text.length <= max) { return text; }
    return text.slice(0, max - 1) + "…";
  }

  function summaryReason(entry) {
    if (entry.error) { return entry.error.message; }
    var problems = [];
    FIELD_ORDER.forEach(function (key) {
      var field = entry.fields[key];
      if (field && (field.verdict === "mismatch" || field.verdict === "review")) {
        problems.push(FIELD_NAMES[key] || key);
      }
    });
    if (problems.length === 0) { return "All the details match."; }
    return "Check: " + problems.join(", ");
  }

  function detailTable(entry) {
    var table = document.createElement("table");
    table.className = "detail-table";
    var head = document.createElement("tr");
    ["What we checked", "Result", "On the label", "On the application", "Explanation"]
      .forEach(function (title) {
        var th = document.createElement("th");
        th.scope = "col";
        th.textContent = title;
        head.appendChild(th);
      });
    table.appendChild(head);
    var CELL_LABELS = ["", "Result", "On the label", "On the application", "Explanation"];
    FIELD_ORDER.forEach(function (key) {
      var field = entry.fields[key];
      if (!field) { return; }
      var row = document.createElement("tr");
      var verdict = VERDICTS[field.verdict] || VERDICTS.review;
      [FIELD_NAMES[key] || key,
       verdict.icon + " " + verdict.text,
       field.extracted == null || field.extracted === "" ? "—" : shorten(String(field.extracted), 120),
       field.expected == null || field.expected === "" ? "—" : shorten(String(field.expected), 120),
       field.reason || ""].forEach(function (value, index) {
        var td = document.createElement("td");
        td.textContent = value;
        if (index === 0) { td.className = "field-name"; }
        if (index === 1) { td.className = "verdict verdict-" + field.verdict; }
        if (CELL_LABELS[index]) { td.setAttribute("data-label", CELL_LABELS[index]); }
        row.appendChild(td);
      });
      table.appendChild(row);
    });
    return table;
  }

  function appendResultRow(entry) {
    var row = document.createElement("tr");

    var nameCell = document.createElement("td");
    nameCell.className = "field-name";
    nameCell.textContent = entry.filename;
    row.appendChild(nameCell);

    var verdictCell = document.createElement("td");
    verdictCell.setAttribute("data-label", "Result");
    if (entry.error) {
      verdictCell.className = "verdict verdict-review";
      verdictCell.textContent = "⚠️ Couldn't check";
    } else {
      var verdict = VERDICTS[entry.overall_status] || VERDICTS.review;
      verdictCell.className = "verdict verdict-" + entry.overall_status;
      verdictCell.textContent = verdict.icon + " " + verdict.text;
    }
    row.appendChild(verdictCell);

    var reasonCell = document.createElement("td");
    reasonCell.className = "reason-cell";
    reasonCell.setAttribute("data-label", "What to look at");
    reasonCell.textContent = shorten(summaryReason(entry), 160);
    row.appendChild(reasonCell);

    var toggleCell = document.createElement("td");
    if (!entry.error) {
      var toggle = document.createElement("button");
      toggle.type = "button";
      toggle.className = "button button-secondary detail-toggle";
      toggle.textContent = "Show details";
      toggleCell.appendChild(toggle);
    }
    row.appendChild(toggleCell);
    resultsBody.appendChild(row);

    if (!entry.error) {
      var detailRow = document.createElement("tr");
      detailRow.className = "detail-row";
      detailRow.hidden = true;
      var detailCell = document.createElement("td");
      detailCell.colSpan = 4;
      detailCell.appendChild(detailTable(entry));
      detailRow.appendChild(detailCell);
      resultsBody.appendChild(detailRow);

      toggle.addEventListener("click", function () {
        detailRow.hidden = !detailRow.hidden;
        toggle.textContent = detailRow.hidden ? "Show details" : "Hide details";
      });
    }
  }

  function renderSummary(totalTimeMs) {
    var counts = { match: 0, review: 0, mismatch: 0, error: 0 };
    allResults.forEach(function (entry) {
      counts[entry.error ? "error" : entry.overall_status] += 1;
    });

    var status = "match";
    if (counts.mismatch > 0) { status = "mismatch"; }
    else if (counts.review > 0 || counts.error > 0) { status = "review"; }

    var icons = { match: "✅", review: "⚠️", mismatch: "❌" };
    banner.className = "banner banner-" + status;
    bannerIcon.textContent = icons[status];

    var parts = [counts.match + " match"];
    if (counts.review > 0) { parts.push(counts.review + " need review"); }
    if (counts.mismatch > 0) { parts.push(counts.mismatch + " don't match"); }
    if (counts.error > 0) { parts.push(counts.error + " couldn't be checked"); }
    bannerText.textContent = allResults.length + " labels checked — " + parts.join(", ");
    timing.textContent = "Finished in " + (totalTimeMs / 1000).toFixed(1) + " seconds.";
    // Batch is done: move focus to the summary banner so screen readers
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

  function buildCsv(results) {
    var header = ["filename", "overall_status"];
    FIELD_ORDER.forEach(function (key) {
      header.push(key + "_verdict");
      header.push(key + "_reason");
    });
    header.push("error");

    var lines = [header.map(csvCell).join(",")];
    results.forEach(function (entry) {
      var cells = [entry.filename, entry.error ? "error" : entry.overall_status];
      FIELD_ORDER.forEach(function (key) {
        var field = entry.fields && entry.fields[key];
        cells.push(field ? field.verdict : "");
        cells.push(field ? field.reason : "");
      });
      cells.push(entry.error ? entry.error.message : "");
      lines.push(cells.map(csvCell).join(","));
    });
    // UTF-8 BOM + CRLF so Excel opens it as UTF-8 with proper rows.
    return "﻿" + lines.join("\r\n") + "\r\n";
  }

  downloadButton.addEventListener("click", function () {
    var blob = new Blob([buildCsv(allResults)], { type: "text/csv;charset=utf-8" });
    var link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = "label-check-results.csv";
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    URL.revokeObjectURL(link.href);
  });

  /* ---------- submit: chunked requests for real progress ---------- */

  function sendChunk(chunk) {
    var formData = new FormData();
    chunk.forEach(function (file) { formData.append("files", file, file.name); });
    if (manifestFile) {
      // The server ignores manifest rows for files not in this chunk, so the
      // full spreadsheet can ride along with every sub-batch.
      formData.append("manifest", manifestFile, manifestFile.name);
    } else {
      formData.append("brand", document.getElementById("batch_brand").value.trim());
      ["class_type", "abv", "net_contents", "producer"].forEach(function (name) {
        formData.append(name, document.getElementById("batch_" + name).value.trim());
      });
      formData.append("is_import", importCheckbox.checked ? "true" : "false");
      formData.append(
        "origin_country",
        importCheckbox.checked ? document.getElementById("batch_origin_country").value.trim() : ""
      );
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
      showError("That's " + selectedFiles.length + " photos — we can check up to " +
        MAX_FILES + " at a time. Please split them into smaller batches.");
      return;
    }
    if (!manifestFile && !document.getElementById("batch_brand").value.trim()) {
      showError("Please either add a spreadsheet (CSV) with each label's details, or type at least the brand name to use for every photo.");
      return;
    }

    var total = selectedFiles.length;
    var chunks = [];
    for (var i = 0; i < total; i += CHUNK_SIZE) {
      chunks.push(selectedFiles.slice(i, i + CHUNK_SIZE));
    }

    allResults = [];
    resultsBody.textContent = "";
    resultsSection.hidden = true;
    setBusy(true);
    progressBlock.hidden = false;
    updateProgress(0, total);
    var startedAt = Date.now();

    var sequence = Promise.resolve();
    chunks.forEach(function (chunk) {
      sequence = sequence.then(function () {
        return sendChunk(chunk).catch(function (error) {
          // A failed sub-batch becomes error entries; the run continues.
          var message = error instanceof TypeError
            ? "We couldn't reach the checking service for these photos."
            : error.message;
          return chunk.map(function (file) {
            return { filename: file.name, error: { code: "request_failed", message: message } };
          });
        }).then(function (chunkResults) {
          chunkResults.forEach(function (entry) {
            allResults.push(entry);
            appendResultRow(entry);
          });
          updateProgress(allResults.length, total);
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
