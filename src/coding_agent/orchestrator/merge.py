"""implement 补丁的宿主侧合并。

并发只发生在 worktree；主工作区的 `git apply` 必须串行。这里把
「check → 解析路径 → 留快照 → apply」收成一个可复用函数，旧 dispatch
和事件驱动 scheduler 共用同一套判定，避免两条路径的合并语义分叉。
"""

from __future__ import annotations

import asyncio
import json
import posixpath
import shlex
from typing import Any
from uuid import uuid4

from coding_agent.sandbox.fs import SandboxFs, SandboxFsError, SandboxPathError
from coding_agent.sandbox.snapshots import MAX_OPERATIONAL_BYTES, SnapshotStore
from coding_agent.sandbox.wsl_exec import WslSandbox


def _normalize_patch_path(raw: str) -> str:
    """补丁路径归一化；越界或 agent 内部路径返回空串。"""
    text = str(raw).strip().replace("\\", "/")
    normalized = posixpath.normpath(text)
    if (
        not normalized
        or normalized in {".", ".."}
        or normalized.startswith("../")
        or normalized.startswith("/")
        or normalized == ".agent"
        or normalized.startswith(".agent/")
        or "__pycache__" in normalized.split("/")
    ):
        return ""
    return normalized


def _patch_paths(
    sandbox: WslSandbox, patch_path: str, root: str
) -> tuple[list[str], str]:
    """补丁会碰到的路径（不应用补丁）。"""
    result = sandbox.run(
        f"git apply --numstat -z {shlex.quote(patch_path)}", cwd=root
    )
    if not result.ok:
        return [], result.render(400)

    paths: list[str] = []
    fields = result.stdout.split("\0")
    index = 0
    while index < len(fields):
        field = fields[index]
        if not field:
            index += 1
            continue
        parts = field.split("\t")
        if len(parts) >= 3:
            candidates = [parts[2]] if parts[2] else fields[index + 1 : index + 3]
            index += 1 if parts[2] else 3
        else:
            # 纯重命名可能是 0\t0\tnew\0；旧路径由 worker 的 files 补齐。
            candidates = [field]
            index += 1
        for candidate in candidates:
            normalized = _normalize_patch_path(str(candidate))
            if not normalized:
                return [], f"补丁包含不允许合并的路径：{candidate}"
            paths.append(normalized)
    return sorted(set(paths)), ""


