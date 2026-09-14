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
-- Note: account_id and funding_account_id both hold the funding source
-- account id (kept as two columns per the original schema design).
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

CREATE INDEX IF NOT EXISTS idx_accounts_member ON accounts(member_id);
CREATE INDEX IF NOT EXISTS idx_apps_member ON sub_account_apps(member_id);
CREATE INDEX IF NOT EXISTS idx_audit_member ON audit_log(member_id);
CREATE INDEX IF NOT EXISTS idx_fault_active ON fault_profiles(active);

-- ==== SEED DATA ====

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

-- Audit log seed (empty -- populated at runtime)
