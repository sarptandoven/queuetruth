"""Certification of an episode from its committed log: `verify(log, dataset=...)`.

Four checks, all on the log alone plus the dataset:

1. **Chain**: every entry hash matches its content and names the previous one; the root is the last hash.
2. **Header**: the log was made by this engine version, its `spec_id` is the one its spec gives on this dataset, and
   its `open` entry (inside the chain) names the same engine version, dataset id and spec id; the `open` entry also
   holds the full spec digest, so the root commits to every setting (latency, fees, seed, ...).
3. **Dataset**: the dataset's `dataset_id` is the one the log was made on, and every partition file's sha256 is the
   one its manifest lists (a changed file under an unchanged manifest is caught here, not only when it diverges).
4. **Re-execution**: a fresh sandbox with the logged spec replays the logged instructions at the logged clocks
   (`submit`, agent `cancel`, `advance`, `finish`; rejected attempts are re-attempted with the logged arguments and
   must be rejected the same way). Each resulting entry must hash exactly like the logged one, which covers the
   information set of every decision (`visible_ns`, `rows_seen`), the observation the agent received at every step
   (`observation_sha256`), the orders the sandbox derived (ids, latencies, collar prices) and the final orders and
   fills. The first entry that does not reproduce is reported as `first_divergence`.

With a scorecard, its content (digest recomputed), its `certificate` and its `run_id` must equal the
re-executed ones.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from queuetruth._version import __version__
from queuetruth.errors import QtError
from queuetruth.sandbox.log import GENESIS, check_chain
from queuetruth.sandbox.models import SandboxOrderLog, SandboxScorecard, SandboxVerification
from queuetruth.sandbox.scorecard import scorecard_digest
from queuetruth.sandbox.tape import check_partition_files


def _load(value: Any) -> dict[str, Any]:
    if isinstance(value, SandboxOrderLog | SandboxScorecard):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return value
    p = Path(value)
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise QtError("QT-INPUT-001", f"{p}: not found") from None
    except (OSError, json.JSONDecodeError) as e:
        raise QtError("QT-INPUT-002", f"{p}: not a JSON document: {e}") from None
    if not isinstance(doc, dict):
        raise QtError("QT-INPUT-002", f"{p}: not a JSON object")
    return doc


def _replay_entry(sb: Any, e: dict[str, Any]) -> Any:
    kind, data = e["kind"], e["data"]
    if kind == "submit":
        return sb.submit(**data["request"])
    if kind == "reject":
        req = dict(data["request"])
        try:
            if data["action"] == "submit":
                sb.submit(**req)
            else:
                sb.cancel(req.get("order_id"), req.get("qty"))
        except QtError:
            return None
        raise QtError("QT-GATE-001", f"entry {e['seq']}: a rejected {data['action']} was accepted on re-execution")
    if kind == "cancel":
        if data.get("by") == "finish":
            return sb.finish()  # finish logs its own cancels, then the finish entry
        return sb.cancel(data["order_id"], data.get("qty"))
    if kind == "advance":
        return sb.advance(to=int(data["to_ns"]))
    if kind == "finish":
        return sb.finish()
    raise QtError("QT-INPUT-002", f"entry {e['seq']}: unknown kind {kind!r}")


def verify(
    log: SandboxOrderLog | dict[str, Any] | str | Path,
    *,
    dataset: str | Path | None = None,
    scorecard: SandboxScorecard | dict[str, Any] | str | Path | None = None,
    workdir: str | Path | None = None,
) -> SandboxVerification:
    """Verifies a committed log (module docstring). `dataset` overrides the logged dataset path (the identity is
    checked by `dataset_id` and the partition files' sha256); `scorecard` is checked against the re-execution when
    given."""
    from queuetruth.sandbox.session import Sandbox

    doc = _load(log)
    try:
        SandboxOrderLog.model_validate(doc)
    except Exception as e:  # pydantic ValidationError: a malformed log is an input error
        raise QtError("QT-INPUT-002", f"not a qt.sandbox_order_log: {e}") from None
    problems = check_chain(doc)
    chain_ok = not problems
    entries = doc["entries"]
    header_ok = True
    if doc["engine_version"] != __version__:
        header_ok = False
        problems.append(
            f"the log was made with queuetruth {doc['engine_version']} and this is {__version__}: re-execution is "
            "exact only on the engine version that made it"
        )
    opened = entries[0]["data"] if entries and entries[0]["kind"] == "open" else {}
    for key in ("engine_version", "dataset_id", "spec_id"):
        if opened.get(key) != doc[key]:
            header_ok = False
            problems.append(f"the log header's {key} is not the one its open entry commits to")
    spec = dict(doc["spec"])
    if dataset is not None:
        spec["dataset"] = {"path": str(dataset)}
    dataset_ok = reexec_ok = False
    divergence: int | None = None
    card = None
    sb = None
    try:
        sb = Sandbox.open(spec, workdir=workdir)
        dataset_ok = sb.dataset_id == doc["dataset_id"]
        if not dataset_ok:
            problems.append(f"dataset_id {sb.dataset_id} differs from the logged {doc['dataset_id']}")
        files = check_partition_files(sb.src)
        if files:
            dataset_ok = False
            problems.extend(f"dataset file {p}" for p in files)
        if sb.spec_id != doc["spec_id"]:
            header_ok = False
            problems.append(
                f"the logged spec gives spec_id {sb.spec_id} on this dataset, not the logged {doc['spec_id']}"
            )
        done = 0
        if not entries or entries[0]["kind"] != "open":
            problems.append("the log does not start with an open entry")
            divergence = 0
        for k, e in enumerate(entries):
            if divergence is not None:
                break
            if k < done:
                continue
            if k > 0:
                try:
                    out = _replay_entry(sb, e)
                except QtError as err:
                    problems.append(f"entry {k} ({e['kind']}): re-execution failed: {err}")
                    divergence = k
                    break
                if isinstance(out, SandboxScorecard):
                    card = out
            new = sb.log.entries[done:]
            for j, got in enumerate(new):
                want = entries[done + j] if done + j < len(entries) else None
                if want is None or got["hash"] != want["hash"]:
                    divergence = done + j
                    problems.append(f"entry {done + j}: re-execution produced a different entry")
                    break
            done += len(new)
            if divergence is None and done <= k:
                divergence = k
                problems.append(f"entry {k}: re-execution produced no entry")
        if divergence is None and done != len(entries):
            divergence = done
            problems.append(f"re-execution stopped after {done} of {len(entries)} entries")
        reexec_ok = divergence is None
    except QtError as e:
        problems.append(f"re-execution could not start: {e}")
    finally:
        if sb is not None and not sb.closed:
            sb.close()
    scorecard_ok = None
    run_id = card.run_id if card is not None else None
    root = doc.get("root") if isinstance(doc.get("root"), str) else GENESIS
    if scorecard is not None:
        given = _load(scorecard)
        cert = given.get("certificate") or {}
        want = None if card is None else card.certificate.scorecard_sha256
        scorecard_ok = (
            card is not None
            and scorecard_digest(given) == want
            and cert.get("scorecard_sha256") == want
            and cert.get("log_root") == root
            and given.get("run_id") == card.run_id
        )
        if not scorecard_ok:
            problems.append("the scorecard does not match the re-executed one (content, digest, run id or log root)")
    return SandboxVerification.model_validate(
        {
            "schema": "qt.sandbox_verification",
            "schema_version": 1,
            "ok": chain_ok and header_ok and dataset_ok and reexec_ok and scorecard_ok is not False,
            "run_id": run_id,
            "log_root": root,
            "chain_ok": chain_ok,
            "header_ok": header_ok,
            "dataset_ok": dataset_ok,
            "reexecution_ok": reexec_ok,
            "scorecard_ok": scorecard_ok,
            "first_divergence": divergence,
            "problems": problems,
        }
    )


__all__ = ["verify"]
