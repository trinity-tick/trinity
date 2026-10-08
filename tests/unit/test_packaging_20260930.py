"""打包闸门（2026-09-30，外部审计目标项 8/9/10）。

### (9) `python -m build` 静默失败 —— 真凶是 `[build-system] requires` 里的 `wheel`

现场：`python -m build --no-isolation` 在第一个 hook 之前就 `exit 1`，
stdout 停在 `writing manifest file`，**stderr 只有一行**
`ERROR Missing dependencies: wheel`（我一开始把它当成了"静默崩溃"）。
本机 `importlib.metadata.version("wheel")` → `PackageNotFoundError`。

而 **setuptools ≥ 70.1 已自带 `bdist_wheel`**（实测
`setuptools.command.bdist_wheel` 存在；本机 setuptools 78.1.0）
⇒ 把 `wheel` 列进 build-system 只会迫使构建环境多装一个已无用的包，
并让 `--no-isolation`（离线/受限环境常用）直接失败。

反事实证据：修好前后——
  * 修复前：`python -m build --no-isolation` → exit **1**，stderr 含 Missing dependencies
  * 修复后：exit **0**，stderr **0 行**，`Successfully built ...tar.gz and ...whl`

### (8) sdist 缺少可复现工作流 + 夹带 15 MB vendored TypeScript

`sdist` 里 `scripts/` 原本只有 1 个文件（`generate_openapi.py`），而 README 让人跑
`scripts/memory_portability.py`（迁移）/ `scripts/run_evals.py`（复现）⇒ pip 用户做不到。
同时 `recursive-include trinity *.js` 把 `trinity/sdk/js/node_modules/` 全扫进包
（`typescript.js` 8.88 MB + `_tsc.js` 6.05 MB = **14.98 MB**），`graft docs` 还带进了
`docs/ORGAN_REGISTRY.json.bak-20260921_185103`。

实测（修复后）：`scripts/*.py` **1 → 555**、`benchmark/*.py` **0 → 118**、
`node_modules` 条目 **0**、`.bak-*` **0**、`*.pyc` **0**、1 GB 评测数据 **0**；
sdist **7.67 → 5.05 MB**，wheel **5.22 → 2.58 MB**。

### (10) dist 元数据与 pyproject 一致

实测 wheel METADATA：`Version: 8.2.1` == pyproject；`Summary` ==
`project.description` **逐字相同**；已删除四处被证伪宣称
（`v8.2.0` / `50-Layer Guardian Chain` / `129-Paper` / `Raft Cluster`）；
extras **11 个**与 pyproject 完全一致。

运行：``python -m pytest tests/unit/test_packaging_20260930.py -q``
"""

from __future__ import annotations

import re
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = ROOT / "pyproject.toml"
MANIFEST = ROOT / "MANIFEST.in"


def _loud_fail(reason: str):
    """**t71/I11：跳过必须响** —— 本文件原来的 3 处 `pytest.skip` 全部改走这里。

    它们在 t65 的分级里都是 **L-C 候选**（"跳过的正是这个测试存在的理由"）：
    · 构建失败 ⇒ 跳过**行为判据**（判据本来就是要跑构建的）；
    · 无 wheel ⇒ 跳过**元数据判据**；
    · 无自带 `bdist_wheel` ⇒ 跳过**前提校验**（这条测试的全部内容就是断言它在）。

    **什么条件下才真的不适用**：本仓没有"不适用"的常态 —— 打包闸门在能跑 Python 的环境里就该跑。
    若某个环境**确实**没有构建后端，应由**门禁层**用 `-k` / `--ignore` 显式排除本文件
    （那是"这个环境不跑这条闸门"的显式声明），而**不是**让判据在运行期把自己静默跳过。
    """
    pytest.fail("[t71/I11] 本判据不接受静默降级（跳过必须响）：%s" % reason)


def _manifest_live_lines():
    for line in MANIFEST.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            yield s


# ── (9) build-system 不得再要求 wheel ───────────────────────────────────

