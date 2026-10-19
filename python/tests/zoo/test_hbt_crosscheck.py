"""O-ZOO-3 (optional): released hftbacktest 2.4.4 vs our faithful `hbt_risk_adverse` / `hbt_power3[n=2]` on
`no_partial`, driven by `write_hbt_npz` output of synthetic days with the shadows as GTX orders.

Inputs (`QT_ZOO3_DIR`): per synthetic day `<name>.npz` (`qt_l2::write_hbt_npz`), `<name>.orders.json`
(`{tick_size, lot_size, actions: [{ts, kind, order_id, side, px, qty, tif}]}`, the harness's sidecar format) and
`<name>.zoo_first_fills.json` (`{model_id: {order_id: ts | null}}` from our faithful zoo run on the same day).
Shadows that cross on arrival expire in hbt (GTX) and are excluded by construction (erratum 9); a shadow live across
a book clear differs by SPEC (faithful keeps it, as hbt does, but the exact arm censors it) and is still compared.
The driver runs in the separate venv of `make hbt-crosscheck`.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
VENV_PY = ROOT / ".scratch" / "hbt-venv" / "bin" / "python"
MODELS = "hbt_risk_adverse,hbt_power3[n=2]"


@pytest.mark.requires_hftbacktest
def test_o_zoo_3_hftbacktest_first_fills(tmp_path: Path) -> None:
    d = os.environ.get("QT_ZOO3_DIR")
    if not d:
        pytest.skip("set QT_ZOO3_DIR to a directory of write_hbt_npz days with orders and zoo first-fill sidecars")
    src = Path(d)
    out = tmp_path / "hbt.json"
    subprocess.run(
        [
            str(VENV_PY),
            str(HERE / "hbt_crosscheck.py"),
            str(src),
            "--out",
            str(out),
            "--models",
            MODELS,
            "--exch",
            "no_partial",
            "--coarse",
        ],
        check=True,
    )
    runs = json.loads(out.read_text())["runs"]
    assert runs
    compared = 0
    for r in runs:
        ours = json.loads((src / f"{r['scenario']}.zoo_first_fills.json").read_text()).get(r["model_id"], {})
        meta = json.loads((src / f"{r['scenario']}.orders.json").read_text())
        expired = {e["order_id"] for e in r.get("order_events", []) if e.get("status") == "expired"}
        for a in meta["actions"]:
            if a["kind"] != "submit" or a["order_id"] in expired:
                continue
            key = str(a["order_id"])
            assert r["first_fill"].get(key) == ours.get(key), (r["scenario"], r["model_id"], key)
            compared += 1
    assert compared > 0
