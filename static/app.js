// AI Data Profiling Agent - page interactions (login page + main page)

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

// ---------- toasts fade out ----------
$$(".toast").forEach(t => setTimeout(() => { t.classList.add("hide"); setTimeout(() => t.remove(), 500); }, 3500));

// ---------- login page ----------
const authCard = $(".auth-card");
if (authCard) {
  const setMode = mode => {
    authCard.dataset.mode = mode;
    $(`[data-form="${mode}"] input:not([type=hidden])`, authCard)?.focus();
  };
  $$("[data-tab]", authCard).forEach(el => el.addEventListener("click", e => { e.preventDefault(); setMode(el.dataset.tab); }));

  $$(".pw-toggle").forEach(btn => btn.addEventListener("click", () => {
    const input = btn.previousElementSibling;
    const show = input.type === "password";
    input.type = show ? "text" : "password";
    btn.textContent = show ? "Hide" : "Show";
    btn.setAttribute("aria-label", show ? "Hide password" : "Show password");
  }));

  $$("input[type=password]").forEach(input => input.addEventListener("keyup", e => {
    const warn = $(".caps-warn", input.closest(".field"));
    if (warn && e.getModifierState) warn.hidden = !e.getModifierState("CapsLock");
  }));

  const regForm = $('[data-form="register"]');
  const pw = $('input[name="password"]', regForm);
  const meter = $(".strength", regForm);
  pw.addEventListener("input", () => {
    const v = pw.value;
    meter.dataset.score = v ? [v.length >= 8, /[A-Za-z]/.test(v) && /\d/.test(v), /[^A-Za-z0-9]/.test(v), v.length >= 12]
      .filter(Boolean).length || 1 : 0;
  });

  // live validation on the register form, same rules as the server
  const rules = {
    full_name: v => v.trim().length >= 2 || "Please enter your name.",
    username: v => /^[A-Za-z0-9._-]{3,30}$/.test(v) || "3-30 characters: letters, numbers, dot, dash or underscore.",
    password: v => (v.length >= 8 && /[A-Za-z]/.test(v) && /\d/.test(v)) || "At least 8 characters, with letters and numbers.",
    confirm: v => v === pw.value || "Passwords do not match.",
  };
  const check = input => {
    const result = rules[input.name](input.value);
    const field = input.closest(".field");
    field.classList.toggle("has-error", result !== true);
    $(".err", field).textContent = result === true ? "" : result;
    return result === true;
  };
  $$("input", regForm).filter(i => rules[i.name]).forEach(i => {
    i.addEventListener("blur", () => i.value && check(i));
    i.addEventListener("input", () => i.closest(".field").classList.contains("has-error") && check(i));
  });

  $$(".auth-form").forEach(form => form.addEventListener("submit", e => {
    if (form === regForm) {
      const ok = $$("input", regForm).filter(i => rules[i.name]).map(check).every(Boolean);
      if (!ok) { e.preventDefault(); $(".has-error input", regForm)?.focus(); return; }
    } else if (!$$("input[required]", form).every(i => i.value)) {
      e.preventDefault();
      $$("input[required]", form).find(i => !i.value).focus();
      return;
    }
    const btn = $("button[type=submit]", form);
    btn.classList.add("loading");
    btn.disabled = true;
  }));
}

// ---------- shared by the app pages (profiling + pipeline) ----------
const csrfToken = $('meta[name="csrf-token"]').content;

function el(tag, props = {}, children = []) {
  const node = Object.assign(document.createElement(tag), props);
  children.forEach(c => node.append(c));
  return node;
}

async function api(url, options = {}) {
  const res = await fetch(url, { ...options, headers: { "X-CSRF-Token": csrfToken, ...(options.headers || {}) } });
  const data = await res.json().catch(() => ({ error: `Server error (${res.status})` }));
  if (!res.ok) throw new Error(data.error || `Server error (${res.status})`);
  return data;
}

function busy(btn, on, label) {
  btn.classList.toggle("loading", on);
  btn.disabled = on;
  if (label) $(".btn-label", btn).textContent = label;
}

const fmt = n => (n ?? 0).toLocaleString();

