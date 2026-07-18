/* TTB Label Checker — single-label verification flow.
   Plain-language UI over POST /api/verify. All dynamic text is inserted with
   textContent (never innerHTML) so server/user strings are always inert. */

"use strict";

(function () {
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

  var BANNERS = {
    match: { icon: "✅", text: "Everything matches" },
    review: { icon: "⚠️", text: "Needs a closer look" },
    mismatch: { icon: "❌", text: "Problems found" }
  };

  var form = document.getElementById("verify-form");
  var fileInput = document.getElementById("file-input");
  var dropzone = document.getElementById("dropzone");
  var dropzoneEmpty = document.getElementById("dropzone-empty");
  var dropzonePreview = document.getElementById("dropzone-preview");
  var previewImage = document.getElementById("preview-image");
  var fileName = document.getElementById("file-name");
  var checkButton = document.getElementById("check-button");
  var progress = document.getElementById("progress");
  var errorCallout = document.getElementById("error-callout");
  var errorMessage = document.getElementById("error-message");
  var results = document.getElementById("results");
  var banner = document.getElementById("banner");
  var bannerIcon = document.getElementById("banner-icon");
  var bannerText = document.getElementById("banner-text");
  var timing = document.getElementById("timing");
  var resultImage = document.getElementById("result-image");
  var resultsBody = document.getElementById("results-body");
  var importCheckbox = document.getElementById("is_import");
  var originField = document.getElementById("origin-field");

  var selectedFile = null;
  var previewUrl = null;

  /* ---------- file selection ---------- */

  function setFile(file) {
    if (!file) { return; }
    selectedFile = file;
    if (previewUrl) { URL.revokeObjectURL(previewUrl); }
    previewUrl = URL.createObjectURL(file);
    previewImage.src = previewUrl;
    fileName.textContent = file.name;
    dropzoneEmpty.hidden = true;
    dropzonePreview.hidden = false;
    // A new photo starts a new check: clear any previous verdicts and errors
    // so stale results never sit next to a photo they don't describe.
    results.hidden = true;
    hideError();
  }

  fileInput.addEventListener("change", function () {
    if (fileInput.files && fileInput.files.length > 0) {
      setFile(fileInput.files[0]);
    }
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
    if (files && files.length > 0) {
      setFile(files[0]);
    }
  });

  // Clicking anywhere in the empty dropzone opens the file picker
  // (the labels inside already do this natively; don't double-open).
  dropzone.addEventListener("click", function (event) {
    if (event.target.closest("label") || event.target === fileInput) { return; }
    fileInput.click();
  });

  /* ---------- import checkbox reveals the country field ---------- */

  function syncOriginField() {
    originField.hidden = !importCheckbox.checked;
  }
  importCheckbox.addEventListener("change", syncOriginField);
  syncOriginField();

  /* ---------- status helpers ---------- */

  function showError(message) {
    errorMessage.textContent = message;
    errorCallout.hidden = false;
    // Move focus so screen readers announce the problem (aria role=alert is
    // the backup); callers that focus a specific field do so after this.
    errorCallout.focus({ preventScroll: false });
  }

  function hideError() {
    errorCallout.hidden = true;
  }

  function setBusy(busy) {
    checkButton.disabled = busy;
    progress.hidden = !busy;
    checkButton.textContent = busy ? "Checking…" : "Check This Label";
  }

  /* ---------- results rendering ---------- */

  function shorten(text, max) {
    if (text.length <= max) { return text; }
    return text.slice(0, max - 1) + "…";
  }

  function valueCell(value, label) {
    var td = document.createElement("td");
    td.className = "value-cell";
    td.setAttribute("data-label", label);
    if (value === null || value === undefined || value === "") {
      td.textContent = "—";
      td.className += " muted";
    } else {
      var text = String(value);
      td.textContent = shorten(text, 140);
      if (text.length > 140) { td.title = text; }
    }
    return td;
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

  function renderResults(data) {
    var overall = BANNERS[data.overall_status] || BANNERS.review;
    banner.className = "banner banner-" + data.overall_status;
    bannerIcon.textContent = overall.icon;
    bannerText.textContent = overall.text;

    var ms = data.processing_time_ms;
    timing.textContent = ms < 1000
      ? "Checked in less than a second."
      : "Checked in " + (ms / 1000).toFixed(1) + " seconds.";

    resultImage.src = previewUrl;

    resultsBody.textContent = "";
    Object.keys(data.fields).forEach(function (key) {
      var field = data.fields[key];
      var row = document.createElement("tr");

      var nameCell = document.createElement("td");
      nameCell.className = "field-name";
      nameCell.textContent = FIELD_NAMES[key] || key;
      row.appendChild(nameCell);

      var verdict = VERDICTS[field.verdict] || VERDICTS.review;
      var verdictCell = document.createElement("td");
      verdictCell.className = "verdict verdict-" + field.verdict;
      verdictCell.setAttribute("data-label", "Result");
      verdictCell.textContent = verdict.icon + " " + verdict.text;
      row.appendChild(verdictCell);

      row.appendChild(valueCell(field.extracted, "On the label"));
      row.appendChild(valueCell(field.expected, "On the application"));

      var reasonCell = document.createElement("td");
      reasonCell.className = "reason-cell";
      reasonCell.setAttribute("data-label", "Explanation");
      reasonCell.textContent = field.reason || "";
      if (
        key === "government_warning" &&
        field.detail &&
        Array.isArray(field.detail.clause_diff) &&
        field.detail.clause_diff.length > 0
      ) {
        reasonCell.appendChild(clauseDiffBlock(field.detail.clause_diff));
      }
      row.appendChild(reasonCell);

      resultsBody.appendChild(row);
    });

    results.hidden = false;
    results.scrollIntoView({ behavior: "smooth", block: "start" });
    // Move focus to the outcome banner so screen readers announce the result
    // as soon as it appears (aria-live on the section is the backup).
    banner.focus({ preventScroll: false });
  }

  /* ---------- submit ---------- */

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    hideError();

    if (!selectedFile) {
      showError("Please add a photo of the label first — drag it into the box above or use “Choose a photo”.");
      return;
    }
    var brand = document.getElementById("brand").value.trim();
    if (!brand) {
      showError("Please enter the brand name — it’s the one detail we need to get started.");
      document.getElementById("brand").focus();
      return;
    }

    var formData = new FormData();
    formData.append("file", selectedFile);
    formData.append("brand", brand);
    ["class_type", "abv", "net_contents", "producer"].forEach(function (name) {
      formData.append(name, document.getElementById(name).value.trim());
    });
    formData.append("is_import", importCheckbox.checked ? "true" : "false");
    formData.append(
      "origin_country",
      importCheckbox.checked ? document.getElementById("origin_country").value.trim() : ""
    );

    setBusy(true);
    results.hidden = true;

    fetch("/api/verify", { method: "POST", body: formData })
      .then(function (response) {
        return response.json().catch(function () { return null; }).then(function (body) {
          if (!response.ok || !body || !body.fields) {
            var message = (body && body.error && body.error.message) ||
              "Something went wrong on our end. Please try again.";
            throw new Error(message);
          }
          renderResults(body);
        });
      })
      .catch(function (error) {
        var message = error instanceof TypeError
          ? "We couldn't reach the checking service. Please make sure you're connected and try again."
          : error.message;
        showError(message);
      })
      .finally(function () {
        setBusy(false);
      });
  });
})();