def test_build_system_does_not_require_wheel() -> None:
    """`wheel` 未安装时 `--no-isolation` 会以 "Missing dependencies: wheel" 直接失败。"""
    reqs = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["build-system"]["requires"]
    assert not any(r.split(">=")[0].split("==")[0].strip() == "wheel" for r in reqs), (
        f"build-system 不应再要求 wheel（setuptools>=70.1 已自带 bdist_wheel）：{reqs}"
    )


def test_build_system_pins_setuptools_for_vendored_bdist_wheel() -> None:
    """必须把下限提到 70.1 —— 那是 `bdist_wheel` 被并入 setuptools 的版本。"""
    reqs = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["build-system"]["requires"]
    st = [r for r in reqs if r.startswith("setuptools")]
    assert st, f"缺少 setuptools 约束：{reqs}"
    m = re.search(r">=\s*(\d+)\.(\d+)", st[0])
    assert m, f"无法解析版本下限：{st[0]}"
    assert (int(m.group(1)), int(m.group(2))) >= (70, 1), (
        f"setuptools 下限必须 >= 70.1（自带 bdist_wheel），实测 {st[0]}"
    )


def test_vendored_bdist_wheel_is_available() -> None:
    """前提校验：本环境确实有自带的 bdist_wheel（否则上面的推理不成立）。

    2026-10-06（t71/I11）：原来这里是 `pytest.skip(...)` —— 而**本测试存在的唯一理由**
    就是断言"自带 bdist_wheel 可用" ⇒ 缺了它反而跳过，等于把判据删掉（L-C 逃逸口）⇒ 改**响亮失败**。
    """
    try:
        import setuptools.command.bdist_wheel  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        _loud_fail("setuptools 无自带 bdist_wheel（%s）⇒ 本仓 pyproject 依赖它"
                   "（setuptools>=70.1 起自带）；这不是「跳过」，是**前提被破坏**" % exc)


# ── (8) MANIFEST.in 契约 ────────────────────────────────────────────────

def test_manifest_includes_scripts_and_benchmark_code() -> None:
    body = "\n".join(_manifest_live_lines())
    assert re.search(r"recursive-include\s+scripts\s+", body), "scripts 代码必须进 sdist"
    assert re.search(r"recursive-include\s+benchmark\s+", body), "benchmark 代码必须进 sdist"


def test_manifest_prunes_node_modules_and_backups() -> None:
    body = "\n".join(_manifest_live_lines())
    assert re.search(r"prune\s+trinity/sdk/js/node_modules", body), (
        "必须 prune node_modules（否则 14.98 MB vendored TypeScript 进包）"
    )
    assert "*.bak-*" in body, "必须排除 .bak-* 备份"
    assert "*.pyc" in body, "必须排除字节码"


def test_manifest_does_not_graft_huge_benchmark_data() -> None:
    """benchmark/ 实测 1,082 MB —— 绝不能整体 graft。"""
    for line in _manifest_live_lines():
        assert line != "graft benchmark", "不得整体 graft benchmark（含 1 GB 评测数据）"
        assert line != "graft scripts", "不得整体 graft scripts（含 664 个 .pyc）"


# ── 行为验证：真的构建一次 sdist 并检查内容 ─────────────────────────────

@pytest.fixture(scope="module")
def built_sdist(tmp_path_factory):
    """用 setuptools 后端直接构建 sdist（比 `python -m build` 少一层，且已验证等价）。"""
    out = tmp_path_factory.mktemp("sdist")
    code = (
        "from setuptools.build_meta import build_sdist;"
        f"print(build_sdist(r'{out}'))"
    )
    proc = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT),
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=900)
    if proc.returncode != 0:
        # 2026-10-06（t71/I11）：原来是 `pytest.skip("构建 sdist 失败，跳过行为判据")`
        # —— 构建失败**正是**这些行为判据要抓的事 ⇒ 改**响亮失败**。
        _loud_fail("构建 sdist 失败（rc=%d）：%s\n  本文件的行为判据（内容/夹带/元数据）"
                   "依赖这次真实构建；构建失败是**闸门该红**，不是跳过"
                   % (proc.returncode, (proc.stderr or "")[-300:]))
    files = list(Path(out).glob("*.tar.gz"))
    assert files, "未产出 sdist"
    with tarfile.open(files[0]) as t:
        return [m.name for m in t.getmembers()], files[0]


