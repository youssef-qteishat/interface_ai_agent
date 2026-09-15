from dataclasses import dataclass

from apps.cred_union_sim import db


@dataclass
class FaultProfile:
    profile_id: str
    name: str
    overlay_delay_ms: int
    unexpected_dialog: bool
    session_warning: bool
    tenant_theme: str


def _row_to_profile(row) -> FaultProfile:
    return FaultProfile(
        profile_id=row["profile_id"],
        name=row["name"],
        overlay_delay_ms=row["overlay_delay_ms"],
        unexpected_dialog=bool(row["unexpected_dialog"]),
        session_warning=bool(row["session_warning"]),
        tenant_theme=row["tenant_theme"],
    )


def get_active_fault_profile() -> FaultProfile:
    conn = db.get_connection()
    row = conn.execute(
        "SELECT * FROM fault_profiles WHERE active = 1 LIMIT 1"
    ).fetchone()
    if row is None:
        raise RuntimeError("No active fault profile configured")
    return _row_to_profile(row)


_PROFILE_NAME_ALIASES = {"tenantb": "tenant_b"}


def set_fault_profile(name: str) -> FaultProfile:
    name = _PROFILE_NAME_ALIASES.get(name, name)
    conn = db.get_connection()
    row = conn.execute(
        "SELECT * FROM fault_profiles WHERE name = ?", (name,)
    ).fetchone()
    if row is None:
        raise ValueError(f"Unknown fault profile: {name}")

    conn.execute("UPDATE fault_profiles SET active = 0")
    conn.execute("UPDATE fault_profiles SET active = 1 WHERE name = ?", (name,))
    conn.commit()

    return get_active_fault_profile()
