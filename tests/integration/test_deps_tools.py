"""集成测试：依赖工具的安装器识别与非法包名拦截。

真实装包会联网且执行构建脚本，这里只验证「识别对了」和「非法输入被挡在
执行之前」，不真的安装。
"""

from __future__ import annotations

import shlex
from uuid import uuid4

import pytest

from coding_agent.sandbox.wsl_exec import WslSandbox, resolve_workspace
from coding_agent.tools.artifacts import ShellArtifact, unpack
from coding_agent.tools.deps import (
    DEPS_INSTALL,
    DEPS_LIST,
    build_deps_tools,
    detect_manager,
)

pytestmark = pytest.mark.wsl


@pytest.fixture
def workdir(require_wsl: WslSandbox) -> str:
    root = f"{resolve_workspace(require_wsl.settings, require_wsl)}-deps-{uuid4().hex[:8]}"
    require_wsl.run(f"mkdir -p {shlex.quote(root)}")
    yield root
    require_wsl.run(f"rm -rf {shlex.quote(root)}")


def _tools(require_wsl, settings, workdir: str) -> dict:
    scoped = settings.model_copy(update={"wsl_workspace": workdir})
    return {t.name: t for t in build_deps_tools(scoped, require_wsl)}


def _invoke(tool, **args) -> tuple[str, ShellArtifact]:
    text, artifact = unpack(tool.invoke(args))
    assert artifact is not None
    return text, ShellArtifact.model_validate(artifact)


# 这些用例只关心「选了哪个包管理器」，因此直接验证探测结果 ——
# 走一遍 deps_install 会**真的执行安装**（pip install / npm install 都要联网），
# 既慢又让单测依赖外部网络。

def test_detects_pip_from_requirements(require_wsl, workdir) -> None:
    require_wsl.run(f"printf 'requests\\n' > {shlex.quote(workdir + '/requirements.txt')}")
    install, listing = detect_manager(require_wsl, workdir)
    assert install == ["pip", "install"]
    assert listing == ["pip", "list"]


def test_detects_npm_from_package_json(require_wsl, workdir) -> None:
    require_wsl.run(f"printf '{{\"scripts\": {{\"test\": \"x\"}}}}' > "
                    f"{shlex.quote(workdir + '/package.json')}")
    install, _ = detect_manager(require_wsl, workdir)
    assert install == ["npm", "install"]


def test_detects_uv_from_lockfile(require_wsl, workdir) -> None:
    require_wsl.run(f"touch {shlex.quote(workdir + '/uv.lock')}")
    install, listing = detect_manager(require_wsl, workdir)
    assert install == ["uv", "add"]
    assert listing == ["uv", "pip", "list"]


def test_lockfile_wins_over_manifest(require_wsl, workdir) -> None:
    """同时存在 uv.lock 与 pyproject.toml 时应当选 uv。"""
    require_wsl.run(
        f"touch {shlex.quote(workdir + '/uv.lock')} {shlex.quote(workdir + '/pyproject.toml')}"
    )
    install, _ = detect_manager(require_wsl, workdir)
    assert install == ["uv", "add"]


def test_defaults_to_pip_without_any_manifest(require_wsl, workdir) -> None:
    assert detect_manager(require_wsl, workdir) == (["pip", "install"], ["pip", "list"])


def test_install_command_is_built_from_detected_manager(require_wsl, settings, workdir) -> None:
    """命令构造仍要过一遍真实入口 —— 用 uv 是因为沙箱里没装它，
    会立刻以「command not found」失败，不会联网装东西。"""
    require_wsl.run(f"touch {shlex.quote(workdir + '/uv.lock')}")
    _, artifact = _invoke(_tools(require_wsl, settings, workdir)[DEPS_INSTALL],
                          reason="装依赖", packages=["rich>=13"])
    assert artifact.command.startswith("uv add")
    assert "rich>=13" in artifact.command


# ---------------- 非法输入必须在执行之前被挡下 ----------------

@pytest.mark.parametrize(
    "bad",
    ["--index-url=http://evil", "requests; rm -rf /", "$(whoami)", "-e", "req uests"],
)
def test_illegal_package_never_reaches_the_manager(
    require_wsl, settings, workdir, bad: str
) -> None:
    text, artifact = _invoke(_tools(require_wsl, settings, workdir)[DEPS_INSTALL],
                             reason="装依赖", packages=[bad])
    assert artifact.rejected is True
    assert artifact.ok is False
    # 关键：没有构造出任何安装命令
    assert artifact.command == ""
    assert "未执行任何安装" in text


def test_one_bad_package_blocks_the_whole_batch(require_wsl, settings, workdir) -> None:
    """批次里只要有一个非法包名，整批都不执行 —— 不做部分安装。"""
    _, artifact = _invoke(_tools(require_wsl, settings, workdir)[DEPS_INSTALL],
                          reason="装依赖", packages=["rich", "requests; rm -rf /"])
    assert artifact.rejected is True
    assert artifact.command == ""


def test_empty_package_list_is_rejected(require_wsl, settings, workdir) -> None:
    text, artifact = _invoke(_tools(require_wsl, settings, workdir)[DEPS_INSTALL],
                             reason="装依赖", packages=[])
    assert artifact.rejected is True
    assert "不能为空" in text


def test_valid_batch_is_passed_through_intact(require_wsl, settings, workdir) -> None:
    require_wsl.run(f"touch {shlex.quote(workdir + '/uv.lock')}")  # uv 未安装，会秒失败
    _, artifact = _invoke(_tools(require_wsl, settings, workdir)[DEPS_INSTALL],
                          reason="装依赖", packages=["rich>=13", "requests[socks]"])
    assert "rich>=13" in artifact.command
    assert "requests[socks]" in artifact.command


def test_list_is_read_only(require_wsl, settings, workdir) -> None:
    require_wsl.run(f"touch {shlex.quote(workdir + '/uv.lock')}")
    _, artifact = _invoke(_tools(require_wsl, settings, workdir)[DEPS_LIST], reason="看看装了啥")
    assert artifact.level_label == "L0 只读"
    assert "list" in artifact.command
