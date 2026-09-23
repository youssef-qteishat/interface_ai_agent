/*
 * probe.js — the fixed semantic probe.
 *
 * This file is SHIPPED IN THE IMAGE and applied to an element handle via
 * Runtime.callFunctionOn. The wire never carries JavaScript: /probe accepts two
 * integers and nothing else. Do not add an endpoint that evaluates caller-supplied
 * script, and do not build this function by string concatenation with request data.
 *
 * `this` is the element CDP hit-tested at the requested coordinate, already inside
 * the correct frame. Everything below runs in that frame's own document, so
 * match_count answers "how many things would this locator match on the page the
 * driver is actually looking at" — which is the question the canonicalizer needs.
 *
 * Returns a plain object (returnByValue: true), so keep it JSON-serializable.
 */
function probeElement() {
  const hit = this;
  const doc = hit.ownerDocument;

  // A coordinate often lands on a decorative child — the <img> inside an icon-only
  // button, a <span> inside a link. The pixel is on the image; the *control* is the
  // button, and that is what belongs in the artifact. Climb to the nearest
  // interactive ancestor and describe that, while recording what was literally hit
  // so the trace stays honest about it.
  const INTERACTIVE =
    'button, a[href], input, select, textarea, [role=button], [role=link], [onclick]';
  const isInteractive = (n) => n.matches && n.matches(INTERACTIVE);
  const el = isInteractive(hit) ? hit : hit.closest(INTERACTIVE) || hit;
  const retargetedFrom = el === hit ? null : hit.tagName.toLowerCase();

  // ---------- helpers ----------

  const clean = (s) => (s || "").replace(/\s+/g, " ").trim();

  // Server-generated ids look like inp_8a3f2b1c: a short prefix, underscore, hex.
  // They change on every request, so they are evidence, never a locator.
  const isGeneratedId = (id) => !id || /_[0-9a-f]{6,}$/i.test(id);

  const cssEscape = (s) =>
    window.CSS && CSS.escape ? CSS.escape(s) : String(s).replace(/["\\]/g, "\\$&");

  // null means "not counted" — never 0, which would read as "matches nothing".
  const countMatches = (selector) => {
    try {
      return doc.querySelectorAll(selector).length;
    } catch (e) {
      return null;
    }
  };

  // Visible text of a control, ignoring nested markup.
  const visibleText = (node) => clean(node.innerText || node.textContent || "");

  const isVisible = (node) => {
    const r = node.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) return false;
    const st = window.getComputedStyle(node);
    return st.visibility !== "hidden" && st.display !== "none" && st.opacity !== "0";
  };

  // ---------- table-relative label ----------
  //
  // The simulator's labels carry no `for=` and do not wrap their inputs; they sit in
  // a sibling <td> of the same <tr>. That is the whole reason this function exists:
  // on this app, accessible names for inputs are empty and the row label is the only
  // human-meaningful identifier.
  // A real field label is short. This app also uses tables for PAGE LAYOUT, so a
  // <td> can be a whole panel: without a length bound the "label" for a button came
  // back as the entire panel's text, which would then flow into the artifact as a
  // locator anchor. Anything longer than this is layout, not a label.
  const MAX_LABEL_LEN = 60;

  const looksLikeLabel = (text) =>
    !!text && text.length <= MAX_LABEL_LEN && !/\n/.test(text);

  // A cell holding its own controls is a field cell, not a label cell.
  const holdsControls = (node) =>
    !!node.querySelector("input, select, textarea, button, a[href], table, fieldset");

  const tableRelativeLabel = () => {
    const cell = el.closest("td, th");
    if (!cell) return null;
    const row = cell.parentElement;
    if (!row) return null;
    const cells = Array.from(row.children);
    const idx = cells.indexOf(cell);
    for (let i = idx - 1; i >= 0; i--) {
      if (holdsControls(cells[i])) continue;
      const text = clean(visibleText(cells[i])).replace(/:\s*$/, "");
      if (looksLikeLabel(text)) return text;
    }
    // Single-cell rows (the disclosure checkbox wraps its own text): the row's text
    // minus the control's own, and only when it is short enough to be a label.
    const rest = clean(visibleText(row).replace(visibleText(el), ""));
    return looksLikeLabel(rest) ? rest.replace(/:\s*$/, "") : null;
  };

  // Any <label> that legitimately names this control.
  const associatedLabel = () => {
    if (el.id && !isGeneratedId(el.id)) {
      const forLabel = doc.querySelector(`label[for="${cssEscape(el.id)}"]`);
      if (forLabel) return visibleText(forLabel);
    }
    const wrapping = el.closest("label");
    if (wrapping) return visibleText(wrapping);
    return null;
  };

  // ---------- enclosing region ----------
  //
  // The nearest ancestor with a STABLE id (or a form/fieldset), used both as the
  // human-readable region and to scope structural selectors.
  const enclosingRegion = () => {
    let node = el.parentElement;
    while (node && node !== doc.documentElement) {
      if (node.id && !isGeneratedId(node.id)) {
        return { selector: `${node.tagName.toLowerCase()}#${node.id}`, node };
      }
      if (node.tagName === "FORM" || node.tagName === "FIELDSET") {
        const legend = node.querySelector("legend");
        return {
          selector: node.tagName.toLowerCase(),
          node,
          label: legend ? visibleText(legend) : null,
        };
      }
      node = node.parentElement;
    }
    return { selector: null, node: null };
  };

  // ---------- structural selector ----------
  //
  // Scoped to the nearest stable ancestor so it does not encode the whole document
  // path. Still the weakest candidate: it breaks when markup is reordered.
  const structuralSelector = (regionNode) => {
    const parts = [];
    let node = el;
    while (node && node !== regionNode && node !== doc.body) {
      let part = node.tagName.toLowerCase();
      const classes = Array.from(node.classList).filter((c) => !/\d/.test(c));
      if (classes.length) part += "." + classes.join(".");
      const siblings = node.parentElement
        ? Array.from(node.parentElement.children).filter(
            (s) => s.tagName === node.tagName
          )
        : [];
      if (siblings.length > 1) {
        part += `:nth-of-type(${siblings.indexOf(node) + 1})`;
      }
      parts.unshift(part);
      node = node.parentElement;
    }
    const scope = regionNode && regionNode.id && !isGeneratedId(regionNode.id)
      ? `#${regionNode.id} `
      : "";
    return parts.length ? scope + parts.join(" > ") : null;
  };

  // ---------- gather ----------

  const tag = el.tagName.toLowerCase();
  const type = el.getAttribute("type");
  const nameAttr = el.getAttribute("name");
  const ownText = visibleText(el);
  const region = enclosingRegion();
  const rect = el.getBoundingClientRect();

  const candidates = [];

  // 1. Accessible-name-ish: exact visible text on an interactive control.
  //    (The authoritative role/name come from the AX tree on the Python side; this
  //    is the locator form of the same idea, with its match count.)
  if (ownText && ["button", "a", "summary"].includes(tag)) {
    const sameText = Array.from(doc.querySelectorAll(tag)).filter(
      (n) => visibleText(n) === ownText
    );
    candidates.push({
      kind: "text",
      tag,
      text: ownText,
      match_count: sameText.length,
    });
  }

  // 2. Table-relative contextual text — the workhorse on this app.
  const rowLabel = tableRelativeLabel();
  if (rowLabel) {
    // How many controls of this tag sit in a row whose label text matches?
    let count = 0;
    doc.querySelectorAll(tag).forEach((cand) => {
      const c = cand.closest("td, th");
      const r = c && c.parentElement;
      if (!r) return;
      const cells = Array.from(r.children);
      const ci = cells.indexOf(c);
      for (let i = ci - 1; i >= 0; i--) {
        const t = visibleText(cells[i]).replace(/:\s*$/, "");
        if (t) {
          if (t === rowLabel) count++;
          break;
        }
      }
    });
    candidates.push({
      kind: "contextual_text",
      anchor: rowLabel,
      relative: tag,
      match_count: count,
    });
  }

  // 3. Stable attribute selector. On this app `name=` survives restarts while ids
  //    do not, so it outranks anything structural.
  if (nameAttr) {
    const sel = `${tag}[name="${cssEscape(nameAttr)}"]`;
    candidates.push({
      kind: "attribute",
      selector: sel,
      attribute: "name",
      value: nameAttr,
      match_count: countMatches(sel),
    });
  }

  // 4. An associated <label>, when one genuinely exists (the disclosure checkbox).
  const label = associatedLabel();
  if (label) {
    candidates.push({
      kind: "label",
      text: label,
      control: tag + (type ? `[type=${type}]` : ""),
      match_count: Array.from(doc.querySelectorAll("label")).filter(
        (l) => visibleText(l) === label
      ).length,
    });
  }

  // 5. Structural CSS, scoped. Lowest confidence; always marked unverified.
  const structural = structuralSelector(region.node);
  if (structural) {
    candidates.push({
      kind: "css",
      selector: structural,
      match_count: countMatches(structural),
      stability: "unverified",
    });
  }

  // Role/name hints for the RETARGETED element. The AX tree on the Python side is
  // keyed to the node actually hit, so after a retarget its answer describes the
  // decorative child (role "none" for an <img>) rather than the control. These
  // hints let the reported role match the element being described.
  const roleHint = (() => {
    const explicit = el.getAttribute("role");
    if (explicit) return explicit;
    const map = { button: "button", a: "link", select: "combobox", textarea: "textbox" };
    if (map[tag]) return map[tag];
    if (tag === "input") {
      const t = (type || "text").toLowerCase();
      return { checkbox: "checkbox", radio: "radio", submit: "button", button: "button" }[t] || "textbox";
    }
    return null;
  })();

  const nameHint =
    clean(el.getAttribute("aria-label")) ||
    clean(el.getAttribute("title")) ||
    (["button", "a"].includes(tag) ? ownText : "") ||
    null;

  return {
    tag,
    // What the coordinate literally landed on, when that differs from the control
    // described above (e.g. the <img> inside an icon-only button).
    hit_tag: hit.tagName.toLowerCase(),
    retargeted_from: retargetedFrom,
    role_hint: roleHint,
    name_hint: nameHint || null,
    type,
    name_attr: nameAttr,
    // Recorded so the trace shows WHY an id was not used as a locator.
    dom_id: el.id || null,
    dom_id_stability: el.id ? (isGeneratedId(el.id) ? "generated" : "stable") : null,
    visible_text: ownText || null,
    nearby_label: rowLabel || label || null,
    placeholder: el.getAttribute("placeholder"),
    title: el.getAttribute("title") || null,
    value: el.value !== undefined && type !== "password" ? el.value : null,
    disabled: el.disabled === true,
    visible: isVisible(el),
    enclosing_region: region.selector,
    enclosing_region_label: region.label || null,
    classes: Array.from(el.classList),
    rect: {
      x: Math.round(rect.left),
      y: Math.round(rect.top),
      width: Math.round(rect.width),
      height: Math.round(rect.height),
    },
    candidates,
  };
}