if ($(".avatar")) {
  // user menu
  const avatar = $(".avatar"), menu = $(".menu");
  avatar.addEventListener("click", e => {
    e.stopPropagation();
    menu.hidden = !menu.hidden;
    avatar.setAttribute("aria-expanded", String(!menu.hidden));
  });
  document.addEventListener("click", e => { if (!menu.contains(e.target)) menu.hidden = true; });
  document.addEventListener("keydown", e => { if (e.key === "Escape") menu.hidden = true; });

  // Snowflake status
  fetch("/api/status").then(r => r.json()).then(s => {
    const sfChip = $("#sf-status"), aiChip = $("#ai-status");
    sfChip.classList.add(s.snowflake_configured ? "ok" : "warn");
    $("span", sfChip).textContent = s.snowflake_configured ? "Snowflake connected" : "Snowflake not set up (.env)";
    aiChip.classList.add(s.ai.ready ? "ok" : "warn");
    $("span", aiChip).textContent = s.ai.ready ? `AI: ${s.ai.model}` : s.ai.problem;
    aiChip.title = s.ai.ready ? `AI provider: ${s.ai.provider}` : s.ai.problem;
  }).catch(() => {});
}

// ---------- main page ----------
const dropzone = $("#dropzone");
if (dropzone) {

  const input = $("#file-input");
  const summary = $("#file-summary");
  let currentFile = null;
  const DELIMS = { "|": "Pipe ( | )", ",": "Comma ( , )", "\t": "Tab", ";": "Semicolon ( ; )" };

  ["dragenter", "dragover"].forEach(ev => dropzone.addEventListener(ev, e => { e.preventDefault(); dropzone.classList.add("drag"); }));
  ["dragleave", "drop"].forEach(ev => dropzone.addEventListener(ev, e => { e.preventDefault(); dropzone.classList.remove("drag"); }));
  dropzone.addEventListener("drop", e => e.dataTransfer.files[0] && showFile(e.dataTransfer.files[0]));
  input.addEventListener("change", () => input.files[0] && showFile(input.files[0]));
  $("#clear-file").addEventListener("click", () => input.click());

  const formatSize = b => b < 1024 ? `${b} B` : b < 1048576 ? `${(b / 1024).toFixed(1)} KB` : `${(b / 1048576).toFixed(1)} MB`;

  // split one line on the delimiter, honouring "quoted, values"
  function splitLine(line, d) {
    const out = []; let cur = "", q = false;
    for (let i = 0; i < line.length; i++) {
      const c = line[i];
      if (c === '"') { if (q && line[i + 1] === '"') { cur += '"'; i++; } else q = !q; }
      else if (c === d && !q) { out.push(cur); cur = ""; }
      else cur += c;
    }
    out.push(cur);
    return out;
  }

  async function showFile(file) {
    if (!/\.(csv|txt|tsv|psv)$/i.test(file.name)) { alert("Please choose a CSV, TXT or TSV file."); return; }
    const text = await file.text();
    const lines = text.split(/\r?\n/).filter(l => l.trim() !== "");
    if (lines.length < 2) { alert("The file needs a header row and at least one data row."); return; }
    const delim = Object.keys(DELIMS).reduce((a, b) => lines[0].split(b).length > lines[0].split(a).length ? b : a);
    const header = splitLine(lines[0], delim);
    const rows = lines.slice(1, 11).map(l => splitLine(l, delim));

    $("#s-name").textContent = file.name;
    $("#s-name").title = file.name;
    $("#s-size").textContent = formatSize(file.size);
    $("#s-rows").textContent = (lines.length - 1).toLocaleString();
    $("#s-cols").textContent = header.length;
    $("#s-delim").textContent = DELIMS[delim];

    const table = $("#preview");
    table.replaceChildren();
    const thead = table.createTHead().insertRow();
    header.forEach(h => { const th = document.createElement("th"); th.textContent = h; thead.appendChild(th); });
    const tbody = table.createTBody();
    rows.forEach(r => {
      const tr = tbody.insertRow();
      header.forEach((_, i) => {
        const td = tr.insertCell();
        const v = r[i] ?? "";
        td.textContent = v === "" ? "empty" : v;
        if (v === "") td.className = "empty";
      });
    });

    currentFile = file;
    summary.hidden = false;
    $("#upload-error").hidden = true;
    $("#rules-section").hidden = true;
    dropzone.classList.add("compact");
    setStep(1);
  }

  function setStep(done) {
    $$(".stepper li").forEach(li => {
      const n = Number(li.dataset.step);
      li.classList.toggle("done", n <= done);
      li.classList.toggle("current", n === done + 1);
    });
  }

  // ---------- step 2: propose, review and approve checks ----------
  const TYPE_LABELS = {
    ID: "ID / key", NAME: "Person name", DATE: "Date", BIRTH_DATE: "Date of birth", PHONE: "Phone", SSN: "SSN", TAX_ID: "Tax ID / EIN",
    ZIP: "ZIP code", STATE: "State code", CODE: "Code (fixed list)", AMOUNT: "Amount ($)", NUMBER: "Number", TEXT: "Free text",
  };
  // Keep in step with the SQL checks built in step 3
  const TYPE_CHECKS = {
    ID: () => ["Special characters"],
    NAME: () => ["Contains digits", "Special characters"],
    DATE: r => [`Invalid date / not ${r.date_format || "MM/DD/YYYY"}`, "Future date (except 12/31/9999)", "Before 1900"],
    BIRTH_DATE: r => [`Invalid date / not ${r.date_format || "MM/DD/YYYY"}`, "Future date", "Older than 120 years"],
    PHONE: () => ["Not 10 digits", "Special characters / formatting", "All same digit (0000000000)"],
    SSN: () => ["Not 9 digits", "Dashes or other characters", "Invalid number (000, 666, 9xx…)"],
    TAX_ID: () => ["Not 9 digits", "Dashes or other characters"],
    ZIP: () => ["Not 5 or 9 digits"],
    STATE: () => ["Not a valid US state code"],
    CODE: r => r.allowed_values.length ? [`Not in: ${r.allowed_values.join(", ")}`] : [],
    AMOUNT: () => ["Not a valid amount", "Negative amount"],
    NUMBER: () => ["Not a number"],
    TEXT: () => ["Unusual special characters"],
  };
  const checksFor = r => [
    ...(r.required ? ["Missing values"] : []),
    ...(r.min_len != null ? [`Shorter than ${r.min_len}`] : []),
    ...(r.max_len != null ? [`Longer than ${r.max_len}`] : []),
    ...(r.unique ? ["Duplicates"] : []),
    ...TYPE_CHECKS[r.type](r),
    "Leading / trailing spaces",
  ];

  let run = null, proposed = [], rules = [];



  $("#generate").addEventListener("click", async () => {
    const btn = $("#generate"), err = $("#upload-error");
    if (!currentFile) return;
    err.hidden = true;
    busy(btn, true, "AI is reading the columns… (up to a minute)");
    try {
      const body = new FormData();
      body.append("file", currentFile);
      run = await api("/api/runs", { method: "POST", body });
      proposed = run.rules.map(r => ({ ...r, allowed_values: [...r.allowed_values] }));
      rules = proposed.map(r => ({ ...r, allowed_values: [...r.allowed_values] }));
      showRules();
    } catch (e) {
      err.textContent = e.message;
      err.hidden = false;
    } finally {
      busy(btn, false, "Generate profiling checks with AI →");
    }
  });

  function showRules() {
    const banner = $("#rules-banner");
    banner.textContent = run.info.message;
    banner.className = "banner" + (run.info.source === "ai" ? "" : " warn");
    $("#rules-sub").textContent = `${run.filename} · ${run.rows.toLocaleString()} rows · ${run.columns.length} columns`;
    $("#approved").hidden = true;
    $("#rules-error").hidden = true;
    renderRules();
    $("#rules-section").hidden = false;
    $("#rules-section").scrollIntoView({ behavior: "smooth", block: "start" });
  }


  function renderRules() {
    const body = $("#rules-body");
    body.replaceChildren(...rules.map((r, i) => {
      const tr = el("tr", { className: r.source === "user" ? "edited" : "" });
      const badge = { ai: ["AI", "ai"], rules: ["RULE", ""], user: ["EDITED", "user"] }[r.source] || ["", ""];
      tr.append(el("td", {}, [
        el("div", { className: "col-name" }, [r.column, el("span", { className: `badge ${badge[1]}`, textContent: badge[0] })]),
        el("div", { className: "col-samples", textContent: (run.samples[r.column] || []).join(" · ") || "all empty",
                    title: (run.samples[r.column] || []).join("\n") }),
        el("div", { className: "col-reason", textContent: r.reason }),
      ]));

      const type = el("select", { ariaLabel: `${r.column} type` },
        Object.keys(TYPE_LABELS).map(t => el("option", { value: t, textContent: TYPE_LABELS[t], selected: t === r.type })));
      type.addEventListener("change", () => edit(i, { type: type.value,
        date_format: ["DATE", "BIRTH_DATE"].includes(type.value) ? (r.date_format || "MM/DD/YYYY") : null }, true));
      tr.append(el("td", {}, [type]));

      const flags = el("td", { className: "flags" }, [["required", "Required"], ["unique", "Unique"]].map(([key, label]) => {
        const cb = el("input", { type: "checkbox", checked: r[key], ariaLabel: `${r.column} ${key}` });
        cb.addEventListener("change", () => edit(i, { [key]: cb.checked }));
        return el("label", { className: "flag" }, [cb, label]);
      }));
      tr.append(flags);

      const lens = ["min_len", "max_len"].map(key => {
        const num = el("input", { type: "number", min: 0, value: r[key] ?? "", placeholder: key === "min_len" ? "min" : "max",
                                  ariaLabel: `${r.column} ${key}` });
        num.addEventListener("input", () => edit(i, { [key]: num.value === "" ? null : Math.max(0, parseInt(num.value, 10)) }));
        return num;
      });
      tr.append(el("td", {}, [el("div", { className: "len" }, [lens[0], "–", lens[1]])]));

      const setting = el("td");
      if (["DATE", "BIRTH_DATE"].includes(r.type)) {
        const fmt = el("input", { type: "text", value: r.date_format || "MM/DD/YYYY", ariaLabel: `${r.column} date format` });
        fmt.addEventListener("change", () => edit(i, { date_format: fmt.value.trim() || "MM/DD/YYYY" }, true));
        setting.append(el("span", { className: "set-label", textContent: "Date format" }), fmt);
      } else if (r.type === "CODE") {
        const allowed = el("input", { type: "text", value: r.allowed_values.join(", "), placeholder: "any - e.g. M, F, U",
                                      ariaLabel: `${r.column} allowed values` });
        allowed.addEventListener("change", () => edit(i, { allowed_values: allowed.value.split(",").map(v => v.trim()).filter(Boolean) }, true));
        setting.append(el("span", { className: "set-label", textContent: "Allowed values" }), allowed);
      } else {
        setting.append(el("span", { className: "muted", textContent: "—" }));
      }
      tr.append(setting);

      tr.append(el("td", {}, [el("div", { className: "chips" }, checksFor(r).map(c => el("span", { className: "chip", textContent: c })))]));
      return tr;
    }));
    updateStats();
  }

  // rerender = true when the change affects other inputs in the row (type, date format, allowed values)
  function edit(i, changes, rerender = false) {
    Object.assign(rules[i], changes, { source: "user" });
    $("#approved").hidden = true;
    $("#results-section").hidden = true;
    $("#report-section").hidden = true;
    if (rerender) { renderRules(); return; }
    const row = $("#rules-body").rows[i];
    row.classList.add("edited");
    $(".badge", row).className = "badge user";
    $(".badge", row).textContent = "EDITED";
    row.cells[5].firstChild.replaceChildren(...checksFor(rules[i]).map(c => el("span", { className: "chip", textContent: c })));
    const [minIn, maxIn] = $$("input[type=number]", row.cells[3]);
    const bad = rules[i].min_len != null && rules[i].max_len != null && rules[i].min_len > rules[i].max_len;
    minIn.classList.toggle("invalid", bad);
    maxIn.classList.toggle("invalid", bad);
    updateStats();
  }

  function updateStats() {
    const total = rules.reduce((n, r) => n + checksFor(r).length, 0);
    const edited = rules.filter(r => r.source === "user").length;
    $("#rules-stats").replaceChildren(
      el("span", {}, [el("strong", { textContent: rules.length }), "columns"]),
      el("span", {}, [el("strong", { textContent: total }), "checks"]),
      el("span", {}, [el("strong", { textContent: edited }), "edited"]),
    );
  }

  $("#reset-rules").addEventListener("click", () => {
    rules = proposed.map(r => ({ ...r, allowed_values: [...r.allowed_values] }));
    $("#approved").hidden = true;
    renderRules();
  });

  $("#approve").addEventListener("click", async () => {
    const btn = $("#approve"), err = $("#rules-error");
    err.hidden = true;
    busy(btn, true, "Saving…");
    try {
      await api(`/api/runs/${run.run_id}/rules`, {
        method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ rules }),
      });
      $("#approved").hidden = false;
      $("#results-section").hidden = true;
      $("#report-section").hidden = true;
      setStep(2);
    } catch (e) {
      err.textContent = e.message;
      err.hidden = false;
    } finally {
      busy(btn, false, "Approve checks →");
    }
  });

  // ---------- step 3: run checks in Snowflake and show results ----------
  let results = null;

  $("#run-checks").addEventListener("click", async () => {
    const btn = $("#run-checks"), err = $("#run-error");
    err.hidden = true;
    busy(btn, true, "Running in Snowflake…");
    try {
      results = await api(`/api/runs/${run.run_id}/execute`, { method: "POST" });
      showResults();
    } catch (e) {
      err.textContent = e.message;
      err.hidden = false;
    } finally {
      busy(btn, false, "Run checks in Snowflake →");
    }
  });

  function showResults() {
    $("#results-sub").textContent = `${run.filename} · checked in Snowflake in ${results.seconds}s`;
    const tiles = [
      ["Rows checked", fmt(results.rows)], ["Checks run", fmt(results.checks_run)],
      ["Checks with issues", fmt(results.checks_failed)], ["Issues found", fmt(results.issues)],
      ["Columns with issues", `${results.columns_with_issues} of ${results.profile.length}`],
    ];
    $("#results-stats").replaceChildren(...tiles.map(([label, value], i) =>
      el("div", { className: i >= 2 && results.issues ? "bad" : "" }, [el("span", { className: "muted", textContent: label }),
                                                                        el("strong", { textContent: value })])));
    renderIssues();
    renderProfile();
    renderSaved();
    $("#results-section").hidden = false;
    $("#report-section").hidden = false;
    $("#summary").hidden = true;
    $("#download-xlsx").href = `/api/runs/${run.run_id}/report.xlsx`;
    setStep(3);
    $("#results-section").scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function renderIssues() {
    const showPassed = $("#show-passed").checked;
    const rows = results.checks.filter(c => showPassed || c.failed);
    const passed = results.checks.length - results.checks_failed;
    $("#issues-note").textContent = `(${results.checks_failed} checks with issues, ${passed} passed)`;
    $("#issues-body").replaceChildren(...(rows.length ? rows.map(c => el("tr", { className: c.failed ? "" : "passed" }, [
      el("td", { className: "strong", textContent: c.column }),
      el("td", { textContent: c.check }),
      el("td", { className: "num" }, [c.failed
        ? el("button", { type: "button", className: "num-link", textContent: fmt(c.failed), title: "Show these rows",
                         onclick: () => openRows(c) })
        : "✓ 0"]),
      el("td", {}, [el("div", { className: "pct" }, [
        el("div", { className: "bar" }, [el("i", { style: `width:${Math.min(100, Math.max(c.failed ? 2 : 0, c.pct))}%` })]),
        el("span", { textContent: `${c.pct}%` })])]),
      el("td", {}, [el("div", { className: "examples" },
        c.examples.length ? c.examples.map(x => el("code", { textContent: x })) : [el("span", { className: "muted", textContent: c.failed ? "(empty)" : "" })])]),
    ])) : [el("tr", {}, [el("td", { colSpan: 5, className: "muted", textContent: "No issues found - every check passed." })])]));
  }
  $("#show-passed").addEventListener("change", renderIssues);

  function renderProfile() {
    $("#profile-body").replaceChildren(...results.profile.map(p => el("tr", {}, [
      el("td", { className: "strong", textContent: p.column }),
      el("td", { textContent: TYPE_LABELS[p.type] || p.type }),
      el("td", { className: "num", textContent: fmt(p.nulls) }),
      el("td", { className: "num", textContent: `${p.null_pct}%` }),
      el("td", { className: "num", textContent: fmt(p.distinct) }),
      el("td", { className: "num", textContent: p.min_len ?? "—" }),
      el("td", { className: "num", textContent: p.max_len ?? "—" }),
      el("td", {}, [el("div", { className: "examples" }, p.top_values.map(t =>
        el("code", { textContent: `${t.value ?? "(empty)"} · ${fmt(t.count)}` })))]),
    ])));
  }

  $("#download-csv").addEventListener("click", () => {
    const esc = v => `"${String(v).replaceAll('"', '""')}"`;
    const lines = [["Column", "Check", "Failed rows", "% of rows", "Examples"].map(esc).join(",")]
      .concat(results.checks.map(c => [c.column, c.check, c.failed, c.pct, c.examples.join(" | ")].map(esc).join(",")));
    const a = el("a", { href: URL.createObjectURL(new Blob([lines.join("\r\n")], { type: "text/csv" })),
                        download: run.filename.replace(/\.[^.]+$/, "") + "_profiling_issues.csv" });
    a.click();
    URL.revokeObjectURL(a.href);
  });

  // ---------- step 4: AI summary + Excel report ----------
  $("#write-summary").addEventListener("click", async () => {
    const btn = $("#write-summary"), err = $("#summary-error");
    err.hidden = true;
    busy(btn, true, "AI is writing… (up to a minute)");
    try {
      const data = await api(`/api/runs/${run.run_id}/summary`, { method: "POST" });
      showSummary(data);
    } catch (e) {
      err.textContent = e.message;
      err.hidden = false;
    } finally {
      busy(btn, false, "Rewrite summary with AI");
    }
  });

  function showSummary({ summary, info, findings }) {
    const banner = $("#summary-banner");
    banner.textContent = info.message;
    banner.className = "banner" + (info.source === "ai" ? " ok" : " warn");
    $("#s-headline").textContent = summary.headline;
    $("#s-overview").textContent = summary.overview;
    const facts = Object.fromEntries(findings.map(f => [f.id, f]));
    $("#s-findings").replaceChildren(...summary.findings.map(f => {
      const x = facts[f.id];
      return el("li", {}, [
        el("div", { className: "finding-head" }, [
          el("span", { className: `sev ${x.severity.toLowerCase()}`, textContent: x.severity }),
          el("strong", { textContent: `${x.column} · ${x.check}` }),
          el("span", { className: "muted", textContent: `${fmt(x.failed_rows)} rows` }),
        ]),
        el("p", { textContent: f.explanation }),
        el("p", { className: "rec", textContent: `→ ${f.recommendation}` }),
      ]);
    }));
    $("#s-steps").replaceChildren(...summary.next_steps.map(s => el("li", { textContent: s })));
    $("#summary").hidden = false;
    setStep(4);
  }

  // ---------- drill-down: the exact rows behind a count ----------
  const dialog = $("#rows-dialog");
  let drill = null;

  // make spaces, tabs and empty values visible
  function showValue(v) {
    if (v === null || v === undefined || v === "") return el("span", { className: "muted", textContent: "(empty)" });
    const text = v.replaceAll("\t", "⇥");
    return el("code", { className: "val", textContent: /^\s|\s$/.test(v) ? `"${text}"` : text });
  }

  async function openRows(check) {
    $("#rd-title").textContent = `${check.column} · ${check.check}`;
    $("#rd-sub").textContent = "Loading rows…";
    $("#rd-meta").replaceChildren();
    $("#rd-table").replaceChildren();
    $("#rd-count").textContent = "";
    dialog.showModal();
    try {
      drill = await api(`/api/runs/${run.run_id}/checks/${check.id}/rows`);
    } catch (e) {
      $("#rd-sub").textContent = e.message;
      return;
    }
    const r = drill.rule, o = drill.observed;
    const lengthRule = r.min_len != null && r.max_len != null ? `${r.min_len} – ${r.max_len}`
      : r.max_len != null ? `max ${r.max_len}` : r.min_len != null ? `min ${r.min_len}` : "no limit";
    $("#rd-sub").textContent = `${fmt(drill.failed)} rows failed (${drill.pct}% of the file)`;
    const meta = [
      ["Table", drill.table], ["Column", drill.column], ["Data type", TYPE_LABELS[drill.type] || drill.type],
      ["Loaded as", drill.loaded_as], ["Length rule", lengthRule],
      ["Actual length in file (min – max, trimmed)", o.min_len != null ? `${o.min_len} – ${o.max_len}` : "all empty"],
      ...(r.date_format ? [["Date format", r.date_format]] : []),
      ...(r.allowed_values.length ? [["Allowed values", r.allowed_values.join(", ")]] : []),
      ["Required", r.required ? "Yes" : "No"],
    ];
    $("#rd-meta").replaceChildren(...meta.map(([k, v]) => el("div", {}, [el("span", { className: "muted", textContent: k }),
                                                                        el("strong", { textContent: v })])));
    const shown = drill.rows.length;
    $("#rd-count").textContent = shown < drill.failed
      ? `Showing the first ${fmt(shown)} of ${fmt(drill.failed)} rows` : `Showing all ${fmt(shown)} rows`;
    renderDrill();
  }

  function renderDrill() {
    if (!drill) return;
    const all = $("#rd-all-cols").checked;
    const cols = all ? drill.columns : [...new Set([drill.key_column, drill.column])];
    const head = el("tr", {}, [el("th", { className: "num", textContent: "Row #", title: "1 = first data row after the header" }),
      ...cols.map(c => el("th", { className: c === drill.column ? "hl" : "", textContent: c })),
      ...(all ? [] : [el("th", { className: "num", textContent: "Length", title: "Length of the value as it is in the file, spaces included" })])]);
    const body = drill.rows.map(row => el("tr", {}, [
      el("td", { className: "num", textContent: fmt(row.row) }),
      ...cols.map(c => el("td", { className: c === drill.column ? "hl" : "" }, [showValue(row.record[c])])),
      ...(all ? [] : [el("td", { className: "num", textContent: row.length })]),
    ]));
    const table = $("#rd-table");
    table.replaceChildren(table.createTHead(), table.createTBody());
    table.tHead.append(head);
    table.tBodies[0].append(...body);
  }

  $("#rd-all-cols").addEventListener("change", renderDrill);
  $("#rd-close").addEventListener("click", () => dialog.close());
  dialog.addEventListener("click", e => { if (e.target === dialog) dialog.close(); });
  dialog.addEventListener("close", () => { drill = null; $("#rd-all-cols").checked = false; });

  $("#rd-csv").addEventListener("click", () => {
    if (!drill) return;
    const esc = v => `"${String(v ?? "").replaceAll('"', '""')}"`;
    const header = ["Row #", ...drill.columns, `${drill.column} length`];
    const lines = [header.map(esc).join(",")].concat(drill.rows.map(r =>
      [r.row, ...drill.columns.map(c => r.record[c]), r.length].map(esc).join(",")));
    const name = `${drill.table}_${drill.column}_${drill.check.replace(/[^A-Za-z0-9]+/g, "_")}`.replace(/_+$/, "");
    const a = el("a", { href: URL.createObjectURL(new Blob([lines.join("\r\n")], { type: "text/csv" })), download: `${name}.csv` });
    a.click();
    URL.revokeObjectURL(a.href);
  });

  // ---------- permanent Snowflake tables + export queries ----------
  function renderSaved() {
    const box = $("#saved-box"), saved = results.saved;
    box.hidden = !saved;
    if (!saved) return;
    if (!saved.ok) {
      $("#saved-title").textContent = "⚠ Results were not saved to Snowflake";
      $("#saved-sub").textContent = saved.error;
      $("#saved-tables").replaceChildren();
      $("#query-tabs").replaceChildren();
      $(".query-box").hidden = true;
      return;
    }
    $(".query-box").hidden = false;
    $("#saved-title").textContent = `✓ Saved to Snowflake: ${saved.schema}`;
    $("#saved-sub").textContent = saved.replaced_previous
      ? `The previous results for ${saved.source_file} were replaced with this run.`
      : `First run for ${saved.source_file}. Re-running this file will replace these rows.`;
    $("#saved-tables").replaceChildren(...Object.entries(saved.rows_saved).map(([t, n]) =>
      el("div", {}, [el("code", { textContent: t }), el("span", { className: "muted", textContent: `${fmt(n)} rows` })])));
    const queries = Object.fromEntries(saved.export_queries);
    const names = saved.export_queries.map(([name]) => name);
    const show = name => {
      $("#query-text").textContent = queries[name];
      $$("#query-tabs button").forEach(b => b.classList.toggle("active", b.textContent === name));
    };
    $("#query-tabs").replaceChildren(...names.map(n => el("button", { type: "button", textContent: n, onclick: () => show(n) })));
    show(names[0]);
  }

  $("#copy-query").addEventListener("click", async () => {
    const btn = $("#copy-query");
    try {
      await navigator.clipboard.writeText($("#query-text").textContent);
      btn.textContent = "Copied ✓";
    } catch {
      const range = document.createRange();
      range.selectNodeContents($("#query-text"));
      getSelection().removeAllRanges();
      getSelection().addRange(range);
      btn.textContent = "Selected - press ⌘C";
    }
    setTimeout(() => { btn.textContent = "Copy query"; }, 1800);
  });
}

