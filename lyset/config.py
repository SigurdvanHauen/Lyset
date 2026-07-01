"""
Runtime configuration backed by the project ``.env`` file.

The web UI's Settings dialog reads the schema below to render its form, shows
the values currently in effect (``.env`` → process env → built-in default), and
writes edits back to ``.env``. ``.env`` is gitignored, so a user can configure a
public checkout with their private keys without ever committing them.

A single schema (``SCHEMA``) is the source of truth for both the form layout and
the env-file round-trip. Add a field here and it appears in the dialog
automatically.
"""

from __future__ import annotations

import os
from pathlib import Path

# Repo root .env — two levels up from this file (lyset/config.py → repo/).
ENV_PATH = Path(__file__).resolve().parent.parent / '.env'


# ── Settings schema ───────────────────────────────────────────────────────────
# Each field: key, label, type ('text'|'number'|'password'|'bool'), default,
# placeholder, help. type 'password' is masked in the UI (reveal toggle) but the
# value is still sent/stored in clear — this is a LAN dashboard with no auth.

SCHEMA: list[dict] = [
    {
        'group': 'Inverter (Modbus TCP)',
        'note': 'Applied immediately — the poller reconnects on save.',
        'fields': [
            {'key': 'INVERTER_HOST', 'label': 'IP address', 'type': 'text',
             'default': '192.168.1.185', 'placeholder': '192.168.1.185',
             'help': 'LAN address of the inverter SDongle.'},
            {'key': 'INVERTER_PORT', 'label': 'Port', 'type': 'number',
             'default': '502', 'help': 'Modbus TCP port (usually 502).'},
            {'key': 'INVERTER_SLAVE_ID', 'label': 'Slave / unit ID', 'type': 'number',
             'default': '1', 'help': 'Modbus unit ID (usually 1).'},
            {'key': 'INVERTER_POLL_INTERVAL', 'label': 'Poll interval (s)', 'type': 'number',
             'default': '10', 'help': 'Seconds between inverter reads.'},
        ],
    },
    {
        'group': 'Electricity prices (Strømligning)',
        'note': 'Applied immediately — the price worker restarts on save.',
        'fields': [
            {'key': 'STROMLIGNING_API_KEY', 'label': 'API key', 'type': 'password',
             'default': '', 'placeholder': 'required for prices',
             'help': 'Free key from stromligning.dk. Leave blank to disable price data.'},
            {'key': 'STROMLIGNING_POSTAL_CODE', 'label': 'Postal code', 'type': 'text',
             'default': '5500', 'help': 'Danish postal code — used to auto-detect your DSO/tariffs.'},
            {'key': 'STROMLIGNING_SUPPLIER_ID', 'label': 'Supplier ID', 'type': 'text',
             'default': '', 'placeholder': 'optional',
             'help': 'Override DSO auto-lookup. Only needed if the postal code maps to multiple DSOs.'},
            {'key': 'PRICE_EXPORT_FEE', 'label': 'Export fee (DKK/kWh)', 'type': 'number',
             'default': '0.033375',
             'help': 'Feed-in/balance tariffs deducted from spot to get the export price.'},
            {'key': 'PRICE_POLL_INTERVAL', 'label': 'Price poll (s)', 'type': 'number',
             'default': '1800', 'help': 'Seconds between price refreshes (default 30 min).'},
        ],
    },
    {
        'group': 'Solar forecast (Solcast)',
        'note': 'Applied immediately — the Solcast worker restarts on save.',
        'fields': [
            {'key': 'SOLCAST_API_KEY', 'label': 'API key', 'type': 'password',
             'default': '', 'placeholder': 'required for solar forecast',
             'help': 'Free Hobbyist key from toolkit.solcast.com.au. Leave blank to disable.'},
            {'key': 'SOLCAST_RESOURCE_ID', 'label': 'Resource ID(s)', 'type': 'text',
             'default': '', 'placeholder': 'auto-discover if blank',
             'help': 'Rooftop site UUID(s), comma-separated. Their forecasts are summed.'},
            {'key': 'SOLCAST_FETCH_HOURS', 'label': 'Fetch hours', 'type': 'text',
             'default': '6,12,18',
             'help': 'Local hours to fetch (free tier: 10 calls/day). E.g. 6,12,18.'},
            {'key': 'SOLCAST_POLL_INTERVAL', 'label': 'Poll interval (s)', 'type': 'number',
             'default': '21600', 'help': 'Seconds between refreshes (default 6 h).'},
        ],
    },
    {
        'group': 'Consumption model',
        'note': 'Applied on next server restart.',
        'fields': [
            {'key': 'CONSUMPTION_HISTORY_PATH', 'label': 'MeterData.xlsx path', 'type': 'text',
             'default': '', 'placeholder': 'optional one-time seed',
             'help': 'Path to a grid-operator export used to seed the model once (when no model file exists).'},
        ],
    },
    {
        'group': 'PV system (payback / ROI)',
        'note': 'Drives the payback estimate card on the Plan tab. Applied immediately.',
        'fields': [
            {'key': 'PV_SYSTEM_COST', 'label': 'System cost (DKK)', 'type': 'number',
             'default': '', 'placeholder': 'e.g. 120000',
             'help': 'Total installed cost of your PV + battery system, in DKK.'},
            {'key': 'PV_INSTALL_DATE', 'label': 'Installation date', 'type': 'text',
             'default': '', 'placeholder': 'YYYY-MM-DD',
             'help': 'Commissioning date. Break-even is measured from here using your '
             'average daily savings so far.'},
        ],
    },
    {
        'group': 'Auto controller',
        'note': 'Auto-start applies on next server restart; arbitrage applies immediately.',
        'fields': [
            {'key': 'AUTO_CONTROLLER_AUTOSTART', 'label': 'Auto-start controller', 'type': 'bool',
             'default': '1', 'help': 'Start the battery auto-controller when the server launches.'},
            {'key': 'ARBITRAGE_ENABLED', 'label': 'Allow discharge arbitrage', 'type': 'bool',
             'default': '1', 'help': 'Let the planner force-discharge the battery to the grid when '
             'the export price beats the cheapest upcoming import. Turn off to keep stored energy '
             'for self-consumption only. Takes effect immediately.'},
            {'key': 'ARBITRAGE_MIN_GAIN_PCT', 'label': 'Min. arbitrage gain (%)', 'type': 'number',
             'default': '5', 'help': 'Required predicted round-trip gain before the planner will '
             'discharge to the grid for arbitrage. Measured against the most expensive upcoming '
             'import the stored energy could otherwise offset, so the trade can never lose money to '
             'self-consumption. 0 = break-even allowed; higher = more conservative. Takes effect immediately.'},
        ],
    },
]

