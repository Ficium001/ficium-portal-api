#!/usr/bin/env python3
"""Expand the pipe-separated schema dumps into DATA_DICTIONARY.md."""
from collections import OrderedDict
from pathlib import Path

GEN = Path(__file__).parent


def load(fn):
    """Return OrderedDict[(schema, table)] -> list of column tuples."""
    out = OrderedDict()
    for raw in (GEN / fn).read_text().splitlines():
        if not raw.strip():
            continue
        p = raw.split("|")
        # schema|table|col|type|null|default|key/note[|comment]
        sch, tbl, col, typ = p[0], p[1], p[2], p[3]
        nul = p[4] if len(p) > 4 else "Y"
        dflt = p[5] if len(p) > 5 else ""
        key = p[6] if len(p) > 6 else ""
        note = p[7] if len(p) > 7 else ""
        # appdb.psv encodes FK as ">schema.table.col" appended to the key field
        if ">" in key:
            k, _, ref = key.partition(">")
            key = (k + " FK " + ref).strip()
        out.setdefault((sch, tbl), []).append((col, typ, nul, dflt, key, note))
    return out


ESC = str.maketrans({"|": "\\|"})


def render(tables, heading_level=3):
    lines = []
    cur_schema = None
    for (sch, tbl), cols in tables.items():
        if sch != cur_schema:
            cur_schema = sch
            lines.append(f"\n{'#' * (heading_level - 1)} Schema `{sch}`\n")
        lines.append(f"{'#' * heading_level} `{sch}.{tbl}`\n")
        lines.append("| Column | Type | Null | Default | Key / Notes |")
        lines.append("|---|---|---|---|---|")
        for col, typ, nul, dflt, key, note in cols:
            tail = " ".join(x for x in (key, note) if x).translate(ESC)
            d = (dflt or "").translate(ESC)
            lines.append(
                f"| `{col}` | {typ} | {'yes' if nul == 'Y' else 'no'} | "
                f"{'`' + d + '`' if d else ''} | {tail} |"
            )
        lines.append("")
    return "\n".join(lines)


def stats(*groups):
    t = sum(len(g) for g in groups)
    c = sum(len(v) for g in groups for v in g.values())
    return t, c


app = load("appdb.psv")
app2 = load("appdb2.psv")
p1, p2, p3 = load("portal1.psv"), load("portal2.psv"), load("portal3.psv")

app_t, app_c = stats(app, app2)
por_t, por_c = stats(p1, p2, p3)

header = Path(GEN / "dd_header.md").read_text()
mid = Path(GEN / "dd_mid.md").read_text()

body = (
    header.replace("{{APP_T}}", str(app_t))
    .replace("{{APP_C}}", str(app_c))
    .replace("{{POR_T}}", str(por_t))
    .replace("{{POR_C}}", str(por_c))
    + render(app)
    + render(app2)
    + mid
    + render(p1)
    + render(p2)
    + render(p3)
)

out = GEN / "DATA_DICTIONARY.md"
out.write_text(body)
print(f"wrote {out}  {len(body.splitlines())} lines")
print(f"App DB: {app_t} tables / {app_c} columns")
print(f"Portal DB: {por_t} tables / {por_c} columns")