// ---------- approvals badge in the sidebar (every app page; local file, free to poll) ----------
const navBadge = $("#nav-approvals");
async function refreshBadge() {
  try {
    const { pending } = await api("/api/pipeline/approvals");
    navBadge.hidden = !pending.length;
    navBadge.textContent = pending.length;
    return pending;
  } catch { return null; }
}
if (navBadge && !$("#approvals")) { refreshBadge(); setInterval(refreshBadge, 30000); }

// ---------- pipeline page ----------
if ($("#approvals")) {
  const STATUS = { LOADED: "ok", QUARANTINED: "warn", NEEDS_HUMAN: "bad", ABORTED: "bad" };
  const when = t => t ? new Date(t).toLocaleString([], { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }) : "—";
  const dur = s => s == null ? "running…" : s >= 60 ? `${Math.floor(s / 60)}m ${s % 60}s` : `${s}s`;

  async function loadApprovals() {
    const data = await refreshBadge();
    if (!data) return;
    const { recent } = await api("/api/pipeline/approvals");
    const box = $("#approvals");
    if (!data.length) {
      box.replaceChildren(el("p", { className: "empty-state muted", textContent: "Nothing waiting for you." }));
    } else {
      box.replaceChildren(...data.map(a => {
        const note = el("input", { type: "text", placeholder: "Note for the log (optional, e.g. why you reject)", className: "note-input" });
        const answer = async (approve, btn) => {
          busy(btn, true);
          try { await api(`/api/pipeline/approvals/${a.id}`, { method: "POST", headers: { "Content-Type": "application/json" },
                                                            body: JSON.stringify({ approve, note: note.value }) }); }
          catch (e) { alert(e.message); }
          loadApprovals();
        };
        const yes = el("button", { type: "button", className: "btn primary", onclick: () => answer(true, yes) }, [el("span", { className: "btn-label", textContent: "Approve" })]);
        const no = el("button", { type: "button", className: "btn ghost danger", onclick: () => answer(false, no) }, [el("span", { className: "btn-label", textContent: "Reject" })]);
        return el("div", { className: "approval" }, [
          el("div", { className: "approval-head" }, [
            el("span", { className: "pill", textContent: a.batch }),
            el("strong", { textContent: `Step ${a.step}: ${a.tool.replaceAll("_", " ")}` }),
            el("span", { className: "muted small", textContent: `asked ${when(a.requested_at)}` }),
          ]),
          el("p", { className: "approval-reason", textContent: `“${a.reason}”` }),
          el("div", { className: "approval-actions" }, [note, no, yes]),
        ]);
      }));
    }
    $("#recent-wrap").hidden = !recent.length;
    $("#recent-approvals").replaceChildren(...recent.map(a => el("div", { className: "recent small" }, [
      el("span", { className: `sev ${a.status === "APPROVED" ? "low" : "high"}`, textContent: a.status }),
      ` ${a.batch} · ${a.tool} · ${a.note || ""}`])));
  }

  async function loadRuns() {
    const btn = $("#refresh-runs"), err = $("#runs-error");
    busy(btn, true); err.hidden = true;
    try {
      const { runs } = await api("/api/pipeline/runs");
      $("#runs-body").replaceChildren(...(runs.length ? runs.flatMap(r => {
        const row = el("tr", { className: "run-row", title: "Show decisions" }, [
          el("td", {}, [el("span", { className: `run-status ${STATUS[r.final_status] || "running"}`, textContent: r.final_status || "RUNNING" })]),
          el("td", { className: "strong", textContent: r.batch_id }),
          el("td", { textContent: when(r.started_at) }),
          el("td", { className: "num", textContent: dur(r.seconds) }),
          el("td", { className: "num", textContent: r.steps ?? "—" }),
          el("td", { textContent: r.approval_mode }),
          el("td", { className: "run-summary", textContent: r.summary || "" }),
        ]);
        const detail = el("tr", { className: "run-detail", hidden: true }, [el("td", { colSpan: 7 })]);
        row.addEventListener("click", () => toggleRun(r, detail));
        return [row, detail];
      }) : [el("tr", {}, [el("td", { colSpan: 7, className: "muted", textContent: "No agent runs yet. Drop a batch into s3://…/incoming/<BATCH_ID>/." })])]));
      $("#runs-sub").textContent = `${runs.length} most recent runs · click one to see every decision`;
    } catch (e) {
      err.textContent = e.message; err.hidden = false;
    } finally { busy(btn, false, "Refresh"); }
  }

  async function toggleRun(run, detail) {
    detail.hidden = !detail.hidden;
    if (detail.hidden || detail.dataset.loaded) return;
    const cell = detail.firstChild;
    cell.replaceChildren(el("span", { className: "muted", textContent: "Loading decisions…" }));
    try {
      const { steps } = await api(`/api/pipeline/runs/${encodeURIComponent(run.run_id)}`);
      cell.replaceChildren(el("ul", { className: "timeline" }, steps.map(s => {
        const res = s.result || {};
        const facts = res.error || res.message || (res.problems && res.problems.join("; ")) || res.summary_replaced && "summary rewritten by code" || "";
        return el("li", { className: s.verdict === "BLOCKED" ? "blocked" : s.result_ok === false ? "failed" : "" }, [
          el("div", { className: "tl-head" }, [
            el("strong", { textContent: `${s.step_no}. ${s.tool}` }),
            s.verdict === "BLOCKED" ? el("span", { className: "sev high", textContent: "blocked by guard" }) : "",
            s.approval ? el("span", { className: `sev ${s.approval === "APPROVED" ? "low" : "high"}`, textContent: s.approval }) : "",
            s.result_ok === false && s.verdict !== "BLOCKED" ? el("span", { className: "sev medium", textContent: "not ok" }) : "",
          ]),
          el("p", { className: "tl-reason", textContent: s.reason ? `why: ${s.reason}` : "" }),
          facts ? el("p", { className: "tl-facts muted small", textContent: String(facts).slice(0, 300) }) : "",
        ]);
      })));
      detail.dataset.loaded = "1";
    } catch (e) { cell.replaceChildren(el("span", { className: "alert", textContent: e.message })); }
  }

  $("#refresh-runs").addEventListener("click", loadRuns);
  loadApprovals(); loadRuns();
  setInterval(loadApprovals, 15000);
}