# Flat key → field def, for validation/round-trip.
_FIELDS: dict[str, dict] = {f['key']: f for g in SCHEMA for f in g['fields']}


# ── .env round-trip ───────────────────────────────────────────────────────────

def _parse_env_file() -> dict[str, str]:
    """Return {KEY: value} for uncommented ``KEY=value`` lines in ``.env``."""
    out: dict[str, str] = {}
    if not ENV_PATH.exists():
        return out
    for raw in ENV_PATH.read_text(encoding='utf-8').splitlines():
        line = raw.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, _, val = line.partition('=')
        key = key.strip()
        val = val.strip()
        if (len(val) >= 2) and val[0] == val[-1] and val[0] in ('"', "'"):
            val = val[1:-1]
        if key:
            out[key] = val
    return out


def read_settings() -> dict[str, str]:
    """Current value of every schema field: ``.env`` → process env → default."""
    env_file = _parse_env_file()
    values: dict[str, str] = {}
    for key, field in _FIELDS.items():
        if key in env_file:
            values[key] = env_file[key]
        else:
            values[key] = os.getenv(key, field.get('default', ''))
    return values


def schema_with_values() -> list[dict]:
    """SCHEMA with each field's current ``value`` filled in, for the API/UI."""
    values = read_settings()
    groups = []
    for g in SCHEMA:
        fields = [{**f, 'value': values.get(f['key'], f.get('default', ''))} for f in g['fields']]
        groups.append({'group': g['group'], 'note': g.get('note', ''), 'fields': fields})
    return groups


def _format_value(val: str) -> str:
    """Quote a value if it contains characters dotenv would mis-parse."""
    if val == '' or (val == val.strip() and '#' not in val and not val[:1].isspace()):
        return val
    return '"' + val.replace('"', '\\"') + '"'


def write_settings(updates: dict[str, str]) -> list[str]:
    """
    Merge ``updates`` into ``.env``, preserving comments, blank lines, ordering,
    and any keys we don't manage. Returns the list of keys actually written.

    Only keys present in the schema are accepted; unknown keys are ignored.
    """
    updates = {k: ('' if v is None else str(v)) for k, v in updates.items() if k in _FIELDS}
    if not updates:
        return []

    existing = ENV_PATH.read_text(encoding='utf-8').splitlines() if ENV_PATH.exists() else []
    remaining = dict(updates)
    out_lines: list[str] = []

    for raw in existing:
        stripped = raw.strip()
        if stripped and not stripped.startswith('#') and '=' in stripped:
            key = stripped.partition('=')[0].strip()
            if key in remaining:
                out_lines.append(f'{key}={_format_value(remaining.pop(key))}')
                continue
        out_lines.append(raw)

    # Append any keys that weren't already in the file.
    if remaining:
        if out_lines and out_lines[-1].strip() != '':
            out_lines.append('')
        out_lines.append('# Added by Settings dialog')
        for key, val in remaining.items():
            out_lines.append(f'{key}={_format_value(val)}')

    ENV_PATH.write_text('\n'.join(out_lines) + '\n', encoding='utf-8')
    return list(updates.keys())