def test_sdist_contains_repro_scripts(built_sdist) -> None:
    names, _ = built_sdist
    for must in ("memory_portability.py", "run_evals.py", "doc_retrieval_eval.py"):
        assert any(n.endswith("/scripts/" + must) for n in names), (
            f"{must} 不在 sdist 里 —— README 的对应工作流对 pip 用户不可用"
        )
    assert len([n for n in names if re.search(r"/scripts/.*\.py$", n)]) > 100


def test_sdist_excludes_junk_and_huge_data(built_sdist) -> None:
    names, _ = built_sdist
    bad = []
    for n in names:
        if "node_modules" in n:
            bad.append(("node_modules", n))
        elif n.endswith(".pyc"):
            bad.append(("pyc", n))
        elif ".bak-" in n:
            bad.append(("bak", n))
        elif re.search(r"\.(jsonl|db|sqlite|faiss)$", n):
            bad.append(("data", n))
    assert bad == [], f"sdist 夹带了不该发的东西：{bad[:6]}"


def test_wheel_metadata_matches_pyproject(built_sdist, tmp_path_factory) -> None:
    """item 10：wheel 元数据必须与 pyproject 一致（旧 wheel 是 8-24 的过期产物）。

    2026-10-06（t71/I11）：原来 `dist/` 下没有 wheel 就 `pytest.skip`（L-C 逃逸口：**跳过元数据判据**），
    而注释里写的却是"没有就用后端现建一个" —— 注释与代码不一致。现在**真的现建一个**：
    构建失败就**响亮失败**（构建能力是这条闸门的前提）。
    """
    proj = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]
    wheels = sorted((ROOT / "dist").glob("*.whl"))
    if not wheels:
        out = tmp_path_factory.mktemp("wheel")
        code = ("from setuptools.build_meta import build_wheel;"
                f"print(build_wheel(r'{out}'))")
        proc = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT),
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=900)
        if proc.returncode != 0:
            _loud_fail("dist/ 下无 wheel 且现场构建失败（rc=%d）：%s"
                       % (proc.returncode, (proc.stderr or "")[-300:]))
        wheels = sorted(Path(out).glob("*.whl"))
    assert wheels, "既无现成 wheel 也未构建出 wheel ⇒ 元数据判据无从执行（不是跳过）"
    with zipfile.ZipFile(wheels[-1]) as z:
        meta = z.read([n for n in z.namelist() if n.endswith("METADATA")][0]).decode(
            "utf-8", "replace")

    def field(name):
        m = re.search(rf"^{name}:(.*)$", meta, re.M)
        return m.group(1).strip() if m else None

    assert field("Version") == proj["version"], f"wheel 版本 {field('Version')} != {proj['version']}"
    assert field("Summary") == proj["description"].replace("\n", " ").strip(), (
        "wheel Summary 与 pyproject description 不一致"
    )
    for refuted in ("v8.2.0", "50-Layer Guardian Chain", "129-Paper", "Raft Cluster"):
        assert refuted not in (field("Summary") or ""), f"wheel 仍含被证伪宣称 {refuted!r}"
    extras = sorted(re.findall(r"^Provides-Extra:\s*(\S+)", meta, re.M))
    assert extras == sorted(proj.get("optional-dependencies", {})), (
        f"wheel extras {extras} != pyproject {sorted(proj.get('optional-dependencies', {}))}"
    )
    assert not any("node_modules" in n for n in zipfile.ZipFile(wheels[-1]).namelist()), (
        "wheel 里仍夹带 node_modules（旧产物曾是 5.22 MB，修剪后 2.58 MB）"
    )