def _snapshot_paths(
    sandbox: WslSandbox, root: str, paths: list[str]
) -> tuple[list[str], str]:
    """合并前给主工作区里的目标文件留底。

    正常路径用一次沙箱脚本批量读内容、一次 `write_many` 批量落盘；
    脚本不可用（例如没有 python3）时回退逐文件读写。
    """
    if not paths:
        return [], ""
    encoded = json.dumps(paths, ensure_ascii=False)
    script = f"""\
python3 - <<'__AGENT_SNAPSHOT__'
import json, os
paths = {encoded!r}
paths = json.loads(paths)
root = os.getcwd()
root_real = os.path.realpath(root)
out = {{}}
for rel in paths:
    full = os.path.realpath(os.path.join(root, rel))
    if not (full == root_real or full.startswith(root_real + os.sep)):
        out[rel] = {{"error": "路径越界"}}
        continue
    try:
        st = os.stat(full)
    except FileNotFoundError:
        out[rel] = {{"exists": False}}
        continue
    except OSError as exc:
        out[rel] = {{"error": str(exc)}}
        continue
    if not os.path.isfile(full):
        out[rel] = {{"error": "不是普通文件"}}
        continue
    try:
        with open(full, "r", encoding="utf-8", errors="replace") as fh:
            content = fh.read({int(MAX_OPERATIONAL_BYTES)})
    except OSError as exc:
        out[rel] = {{"error": str(exc)}}
        continue
    out[rel] = {{"exists": True, "path": full, "content": content}}
print("AGENT_SNAPSHOT_JSON:" + json.dumps(out))
__AGENT_SNAPSHOT__
"""
    result = sandbox.run(script, cwd=root)
    payload: dict[str, dict[str, Any]] | None = None
    if result.ok:
        for line in reversed(result.stdout.splitlines()):
            if line.startswith("AGENT_SNAPSHOT_JSON:"):
                try:
                    raw = json.loads(line[len("AGENT_SNAPSHOT_JSON:") :])
                except json.JSONDecodeError:
                    raw = None
                if isinstance(raw, dict):
                    payload = raw
                break
    if payload is not None:
        items: list[tuple[str, str, bool]] = []
        for relpath in paths:
            item = payload.get(relpath) or {}
            if item.get("error"):
                return [], f"无法为 {relpath} 留底，补丁未合并：{item['error']}"
            existed = bool(item.get("exists"))
            resolved = str(item.get("path") or posixpath.join(root, relpath))
            items.append((resolved, str(item.get("content") or ""), existed))
        store = SnapshotStore(sandbox, SandboxFs(sandbox, root), root)
        try:
            return store.save_many(items), ""
        except (SandboxFsError, SandboxPathError) as exc:
            return [], (
                f"无法批量留底，补丁未合并："
                f"{type(exc).__name__}: {exc}"
            )

    # 回退：逐文件 probe/read/save
    fs = SandboxFs(sandbox, root)
    store = SnapshotStore(sandbox, fs, root)
    snapshot_ids: list[str] = []
    for relpath in paths:
        target = posixpath.join(root, relpath)
        try:
            resolved, info = fs.probe(target)
            if info.exists and not info.is_file:
                return snapshot_ids, f"补丁目标不是普通文件：{relpath}"
            existed = bool(info.exists and info.is_file)
            original = (
                fs.read_text(resolved, max_bytes=MAX_OPERATIONAL_BYTES)
                if existed
                else ""
            )
            snapshot_ids.append(store.save(resolved, original, existed=existed))
        except (SandboxFsError, SandboxPathError) as exc:
            return snapshot_ids, (
                f"无法为 {relpath} 留底，补丁未合并："
                f"{type(exc).__name__}: {exc}"
            )
    return snapshot_ids, ""


async def merge_implement_patch(
    sandbox: WslSandbox,
    root: str,
    record: dict[str, Any],
) -> tuple[bool, str, list[str]]:
    """把一条 implement WorkerResult 的补丁合并进主工作区。

    返回 `(merged, error, snapshot_ids)`。任何一步失败都不应用补丁。
    """
    patch_path = str(record.get("patch_path") or "")
    if not patch_path:
        return False, "补丁路径为空", []
    if record.get("status") in {"failed", "cancelled"}:
        return False, "worker 未成功，补丁不合并", []

    check = await asyncio.to_thread(
        sandbox.run,
        f"git apply --check {shlex.quote(patch_path)}",
        cwd=root,
    )
    if not check.ok:
        return False, check.render(400), []

    patch_paths, path_error = await asyncio.to_thread(
        _patch_paths, sandbox, patch_path, root
    )
    if path_error:
        return False, path_error, []

    snapshot_paths = sorted(
        {
            normalized
            for normalized in (
                _normalize_patch_path(str(item))
                for item in [
                    *(record.get("files") or []),
                    *patch_paths,
                ]
            )
            if normalized
        }
    )
    snapshot_ids, snapshot_error = await asyncio.to_thread(
        _snapshot_paths, sandbox, root, snapshot_paths
    )
    if snapshot_error:
        return False, snapshot_error, snapshot_ids

    applied = await asyncio.to_thread(
        sandbox.run,
        f"git apply {shlex.quote(patch_path)}",
        cwd=root,
    )
    if applied.ok:
        return True, "", snapshot_ids
    return False, applied.render(400), snapshot_ids


