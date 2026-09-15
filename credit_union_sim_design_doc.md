# Credit Union Ops Simulator — Mock Browser Surface Design Document

## 1. Overview

This document specifies the mock browser application that serves as the discovery target for the Interface.ai Computer-Use take-home project. The simulator is a **FastAPI + Jinja2 + HTMX** application that mimics a legacy credit union teller servicing portal. It is deliberately designed with characteristics that make selector-based automation difficult (iframe-servicing panel, table-based layouts, generated DOM IDs, duplicate labels, icon-only controls) while remaining fully functional and deterministic.

The mock supports four screens in a single workflow: **Member Search → Member Detail → Open Sub-Account → Review**. The workflow terminates at the review checkpoint — the final "Open Account" button is classified as irreversible and must not be clicked without human approval.

---

## 2. Technology Stack

| Component     | Technology                             | Purpose                                                |
| ------------- | -------------------------------------- | ------------------------------------------------------ |
| Web framework | FastAPI (Python 3.12)                  | Routes, form handling, fault injection endpoints       |
| Templates     | Jinja2                                 | Server-rendered HTML with base layout and partials     |
| Interactivity | HTMX 1.9.x                             | Partial page swaps inside the iframe (no full reloads) |
| Database      | SQLite 3                               | Mock member data, accounts, run state                  |
| Styling       | Plain CSS (no framework)               | Deliberately utilitarian — looks like a legacy portal  |
| Container     | Docker (in the project's compose.yaml) | Runs alongside the main application stack              |

### Dependencies

```
fastapi>=0.115.0
jinja2>=3.1.0
python-multipart>=0.0.9
uvicorn>=0.30.0
```

HTMX is loaded from a local static file (no CDN) to keep the container network-isolated.

---

## 3. Application Layout

### 3.1 Outer Shell (`base.html`)

The outer page provides application chrome that stays static throughout the workflow. All servicing screens render inside a single `<iframe>`.

```
┌─────────────────────────────────────────────────────────────┐
│  [CU Logo]  Credit Union Servicing Portal     [User: Teller1] │
├──────────┬──────────────────────────────────────────────────┤
│          │                                                    │
│  Nav     │   ┌──────────────────────────────────────────┐    │
│  ─────   │   │  <iframe name="servicing-frame">        │    │
│  Members │   │                                          │    │
│  Accounts│   │    (all 4 servicing screens render here) │    │
│  Reports │   │                                          │    │
│  Admin   │   │                                          │    │
│          │   └──────────────────────────────────────────┘    │
│          │                                                    │
└──────────┴──────────────────────────────────────────────────┘
```

**Key HTML structure:**

```html
<!-- base.html -->
<!DOCTYPE html>
<html>
  <head>
    <title>Credit Union Servicing Portal</title>
    <link rel="stylesheet" href="/static/styles.css" />
    <script src="/static/htmx.min.js"></script>
  </head>
  <body>
    <div id="app-shell">
      <header id="top-bar">
        <div class="logo">Credit Union Servicing Portal</div>
        <div class="user-info">User: Teller1 | Branch: Main</div>
      </header>
      <div id="main-layout">
        <nav id="side-nav">
          <ul>
            <li>
              <a href="/servicing/members/search" target="servicing-frame"
                >Members</a
              >
            </li>
            <li>
              <a href="/servicing/accounts" target="servicing-frame"
                >Accounts</a
              >
            </li>
            <li>
              <a href="/servicing/reports" target="servicing-frame">Reports</a>
            </li>
            <li>
              <a href="/servicing/admin" target="servicing-frame">Admin</a>
            </li>
          </ul>
        </nav>
        <main id="content-area">
          <iframe
            name="servicing-frame"
            src="/servicing/members/search"
            class="servicing-iframe"
            title="Servicing Workspace"
          >
          </iframe>
        </main>
      </div>
    </div>
  </body>
</html>
```

**Design notes:**

- The `<iframe name="servicing-frame">` is the only target for all servicing navigation. Links use `target="servicing-frame"` so they load inside the frame, not as full-page reloads.
- The outer page URL never changes during the workflow. This mirrors how vendor-hosted core banking screens work — the host portal provides chrome, and the vendor's screens live in a frame.
- The `title` attribute on the iframe is present but generic. No `data-testid` anywhere.
- CSS is plain and utilitarian — system fonts, table borders, no animations beyond the loading overlay.

### 3.2 Servicing Frame Layout (`servicing_base.html`)

All four screens share a common base inside the iframe:

```html
<!-- servicing_base.html -->
<!DOCTYPE html>
<html>
  <head>
    <link rel="stylesheet" href="/static/servicing.css" />
    <script src="/static/htmx.min.js"></script>
  </head>
  <body class="servicing-body">
    <div class="servicing-container">{% block content %}{% endblock %}</div>
    {% block scripts %}{% endblock %}
  </body>
</html>
```

---

## 4. SQLite Database Schema

### 4.1 Schema Overview

```
┌─────────────┐     ┌──────────────────┐     ┌─────────────────────┐
│  members    │────<│  accounts        │────<│  sub_account_apps    │
│             │     │                  │     │                     │
│ member_id   │     │ account_id       │     │ app_id              │
│ first_name  │     │ member_id (FK)   │     │ account_id (FK)     │
│ last_name   │     │ account_type     │     │ requested_type      │
│ dob         │     │ account_number   │     │ opening_amount      │
│ phone       │     │ balance          │     │ funding_account_id  │
│ email       │     │ status           │     │ disclosure_accepted │
│ status      │     │ opened_date      │     │ created_at          │
│ branch_code │     │ created_at       │     │                     │
│ created_at  │     └──────────────────┘     └─────────────────────┘
└─────────────┘
                                           ┌─────────────────────┐
                                           │  fault_profiles     │
                                           │                     │
                                           │ profile_id          │
                                           │ name                │
                                           │ overlay_delay_ms    │
                                           │ unexpected_dialog   │
                                           │ session_warning     │
                                           │ tenant_theme        │
                                           └─────────────────────┘
```

### 4.2 `CREATE TABLE` Statements

```sql
-- Members table: synthetic credit union members
CREATE TABLE IF NOT EXISTS members (
    member_id      TEXT PRIMARY KEY,          -- 5-digit string, e.g. "12345"
    first_name     TEXT NOT NULL,
    last_name      TEXT NOT NULL,
    dob            TEXT NOT NULL,             -- ISO 8601 date, e.g. "1985-03-15"
    phone          TEXT NOT NULL,             -- E.164-ish, e.g. "9165550142"
    email          TEXT NOT NULL,
    ssn_last4      TEXT NOT NULL,             -- last 4 of SSN for verification, e.g. "1234"
    status         TEXT NOT NULL DEFAULT 'active',  -- active, inactive, frozen
    branch_code    TEXT NOT NULL DEFAULT 'MAIN',
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Accounts table: existing accounts for each member
CREATE TABLE IF NOT EXISTS accounts (
    account_id      TEXT PRIMARY KEY,          -- UUID, e.g. "acct_8a3f2b"
    member_id       TEXT NOT NULL,
    account_type    TEXT NOT NULL,             -- savings, checking, money_market
    account_number  TEXT NOT NULL,             -- masked display, e.g. "****-0042"
    balance         REAL NOT NULL DEFAULT 0.0,
    status          TEXT NOT NULL DEFAULT 'open',  -- open, closed, restricted
    opened_date     TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (member_id) REFERENCES members(member_id)
);

-- Sub-account applications: the workflow's data record
CREATE TABLE IF NOT EXISTS sub_account_apps (
    app_id              TEXT PRIMARY KEY,      -- UUID
    account_id           TEXT NOT NULL,        -- funding source account
    member_id           TEXT NOT NULL,
    requested_type      TEXT NOT NULL,         -- savings, checking
    opening_amount      REAL NOT NULL,
    funding_account_id  TEXT NOT NULL,
    disclosure_accepted INTEGER NOT NULL DEFAULT 0,  -- 0 or 1
    review_status       TEXT DEFAULT 'pending',     -- pending, reviewed, submitted
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (member_id) REFERENCES members(member_id),
    FOREIGN KEY (account_id) REFERENCES accounts(account_id)
);

-- Fault profiles: deterministic fault injection for testing
CREATE TABLE IF NOT EXISTS fault_profiles (
    profile_id          TEXT PRIMARY KEY,
    name                TEXT NOT NULL,         -- "default", "overlay", "unknown_dialog"
    overlay_delay_ms    INTEGER DEFAULT 0,    -- loading overlay duration
    unexpected_dialog   INTEGER DEFAULT 0,    -- 1 = inject unexpected modal
    session_warning     INTEGER DEFAULT 0,    -- 1 = inject session timeout warning
    tenant_theme        TEXT DEFAULT 'base',  -- base, tenant_b
    active              INTEGER DEFAULT 1
);

-- Audit log: records all servicing actions (for operator review)
CREATE TABLE IF NOT EXISTS audit_log (
    event_id    TEXT PRIMARY KEY,
    member_id   TEXT,
    action      TEXT NOT NULL,                -- search, view_detail, open_subaccount, review
    actor       TEXT DEFAULT 'teller',
    timestamp   TEXT NOT NULL DEFAULT (datetime('now')),
    details     TEXT                          -- JSON string
);
```

### 4.3 Indexes

```sql
CREATE INDEX idx_accounts_member ON accounts(member_id);
CREATE INDEX idx_apps_member ON sub_account_apps(member_id);
CREATE INDEX idx_audit_member ON audit_log(member_id);
CREATE INDEX idx_fault_active ON fault_profiles(active);
```

### 4.4 Mock Data Seed

```sql
-- Members (all synthetic)
INSERT INTO members (member_id, first_name, last_name, dob, phone, email, ssn_last4, status, branch_code) VALUES
    ('12345', 'John',   'Martinez', '1985-03-15', '9165550142', 'j.martinez@example.com', '1234', 'active', 'MAIN'),
    ('23456', 'Sarah',  'Chen',     '1990-07-22', '9165550283', 's.chen@example.com',     '5678', 'active', 'MAIN'),
    ('34567', 'Robert', 'O''Brien', '1978-11-03', '9165550456', 'r.obrien@example.com',   '9012', 'active', 'WEST'),
    ('45678', 'Maria',  'Santos',   '1992-01-30', '9165550678', 'm.santos@example.com',   '3456', 'frozen', 'MAIN'),
    ('99999', 'Test',   'NotFound', '2000-01-01', '0000000000', 'nobody@example.com',     '0000', 'inactive', 'NONE');

-- Accounts for member 12345
INSERT INTO accounts (account_id, member_id, account_type, account_number, balance, status, opened_date) VALUES
    ('acct_0042', '12345', 'savings',   '****-0042', 5420.50,  'open', '2019-06-12'),
    ('acct_0091', '12345', 'checking',  '****-0091', 1830.22,  'open', '2019-06-12');

-- Accounts for member 23456
INSERT INTO accounts (account_id, member_id, account_type, account_number, balance, status, opened_date) VALUES
    ('acct_0153', '23456', 'savings',    '****-0153', 12750.00, 'open', '2020-03-01'),
    ('acct_0204', '23456', 'checking',   '****-0204', 4100.75,  'open', '2020-03-01'),
    ('acct_0277', '23456', 'money_market','****-0277', 25000.00, 'open', '2021-09-15');

-- Accounts for member 34567
INSERT INTO accounts (account_id, member_id, account_type, account_number, balance, status, opened_date) VALUES
    ('acct_0388', '34567', 'checking',  '****-0388', 890.10,   'open', '2018-02-20');

-- Member 45678 (frozen) has one restricted account
INSERT INTO accounts (account_id, member_id, account_type, account_number, balance, status, opened_date) VALUES
    ('acct_0501', '45678', 'savings',   '****-0501', 0.00,     'restricted', '2017-11-05');

-- Member 99999 has no accounts (for MEMBER_NOT_FOUND testing)

-- Fault profiles
INSERT INTO fault_profiles (profile_id, name, overlay_delay_ms, unexpected_dialog, session_warning, tenant_theme, active) VALUES
    ('fp_default', 'default',       0,    0, 0, 'base',     1),
    ('fp_overlay', 'overlay',    1200,    0, 0, 'base',     0),
    ('fp_dialog',  'dialog',        0,    1, 0, 'base',     0),
    ('fp_session', 'session',       0,    0, 1, 'base',     0),
    ('fp_tenantb', 'tenant_b',      0,    0, 0, 'tenant_b', 0);

-- Audit log seed (empty — populated at runtime)
```

### 4.5 Data Design Rationale

| Choice                                         | Reason                                                                                    |
| ---------------------------------------------- | ----------------------------------------------------------------------------------------- |
| `member_id` is TEXT, 5 digits                  | Matches the capability contract's `^[0-9]{5}$` pattern                                    |
| `ssn_last4` stored but never displayed         | Demonstrates that sensitive data exists in the backend but the UI masks it                |
| `account_number` stored masked                 | Real legacy systems store masked display values; the full number is in a different system |
| Member 99999 exists with no accounts           | Tests the "member found but no accounts" edge case                                        |
| Member 45678 is frozen with restricted account | Tests business outcomes for restricted members                                            |
| `fault_profiles` table                         | Allows switching fault behavior via a dev-only endpoint without code changes              |
| `audit_log` table                              | Provides server-side evidence of what the teller (or automation) did                      |

---

## 5. Page-by-Page Design

### 5.1 Screen 1: Member Search

**Route:** `GET /servicing/members/search`
**Template:** `member_search.html`

#### Visual Layout

```
┌─────────────────────────────────────────────────────┐
│  Member Search                                       │
│  ═════════════════════                               │
│                                                      │
│  ┌───────────────────────────────────────────────┐   │
│  │  Member ID:  [_________]   [Search]          │   │
│  └───────────────────────────────────────────────┘   │
│                                                      │
│  ┌───────────────────────────────────────────────┐   │
│  │  (results panel — initially empty)            │   │
│  │                                               │   │
│  │  After search:                                │   │
│  │  ┌─────────┬──────────────┬──────────────┐    │   │
│  │  │ Member  │ Name         │ Status       │    │   │
│  │  ├─────────┼──────────────┼──────────────┤    │   │
│  │  │ 12345   │ Martinez, J. │ Active       │    │   │
│  │  └─────────┴──────────────┴──────────────┘    │   │
│  │  Click a row to view member details.         │   │
│  └───────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────┘
```

#### Jinja Template Structure

```html
<!-- member_search.html -->
{% extends "servicing_base.html" %} {% block content %}
<h2>Member Search</h2>

<form
  class="search-form"
  hx-post="/servicing/members/search"
  hx-target="#results-panel"
  hx-swap="innerHTML"
  hx-indicator="#loading-overlay"
>
  <table class="form-table">
    <tr>
      <td><label>Member ID:</label></td>
      <td>
        <input
          type="text"
          name="member_id"
          id="inp_{{ id_suffix }}"
          maxlength="5"
          pattern="[0-9]{5}"
          autocomplete="off"
        />
      </td>
      <td><button type="submit">Search</button></td>
    </tr>
  </table>
</form>

<div id="loading-overlay" class="overlay hidden">
  <div class="spinner">Searching...</div>
</div>

<div id="results-panel">
  <!-- Results partial loaded here via HTMX -->
  {% if error %}
  <div class="error-banner">{{ error }}</div>
  {% endif %}
</div>
{% endblock %}
```

#### HTMX Results Partial (`_search_results.html`)

```html
<!-- _search_results.html (returned by POST /servicing/members/search) -->
{% if members %}
<table class="data-table">
  <thead>
    <tr>
      <th>Member</th>
      <th>Name</th>
      <th>Status</th>
    </tr>
  </thead>
  <tbody>
    {% for m in members %}
    <tr
      class="member-row"
      hx-get="/servicing/members/{{ m.member_id }}"
      hx-target="body"
      hx-swap="outerHTML"
      onclick="window.location='/servicing/members/{{ m.member_id }}'"
    >
      <td>{{ m.member_id }}</td>
      <td>{{ m.last_name }}, {{ m.first_name[:1] }}.</td>
      <td>{{ m.status | capitalize }}</td>
    </tr>
    {% endfor %}
  </tbody>
</table>
<p class="hint">Click a row to view member details.</p>
{% else %}
<div class="info-banner">
  No members found. Check the Member ID and try again.
</div>
{% endif %}
```

#### FastAPI Route

```python
@app.get("/servicing/members/search")
async def member_search_page(request: Request):
    id_suffix = secrets.token_hex(4)  # Generated DOM ID suffix
    return templates.TemplateResponse("member_search.html", {
        "request": request,
        "id_suffix": id_suffix,
        "error": None
    })

@app.post("/servicing/members/search")
async def member_search_submit(request: Request, member_id: str = Form(...)):
    # Check fault profile for overlay delay
    fault = get_active_fault_profile()
    if fault.overlay_delay_ms > 0:
        await asyncio.sleep(fault.overlay_delay_ms / 1000)

    # Query database
    member = db.get_member(member_id)
    if member is None:
        # Return "not found" partial
        return templates.TemplateResponse("_search_results.html", {
            "request": request,
            "members": None
        })

    members = [member]
    return templates.TemplateResponse("_search_results.html", {
        "request": request,
        "members": members
    })
```

#### Difficulty Elements

- The input field's `id` is `inp_{{ id_suffix }}` where `id_suffix` is a random hex generated per request. No stable DOM ID.
- The search form is inside a `<table>` for layout — legacy form alignment.
- The member row is clickable via `onclick` (not a link), so there's no `href` to read.
- Results appear via HTMX partial swap — the URL stays `/servicing/members/search`.
- The "Member Search" `<h2>` heading is the stable OCR anchor.

#### "Member not found" Display

When searching for a non-existent member (e.g., `88888`):

```
┌─────────────────────────────────────────────────────┐
│  Member Search                                       │
│  ═════════════════════                               │
│                                                      │
│  [Member ID: 88888]  [Search]                       │
│                                                      │
│  ┌───────────────────────────────────────────────┐   │
│  │  No members found. Check the Member ID and    │   │
│  │  try again.                                   │   │
│  └───────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────┘
```

The visible text "No members found" is the `outcome_rules` trigger for `MEMBER_NOT_FOUND`.

---

### 5.2 Screen 2: Member Detail

**Route:** `GET /servicing/members/{member_id}`
**Template:** `member_detail.html`

#### Visual Layout

```
┌─────────────────────────────────────────────────────────────┐
│  Member Detail — 12345                                       │
│  ════════════════════════════                                │
│                                                              │
│  ┌─────────────────────┬───────────────────────────────────┐ │
│  │ Identity            │ Existing Accounts                 │ │
│  │ ─────────           │ ──────────────────                │ │
│  │ Name: Martinez, J.  │ ┌──────────┬──────────┬────────┐  │ │
│  │ DOB:  **/**/****    │ │ Acct No  │ Type     │ Balance│  │ │
│  │ Phone: (***) ***-**│ ├──────────┼──────────┼────────┤  │ │
│  │ Email: ****@***.com│ │ ****-0042│ Savings  │ $5,420 │  │ │
│  │ Status: Active      │ │ ****-0091│ Checking │ $1,830 │  │ │
│  │                     │ └──────────┴──────────┴────────┘  │ │
│  │ [Back]              │                                   │ │
│  │                     │ [Open Sub-Account]  [Back]        │ │
│  └─────────────────────┴───────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────┘
```

#### Jinja Template Structure

```html
<!-- member_detail.html -->
{% extends "servicing_base.html" %} {% block content %}
<h2>Member Detail — {{ member.member_id }}</h2>

<table class="layout-table">
  <tr>
    <!-- Left panel: Identity -->
    <td class="panel-left">
      <fieldset>
        <legend>Identity</legend>
        <table class="data-table">
          <tr>
            <td class="field-label">Name:</td>
            <td>{{ member.last_name }}, {{ member.first_name[:1] }}.</td>
          </tr>
          <tr>
            <td class="field-label">DOB:</td>
            <td>**/**/****</td>
          </tr>
          <tr>
            <td class="field-label">Phone:</td>
            <td>(***) ***-**{{ member.phone[-2:] }}</td>
          </tr>
          <tr>
            <td class="field-label">Email:</td>
            <td>****@***.com</td>
          </tr>
          <tr>
            <td class="field-label">Status:</td>
            <td>{{ member.status | capitalize }}</td>
          </tr>
        </table>
        <button class="nav-button" onclick="history.back()">Back</button>
      </fieldset>
    </td>

    <!-- Right panel: Existing Accounts -->
    <td class="panel-right">
      <fieldset>
        <legend>Existing Accounts</legend>
        <table class="data-table">
          <thead>
            <tr>
              <th>Acct No</th>
              <th>Type</th>
              <th>Balance</th>
            </tr>
          </thead>
          <tbody>
            {% for acct in accounts %}
            <tr>
              <td>{{ acct.account_number }}</td>
              <td>{{ acct.account_type | capitalize }}</td>
              <td>${{ "%.2f"|format(acct.balance) }}</td>
            </tr>
            {% endfor %} {% if accounts|length == 0 %}
            <tr>
              <td colspan="3">No accounts found.</td>
            </tr>
            {% endif %}
          </tbody>
        </table>

        {% if member.status == 'active' %}
        <a href="/servicing/accounts/open?member_id={{ member.member_id }}">
          <button class="action-button">Open Sub-Account</button>
        </a>
        {% else %}
        <div class="warning-banner">
          Member is {{ member.status }}. Sub-account opening is not available.
        </div>
        {% endif %}
        <button class="nav-button" onclick="history.back()">Back</button>
      </fieldset>
    </td>
  </tr>
</table>
{% endblock %}
```

#### Difficulty Elements

- **Two "Back" buttons** — one in the identity panel (top-left), one in the accounts panel (bottom-right). Both have identical visible text "Back". The canonicalizer must distinguish them by region/position.
- The entire layout uses a `<table>` with two `<td>` cells for the two-panel layout — the legacy table-layout approach.
- PII is masked server-side: DOB shows `**/**/****`, phone shows last 2 digits only, email is fully masked.
- The "Member Detail — 12345" `<h2>` is the stable OCR anchor for this screen.
- If the member is frozen/inactive, the "Open Sub-Account" button is replaced with a warning banner — this is a business outcome edge case.

---

### 5.3 Screen 3: Open Sub-Account

**Route:** `GET /servicing/accounts/open?member_id={member_id}`
**Template:** `open_subaccount.html`

#### Visual Layout

```
┌─────────────────────────────────────────────────────────────┐
│  Open Sub-Account                                            │
│  ══════════════════                                          │
│                                                              │
│  ┌─────────────────────────────────────────────────────────┐│
│  │                                                         ││
│  │  ┌─────────────────────────────────────────────────┐    ││
│  │  │ Account Type:   [Savings          ▼]           │    ││
│  │  │ Opening Amount: [$_______________]              │    ││
│  │  │ Funding Source: [****-0042         ▼]           │    ││
│  │  │                                                 │    ││
│  │  │ ☐ I have reviewed the account disclosure        │    ││
│  │  │    and agree to the terms.                      │    ││
│  │  │                                                 │    ││
│  │  │ [Continue]                      [Continue]     │    ││
│  │  │  ↑ form submit (bottom)         ↑ panel nav    │    ││
│  │  └─────────────────────────────────────────────────┘    ││
│  │                                                         ││
│  │  [🔄]  ← icon-only refresh button (no text label)       ││
│  └─────────────────────────────────────────────────────────┘│
└─────────────────────────────────────────────────────────────┘
```

#### Jinja Template Structure

```html
<!-- open_subaccount.html -->
{% extends "servicing_base.html" %} {% block content %}
<h2>Open Sub-Account</h2>

<div class="form-container">
  <form
    id="open-account-form"
    hx-post="/servicing/accounts/open/submit"
    hx-target="#review-panel"
    hx-swap="innerHTML"
    hx-indicator="#form-loading"
  >
    <input type="hidden" name="member_id" value="{{ member.member_id }}" />

    <table class="form-table">
      <tr>
        <td class="field-label"><label>Account Type:</label></td>
        <td>
          <select name="account_type" id="sel_{{ suffix }}">
            <option value="savings">Savings</option>
            <option value="checking">Checking</option>
          </select>
        </td>
      </tr>
      <tr>
        <td class="field-label"><label>Opening Amount:</label></td>
        <td>
          <input
            type="text"
            name="opening_amount"
            id="amt_{{ suffix }}"
            placeholder="$0.00"
            pattern="^\d+\.\d{2}$"
          />
        </td>
      </tr>
      <tr>
        <td class="field-label"><label>Funding Source:</label></td>
        <td>
          <select name="funding_account_id" id="fund_{{ suffix }}">
            {% for acct in accounts %}
            <option value="{{ acct.account_id }}">
              {{ acct.account_number }} ({{ acct.account_type | capitalize }})
            </option>
            {% endfor %}
          </select>
        </td>
      </tr>
      <tr>
        <td colspan="2">
          <label class="checkbox-label">
            <input
              type="checkbox"
              name="disclosure_accepted"
              id="chk_{{ suffix }}"
              value="1"
            />
            I have reviewed the account disclosure and agree to the terms.
          </label>
        </td>
      </tr>
      <tr>
        <td>
          <button type="submit" class="action-button">Continue</button>
        </td>
        <td class="right-align">
          <button
            type="button"
            class="nav-button"
            onclick="window.location='/servicing/members/{{ member.member_id }}'"
          >
            Continue
          </button>
        </td>
      </tr>
    </table>
  </form>

  <!-- Icon-only refresh button: no visible text, no aria-label -->
  <button
    class="icon-only-button"
    id="icon_{{ suffix }}"
    hx-get="/servicing/accounts/open?member_id={{ member.member_id }}"
    hx-target="body"
    hx-swap="outerHTML"
    title=""
  >
    <img src="/static/icons/refresh.png" alt="" />
  </button>

  <div id="form-loading" class="overlay hidden">
    <div class="spinner">Processing...</div>
  </div>

  <div id="review-panel">
    <!-- Review partial loaded here after form submission -->
  </div>
</div>
{% endblock %}
```

#### Difficulty Elements

- **Two "Continue" buttons**: The left one is `type="submit"` (submits the form to `/servicing/accounts/open/submit`). The right one is `type="button"` with `onclick` (navigates back to member detail). Both display "Continue" as visible text. The canonicalizer must resolve the correct one by region (form area vs. navigation area) or by button type (submit vs. button).
- **Icon-only refresh button**: An `<img>` inside a `<button>` with no text, no `aria-label`, and an empty `title`. The only way to target it is template matching — crop the icon from the discovery screenshot and save it as `targets/refresh-icon.png` in the capability artifact.
- **Generated DOM IDs**: All form elements have IDs with `{{ suffix }}` — a random hex generated per request. The `name` attributes are stable (needed for form POST), but IDs are not.
- **Form validation**: The opening amount field has a `pattern` attribute for client-side validation, but the server also validates. Invalid amounts return an inline error banner via HTMX, not a new page.
- The "Open Sub-Account" `<h2>` heading is the stable OCR anchor.

#### Form Submission Handler

```python
@app.post("/servicing/accounts/open/submit")
async def submit_subaccount_form(
    request: Request,
    member_id: str = Form(...),
    account_type: str = Form(...),
    opening_amount: str = Form(...),
    funding_account_id: str = Form(...),
    disclosure_accepted: str = Form(default="")
):
    # Validate disclosure
    if not disclosure_accepted:
        return templates.TemplateResponse("_form_error.html", {
            "request": request,
            "error": "You must accept the account disclosure to continue."
        })

    # Validate amount
    try:
        amount = float(opening_amount)
        if amount <= 0:
            raise ValueError
    except ValueError:
        return templates.TemplateResponse("_form_error.html", {
            "request": request,
            "error": "Opening amount must be a positive number (e.g., 25.00)."
        })

    # Check fault profile for overlay
    fault = get_active_fault_profile()
    if fault.overlay_delay_ms > 0:
        await asyncio.sleep(fault.overlay_delay_ms / 1000)

    # Create sub-account application record
    app = create_subaccount_app(
        member_id=member_id,
        account_type=account_type,
        opening_amount=amount,
        funding_account_id=funding_account_id
    )

    # Return review partial
    member = db.get_member(member_id)
    funding_acct = db.get_account(funding_account_id)
    return templates.TemplateResponse("_review_panel.html", {
        "request": request,
        "app": app,
        "member": member,
        "funding_acct": funding_acct,
        "account_type": account_type,
        "opening_amount": amount
    })
```

---

### 5.4 Screen 4: Review

**Route:** Loaded via HTMX partial swap (not a direct route)
**Template:** `_review_panel.html`

#### Visual Layout

```
┌─────────────────────────────────────────────────────────────┐
│  Review New Account                                          │
│  ════════════════                                            │
│                                                              │
│  ┌─────────────────────────────────────────────────────────┐│
│  │                                                         ││
│  │  ┌────────────────────┬──────────────────────────────┐  ││
│  │  │ Member:            │ 12345                        │  ││
│  │  │ Account Type:      │ Savings                      │  ││
│  │  │ Opening Amount:    │ $25.00                       │  ││
│  │  │ Funding Source:    │ ****-0042                     │  ││
│  │  └────────────────────┴──────────────────────────────┘  ││
│  │                                                         ││
│  │  ┌─────────────────────────────────────────────────┐    ││
│  │  │  ⚠  WARNING: This action will open a new         │    ││
│  │  │     account. Verify all details before           │    ││
│  │  │     proceeding. This cannot be undone.           │    ││
│  │  └─────────────────────────────────────────────────┘    ││
│  │                                                         ││
│  │  [Edit]                              [Open Account]    ││
│  │                                        ↑ IRREVERSIBLE   ││
│  └─────────────────────────────────────────────────────────┘│
└─────────────────────────────────────────────────────────────┘
```

#### Jinja Template Structure

```html
<!-- _review_panel.html (HTMX partial, not a full page) -->
<div id="review-container">
  <h3>Review New Account</h3>

  <table class="review-table">
    <tr>
      <td class="field-label">Member:</td>
      <td>{{ member.member_id }}</td>
    </tr>
    <tr>
      <td class="field-label">Account Type:</td>
      <td>{{ account_type | capitalize }}</td>
    </tr>
    <tr>
      <td class="field-label">Opening Amount:</td>
      <td>${{ "%.2f"|format(opening_amount) }}</td>
    </tr>
    <tr>
      <td class="field-label">Funding Source:</td>
      <td>{{ funding_acct.account_number }}</td>
    </tr>
  </table>

  <div class="warning-box">
    ⚠ WARNING: This action will open a new account. Verify all details before
    proceeding. This cannot be undone.
  </div>

  <div class="review-actions">
    <button
      class="nav-button"
      onclick="window.location='/servicing/accounts/open?member_id={{ member.member_id }}'"
    >
      Edit
    </button>

    <form
      hx-post="/servicing/accounts/open/finalize"
      hx-target="#review-container"
      hx-swap="innerHTML"
    >
      <input type="hidden" name="app_id" value="{{ app.app_id }}" />
      <button type="submit" class="danger-button">Open Account</button>
    </form>
  </div>
</div>
```

#### Difficulty Elements

- **"Review New Account" `<h3>` heading** — the stable OCR anchor for the checkpoint verifier.
- The review values appear in a labeled table — each row has a label `<td>` and value `<td>`. The replay engine's `labeled_region_ocr` output extraction reads these.
- **Warning dialog**: The warning box appears deterministically when the fault profile has `unexpected_dialog=1`. If injected, an additional modal overlay covers the screen with an "Unknown" dialog that the engine cannot dismiss — triggering escalation.
- **"Open Account" button** — classified as `irreversible` in the policy engine. The executor must block this click or escalate to human approval. The discovery loop stops before reaching this button.
- **"Edit" button** — reversible, returns to the Open Sub-Account form.

---

## 6. HTMX Interaction Flow Summary

```
                    ┌─────────────────┐
                    │  Member Search  │
                    │  (Screen 1)     │
                    └────────┬────────┘
                             │ POST /servicing/members/search
                             │ (HTMX partial swap → results table)
                             │ Click member row → navigation
                             ▼
                    ┌─────────────────┐
                    │  Member Detail  │
                    │  (Screen 2)     │
                    └────────┬────────┘
                             │ Click "Open Sub-Account" → navigation
                             ▼
                    ┌─────────────────┐
                    │  Open Sub-      │
                    │  Account (S3)   │
                    └────────┬────────┘
                             │ POST /servicing/accounts/open/submit
                             │ (HTMX partial swap → review panel)
                             ▼
                    ┌─────────────────┐
                    │  Review (S4)    │
                    │  ← CHECKPOINT   │
                    └────────┬────────┘
                             │ "Open Account" → BLOCKED by policy
                             │ (irreversible action)
                             ▼
                        [STOP]
```

Key: the URL inside the iframe changes with each navigation (e.g., `/servicing/members/12345`), but the **outer page URL never changes**. The discovery loop cannot rely on the outer page URL to detect screen transitions — it must read visible headings.

---

## 7. Fault Injection System

### 7.1 Dev-Only Endpoint

A development-only endpoint (not accessible to the Claude agent) switches the active fault profile:

```python
@app.post("/dev/fault-profile/{profile_id}")
async def set_fault_profile(profile_id: str):
    """Development-only endpoint. Not linked from the UI.
    Not accessible to the discovery agent (blocked by policy engine)."""
    db.deactivate_all_faults()
    db.activate_fault(profile_id)
    return {"status": "ok", "active_profile": profile_id}
```

### 7.2 Fault Profiles

| Profile   | Overlay Delay | Unexpected Dialog | Session Warning | Tenant   | Purpose                                                       |
| --------- | ------------- | ----------------- | --------------- | -------- | ------------------------------------------------------------- |
| `default` | 0ms           | No                | No              | base     | Happy path discovery and replay                               |
| `overlay` | 1200ms        | No                | No              | base     | Recoverable runtime condition — bounded wait/retry            |
| `dialog`  | 0ms           | Yes               | No              | base     | Unknown dialog — escalation to human                          |
| `session` | 0ms           | No                | Yes             | base     | Session timeout warning — reauthentication escalation         |
| `tenantb` | 0ms           | No                | No              | tenant_b | Cross-tenant replay — changed colors, logo, one renamed label |

### 7.3 Overlay Implementation

When `overlay_delay_ms > 0`, the HTMX response includes a loading overlay div that covers the iframe content for the specified duration:

```html
<!-- Injected at the start of an HTMX partial response -->
<div
  class="loading-overlay"
  id="overlay_{{ suffix }}"
  style="display: block; animation: fadeOut 0ms {{ delay }}ms forwards;"
>
  <div class="spinner">Processing request...</div>
</div>
<!-- Actual content follows after delay -->
```

The overlay is a CSS-positioned div with a semi-transparent background and a spinner. It disappears after the delay. During replay, the engine's bounded-wait logic detects it and waits.

### 7.4 Unexpected Dialog Implementation

When `unexpected_dialog=1`, a modal overlay is injected after the review panel loads:

```html
<!-- Unexpected modal dialog -->
<div class="modal-overlay" id="modal_unknown_{{ suffix }}">
  <div class="modal-box">
    <h4>⚠ System Notice</h4>
    <p>
      An unexpected condition was detected. Please contact your system
      administrator.
    </p>
    <p>Reference: ERR-{{ random_code }}</p>
    <button class="modal-button" id="modal_dismiss_{{ suffix }}">OK</button>
  </div>
</div>
```

This dialog is not in the capability artifact's known dialogs. The replay engine cannot resolve it, triggering escalation to `HUMAN`.

### 7.5 Tenant B Implementation

When `tenant_theme='tenant_b'`, the servicing templates load a different CSS file (`servicing_tenant_b.css`) and one label is changed:

| Base (Tenant A)                 | Tenant B              |
| ------------------------------- | --------------------- |
| "Continue" (form submit button) | "Proceed"             |
| Blue header                     | Green header          |
| "Credit Union Servicing Portal" | "Community CU Portal" |
| Logo: generic shield            | Logo: tree icon       |

The capability artifact handles this via `tenant_override`:

```yaml
variants:
  - id: tenant_b
    label_overrides:
      "Continue": "Proceed"
    css_theme: "servicing_tenant_b.css"
```

---

## 8. CSS Design (Utilitarian Legacy Look)

### 8.1 Key Style Decisions

- System fonts: `font-family: Arial, Helvetica, sans-serif`
- Table borders: 1px solid `#999`
- Background: `#f0f0f0` (light gray)
- Header bar: `#336699` (dark blue, classic portal blue)
- Form labels: right-aligned, bold
- Buttons: gray background, 1px border, no rounded corners
- Warning box: yellow background (`#fff3cd`), dark border
- Danger button: red background (`#dc3545`), white text
- Loading overlay: semi-transparent white with centered spinner text
- No transitions, no animations (except the overlay fade)

### 8.2 CSS File Structure

```css
/* servicing.css — shared by all servicing screens */

body.servicing-body {
  font-family: Arial, Helvetica, sans-serif;
  font-size: 13px;
  background: #f0f0f0;
  margin: 0;
  padding: 8px;
}

h2 {
  font-size: 16px;
  margin: 0 0 10px 0;
}
h3 {
  font-size: 14px;
  margin: 0 0 8px 0;
}

table.data-table,
table.form-table,
table.layout-table {
  border-collapse: collapse;
  width: 100%;
}

table.data-table th,
table.data-table td,
table.form-table td,
table.layout-table td {
  border: 1px solid #999;
  padding: 4px 8px;
}

table.data-table th {
  background: #336699;
  color: white;
  text-align: left;
}

.field-label {
  font-weight: bold;
  text-align: right;
  white-space: nowrap;
}

.action-button,
.nav-button,
.danger-button {
  padding: 4px 16px;
  border: 1px solid #666;
  cursor: pointer;
  font-size: 13px;
}

.action-button {
  background: #e0e0e0;
}
.nav-button {
  background: #d0d0d0;
}
.danger-button {
  background: #dc3545;
  color: white;
  font-weight: bold;
}

.icon-only-button {
  border: 1px solid #999;
  background: #e8e8e8;
  padding: 2px;
  cursor: pointer;
}

.overlay {
  position: fixed;
  top: 0;
  left: 0;
  width: 100%;
  height: 100%;
  background: rgba(255, 255, 255, 0.8);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 9999;
}

.overlay.hidden {
  display: none;
}

.warning-box {
  background: #fff3cd;
  border: 1px solid #ffc107;
  padding: 8px 12px;
  margin: 10px 0;
}

.warning-banner {
  background: #fff3cd;
  border: 1px solid #ffc107;
  padding: 6px 10px;
  color: #856404;
}

.error-banner {
  background: #f8d7da;
  border: 1px solid #dc3545;
  padding: 6px 10px;
  color: #721c24;
}

.info-banner {
  background: #d1ecf1;
  border: 1px solid #17a2b8;
  padding: 6px 10px;
}

.modal-overlay {
  position: fixed;
  top: 0;
  left: 0;
  width: 100%;
  height: 100%;
  background: rgba(0, 0, 0, 0.5);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 10000;
}

.modal-box {
  background: white;
  border: 2px solid #666;
  padding: 16px 24px;
  min-width: 300px;
  text-align: center;
}
```

---

## 9. File Structure

```
apps/bank_sim/
├── __init__.py
├── server.py                 # FastAPI app, routes, startup
├── db.py                     # SQLite connection, queries, seed
├── models.py                 # Pydantic models for internal use
├── fault.py                  # Fault profile management
├── templates/
│   ├── base.html             # Outer shell with iframe
│   ├── servicing_base.html   # Base for iframe content
│   ├── member_search.html    # Screen 1
│   ├── _search_results.html  # Screen 1 HTMX partial
│   ├── member_detail.html    # Screen 2
│   ├── open_subaccount.html  # Screen 3
│   ├── _form_error.html      # Screen 3 error partial
│   ├── _review_panel.html    # Screen 4 (HTMX partial)
│   └── _account_finalized.html  # Post-submit confirmation (blocked)
├── static/
│   ├── styles.css            # Outer shell styles
│   ├── servicing.css         # Servicing frame styles
│   ├── servicing_tenant_b.css # Tenant B override styles
│   ├── htmx.min.js           # HTMX library (local copy)
│   └── icons/
│       └── refresh.png       # Icon for icon-only button
├── seed.sql                  # Database seed data
└── Dockerfile                # Container for the bank sim
```

---

## 10. Implementation Steps

Each step is scoped so it can be delegated to Claude Code or done manually. Steps are ordered by dependency — each step builds on the previous.

### Step 1: Project Scaffold and Dependencies

**Owner:** Claude Code or manual
**Estimated time:** 30 minutes

- [ ] Create `apps/bank_sim/` directory structure (see Section 9)
- [ ] Create `apps/bank_sim/__init__.py` (empty)
- [ ] Create `apps/bank_sim/server.py` with a minimal FastAPI app:

  ```python
  from fastapi import FastAPI
  app = FastAPI(title="Credit Union Ops Simulator")

  @app.get("/")
  async def index():
      return {"status": "ok"}
  ```

- [ ] Add dependencies to `pyproject.toml`:
  - `fastapi>=0.115.0`
  - `jinja2>=3.1.0`
  - `python-multipart>=0.0.9`
  - `uvicorn>=0.30.0`
- [ ] Download `htmx.min.js` (v1.9.x) into `apps/bank_sim/static/`
- [ ] Create a placeholder `refresh.png` icon (16x16, simple circular arrow)
- [ ] Verify the app starts with `uvicorn apps.bank_sim.server:app --port 8001`

**Verification:** `curl http://localhost:8001/` returns `{"status": "ok"}`

---

### Step 2: SQLite Database Layer

**Owner:** Claude Code or manual
**Estimated time:** 45 minutes

- [ ] Create `apps/bank_sim/db.py` with:
  - SQLite connection helper (using `sqlite3` from stdlib, no ORM)
  - `init_db()` function that runs all `CREATE TABLE` statements from Section 4.2
  - `seed_db()` function that runs all `INSERT` statements from Section 4.4
  - Query functions:
    - `get_member(member_id) -> dict | None`
    - `get_accounts_by_member(member_id) -> list[dict]`
    - `get_account(account_id) -> dict | None`
    - `create_subaccount_app(member_id, account_type, opening_amount, funding_account_id) -> dict`
    - `get_subaccount_app(app_id) -> dict | None`
    - `log_audit(member_id, action, details)`
- [ ] Create `apps/bank_sim/seed.sql` with all CREATE TABLE + INSERT statements
- [ ] Call `init_db()` and `seed_db()` on application startup
- [ ] Use an in-memory SQLite database (`:memory:`) for dev, file-based for persistence

**Verification:** Run `python -c "from apps.bank_sim.db import init_db, seed_db; init_db(); seed_db()"` without errors. Query `SELECT * FROM members` returns 5 rows.

---

### Step 3: Fault Profile System

**Owner:** Claude Code or manual
**Estimated time:** 30 minutes

- [ ] Create `apps/bank_sim/fault.py` with:
  - `get_active_fault_profile() -> FaultProfile` — queries the `fault_profiles` table for the active profile
  - `set_fault_profile(profile_id)` — dev-only function to switch active profile
  - `FaultProfile` dataclass with fields: `overlay_delay_ms`, `unexpected_dialog`, `session_warning`, `tenant_theme`
- [ ] Add the dev-only endpoint `POST /dev/fault-profile/{profile_id}` to `server.py`
- [ ] Add a startup event that ensures the `default` profile is active

**Verification:** `curl -X POST http://localhost:8001/dev/fault-profile/overlay` then `curl http://localhost:8001/dev/fault-profile` shows the overlay profile is active.

---

### Step 4: CSS Files

**Owner:** Claude Code or manual
**Estimated time:** 30 minutes

- [ ] Create `apps/bank_sim/static/styles.css` — outer shell styles (header, nav, iframe container)
- [ ] Create `apps/bank_sim/static/servicing.css` — all servicing frame styles (Section 8.2)
- [ ] Create `apps/bank_sim/static/servicing_tenant_b.css` — Tenant B overrides:
  - Header background: `#2d7a3e` (green) instead of `#336699` (blue)
  - Logo text: "Community CU Portal" instead of "Credit Union Servicing Portal"
  - One label override handled in Jinja template (not CSS)

**Verification:** Load any page and confirm it renders with the utilitarian legacy look — gray background, blue header, table borders, no rounded corners.

---

### Step 5: Outer Shell Template

**Owner:** Claude Code or manual
**Estimated time:** 20 minutes

- [ ] Create `apps/bank_sim/templates/base.html` (Section 3.1)
  - Header bar with logo text and user info
  - Left nav with 4 links (Members, Accounts, Reports, Admin)
  - All nav links use `target="servicing-frame"`
  - `<iframe name="servicing-frame" src="/servicing/members/search">`
- [ ] Create `apps/bank_sim/templates/servicing_base.html` (Section 3.2)
  - Minimal HTML skeleton with `{% block content %}`

**Verification:** `curl http://localhost:8001/` returns the outer shell HTML with the iframe pointing to `/servicing/members/search`.

---

### Step 6: Screen 1 — Member Search

**Owner:** Claude Code or manual
**Estimated time:** 60 minutes

- [ ] Create `apps/bank_sim/templates/member_search.html` (Section 5.1)
  - `<h2>Member Search</h2>` heading (OCR anchor)
  - Search form with generated DOM ID suffix
  - Table-based form layout
  - Results panel div (`#results-panel`)
  - Loading overlay div (`#loading-overlay`)
- [ ] Create `apps/bank_sim/templates/_search_results.html` (Section 5.1)
  - Results table with member rows
  - "No members found" message for not-found case
  - Rows are clickable via `onclick` (not `<a>` tags)
- [ ] Add routes to `server.py`:
  - `GET /servicing/members/search` — renders `member_search.html`
  - `POST /servicing/members/search` — queries DB, returns `_search_results.html` partial
- [ ] Test with member ID `12345` → should show one result row
- [ ] Test with member ID `88888` → should show "No members found"
- [ ] Test with member ID `99999` → should show the member (exists, but has no accounts)

**Verification:** Open browser to `http://localhost:8001/`, search for `12345`, see Martinez result. Search for `88888`, see "No members found."

---

### Step 7: Screen 2 — Member Detail

**Owner:** Claude Code or manual
**Estimated time:** 60 minutes

- [ ] Create `apps/bank_sim/templates/member_detail.html` (Section 5.2)
  - `<h2>Member Detail — {{ member.member_id }}</h2>` heading (OCR anchor)
  - Two-panel layout using `<table>` with two `<td>` cells
  - Left panel: Identity fieldset with masked PII
  - Right panel: Existing Accounts fieldset with accounts table
  - Two "Back" buttons (one in each panel) — duplicate labels
  - "Open Sub-Account" button (or warning banner for frozen/inactive members)
- [ ] Add route to `server.py`:
  - `GET /servicing/members/{member_id}` — renders `member_detail.html`
- [ ] Test with member `12345` → shows 2 accounts, active status, "Open Sub-Account" button
- [ ] Test with member `45678` → shows restricted account, frozen status, warning banner instead of button
- [ ] Test with member `99999` → shows "No accounts found" in the accounts table

**Verification:** Click a member row from Screen 1, land on Screen 2 with correct masked data and two "Back" buttons visible.

---

### Step 8: Screen 3 — Open Sub-Account

**Owner:** Claude Code or manual
**Estimated time:** 90 minutes

- [ ] Create `apps/bank_sim/templates/open_subaccount.html` (Section 5.3)
  - `<h2>Open Sub-Account</h2>` heading (OCR anchor)
  - Form with `<table>` layout
  - Account Type `<select>` (Savings, Checking)
  - Opening Amount `<input>` with pattern validation
  - Funding Source `<select>` populated from member's accounts
  - Disclosure checkbox with visible label text
  - **Two "Continue" buttons**: left is `type="submit"`, right is `type="button"` with `onclick`
  - Icon-only refresh button with `<img>` and no text/aria-label
- [ ] Create `apps/bank_sim/templates/_form_error.html` — inline error banner partial
- [ ] Add routes to `server.py`:
  - `GET /servicing/accounts/open?member_id={id}` — renders `open_subaccount.html`
  - `POST /servicing/accounts/open/submit` — validates form, creates sub_account_app record, returns `_review_panel.html`
- [ ] Test form validation:
  - Empty amount → error banner
  - Non-numeric amount → error banner
  - Negative amount → error banner
  - Disclosure not checked → error banner
- [ ] Test successful submission → review panel appears via HTMX swap

**Verification:** Fill in Savings, $25.00, select funding account, check disclosure, click the correct "Continue" button, see the review panel appear.

---

### Step 9: Screen 4 — Review Panel

**Owner:** Claude Code or manual
**Estimated time:** 45 minutes

- [ ] Create `apps/bank_sim/templates/_review_panel.html` (Section 5.4)
  - `<h3>Review New Account</h3>` heading (checkpoint anchor)
  - Review details table (Member, Account Type, Opening Amount, Funding Source)
  - Warning box with irreversible-action notice
  - "Edit" button (reversible — returns to form)
  - "Open Account" button (irreversible — form posts to `/servicing/accounts/open/finalize`)
- [ ] Create `apps/bank_sim/templates/_account_finalized.html` — confirmation partial (shown only if the policy engine allows the finalize action, which it should not during discovery)
- [ ] Add route to `server.py`:
  - `POST /servicing/accounts/open/finalize` — creates the account (blocked by policy in the real system; in the mock, this just records the action and returns a confirmation)
- [ ] Test that the review panel shows correct values from the form submission
- [ ] Test that "Edit" returns to the form
- [ ] Test that "Open Account" posts to finalize and shows confirmation

**Verification:** Submit the form on Screen 3, see review panel with correct values. Click "Edit," return to form. Click "Open Account," see confirmation.

---

### Step 10: Fault Injection — Loading Overlay

**Owner:** Claude Code or manual
**Estimated time:** 30 minutes

- [ ] Modify the `POST /servicing/members/search` and `POST /servicing/accounts/open/submit` handlers to check `fault.overlay_delay_ms`
- [ ] When `overlay_delay_ms > 0`:
  - Add `await asyncio.sleep(delay / 1000)` before processing
  - Inject the loading overlay div at the start of the HTMX partial response
- [ ] Test with `overlay` profile active: search should show spinner for 1.2s, then results
- [ ] Test with `default` profile: no delay, immediate results

**Verification:** Switch to overlay profile, search for member, observe 1.2s delay with spinner visible, then results appear.

---

### Step 11: Fault Injection — Unexpected Dialog

**Owner:** Claude Code or manual
**Estimated time:** 30 minutes

- [ ] Modify `_review_panel.html` template to conditionally render the modal overlay when `fault.unexpected_dialog == 1`
- [ ] The modal has a "System Notice" title, a generic error message, and an "OK" dismiss button
- [ ] The dismiss button is not in any known capability artifact — it's an unknown dialog
- [ ] Test with `dialog` profile active: submit form, see review panel with modal overlay on top
- [ ] Test with `default` profile: no modal

**Verification:** Switch to dialog profile, submit form, see modal overlay covering the review panel. Switch to default, submit form, see clean review panel.

---

### Step 12: Fault Injection — Session Warning

**Owner:** Claude Code or manual
**Estimated time:** 20 minutes

- [ ] Modify `_review_panel.html` to conditionally render a "Session Expiring" warning banner when `fault.session_warning == 1` (Section 7.4)
- [ ] The warning says "Your session will expire in 5 minutes. Please save your work."
- [ ] This is a different visual from the unexpected dialog — it's a banner, not a modal
- [ ] Test with `session` profile active

**Verification:** Switch to session profile, submit form, see session warning banner above the review panel.

---

### Step 13: Tenant B Variant

**Owner:** Claude Code or manual
**Estimated time:** 45 minutes

- [ ] Create `apps/bank_sim/static/servicing_tenant_b.css` (Section 8.1):
  - Green header (`#2d7a3e`) instead of blue (`#336699`)
  - Logo text changes handled in Jinja
- [ ] Modify `servicing_base.html` to conditionally load the Tenant B CSS (Section 7.5):
  ```html
  {% if tenant_theme == 'tenant_b' %}
  <link rel="stylesheet" href="/static/servicing_tenant_b.css" />
  {% else %}
  <link rel="stylesheet" href="/static/servicing.css" />
  {% endif %}
  ```
- [ ] Modify `base.html` to conditionally show "Community CU Portal" instead of "Credit Union Servicing Portal"
- [ ] Modify `open_subaccount.html` to conditionally show "Proceed" instead of "Continue" for the submit button when tenant is B
- [ ] Pass `tenant_theme` from the fault profile to all templates
- [ ] Test with `tenantb` profile active: different colors, different logo text, one renamed button

**Verification:** Switch to tenantb profile, navigate through the workflow, observe green header, "Community CU Portal" logo, and "Proceed" button on Screen 3.

---

### Step 14: Audit Logging

**Owner:** Claude Code or manual
**Estimated time:** 20 minutes

- [ ] Add `log_audit()` calls to each route handler:
  - `member_search_submit` → `action="search", details={"member_id": member_id}`
  - `member_detail` → `action="view_detail", details={"member_id": member_id}`
  - `submit_subaccount_form` → `action="open_subaccount", details={...}`
  - `finalize` → `action="review", details={"app_id": app_id}`
- [ ] Add a dev-only endpoint `GET /dev/audit-log` to view recent audit entries
- [ ] Ensure no PII is logged (member IDs are synthetic, but log format should still redact in the real system)

**Verification:** Navigate through the workflow, then `curl http://localhost:8001/dev/audit-log` shows the action history.

---

### Step 15: Integration Testing

**Owner:** Manual (with Claude Code assistance)
**Estimated time:** 45 minutes

- [ ] Test the full workflow manually through a browser:
  1. Load `http://localhost:8001/`
  2. Search for member `12345`
  3. Click the member row → land on Member Detail
  4. Click "Open Sub-Account"
  5. Fill in Savings, $25.00, select funding account, check disclosure
  6. Click the correct "Continue" button (the submit one, not the navigation one)
  7. See Review panel with correct values
  8. Click "Edit" → return to form
  9. Click "Open Account" → see confirmation
- [ ] Test error paths:
  - Search for `88888` → "No members found"
  - Search for `45678` → frozen member, no "Open Sub-Account" button
  - Submit form without checking disclosure → error banner
  - Submit form with invalid amount → error banner
- [ ] Test fault profiles:
  - Switch to `overlay`, search → observe delay
  - Switch to `dialog`, submit form → observe modal
  - Switch to `tenantb`, navigate → observe different theme
- [ ] Verify that the iframe URL changes between screens but the outer page URL does not
- [ ] Verify that DOM IDs change on page refresh (generated suffixes)

**Verification:** All scenarios work as described. The app is ready to serve as the discovery target for the Computer Use loop.

---

## 11. Acceptance Criteria

The mock is complete when all of the following are true:

| Criterion                                                     | How to verify                                                            |
| ------------------------------------------------------------- | ------------------------------------------------------------------------ |
| Four screens render correctly inside the iframe               | Manual browser walkthrough                                               |
| Member search returns correct results for `12345`             | `POST /servicing/members/search` with `member_id=12345`                  |
| Member search returns "No members found" for `88888`          | Same endpoint with `member_id=88888`                                     |
| Member detail shows masked PII                                | Visual inspection — DOB, phone, email all masked                         |
| Member detail has two "Back" buttons                          | Visual inspection or DOM query                                           |
| Open Sub-Account form has two "Continue" buttons              | Visual inspection or DOM query                                           |
| Open Sub-Account form has an icon-only refresh button         | `button` with `img` child, no text                                       |
| Form validation rejects empty/invalid amount                  | Submit with empty amount → error banner                                  |
| Form validation rejects unchecked disclosure                  | Submit without checking → error banner                                   |
| Review panel shows correct parameterized values               | Submit with known inputs, verify review shows same values                |
| DOM IDs are generated (unstable across requests)              | Refresh page, observe different ID suffixes                              |
| No `data-testid`, `data-qa`, or `data-cy` attributes anywhere | `grep -r 'data-testid\|data-qa\|data-cy' apps/bank_sim/` returns nothing |
| Loading overlay works with `overlay` fault profile            | Switch profile, search, observe 1.2s delay                               |
| Unexpected dialog works with `dialog` fault profile           | Switch profile, submit form, observe modal                               |
| Tenant B theme works with `tenantb` fault profile             | Switch profile, navigate, observe green header + "Proceed"               |
| Outer page URL never changes during workflow                  | Check browser address bar after navigating between screens               |
| Audit log records all servicing actions                       | `GET /dev/audit-log` shows entries after walkthrough                     |

---

## 12. Notes for Claude Code Implementation

If delegating implementation to Claude Code (the Anthropic coding agent), provide this context:

1. **Working directory:** `apps/bank_sim/` inside the project root
2. **Database:** Use Python's `sqlite3` stdlib. No ORM. Raw SQL queries with parameterized statements.
3. **Templates:** Jinja2 with `Jinja2Templates(directory="apps/bank_sim/templates")`
4. **Static files:** Mount with `app.mount("/static", StaticFiles(directory="apps/bank_sim/static"))`
5. **HTMX:** Use local static copy, not CDN. The container is network-isolated.
6. **Generated IDs:** Use `secrets.token_hex(4)` to generate suffixes per request. Pass as `suffix` to template context.
7. **Fault profiles:** Read from SQLite on each request. Do not cache in memory (so profile switches take effect immediately).
8. **No test IDs:** Do not add `data-testid`, `data-qa`, `data-cy`, `id` (stable), `role` (non-native), or `aria-label` attributes. Use `name` attributes on form inputs (required for POST). Use visible `<label>` text (required for humans).
9. **Table-based layout:** Use `<table>` elements for form alignment and panel layout. Do not use CSS grid or flexbox.
10. **Synthetic data only:** All member names, SSNs, phone numbers, and account numbers are fake. Do not use real-looking PII patterns.

---

## 13. Relationship to the Main System

The bank simulator is a **standalone FastAPI app** that runs in the same Docker network as the main application. The main system's discovery loop launches Chromium, navigates to `http://bank-sim:8001/`, and interacts with the simulator through the Computer Use tool's screenshot-and-coordinate interface.

The simulator has **no knowledge** of the discovery loop, the canonicalizer, or the replay engine. It is a plain web application that responds to HTTP requests and renders HTML. This separation is intentional — the simulator is the "application under test" and should not be coupled to the automation system.

The only integration point is the **fault profile endpoint** (`/dev/fault-profile/{id}`), which the main system calls to switch fault scenarios for testing. This endpoint is not linked from the UI and is blocked by the policy engine during discovery.
