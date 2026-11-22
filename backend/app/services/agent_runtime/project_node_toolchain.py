"""按工程声明提供镜像中已经锁定的离线 npm 依赖。"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

NPM = ("node", "/usr/local/lib/node_modules/npm/bin/npm-cli.js", "--offline")
_TOOLCHAIN = Path("/opt/project-node")
_SECTIONS = ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies")
_COMPARE_RANGES = """
const fs = require('node:fs');
const semver = require('semver');
const { installed, requested, engines } = JSON.parse(fs.readFileSync(0, 'utf8'));
const issues = [];
for (const [name, ranges] of Object.entries(requested)) {
  const version = Object.hasOwn(installed, name) ? installed[name] : undefined;
  const unmatched = ranges.filter(range => !version || !semver.satisfies(version, range));
  if (unmatched.length) {
    const labels = unmatched.map(range => semver.validRange(range) === null ? '非 npm 版本范围' : range);
    issues.push(`${name} ${labels.join(' / ')}${version ? `（环境为 ${version}）` : ''}`);
  }
}
const actual = { node: process.versions.node, npm: require(process.argv[1]).version };
for (const [name, version] of Object.entries(actual)) {
  if (engines[name] && !semver.satisfies(version, engines[name])) {
    issues.push(`${name} ${engines[name]}（环境为 ${version}）`);
  }
}
process.stdout.write(JSON.stringify(issues));
"""


def dependency_ranges(manifest: dict, lock: dict) -> dict[str, list[str]]:
    requested: dict[str, list[str]] = {}
    packages = lock.get("packages") or {}
    resolved = lock.get("dependencies") or {}
    if not isinstance(packages, dict) or not isinstance(resolved, dict):
        raise ValueError("package-lock.json 的依赖记录应为对象")
    for section in _SECTIONS:
        entries = manifest.get(section) or {}
        if not isinstance(entries, dict):
            raise ValueError(f"package.json 的 {section} 应为对象")
        for name, requirement in entries.items():
            if not isinstance(requirement, str) or not requirement.strip():
                raise ValueError(f"package.json 应为 {name} 声明有效的依赖版本")
            requested.setdefault(name, []).append(requirement)
    for name, ranges in requested.items():
        record = packages.get(f"node_modules/{name}") or resolved.get(name)
        if isinstance(record, dict) and isinstance(version := record.get("version"), str):
            ranges.append(version)
        requested[name] = list(dict.fromkeys(ranges))
    return requested


def provide_dependencies(cwd: Path, manifest: dict, lock: dict, worker_group: int) -> list[str]:
    installed = json.loads((_TOOLCHAIN / "package.json").read_bytes())["dependencies"]
    requested = dependency_ranges(manifest, lock)
    engines = manifest.get("engines") or {}
    if not isinstance(engines, dict) or any(not isinstance(engines.get(name, ""), str) for name in ("node", "npm")):
        raise ValueError("package.json 的 engines 应声明 Node 或 npm 的版本范围")
    # 此进程只读取可信镜像内的版本比较工具。生成的构建命令在低权限子进程中运行。
    request = json.dumps({"installed": installed, "requested": requested, "engines": engines})
    process = subprocess.run(
        ("node", "-e", _COMPARE_RANGES, "/usr/local/lib/node_modules/npm/package.json"),
        cwd=_TOOLCHAIN, env={"PATH": "/usr/local/bin:/usr/bin:/bin"}, input=request,
        capture_output=True, text=True, encoding="utf-8", check=True, timeout=5,
    )
    issues = json.loads(process.stdout)
    if issues:
        return issues

    # 专用目录允许 Vite 写入临时配置，同时保护根用户创建的依赖链接。
    links = cwd / "node_modules"
    links.mkdir()
    os.chown(links, 0, worker_group)
    links.chmod(0o1770)
    commands = links / ".bin"
    commands.mkdir()
    for name in requested:
        package = _TOOLCHAIN / "node_modules" / name
        link = links / name
        link.parent.mkdir(exist_ok=True)
        link.symlink_to(package, target_is_directory=True)
        binaries = json.loads((package / "package.json").read_bytes()).get("bin", {})
        if isinstance(binaries, str):
            binaries = {name.rsplit("/", 1)[-1]: binaries}
        for binary in binaries:
            (commands / binary).symlink_to(_TOOLCHAIN / "node_modules" / ".bin" / binary)
    return []