async def _validate_three_way(
    sandbox: WslSandbox,
    root: str,
    records: list[dict[str, Any]],
) -> tuple[bool, str]:
    """在临时 index 上预演整批 3-way 合并，不触碰工作区。"""
    index_path = f"/tmp/agent-merge-index-{uuid4().hex}"
    env = f"GIT_INDEX_FILE={shlex.quote(index_path)} "
    try:
        read = await asyncio.to_thread(
            sandbox.run, f"{env}git read-tree HEAD", cwd=root
        )
        if not read.ok:
            return False, read.render(400)
        for record in records:
            patch_path = str(record.get("patch_path") or "")
            result = await asyncio.to_thread(
                sandbox.run,
                f"{env}git apply --3way --cached "
                f"{shlex.quote(patch_path)}",
                cwd=root,
            )
            if not result.ok:
                return False, result.render(400)
        return True, ""
    finally:
        await asyncio.to_thread(
            sandbox.run, f"rm -f {shlex.quote(index_path)}", cwd=root
        )


async def merge_implement_patches_atomic(
    sandbox: WslSandbox,
    root: str,
    records: list[dict[str, Any]],
    *,
    allow_overlap: bool = False,
) -> dict[str, tuple[bool, str, list[str]]]:
    """把同一批 implement 补丁作为**一个事务**合并。

    先全部 `--check`、全部留快照，再顺序 apply；任一 apply 失败时反向
    撤销已应用补丁，失败则回退到快照。返回 task_id → (merged, error, ids)。

    当多个补丁命中同一文件且 `allow_overlap=True` 时，先用临时 index 做
    `git apply --3way --cached` 预演，全部无冲突后再落到工作区。
    """
    outcomes: dict[str, tuple[bool, str, list[str]]] = {
        str(record.get("task_id", "")): (False, "", [])
        for record in records
    }
    eligible = [
        record
        for record in records
        if record.get("patch_path")
        and record.get("status") not in {"failed", "cancelled"}
    ]
    if not eligible:
        for record in records:
            task_id = str(record.get("task_id", ""))
            outcomes[task_id] = (
                False,
                "worker 未成功，补丁不合并",
                list(record.get("snapshot_ids") or []),
            )
        return outcomes

    # 1) 全量校验；任何一条冲突都整批不应用。
    patch_paths: dict[str, list[str]] = {}
    for record in eligible:
        task_id = str(record.get("task_id", ""))
        patch_path = str(record.get("patch_path") or "")
        check = await asyncio.to_thread(
            sandbox.run,
            f"git apply --check {shlex.quote(patch_path)}",
            cwd=root,
        )
        if not check.ok:
            outcomes[task_id] = (False, check.render(400), [])
            for other in eligible:
                other_id = str(other.get("task_id", ""))
                if other_id != task_id:
                    outcomes[other_id] = (
                        False,
                        "同批补丁校验失败，整批未合并",
                        [],
                    )
            return outcomes
        paths, path_error = await asyncio.to_thread(
            _patch_paths, sandbox, patch_path, root
        )
        if path_error:
            outcomes[task_id] = (False, path_error, [])
            for other in eligible:
                other_id = str(other.get("task_id", ""))
                if other_id != task_id:
                    outcomes[other_id] = (
                        False,
                        "同批补丁路径校验失败，整批未合并",
                        [],
                    )
            return outcomes
        patch_paths[task_id] = paths

    owners: dict[str, set[str]] = {}
    for task_id, paths in patch_paths.items():
        for path in paths:
            owners.setdefault(path, set()).add(task_id)
    overlaps = {
        path: sorted(task_ids)
        for path, task_ids in owners.items()
        if len(task_ids) > 1
    }
    if overlaps and not allow_overlap:
        detail = "；".join(
            f"{path}（{', '.join(ids)}）" for path, ids in overlaps.items()
        )
        for record in records:
            task_id = str(record.get("task_id", ""))
            outcomes[task_id] = (
                False,
                f"多个补丁命中同一文件，未开启 3-way 合并：{detail}",
                [],
            )
        return outcomes
    use_three_way = bool(overlaps)
    if use_three_way:
        ok, error = await _validate_three_way(
            sandbox, root, eligible
        )
        if not ok:
            for record in records:
                task_id = str(record.get("task_id", ""))
                outcomes[task_id] = (
                    False,
                    f"3-way 预演冲突，整批未合并：{error}",
                    [],
                )
            return outcomes

    # 2) 全部留快照（尚未应用任何补丁）。
    all_paths: set[str] = set()
    for record in eligible:
        task_id = str(record.get("task_id", ""))
        paths = sorted(
            {
                normalized
                for normalized in (
                    _normalize_patch_path(str(item))
                    for item in [
                        *(record.get("files") or []),
                        *patch_paths.get(task_id, []),
                    ]
                )
                if normalized
            }
        )
        all_paths.update(paths)
        snapshot_ids, snapshot_error = await asyncio.to_thread(
            _snapshot_paths, sandbox, root, paths
        )
        if snapshot_error:
            outcomes[task_id] = (False, snapshot_error, snapshot_ids)
            for other in eligible:
                other_id = str(other.get("task_id", ""))
                if other_id != task_id:
                    outcomes[other_id] = (
                        False,
                        "同批快照失败，整批未合并",
                        [],
                    )
            return outcomes
        outcomes[task_id] = (False, "", snapshot_ids)

    # 3) 顺序应用；失败则按相反顺序撤销，必要时回退快照。
    applied: list[dict[str, Any]] = []
    for record in eligible:
        task_id = str(record.get("task_id", ""))
        patch_path = str(record.get("patch_path") or "")
        apply_cmd = "git apply --3way" if use_three_way else "git apply"
        result = await asyncio.to_thread(
            sandbox.run,
            f"{apply_cmd} {shlex.quote(patch_path)}",
            cwd=root,
        )
        if result.ok:
            applied.append(record)
            outcomes[task_id] = (
                True,
                "",
                list(outcomes[task_id][2]),
            )
            continue

        await asyncio.to_thread(
            _rollback_merged, sandbox, root, applied, sorted(all_paths)
        )
        for done in applied:
            done_id = str(done.get("task_id", ""))
            outcomes[done_id] = (
                False,
                "同批后续补丁失败，已回滚",
                list(outcomes[done_id][2]),
            )
        outcomes[task_id] = (
            False,
            result.render(400),
            list(outcomes[task_id][2]),
        )
        applied_ids = {str(item.get("task_id", "")) for item in applied}
        for other in eligible:
            other_id = str(other.get("task_id", ""))
            if other_id not in applied_ids and other_id != task_id:
                outcomes[other_id] = (
                    False,
                    "同批补丁失败，整批未合并",
                    list(outcomes[other_id][2]),
                )
        return outcomes

    if use_three_way:
        # git apply --3way 会顺带更新 index；恢复成“工作区有改动、
        # 未暂存”的旧语义，避免主 agent/用户看到意外的 staged 状态。
        await asyncio.to_thread(sandbox.run, "git reset -q", cwd=root)
    return outcomes


def _rollback_merged(
    sandbox: WslSandbox,
    root: str,
    applied: list[dict[str, Any]],
    snapshot_paths: list[str],
) -> None:
    """撤销本批已应用补丁；反向 apply 失败时回退快照。"""
    reverse_failed = False
    for record in reversed(applied):
        patch_path = str(record.get("patch_path") or "")
        result = sandbox.run(
            f"git apply -R {shlex.quote(patch_path)}", cwd=root
        )
        if not result.ok:
            reverse_failed = True
            break
    if not reverse_failed:
        return
    fs = SandboxFs(sandbox, root)
    store = SnapshotStore(sandbox, fs, root)
    for path in snapshot_paths:
        entry = store.latest_for(path)
        if entry is not None:
            store.restore(entry)
