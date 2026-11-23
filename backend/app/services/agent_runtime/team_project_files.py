"""团队项目源码工作区与静态验收，生成的源码始终按数据处理。"""

from __future__ import annotations

import ast
import builtins
import hashlib
import json
import os
import posixpath
import re
import shutil
import stat
import subprocess
import symtable
import sys
import tempfile
import threading
import tomllib
from collections import Counter
from collections.abc import Collection, Iterable
from html import escape
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import Literal
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from app.core.config import settings

from .backends import workspace_root
from .project_browser_contract import BROWSER_CHECKS_PATH, parse_browser_checks
from .project_node_toolchain import dependency_ranges
from .project_verifier_protocol import MAX_PROJECT_BYTES, MAX_PROJECT_FILE_BYTES, MAX_PROJECT_FILES, source_digest as _digest
from .team_project_coverage import merge_coverage_report
from .team_project_verifier import merge_runtime_report


ProjectScope = Literal["fullstack", "frontend", "backend"]

_PATH_SEGMENT = re.compile(r"[A-Za-z0-9_@().+\[\]-]{1,90}\Z")
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}\Z")
_DEVICE = re.compile(r"(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?\Z", re.I)
_DOTFILES = {".gitignore", ".dockerignore", ".env.example", ".editorconfig"}
_BLOCKED_NAMES = {
    "node_modules", "__pycache__", "venv", "dist", "build", "coverage", "vendor",
    "credentials.json", "secrets.json", "id_rsa", "id_ed25519", "verification.json",
}
_BLOCKED_SUFFIXES = {
    ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar", ".exe", ".com", ".dll",
    ".so", ".dylib", ".bin", ".msi", ".scr", ".pfx", ".p12", ".pem", ".key", ".der",
    ".jar", ".class", ".wasm", ".sqlite", ".sqlite3", ".db", ".pyc", ".lnk",
}
_SECRET = re.compile(
    r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----|\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"
    r"|\bgh[pousr]_[a-zA-Z0-9]{30,}\b|\bsk-(?:proj|svcacct)-[a-zA-Z0-9_-]{20,}\b"
)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_FRONTEND = {"frontend", "client", "web", "ui"}
_BACKEND = {"backend", "server", "api"}
_WEB_CODE = {".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".vue", ".svelte"}
_JAVASCRIPT = {".js", ".mjs", ".cjs"}
_SERVER_CODE = _WEB_CODE | {".py", ".go", ".rb", ".php", ".java", ".kt", ".cs", ".rs"}
_HTML_DOCUMENT = re.compile(r"^\s*(?:<!doctype\s+html\b|<html[\s>])", re.I)
_RAW_HTML_FRAGMENT = re.compile(
    r"^(?>(?:\s|//[^\r\n]*|/\*[\s\S]*?\*/)*)"
    r"<(?P<tag>template|script|style|main|section|div|form)(?:\s[^<>]*)?>[\s\S]*?</(?P=tag)\s*>", re.I,
)
_HTML_ELEMENT = re.compile(r"<[a-z][\w-]*(?:\s[^<>]*|/?)>", re.I)
_MODEL_INTRO = re.compile(r"(?:你好[，,！!。\s]*)?(?:我(?:是|叫)|I(?: am|'m)\b|As an AI\b)", re.I)
_CODE_ADVICE = re.compile(r"(?:你可以|请|可以|只需|下面是|以下是|(?:Here is|You can)\b)", re.I)
_ENTRY_TAG = re.compile(r"<(?:main|section|article|nav|form|button)\b", re.I)
_INTERACTION = re.compile(
    r"addEventListener\s*\(|(?:onClick|onSubmit|onclick|onsubmit)\s*=|v-model\b|@(?:click|submit)\b"
    r"|v-on:(?:click|submit)\b|on:(?:click|submit)\b|\.(?:submit|on)\s*\("
)
_VUE_RENDER_EVENT = re.compile(r"\bon[A-Z][\w$]*\s*:\s*(?!null\b|undefined\b|false\b)\S|\$emit\s*\(")
_VUE_REQUEST = re.compile(r"\b(?:fetch|axios(?:\.(?:request|get|post|put|patch|delete|head|options))?|XMLHttpRequest)\s*\(")
_JS_STRING_OR_COMMENT = re.compile(r'''"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`|//[^\r\n]*|/\*[\s\S]*?\*/''')
_SERIALIZED_EVENT_ARGUMENT = re.compile(r"\bon[a-z]+\s*=\s*['\"][^'\"`\r\n<>]{0,200}\$\{\s*JSON\.stringify\s*\(", re.I)
_SIMPLE_SERIALIZED_EVENT = re.compile(
    r"\b(?P<event>on[a-z]+)=(?P<quote>['\"])(?P<handler>[A-Za-z_$][\w$]*)"
    r"\(\$\{JSON\.stringify\((?P<value>[A-Za-z_$][\w$]*)\)\}\)(?P=quote)",
)
_FLASK_DATABASE_ASSIGNMENT = re.compile(r"(?m)^(?P<target>app\.config\[['\"]SQLALCHEMY_DATABASE_URI['\"]\]\s*=\s*)"
                                        r"(?P<uri>['\"]sqlite:///[^\r\n]+)$")
_TEST_DATABASE_ENV = re.compile(r"\benv\[['\"]([A-Z][A-Z0-9_]*(?:DB_PATH|DATABASE_URL|DATABASE_URI))['\"]\]\s*=")
_COMMAND = re.compile(
    r"\b(?:docker\s+compose\s+(?:up|run)|npm\s+(?:run\s+(?:dev|start|serve)|start)"
    r"|(?:pnpm|yarn)\s+(?:run\s+)?(?:dev|start|serve)|uvicorn\s+\S+|flask\s+run"
    r"|python(?:3)?\s+(?:-m\s+(?!pytest\b|unittest\b)\S+|\S+\.py)|node\s+[^\s-]\S*"
    r"|go\s+run|dotnet\s+run|cargo\s+run|bundle\s+exec)\b", re.I,
)
_ENTRY_COMMAND = re.compile(r"\b(?:python(?:3)?|node|tsx|ts-node)\s+((?:\./)?[\w./-]+\.(?:py|mjs|cjs|js|ts))\b", re.I)
_PACKAGE_COMMAND = re.compile(r"\b(?:npm\s+run|pnpm(?:\s+run)?|yarn(?:\s+run)?)\s+(dev|start|serve)\b", re.I)
_TEST_ASSERTION = re.compile(r"\bassert\s+(?!True\b|true\b|1\s*$)\S|\bassert(?:\.[A-Za-z]+)?\s*\(|\bexpect\s*\(|\bassert[A-Z]\w*\s*\(", re.M)
_SERVER_MARKER = re.compile(
    r"\b(?:FastAPI|Flask|Starlette|APIRouter|HTTPServer|ThreadingHTTPServer|serve_forever|express|Fastify"
    r"|createServer|listen|ServeHTTP|SpringApplication|Controller|urlpatterns)\b"
)
_METHODS = r"get|post|put|patch|delete|head|options"
_METHOD_NAMES = frozenset(_METHODS.split("|"))
_ROUTE_REGISTER = re.compile(rf"\.({_METHODS})\s*\(\s*['\"](/[^'\"\r\n ]*)['\"]", re.I)
_GENERIC_ROUTE = re.compile(r"\.route\s*\(\s*['\"](/[^'\"\r\n ]*)['\"]")
_ROUTE_LITERAL = re.compile(r"['\"](/api(?:/[^'\"\r\n ?#]*)+)['\"]")
_FRONTEND_API = re.compile(r"(?:['\"`])((?:https?://[^'\"`/\s]+)?/api(?:/[^'\"`\r\n ?#]*)+)")
_API_BASE = re.compile(r"\b(?:const|let|var)\s+(\w+)\s*=\s*['\"`](?:https?://[^/\s'\"`]+)?(/api(?:/[^\s'\"`]*)?)['\"`]")
_API_TEMPLATE = re.compile(r"\$\{\s*(\w+)\s*\}(/[^'\"`\r\n ?#]+)")
_JS_FUNCTION = re.compile(r"\bfunction\s+([\w$]+)\s*\(\s*([\w$]+)\s*[,)=]")
_LOCAL_API_BASE = re.compile(
    r"\b(?:const|let|var)\s+([\w$]+)\s*=\s*(['\"\x60])"
    r"(?:https?://(?:localhost|127\.0\.0\.1)(?::\d+)?)?(/api)?/?\2",
)
_LOCAL_WEB_ORIGIN = re.compile(r"['\"\x60]https?://(?:localhost|127\.0\.0\.1):(\d+)", re.I)
_STATIC_SERVER_PORT = re.compile(r"\bpython(?:3)?\s+-m\s+http\.server\s+(\d+)", re.I)
_STATIC_FRONTEND_COMMAND = re.compile(
    r"^(?P<indent>[ \t]*)python3? -m http\.server (?P<port>\d+) +--directory +(?:\./)?frontend[ \t]*(?=\r?$)", re.M,
)
_PYTHON_DISTRIBUTIONS = {
    "fastapi": "fastapi", "flask": "flask", "flask_cors": "flask-cors",
    "flask_sqlalchemy": "flask-sqlalchemy", "httpx": "httpx", "jwt": "pyjwt",
    "pydantic": "pydantic", "requests": "requests", "sqlalchemy": "sqlalchemy", "uvicorn": "uvicorn",
    "werkzeug": "werkzeug",
}
_REQUIREMENT_NAME = re.compile(r"^\s*([A-Za-z][\w.-]*)")
_IMPLICIT_PYTHON_NAMES = frozenset(vars(builtins)) | {
    "__file__", "__cached__", "__spec__", "__annotations__", "__package__", "__loader__", "__path__",
}
_LOCAL_IMPORT = re.compile(r"(?:\bfrom\s+|\bimport\s+(?:\(\s*)?|\brequire\s*\(\s*)['\"](\.{1,2}/[^'\"\r\n]+)['\"]")
_HTML_SOURCE = re.compile(r"<(?:script|link)\b[^>]*\b(?:src|href)\s*=\s*['\"]([^'\"]+)['\"]", re.I)
_VITE_BUILD_COMMAND = re.compile(r"(?:^|&&|\|\||;)\s*vite\s+build(?:\s|$)")
_VUE_3_RANGE = re.compile(r"\s*[~^]?\s*3(?:\.(?:\d+|[xX*])){0,2}\s*")
_VUE_BROWSER_IMPORT = re.compile(
    r"[ \t]*import[ \t]+(?:\{[\w$, \t]+\}|\*[ \t]+as[ \t]+[\w$]+)[ \t]+from[ \t]+"
    r"(?P<literal>(?P<quote>['\"])(?P<url>https://unpkg\.com/vue@3/dist/vue\.esm-browser(?:\.prod)?\.js)(?P=quote))"
    r"[ \t]*;?[ \t]*(?://.*)?",
)
_FLASK_VERSION = re.compile(r"\bflask\s*[\"']?\s*(?:===?|~=|>=)\s*(\d+)\.(\d+)", re.I)
_LOCAL_HTML_START = re.compile(
    r"(?<!避免)(?<!不要)(?<!请勿)(?<!无需)(?<!禁止)直接(?:用|在)浏览器(?:中)?打开\s*[：:]?\s*"
    r"(?:```[^\r\n]*\r?\n|`)?\s*(?:file://|(?:\./)?(?:[\w.-]+/)*[\w.-]+\.html\b)", re.I,
)


class ProjectWorkspaceError(ValueError):
    """项目文件或交付物未通过工作区安全约束。"""


def validate_project_path(path: str) -> str:
    if not isinstance(path, str) or not path or len(path) > 240:
        raise ProjectWorkspaceError("项目路径为空或过长")
    parts = path.split("/")
    for index, piece in enumerate(parts):
        lower = piece.casefold()
        allowed_dotfile = lower in _DOTFILES and index == len(parts) - 1
        if (not _PATH_SEGMENT.fullmatch(piece) or piece.endswith(".") or _DEVICE.fullmatch(piece)
                or (piece.startswith(".") and not allowed_dotfile) or lower in _BLOCKED_NAMES):
            raise ProjectWorkspaceError("项目路径包含非法目录、保留文件名或隐藏配置")
    if len(parts) > 12 or PurePosixPath(path).suffix.casefold() in _BLOCKED_SUFFIXES:
        raise ProjectWorkspaceError("仅允许有界的项目文本源码和配置")
    return path


def validate_project_paths(paths: Iterable[str]) -> list[str]:
    validated = [validate_project_path(path) for path in paths]
    files = {name.casefold() for name in validated}
    aliases: dict[str, str] = {}
    if len(files) != len(validated):
        raise ProjectWorkspaceError("项目文件名存在跨平台冲突")
    for name in validated:
        for parent in PurePosixPath(name).parents:
            path = parent.as_posix()
            folded = path.casefold()
            if folded in files or aliases.setdefault(folded, path) != path:
                raise ProjectWorkspaceError("项目文件路径与目录冲突")
    return validated


def _encode(text: str) -> bytes:
    if not isinstance(text, str) or _CONTROL.search(text) or _SECRET.search(text):
        raise ProjectWorkspaceError("项目文件必须为 UTF-8 文本，且不得包含已识别的凭据")
    try:
        data = text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ProjectWorkspaceError("项目文件无法编码为 UTF-8") from exc
    if len(data) > MAX_PROJECT_FILE_BYTES:
        raise ProjectWorkspaceError("项目单文件超过大小限制")
    return data


def _directory(base: Path, parts: Iterable[str], *, create: bool = False) -> Path:
    for part in parts:
        base = base / part
        if create:
            try:
                base.mkdir()
            except FileExistsError:
                pass
        info = base.lstat()
        is_reparse_point = getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
        if not stat.S_ISDIR(info.st_mode) or is_reparse_point or base.resolve() != base.parent.resolve() / base.name:
            raise ProjectWorkspaceError("项目工作区包含异常目录")
    return base


def _read_regular(path: Path) -> str:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_PROJECT_FILE_BYTES:
        raise ProjectWorkspaceError("项目文件类型或大小不符合限制")
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
        with os.fdopen(fd, "rb") as source:
            opened = os.fstat(source.fileno())
            if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino) or opened.st_nlink != 1:
                raise ProjectWorkspaceError("项目文件在读取时发生变化")
            text = source.read(MAX_PROJECT_FILE_BYTES + 1).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProjectWorkspaceError("项目文件必须为 UTF-8 文本") from exc
    _encode(text)
    return text


def _test_file(path: str) -> bool:
    parts = PurePosixPath(path.casefold()).parts
    name = parts[-1]
    return bool(set(parts[:-1]) & {"test", "tests", "__tests__", "spec", "specs", "e2e"}
                or name.startswith("test_") or ".spec." in name or ".test." in name or name.endswith("_test.go"))


def _layer(path: str, prefixes: set[str]) -> bool:
    return bool(set(PurePosixPath(path.casefold()).parts[:2]) & prefixes)


def _api_path(path: str) -> str:
    result = re.sub(r"^https?://[^/]+", "", path)
    result = re.sub(r"^/api(?=/|$)", "", result).rstrip("/")
    result = re.sub(r"(?:\$?\{[^/{}]+\}|:[\w]+|<[^/<>]+>|\(\\d\+\)|\(\[\^/\]\+\))", "{}", result)
    return result or "/"


def _route_matches(left: str, right: str) -> bool:
    left_parts, right_parts = left.split("/"), right.split("/")
    return len(left_parts) == len(right_parts) and all(a == b or "{}" in (a, b) for a, b in zip(left_parts, right_parts))


def _backend_routes(backend: dict[str, str], trees: dict[str, ast.AST]) -> dict[str, set[str]]:
    routes: dict[str, set[str]] = {}
    for path, content in backend.items():
        for method, route in _ROUTE_REGISTER.findall(content):
            routes.setdefault(_api_path(route), set()).add(method.casefold())
        for declared in (*_GENERIC_ROUTE.findall(content), *_ROUTE_LITERAL.findall(content)):
            routes.setdefault(_api_path(declared), set())
        if path in trees:
            for node in ast.walk(trees[path]):
                if isinstance(node, ast.Call):
                    _ast_route(node, routes)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("do_"):
                    method = node.name[3:].casefold()
                    if method in _METHOD_NAMES:
                        for instruction in ast.walk(node):
                            operands = _route_operands(instruction)
                            for item in operands:
                                if isinstance(item, ast.Constant) and isinstance(item.value, str) and item.value.startswith("/"):
                                    routes.setdefault(_api_path(item.value), set()).add(method)
    return routes


def _route_operands(node: ast.AST) -> list[ast.AST]:
    if isinstance(node, ast.Compare):
        return [node.left, *node.comparators]
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in {"match", "fullmatch"}:
        return node.args[:1]
    return []


def _ast_route(node: ast.Call, routes: dict[str, set[str]]) -> None:
    name = getattr(node.func, "id", getattr(node.func, "attr", ""))
    if name not in {"Route", "add_route", "route", "add_api_route"} or not node.args:
        return
    path = node.args[0]
    if not isinstance(path, ast.Constant) or not isinstance(path.value, str) or not path.value.startswith("/"):
        return
    values = next((keyword.value for keyword in node.keywords if keyword.arg == "methods"), None)
    methods = {"get"} if values is None else set()
    if isinstance(values, (ast.List, ast.Tuple, ast.Set)):
        methods.update(value.value.casefold() for value in values.elts
                       if isinstance(value, ast.Constant) and isinstance(value.value, str))
    routes.setdefault(_api_path(path.value), set()).update(methods & _METHOD_NAMES)


def _test_has_assertion(path: str, content: str, trees: dict[str, ast.AST]) -> bool:
    if path not in trees:
        code = re.sub(r"\b(?:expect|assert)\s*\(\s*(?:true|false|\d+)\s*\)", "", content)
        return bool(_TEST_ASSERTION.search(code))
    for node in ast.walk(trees[path]):
        if isinstance(node, ast.Assert):
            if any(isinstance(item, (ast.Name, ast.Call, ast.Attribute, ast.Subscript)) for item in ast.walk(node.test)):
                return True
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr.startswith("assert"):
            if any(not isinstance(arg, ast.Constant) for arg in node.args):
                return True
    return False


def _undefined_test_names(path: str, content: str, tree: ast.AST) -> list[str]:
    """利用 Python 自身的作用域分析识别测试中缺失的导入和定义。"""
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and any(alias.name == "*" for alias in node.names):
            return []
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {"exec", "globals"}:
            return []
    scopes = [symtable.symtable(content.lstrip("\ufeff"), path, "exec")]
    bound = set(_IMPLICIT_PYTHON_NAMES)
    referenced: set[str] = set()
    while scopes:
        scope = scopes.pop()
        scopes.extend(scope.get_children())
        for symbol in scope.get_symbols():
            if symbol.is_global():
                if symbol.is_assigned() or symbol.is_imported():
                    bound.add(symbol.get_name())
                if symbol.is_referenced():
                    referenced.add(symbol.get_name())
    return sorted(referenced - bound)


def _verified_imports(needed: list[str], modules: Iterable[ast.Module]) -> list[str]:
    """从同一工程的源码复用已声明的顶层导入，歧义留给正常返工。"""
    imports: dict[str, set[str]] = {name: set() for name in needed}
    packages = sys.stdlib_module_names | _PYTHON_DISTRIBUTIONS.keys()
    for tree in modules:
        for node in tree.body:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    package = alias.name.partition(".")[0]
                    if (name := alias.asname or package) in imports and package in packages:
                        imports[name].add(ast.unparse(ast.Import(names=[alias])))
            elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
                if node.module.partition(".")[0] not in packages:
                    continue
                for alias in node.names:
                    if (name := alias.asname or alias.name) in imports:
                        imports[name].add(ast.unparse(ast.ImportFrom(module=node.module, names=[alias], level=0)))
    if all(len(choices) == 1 for choices in imports.values()):
        return sorted({statement for choices in imports.values() for statement in choices})
    return []


def add_verified_imports(path: str, source: str, statements: list[str]) -> str:
    tree = parse_project_source(path, source)
    if not isinstance(tree, ast.Module) or not tree.body:
        raise ProjectWorkspaceError(f"{path} 没有可补齐导入的 Python 源码")
    lines = source.splitlines(keepends=True)
    statements_start = 1 if ast.get_docstring(tree) is not None else 0
    cursor = tree.body[0].end_lineno if statements_start else tree.body[0].lineno - 1
    for node in tree.body[statements_start:]:
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            break
        cursor = node.end_lineno
    updated = "".join([*lines[:cursor], *(f"{statement}\n" for statement in statements), *lines[cursor:]])
    parsed = parse_project_source(path, updated)
    if _undefined_test_names(path, updated, parsed):
        raise ProjectWorkspaceError(f"{path} 的导入补齐后仍有未定义的符号")
    return updated


def _python_dependencies(files: dict[str, str], trees: dict[str, ast.AST]) -> tuple[int, list[tuple[str, str]]]:
    manifests: dict[str, set[str]] = {}
    for path, content in files.items():
        name = PurePosixPath(path).name
        if not (name.startswith("requirements") and name.endswith(".txt")):
            continue
        manifests[path] = {re.sub(r"[-_.]+", "-", match.group(1).casefold())
                           for entry in content.splitlines() if (match := _REQUIREMENT_NAME.match(entry))}
    problems = []
    checked = 0
    for path, tree in trees.items():
        parents = set(PurePosixPath(path).parents)
        applicable = [name for name in manifests if PurePosixPath(name).parent in parents]
        if not applicable:
            continue
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                imported.add(node.module.split(".")[0])
        needed = {_PYTHON_DISTRIBUTIONS[mod] for mod in imported if mod in _PYTHON_DISTRIBUTIONS}
        declared = set.union(*(manifests[name] for name in applicable))
        checked += len(needed)
        if missing := sorted(needed - declared):
            target = max(applicable, key=lambda name: (len(PurePosixPath(name).parts), name.endswith("requirements.txt")))
            problems.append((target, f"{path} 使用了未在启动依赖中声明的 {', '.join(missing)}，请补齐 {target}"))
    return checked, problems


class _VueBlocks(HTMLParser):
    """只识别顶层组件块与可确认的交互入口，具体语法仍由构建器验收。"""

    def __init__(self, source: str):
        super().__init__()
        self.has_component = False
        self.interaction = False
        self.request = False
        self.scripts: list[str] = []
        self.external: set[str] = set()
        self.unmatched_end_tags: list[tuple[str, int]] = []
        self._root = ""
        self._depth = 0
        self._src = ""
        self._body: list[str] = []
        self._has_content = False
        self._malformed = False
        try:
            self.feed(source.lstrip("\ufeff"))
            self.close()
        except (AssertionError, ValueError):
            self._malformed = True

    @property
    def complete(self) -> bool:
        return self.has_component and not self._root and not self._malformed

    def _template_element(self, tag: str, attrs: dict[str, str | None]) -> None:
        self.interaction |= any(bool(value and value.strip()) and (name in {"v-model", "v-on"}
                                or name.startswith(("@", "v-model:", "v-model.", "v-on:")))
                                for name, value in attrs.items())
        self.request |= tag == "form" and bool(attrs.get("action") or attrs.get(":action"))

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        if self._root:
            self._depth += tag == self._root
            self._has_content = True
            if self._root == "template":
                self._template_element(tag, attrs)
        else:
            self._root, self._depth = tag, 1
            self._src = (attrs.get("src") or "").strip()
            self._body, self._has_content = [], False

    def handle_startendtag(self, tag, attributes):
        self.handle_starttag(tag, attributes)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if not self._root:
            self._malformed = True
            self.unmatched_end_tags.append((tag, self.getpos()[0]))
            return
        if tag != self._root:
            return
        self._depth -= 1
        if self._depth:
            return
        if tag in {"template", "script"} and (self._has_content or self._src):
            self.has_component = True
            if self._src:
                self.external.add(self._src)
            if tag == "script":
                script = "".join(self._body)
                script = _JS_STRING_OR_COMMENT.sub(lambda match: " " if match[0].startswith("/") else match[0], script)
                self.scripts.append(script)
                self.interaction |= bool(_INTERACTION.search(script) or _VUE_RENDER_EVENT.search(script))
                self.request |= bool(_VUE_REQUEST.search(script))
        self._root = ""

    def handle_data(self, data):
        if not self._root:
            self._malformed |= bool(data.strip())
        else:
            self._has_content |= bool(data.strip())
            if self._root == "script":
                self._body.append(data)


def _vue_wrapper_line_edit(source: str) -> dict[str, str]:
    tags = _VueBlocks(source).unmatched_end_tags
    if len(tags) != 1 or tags[0][0] != "vue":
        return {}
    lines = source.splitlines(keepends=True)
    index = tags[0][1] - 1
    previous = lines[index].rstrip("\r\n")
    if previous.strip().casefold() != "</vue>" or source.splitlines().count(previous) != 1:
        return {}
    candidate = "".join(lines[:index] + lines[index + 1:])
    return {previous: ""} if _VueBlocks(candidate).complete else {}


class _HTMLScripts(HTMLParser):
    """定位浏览器会忽略或误当作页面内容的脚本边界。"""

    def __init__(self):
        super().__init__()
        self.open_line = 0
        self.problem = ""
        self.module_data: list[tuple[int, str]] = []
        self._inline_module = False
        self._inert_depth = 0

    def handle_starttag(self, tag, attributes):
        if tag == "script":
            self.open_line = self.getpos()[0]
            attrs = dict(attributes)
            self._inline_module = (not self._inert_depth and "src" not in attrs
                                   and (attrs.get("type") or "").strip().casefold() == "module")
        elif tag in {"template", "noscript"}:
            self._inert_depth += 1

    def handle_startendtag(self, tag, attributes):
        if tag == "script" and not self.problem:
            self.problem = f"第 {self.getpos()[0]} 行 script 标签需要显式关闭"

    def handle_endtag(self, tag):
        if tag == "script":
            if not self.open_line and not self.problem:
                self.problem = f"第 {self.getpos()[0]} 行的 </script> 没有对应的开始标签，请检查前面的脚本是否落在标签外"
            self.open_line = 0
            self._inline_module = False
        elif tag in {"template", "noscript"} and self._inert_depth:
            self._inert_depth -= 1

    def handle_data(self, data):
        if self._inline_module:
            self.module_data.append((self.getpos()[0], data))


def vite_vue_entry_repairs(path: str, content: str, manifest: object, *, lock: object = None) -> dict[str, str]:
    """本地已声明同主版本时，将已识别的 Vue 浏览器模块改由 Vite 打包。"""
    if (PurePosixPath(path).name != "index.html" or "unpkg.com/vue@3/dist/vue.esm-browser" not in content
            or not isinstance(manifest, dict) or (lock is not None and not isinstance(lock, dict))):
        return {}
    scripts = manifest.get("scripts")
    if (not isinstance(scripts, dict) or not isinstance(build := scripts.get("build"), str)
            or not _VITE_BUILD_COMMAND.search(build)):
        return {}
    parser = _HTMLScripts()
    try:
        dependencies = dependency_ranges(manifest, lock or {})
        if ("vite" not in dependencies or not (vue := dependencies.get("vue"))
                or not all(_VUE_3_RANGE.fullmatch(item) for item in vue)):
            return {}
        parser.feed(content)
        parser.close()
    except (AssertionError, ValueError):
        return {}
    if parser.problem or parser.open_line:
        return {}
    lines = content.split("\n")
    anchors = Counter(content.splitlines())
    edits = {}
    for start_line, data in parser.module_data:
        literals = {token.span() for token in _JS_STRING_OR_COMMENT.finditer(data) if token[0].startswith(("'", '"'))}
        offset = 0
        for line_number, raw in enumerate(data.split("\n"), start_line):
            line = raw.removesuffix("\r")
            match = _VUE_BROWSER_IMPORT.fullmatch(line)
            if (match and (offset + match.start("literal"), offset + match.end("literal")) in literals
                    and line == lines[line_number - 1].removesuffix("\r") and anchors[line] == 1):
                # 内联模板依赖编译器，因此需要 Vue 的完整打包入口。
                edits[line] = line[:match.start("url")] + "vue/dist/vue.esm-bundler.js" + line[match.end("url"):]
            offset += len(raw) + 1
    return edits


def source_shape_problem(path: str, content: str) -> str:
    """可确认的文件类型错误。写入前和最终验收采用同一规则。"""
    suffix = PurePosixPath(path).suffix.casefold()
    text = content.lstrip("\ufeff").strip()
    if not text:
        return f"{path} 没有内容"
    if suffix in _SERVER_CODE | {".html", ".css", ".json", ".toml", ".md"} and len(text) < 180 and _MODEL_INTRO.match(text):
        return f"{path} 只包含模型自我介绍，需要完整文件内容"
    if suffix == ".html" and not _HTML_ELEMENT.search(text):
        return f"{path} 缺少 HTML 标记，无法提供页面入口"
    if suffix == ".html" and re.search(r"</?script\b", text, re.I):
        scripts = _HTMLScripts()
        try:
            scripts.feed(content.lstrip("\ufeff"))
            scripts.close()
        except (AssertionError, ValueError):
            return f"{path} 的 HTML 标记无法解析"
        if scripts.problem:
            return f"{path} {scripts.problem}"
        if scripts.open_line:
            return f"{path} 第 {scripts.open_line} 行的 script 标签缺少 </script>"
    if suffix in _WEB_CODE | {".html"} and re.search(r"(?m)^(?:<<<<<<< SEARCH|>>>>>>> REPLACE)\b", text):
        return f"{path} 含有未应用的局部编辑块"
    if suffix == ".vue" and not _VueBlocks(text).complete:
        return f"{path} 缺少完整的 Vue 单文件组件，需提供有效的顶层 template 或 script 源码块"
    if suffix in (_WEB_CODE | {".css"}) - {".vue", ".svelte"} and _HTML_DOCUMENT.match(text):
        return f"{path} 误写成了完整 HTML 页面，需要与扩展名一致的源码"
    if suffix in _JAVASCRIPT | {".ts"} and _RAW_HTML_FRAGMENT.match(text):
        return f"{path} 误写成了 HTML 或 Vue 组件片段，需要保留脚本源码和实际入口"
    if (suffix in _SERVER_CODE | {".css", ".json", ".toml"} and len(text) < 400 and _CODE_ADVICE.match(text)
            and re.search(r"复制|粘贴|保存|代码|copy|paste|code", text, re.I) and not re.search(r"(?m)^\s*(?:import |const |def |export )", text)):
        return f"{path} 只收到操作说明，缺少可写入的实际源码"
    return ""


def source_repair_problem(path: str, original: str, updated: str, *, known_paths: Collection[str] | None = None) -> str:
    if known_paths is not None:
        missing_before = set(_local_references(path, original, known_paths))
        added_missing = [ref for ref in _local_references(path, updated, known_paths) if ref not in missing_before]
        if added_missing:
            return f"{path} 的返工新增了工程中不存在的引用 {added_missing[0]}，请使用实际文件或保留现有实现"
    if path.casefold().endswith(".vue") and original:
        before, after = _VueBlocks(original), _VueBlocks(updated)
        if before.complete and not after.complete:
            return "Vue 返工丢失了完整组件。请用原文锚点修改对应位置，保留可运行源码。"
        if before.complete and ((before.interaction and not after.interaction) or (before.request and not after.request)):
            # 允许把行为移交给新引用的组件或实现模块，构建和交互验收会检查移交后的实现。
            old_imports = set(_LOCAL_IMPORT.findall("\n".join(before.scripts)))
            new_imports = set(_LOCAL_IMPORT.findall("\n".join(after.scripts)))
            replacements = (after.external - before.external) | (new_imports - old_imports)
            if not any(not PurePosixPath(ref).suffix or PurePosixPath(ref).suffix in _WEB_CODE for ref in replacements):
                return "Vue 返工移除了原有的用户交互或接口调用。请保留原有行为并只修改本轮涉及的位置。"
    if path.endswith(".html"):
        if _HTML_DOCUMENT.match(original) and not _HTML_DOCUMENT.match(updated):
            return "HTML 返工丢失了完整页面入口。请用原文锚点修改对应位置，保留其他页面结构。"
        if re.search(r"<script\b[^>]*\bsrc\s*=", original, re.I) and not re.search(r"<script\b", updated, re.I):
            return "HTML 返工移除了页面的交互脚本。请保留脚本入口并只修改本轮涉及的页面内容。"
    if path.casefold() != "readme.md" or not original:
        return ""
    if _COMMAND.search(original) and not _COMMAND.search(updated):
        return "README 返工丢失了已有的启动方式。请用原文锚点只更新出错的命令，并保留其余说明。"
    headings = re.compile(r"^#{1,6}[ \t]+(\S[^\r\n]*)", re.M)
    previous = set(headings.findall(original))
    if len(previous) >= 2 and previous.isdisjoint(headings.findall(updated)):
        return "README 返工移除了已有章节。请围绕验收指出的位置提交局部修改。"
    return ""


def _frontend_requests(content: str) -> tuple[set[str], set[str]]:
    literal = _FRONTEND_API.findall(content)
    bases = dict(_API_BASE.findall(content))
    templates = [bases[name].rstrip("/") + path for name, path in _API_TEMPLATE.findall(content) if name in bases]
    request_functions = [r"fetch", r"axios\.(?:get|post|put|patch|delete)"]
    functions = list(_JS_FUNCTION.finditer(content))
    for index, declaration in enumerate(functions):
        name, parameter = map(re.escape, declaration.groups())
        end = functions[index + 1].start() if index + 1 < len(functions) else len(content)
        body = content[declaration.end():min(end, declaration.end() + 3000)]
        template = re.search(rf"\bfetch\s*\(\s*`\$\{{(\w+)\}}\$\{{{parameter}\}}`", body)
        joined = re.search(rf"\bfetch\s*\(\s*(?:(\w+)|['\"](/api(?:/[^'\"]*)?)['\"])\s*\+\s*{parameter}\b", body)
        prefix = bases.get(template.group(1)) if template else None
        if not prefix and joined:
            prefix = joined.group(2) or bases.get(joined.group(1))
        if (template or joined) and not prefix:
            request_functions.append(name)
        if prefix:
            for path in re.findall(rf"\b{name}\s*\(\s*['\"`](/[^'\"`\r\n ?#]+)", content):
                route = re.sub(r"\$\{(?:query|queryString|searchParams)\}$", "", path)
                templates.append(prefix.rstrip("/") + route)
    # 对接口完整性只使用已在 fetch 或实际请求包装函数中出现的明确路径。
    call = rf"\b(?:{'|'.join(request_functions)})\s*\(\s*"
    direct = {route for _, route in re.findall(
        call + r"(['\"\x60])"
        r"((?:https?://(?:localhost|127\.0\.0\.1)(?::\d+)?)?/api/[^'\"\x60\s?#]+)"
        r"(?:[?#][^'\"\x60]*)?\1(?!\s*\+)",
        content,
    )}
    local_prefixes = {name: path for name, _, path in _LOCAL_API_BASE.findall(content)}
    for variable, suffix in re.findall(
        call + r"\x60\$\{\s*([\w$]+)\s*\}(/[^'\x60\s?#]+)(?:[?#][^\x60]*)?\x60(?!\s*\+)", content,
    ):
        if variable in local_prefixes:
            route = local_prefixes[variable] + suffix
            if route.startswith("/api/"):
                direct.add(route)
    return {_api_path(path) for path in (*literal, *templates, *direct)}, direct


def _check_javascript(path: str, source: str) -> bool:
    """Node 只解析 stdin，不载入项目、依赖或项目命令。"""
    executable = shutil.which("node") or shutil.which("nodejs")
    if not executable:
        return False
    modes = {".mjs": ("module",), ".cjs": ("commonjs",)}.get(
        PurePosixPath(path).suffix.casefold(), ("module", "commonjs"),
    )
    errors = []
    for mode in modes:
        try:
            result = subprocess.run(
                [executable, "--check", f"--input-type={mode}"],
                input=source.encode(), capture_output=True, check=False, timeout=3,
                env={key: value for key, value in os.environ.items() if key.upper() in {"SYSTEMROOT", "WINDIR"}},
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        if result.returncode == 0:
            return True
        stderr = result.stderr.decode(errors="replace")
        if "SyntaxError:" not in stderr:
            return False
        errors.append(re.search(r"(?m)^\[stdin\]:(\d+)", stderr))
    line = errors[0]
    raise ProjectWorkspaceError(f"{path} 存在 JavaScript 语法错误" + (f"，第 {line.group(1)} 行" if line else ""))


def parse_project_source(path: str, content: str):
    """解析已支持的源码类型，并给出可用于返工的文件和行号。"""
    suffix = PurePosixPath(path).suffix.casefold()
    if path.casefold() == BROWSER_CHECKS_PATH:
        try:
            return {"version": 1, "scenarios": parse_browser_checks(content)}
        except ValueError as exc:
            raise ProjectWorkspaceError(f"{path} {exc}") from None
    try:
        if suffix == ".py":
            tree = ast.parse(content.lstrip("\ufeff"), filename=path)
            compile(tree, path, "exec", dont_inherit=True)
            return tree
        if suffix == ".json":
            return json.loads(content.lstrip("\ufeff"))
        if suffix == ".toml":
            return tomllib.loads(content.lstrip("\ufeff"))
    except (SyntaxError, ValueError, RecursionError) as exc:
        line = getattr(exc, "lineno", None)
        raise ProjectWorkspaceError(f"{path} 无法解析" + (f"，第 {line} 行" if line else "")) from None
    if suffix in _JAVASCRIPT:
        return _check_javascript(path, content)
    return None


def _frontend_target(files: dict[str, str], paths: Iterable[str], scope: ProjectScope) -> str:
    candidates = [path for path in dict.fromkeys([*files, *paths])
                  if not _test_file(path) and (scope == "frontend" or _layer(path, _FRONTEND))]
    # 无法找到交互或 API 调用时，优先回到脚本和组件的负责人。
    return next((path for path in candidates if PurePosixPath(path).suffix.casefold() in _WEB_CODE),
                next((path for path in candidates if path.lower().endswith(".html")),
                     "frontend/index.html" if scope == "fullstack" else "index.html"))


def _serialized_event_repairs(source: str) -> dict[str, str]:
    if "data-event-payload" in source:
        return {}

    def replace(match: re.Match) -> str:
        handler = f"{match['handler']}(JSON.parse(decodeURIComponent(this.dataset.eventPayload)))"
        payload = "${encodeURIComponent(JSON.stringify(" + match["value"] + "))}"
        return f'{match["event"]}="{handler}" data-event-payload="{payload}"'

    return {line: _SIMPLE_SERIALIZED_EVENT.sub(replace, line) for line in source.splitlines()
            if len(_SIMPLE_SERIALIZED_EVENT.findall(line)) == 1}


class _FormControls(HTMLParser):
    def __init__(self):
        super().__init__()
        self.forms: list[str] = []
        self.unbound: list[tuple[int, str]] = []
        self.classes: dict[str, list[str]] = {}
        self.in_form = False

    def handle_starttag(self, tag, attributes):
        props = dict(attributes)
        if identifier := props.get("id"):
            self.classes[identifier] = (props.get("class") or "").split()
        if tag == "form":
            self.in_form = True
            self.forms.append(props.get("id") or "")
        elif (tag in {"button", "input"} and (props.get("type") or "").casefold() == "submit"
              and not self.in_form and "form" not in props):
            self.unbound.append((self.getpos()[0], self.get_starttag_text()))

    def handle_endtag(self, tag):
        if tag == "form":
            self.in_form = False


def _page_control_repairs(path: str, content: str, frontend: dict[str, str]) -> tuple[list[tuple[str, str]], dict[str, str]]:
    scripts = "\n".join(frontend.get(posixpath.normpath(posixpath.join(posixpath.dirname(path), asset)), "")
                        for asset in _HTML_SOURCE.findall(content))
    parser = _FormControls()
    try:
        parser.feed(content)
    except (AssertionError, ValueError):
        return [], {}
    problems, edits = [], {}
    if len(parser.forms) == 1 and parser.forms[0] and re.search(r"\.addEventListener\(\s*['\"]submit['\"]", scripts):
        form_id = parser.forms[0]
        lines = content.split("\n")
        for number, tag in parser.unbound:
            problems.append((path, f"第 {number} 行提交按钮位于表单外，需要通过 form 属性绑定到 {form_id}。"))
            line = lines[number - 1].rstrip("\r")
            if tag in line and line.count(tag) == 1:
                replacement = re.sub(r"^<(?:button|input)\b", lambda match: match[0] + f' form="{escape(form_id, quote=True)}"',
                                     tag, flags=re.I)
                edits[line] = edits.get(line, line).replace(tag, replacement, 1)

    styles = "\n".join(re.findall(r"<style\b[^>]*>(.*?)</style>", content, re.S | re.I))
    all_styles = styles + "\n" + "\n".join(source for name, source in frontend.items() if name.endswith(".css"))
    bindings = dict(re.findall(r"\b(\w+)\s*:\s*document\.getElementById\(\s*['\"]([\w-]+)['\"]\s*\)", scripts))
    opens = re.findall(r"\b\w+\.(\w+)\.classList\.add\(\s*['\"](open|show|visible)['\"]\s*\)", scripts)
    for binding, state in dict.fromkeys(opens):
        if re.search(rf"\.{state}(?![\w-])", all_styles):
            continue
        element_id = bindings.get(binding, "")
        for style_class in parser.classes.get(element_id, []):
            if not re.fullmatch(r"[\w-]+", style_class):
                continue
            base = re.search(rf"(?m)^(?P<indent>[ \t]*)\.{re.escape(style_class)}[ \t]*\{{[^{{}}\r\n]*"
                             r"\bdisplay\s*:\s*none\b[^{}\r\n]*\}[ \t]*(?=\r?$)", styles)
            if base:
                problems.append((path, f"元素 #{element_id} 添加 {state} 后仍保持隐藏，缺少对应的可见样式。"))
                display = "flex" if "align-items" in base[0] or "justify-content" in base[0] else "block"
                edits[base[0]] = f"{base[0]}\n{base['indent']}.{style_class}.{state} {{ display: {display}; }}"
                break
    return problems, edits


def _shared_origin_repairs(readme: str, files: dict[str, str], backend: dict[str, str],
                           trees: dict[str, ast.AST]) -> tuple[list[tuple[str, str]], dict[str, dict[str, str]]]:
    if "frontend/index.html" not in files:
        return [], {}
    commands = list(_STATIC_FRONTEND_COMMAND.finditer(files.get(readme, "")))
    if len(commands) != 1:
        return [], {}
    command = commands[0]
    port = command["port"]
    for path, source in backend.items():
        if not re.search(rf"\bapp\.run\([^\r\n]*\bport\s*=\s*{port}\b", source):
            continue
        problems = [(readme, f"后端和前端静态服务都使用端口 {port}，按 README 无法同时启动。")]
        edits = {}
        if 'return app.send_static_file("index.html")' not in source:
            problems.append((path, f"后端占用端口 {port}。请让后端提供同源页面与资源，并保留已有 API。"))
            declaration = re.search(r"(?m)^app = Flask\(__name__\)[ \t]*(?=\r?$)", source)
            if not (declaration and path in trees and PurePosixPath(path).parent.as_posix() == "backend"
                    and re.search(r"(?m)^import os[ \t]*$", source) and "/" not in _backend_routes({path: source}, trees)):
                return problems, edits
            edits[path] = {declaration[0]: (
                'app = Flask(__name__, static_folder=os.path.join(os.path.dirname(__file__), "..", "frontend"), static_url_path="")\n\n'
                '@app.get("/")\ndef serve_project_index():\n    return app.send_static_file("index.html")'
            )}
        edits[readme] = {command[0]: f'{command["indent"]}# 后端已提供前端页面，打开 http://localhost:{port}/'}
        return problems, edits
    return [], {}


def _flask_startup_problems(backend: dict[str, str], files: dict[str, str], readme: str) -> list[tuple[str, str]]:
    versions = [version for path, content in files.items()
                if path == readme or PurePosixPath(path).name in {"requirements.txt", "pyproject.toml", "Pipfile"}
                for version in _FLASK_VERSION.findall(content)]
    if not any((int(major), int(minor)) >= (2, 3) for major, minor in versions):
        return []
    return [(path, f"{path} 使用 Flask 2.3 起移除的 before_first_request；请在应用初始化或应用工厂内建表")
            for path, content in backend.items()
            if path.endswith(".py") and re.search(r"\bfrom flask import\b|\bimport flask\b", content)
            and re.search(r"(?m)^\s*@\w+\.before_first_request\b", content)]


def _flask_database_line_repairs(tests: dict[str, str], backend: dict[str, str], trees: dict[str, ast.AST],
                                 issues: list[tuple[str, str]]) -> dict[str, dict[str, str]]:
    applications = [path for path, source in backend.items() if path in trees and "flask_sqlalchemy" in source]
    if len(applications) != 1:
        return {}
    path = applications[0]
    source = backend[path]
    uri_lines = list(_FLASK_DATABASE_ASSIGNMENT.finditer(source))
    names = sorted({name for content in tests.values() for name in _TEST_DATABASE_ENV.findall(content)})
    failing = {file for file, message in issues if "独立的测试数据库" in message or "独立测试库" in message or "切换数据库配置" in message}
    if not failing:
        return {}
    if path in failing and len(uri_lines) == 1 and re.search(r"(?m)^import os\s*$", source):
        match = uri_lines[0]
        initialized = re.search(r"(?m)^\w+\s*=\s*SQLAlchemy\(app\)", source)
        if initialized and match.end() < initialized.start():
            keys = list(dict.fromkeys([*names, "DATABASE_URL"]))
            overrides = [(f'(\"sqlite:///\" + os.environ[\"{key}\"] if os.environ.get(\"{key}\") else None)'
                          if key.endswith("DB_PATH") else f'os.environ.get(\"{key}\")') for key in keys]
            return {path: {match[0]: match["target"] + " or ".join([*overrides, match["uri"]])}}
    if not re.search(r"\bos\.(?:environ\.get|getenv)\(\s*['\"]DATABASE_URL['\"]", source):
        return {}

    repairs = {}
    for test in sorted(failing.intersection(tests, trees)):
        text = tests[test]
        tree = trees[test]
        imports = [node for node in tree.body if isinstance(node, ast.ImportFrom) and node.module
                   and node.module.rsplit(".", 1)[-1] == PurePosixPath(path).stem and node.lineno == node.end_lineno
                   and {alias.name for alias in node.names if not alias.asname} >= {"app", "db"}]
        fixtures = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and any(ast.unparse(decorator).startswith("pytest.fixture")
                            for decorator in node.decorator_list)]
        assignments = []
        for fixture in fixtures:
            nodes = list(ast.walk(fixture))
            uri = [node for node in nodes if isinstance(node, ast.Assign) and node.lineno == node.end_lineno and len(node.targets) == 1
                   and ast.unparse(node.targets[0]) == "app.config['SQLALCHEMY_DATABASE_URI']"]
            schema = [node for node in nodes if isinstance(node, ast.Expr) and ast.unparse(node) == "db.create_all()"]
            if len(uri) == len(schema) == 1 and uri[0].lineno < schema[0].lineno:
                assignments.append((uri[0], schema[0]))
        if len(imports) != 1 or len(assignments) != 1 or not re.search(r"(?m)^import pytest\s*$", text):
            continue
        lines = text.splitlines()
        app_import = lines[imports[0].lineno - 1]
        uri, schema = assignments[0]
        schema_line = lines[schema.lineno - 1]
        indent = schema_line[:len(schema_line) - len(schema_line.lstrip())]
        clear_environment = "".join(f'    database_config.delenv("{name}", raising=False)\n'
                                    for name in names if name != "DATABASE_URL")
        repairs[test] = {
            app_import: ('with pytest.MonkeyPatch.context() as database_config:\n'
                         f'{clear_environment}'
                         '    database_config.setenv("DATABASE_URL", "sqlite:///:memory:")\n'
                         f'    {app_import}'),
            lines[uri.lineno - 1]: "",
            schema_line: (f'{indent}verification_url = db.engine.url\n'
                          f'{indent}if verification_url.get_backend_name() != "sqlite" or verification_url.database not in (None, ":memory:"):\n'
                          f'{indent}    raise RuntimeError("测试必须连接独立内存数据库")\n'
                          f"{indent}db.drop_all()\n{schema_line}"),
        }
    return repairs


def _test_isolation_problems(tests: dict[str, str], backend: dict[str, str]) -> list[tuple[str, str]]:
    problems = []
    flask_sqlalchemy = {path: content for path, content in backend.items() if "flask_sqlalchemy" in content}
    variables = sorted({key for source in flask_sqlalchemy.values()
                        for key in re.findall(r"\bos\.(?:getenv|environ\.get)\(\s*['\"]([A-Z0-9_]+)['\"]", source)
                        if any(term in key for term in ("DB", "DATABASE", "SQLALCHEMY"))})
    for path, content in tests.items():
        if not path.endswith(".py"):
            continue
        imported = re.search(r"(?m)^from [\w.]+ import app\b", content)
        assignments = re.findall(r"(?m)^\s*app\.config\[['\"]SQLALCHEMY_DATABASE_URI['\"]\]\s*=\s*([^\r\n]+)", content)
        memory_uri = re.compile(r"""['"]sqlite:///:memory:['"]\s*(?:#.*)?$""")
        already_isolated = imported and variables and all(memory_uri.fullmatch(value) for value in assignments) and any(
            re.search(rf"""(?m)^os\.environ\[['"]{re.escape(key)}['"]\]\s*=\s*{memory_uri.pattern}""", content[:imported.start()])
            for key in variables
        )
        if flask_sqlalchemy and imported and assignments and not already_isolated and not re.search(r"\b\w+\.init_app\(app\)", content):
            if variables:
                fix = ("在导入应用前绑定数据库变量 " + "、".join(variables)
                       + "，例如设为 sqlite:///:memory:。改动测试模块的应用导入和测试 fixture，取消导入后的 URI 赋值。"
                         "每个用例开始时建表，结束时移除当前会话并清理该独立测试库，保留其余测试和断言。")
            elif any(re.search(r"\bdef\s+create_app\s*\(", source) for source in flask_sqlalchemy.values()):
                fix = "调用 create_app 传入独立库，每次测试重新建表"
            else:
                fix = "先在后端应用初始化阶段提供可注入的独立测试库，再调整测试导入顺序"
                for entry in flask_sqlalchemy:
                    problems.append((entry, f"{entry} 需要在初始化持久化之前读取独立测试库配置，供 {path} 使用"))
            problems.append((path, f"{path} 在导入已初始化应用后才切换数据库配置。{fix}"))
        # 命中直接拼接到项目后端目录的持久化库路径，临时目录内的数据库不受影响。
        project_databases = re.findall(
            r"(?m)^(\w+)\s*=\s*os\.path\.join\(\s*(?:BACKEND|PROJECT)_DIR\s*,\s*['\"][^'\"]+\.(?:db|sqlite3?)['\"]\s*\)",
            content,
        )
        for variable in project_databases:
            if re.search(rf"\bos\.(?:remove|unlink)\(\s*{re.escape(variable)}\s*\)", content):
                problems.append((path, f"{path} 会删除项目目录中的数据库。请为每次验收创建独立临时数据库"))
    return problems


def _flask_integration_problems(backend: dict[str, str], files: dict[str, str], readme: str, scope: ProjectScope) -> list[tuple[str, str]]:
    problems = _flask_startup_problems(backend, files, readme)
    if scope != "fullstack":
        return problems
    database_env = {variable for name, source in files.items() if _test_file(name)
                    for variable in _TEST_DATABASE_ENV.findall(source)}
    web_ports = set(_STATIC_SERVER_PORT.findall(files.get(readme, "")))
    api_ports = {port for path, source in files.items() if _layer(path, _FRONTEND) and path.endswith((".js", ".mjs", ".html"))
                 for port in _LOCAL_WEB_ORIGIN.findall(source)}
    for path, content in backend.items():
        if not path.endswith(".py") or not re.search(r"=\s*(?:flask\.)?Flask\s*\(", content):
            continue
        if (_LOCAL_HTML_START.search(files.get(readme, "")) and not re.search(
            r"send_from_directory|send_file|send_static_file|static_folder\s*=|Access-Control-Allow-Origin|\bCORS\s*\(", content,
        )):
            problems.append((path, f"{path} 缺少前端 HTTP 入口。为已有 HTML 和脚本提供同源访问，同时保留 /api 路由"))
        if web_ports and api_ports and web_ports.isdisjoint(api_ports) and not re.search(
            r"Access-Control-Allow-Origin|\bCORS\s*\(|\bcross_origin\s*\(", content, re.I,
        ):
            problems.append((path, f"{path} 未允许 README 中独立启动的前端跨源访问。只允许实际前端地址，覆盖 JSON 和 Authorization 请求的预检"))
        if "flask_sqlalchemy" not in content:
            continue
        if ignored := sorted(variable for variable in database_env if variable not in content):
            problems.append((path, f"{path} 未读取端到端测试传入的数据库配置 {', '.join(ignored)}。应用初始化时需支持独立的测试数据库"))
    return problems


def _entry_problems(readme: str, files: dict[str, str], parsed_json: dict[str, object], scope: ProjectScope) -> list[tuple[str, str]]:
    content = files.get(readme, "")
    if not _COMMAND.search(content):
        return [(readme, "根目录 README.md 需要列出可执行的安装与启动命令")]
    problems = []
    for entry in dict.fromkeys(_ENTRY_COMMAND.findall(content)):
        expected = entry.removeprefix("./")
        if expected not in files and not any(path.endswith("/" + expected) for path in files):
            problems.append((readme, f"README 引用的入口文件 {entry} 不在下载项目中"))
    packages: dict[str, dict] = {}
    for path, manifest in parsed_json.items():
        if PurePosixPath(path).name == "package.json":
            scripts = manifest.get("scripts") if isinstance(manifest, dict) else None
            if scripts is not None and (not isinstance(scripts, dict) or not all(isinstance(cmd, str) for cmd in scripts.values())):
                problems.append((path, f"{path} 的 scripts 必须是以脚本名称为键的命令对象"))
            elif isinstance(scripts, dict):
                packages[path] = scripts
    for script in sorted({script.casefold() for script in _PACKAGE_COMMAND.findall(content)}):
        if not any(scripts.get(script, "").strip() for scripts in packages.values()):
            problems.append((readme, f"README 中的 {script} 命令没有在 package.json 声明"))
    for path, scripts in packages.items():
        for name in ("dev", "start", "serve"):
            for entry in _ENTRY_COMMAND.findall(scripts.get(name, "")):
                target = posixpath.normpath(posixpath.join(posixpath.dirname(path), entry))
                if target not in files:
                    problems.append((path, f"{path} 的 {name} 脚本引用不存在的文件 {entry}"))
    if (scope == "fullstack" and _LOCAL_HTML_START.search(content)
            and any(path.endswith(".html") and _layer(path, _FRONTEND) for path in files)
            and not any(re.search(r"Access-Control-Allow-Origin|\bCORS\s*\(|\ballow_origins\b", text, re.I)
                        for path, text in files.items() if _layer(path, _BACKEND) and not _test_file(path))):
        problems.append((readme, "README 指示直接打开本地 HTML，但后端未配置跨源访问。请提供可用的 HTTP 启动方式并核对 API 地址"))
    return problems


def _local_references(path: str, content: str, files: Collection[str]) -> list[str]:
    directory = posixpath.dirname(path)
    web_root = path.partition("/")[0] if "/" in path and _layer(path, _FRONTEND) else ""
    references = _LOCAL_IMPORT.findall(content) if PurePosixPath(path).suffix in _WEB_CODE else []
    if path.endswith(".html"):
        references.extend(_HTML_SOURCE.findall(content))
    missing = []
    for ref in dict.fromkeys(references):
        if ref.startswith(("//", "#")) or ":" in ref:
            continue
        local = ref.split("?", 1)[0].split("#", 1)[0]
        target = posixpath.normpath(posixpath.join(web_root if local.startswith("/") else directory, local.lstrip("/")))
        candidates = [target, *(target + ext for ext in _WEB_CODE), *(f"{target}/index{ext}" for ext in _WEB_CODE)]
        # TypeScript 工具链允许用最终 JavaScript 扩展名引用源文件。
        if target.endswith(".js"):
            candidates.extend(target[:-3] + ext for ext in (".ts", ".tsx", ".jsx"))
        if not any(value in files for value in candidates):
            missing.append(ref)
    return missing


def _verify_sources(files: dict[str, str], expected: list[str], scope: ProjectScope) -> dict:
    checks, issues = [], []

    def add(check_id: str, label: str, problems: list[tuple[str, str]], detail: str, *, skipped: bool = False) -> None:
        status = "failed" if problems else "skipped" if skipped else "passed"
        checks.append({"id": check_id, "label": label, "status": status,
                       "detail": problems[0][1] if problems else detail})
        issues.extend({"path": path, "message": message} for path, message in problems)

    missing = [(path, f"未交付计划文件 {path}") for path in expected if path not in files]
    add("planned_files", "计划文件", missing or ([] if files else [("README.md", "工作区还没有项目文件")]), f"已落盘 {len(files)} 份文件")

    parsed_json: dict[str, object] = {}
    trees: dict[str, ast.AST] = {}
    shape_problems = {path: problem for path, content in files.items() if (problem := source_shape_problem(path, content))}
    syntax_problems = list(shape_problems.items())
    javascript_files = [path for path in files if PurePosixPath(path).suffix.casefold() in _JAVASCRIPT]
    javascript_checked = javascript_skipped = 0
    javascript_problems = [(path, shape_problems[path]) for path in javascript_files if path in shape_problems]
    for path, content in files.items():
        if path in shape_problems:
            continue
        suffix = PurePosixPath(path).suffix.casefold()
        try:
            parsed = parse_project_source(path, content)
            if suffix == ".py":
                trees[path] = parsed
            elif suffix == ".json":
                parsed_json[path] = parsed
            elif suffix in _JAVASCRIPT:
                javascript_checked += bool(parsed)
                javascript_skipped += not parsed
        except ProjectWorkspaceError as exc:
            if suffix in _JAVASCRIPT:
                javascript_problems.append((path, str(exc)))
            else:
                syntax_problems.append((path, str(exc)))
    add("source_syntax", "源码格式与语法", syntax_problems,
        "已核对文本源码类型及可识别的 Python、JSON、TOML 语法", skipped=not files)
    if javascript_files:
        detail = ("语法解析器不可用，未确认所有脚本的语法" if javascript_skipped
                  else f"已解析 {javascript_checked} 份 JavaScript 源码")
        add("javascript_syntax", "JavaScript 语法", javascript_problems, detail, skipped=bool(javascript_skipped))
    dependency_count, dependency_problems = _python_dependencies(files, trees)
    if dependency_count:
        add("python_dependencies", "Python 安装依赖", dependency_problems,
            f"已核对 {dependency_count} 处可识别的源码依赖")

    broken_refs = [(path, f"{path} 引用了不存在的本地文件 {ref}")
                   for path, content in files.items() for ref in _local_references(path, content, files)]
    has_web_code = any(PurePosixPath(path).suffix in _WEB_CODE or path.endswith(".html") for path in files)
    add("local_imports", "前端本地引用", broken_refs, "已检查可识别的相对导入及 HTML 入口引用" if has_web_code
        else "本项目未交付可检查的前端源码", skipped=not has_web_code)

    readme = next((path for path in files if path.casefold() == "readme.md"), "README.md")
    add("run_instructions", "启动说明与入口", _entry_problems(readme, files, parsed_json, scope), "README 提供启动命令，已核对可识别的入口")

    source = {path: content for path, content in files.items() if not _test_file(path)}
    frontend = {path: content for path, content in source.items() if scope == "frontend" or _layer(path, _FRONTEND)}
    backend = {path: content for path, content in source.items() if scope == "backend" or _layer(path, _BACKEND)}
    frontend_source = {path: content for path, content in frontend.items()
                       if path not in shape_problems and PurePosixPath(path).suffix.casefold() in _WEB_CODE | {".html"}}
    backend_source = {path: content for path, content in backend.items()
                      if PurePosixPath(path).suffix.casefold() in _SERVER_CODE}
    launch_repairs: dict[str, dict[str, str]] = {}
    page_repairs: dict[str, dict[str, str]] = {}
    if scope == "fullstack":
        startup, launch_repairs = _shared_origin_repairs(readme, files, backend_source, trees)
        if startup:
            add("frontend_startup", "前后端启动方式", startup, "已核对本地端口及页面入口")
    if scope != "backend":
        problems = [(path, message) for path, message in shape_problems.items() if path in frontend]
        problems.extend((path, f"{path} 将 JSON 对象直接拼进 HTML 事件属性，字段引号会截断事件。"
                                "请绑定记录 ID，通过脚本事件监听查找对应对象，并保留现有操作。")
                        for path, content in frontend_source.items() if _SERIALIZED_EVENT_ARGUMENT.search(content))
        for path, content in frontend_source.items():
            if path.endswith(".html"):
                page_issues, edits = _page_control_repairs(path, content, frontend_source)
                problems.extend(page_issues)
                if edits:
                    page_repairs[path] = edits
                directory = PurePosixPath(path).parent
                manifest_path = (directory / "package.json").as_posix()
                lock_path = (directory / "package-lock.json").as_posix()
                if vue_edits := vite_vue_entry_repairs(path, content, parsed_json.get(manifest_path), lock=parsed_json.get(lock_path)):
                    problems.append((path, f"{path} 从外部加载 Vue，隔离浏览器无法访问。请用 {manifest_path} 声明的本地 Vue 3 打包"))
                    page_repairs.setdefault(path, {}).update(vue_edits)
        if not any(_ENTRY_TAG.search(content) for content in frontend_source.values()):
            anchor = next((path for path in [*frontend, *expected] if path.lower().endswith("index.html")),
                          _frontend_target(frontend, expected, scope))
            if not any(path == anchor for path, _ in problems):
                problems.append((anchor, f"{anchor} 及其页面组件缺少实际界面结构"))
        if not any(_INTERACTION.search(content) for content in frontend_source.values()):
            anchor = _frontend_target(frontend, expected, scope)
            if not any(path == anchor for path, _ in problems):
                problems.append((anchor, f"{anchor} 需要实现并绑定页面的用户操作"))
        add("frontend_implementation", "前端界面与交互", problems, "已找到界面结构和交互入口")
    if scope != "frontend":
        # 后端至少有服务入口特征和多行可运行源码，避免把空文件或说明当作实现。
        server = any(_SERVER_MARKER.search(content) for content in backend_source.values())
        lines = sum(sum(bool(line.strip()) and not line.lstrip().startswith(("#", "//"))
                        for line in content.splitlines()) for content in backend_source.values())
        anchor = next(iter(backend_source), "backend/server.py" if scope == "fullstack" else "server.py")
        problems = _flask_integration_problems(backend_source, files, readme, scope)
        if not server or lines < 8:
            problems.append((anchor, "后端需交付实际服务入口与请求处理实现"))
        add("backend_implementation", "后端服务实现", problems, "已找到服务入口和后端实现文件")

    if scope == "fullstack":
        contract_path = next((path for path in files if PurePosixPath(path).name.casefold() == "openapi.json"), "openapi.json")
        contract = parsed_json.get(contract_path)
        declared = contract.get("paths") if isinstance(contract, dict) and isinstance(contract.get("openapi"), str) else None
        documented = {_api_path(path): {key for key in _METHOD_NAMES if isinstance(value.get(key), dict)}
                      for path, value in declared.items() if path.startswith("/") and isinstance(value, dict)} if isinstance(declared, dict) else {}
        documented = {path: methods for path, methods in documented.items() if methods}
        registered = _backend_routes(backend_source, trees)
        requests = {path: _frontend_requests(content) for path, content in frontend_source.items()}
        requested = {path: routes for path, (routes, _) in requests.items() if routes}
        mismatches = []
        for route, methods in sorted(documented.items()):
            implementations = [verbs for path, verbs in registered.items() if _route_matches(route, path)]
            if not implementations:
                mismatches.append(f"没有在后端找到契约接口 {route}")
            elif all(implementations) and (missing := methods - set.union(*implementations)):
                verbs = "、".join(key.upper() for key in sorted(missing))
                mismatches.append(f"后端接口 {route} 缺少契约操作 {verbs}")
        if not documented:
            problems = [(contract_path, "交付 OpenAPI JSON，并为前后端实际接口登记路径与操作")]
        elif mismatches:
            problems = [(next(iter(backend_source), contract_path), message) for message in mismatches]
        elif not any(_route_matches(value, route) for routes in requested.values() for value in routes for route in documented):
            targets = requested.keys() or [_frontend_target(frontend, expected, scope)]
            problems = [(path, f"{path} 找不到调用 OpenAPI 已实现接口的前端请求") for path in targets]
        else:
            problems = [(path, f"{path} 请求了尚未实现的 {route}。请根据现有 API 契约修正调用")
                        for path, (_, direct) in requests.items() for route in sorted(direct)
                        if not any(_route_matches(_api_path(route), reference) for reference in registered)]
        add("api_contract", "前后端接口契约", problems, f"核对了 {len(documented)} 个接口路径及前端调用")

    tests = {path: content for path, content in files.items() if _test_file(path)}
    tested = [path for path, content in tests.items() if _test_has_assertion(path, content, trees)]
    test_problems = _test_isolation_problems(tests, backend_source)
    import_repairs: dict[str, list[str]] = {}
    source_modules = [tree for path, tree in trees.items() if path in source]
    for path, content in tests.items():
        if path in trees and (undefined := _undefined_test_names(path, content, trees[path])):
            test_problems.append((path, f"{path} 引用了未定义的 {', '.join(undefined[:8])}，请补齐实际导入或定义"))
            if imports := _verified_imports(undefined, source_modules):
                import_repairs[path] = imports
    if not tested:
        test_problems.append((next(iter(tests), "tests/"), "需交付包含实际断言的测试源码"))
    add("test_sources", "可执行测试源码", test_problems, f"已找到 {len(tested)} 份带断言的测试文件")
    add("runtime", "运行和功能测试", [], "本次交付执行静态验收，运行与功能测试由下载后的环境执行", skipped=True)
    distinct_issues = list({(issue["path"], issue["message"]): issue for issue in issues}.values())
    line_repairs = _flask_database_line_repairs(tests, backend_source, trees,
                                              [(issue["path"], issue["message"]) for issue in distinct_issues])
    line_repairs.update({path: edits for path in shape_problems if path.casefold().endswith(".vue")
                         and (edits := _vue_wrapper_line_edit(files[path]))})
    line_repairs.update({path: edits for path, content in frontend_source.items()
                         if PurePosixPath(path).suffix in _WEB_CODE and (edits := _serialized_event_repairs(content))})
    for repairs in (launch_repairs, page_repairs):
        for path, edits in repairs.items():
            line_repairs.setdefault(path, {}).update(edits)
    return {"scope": "static", "project_scope": scope, "passed": not issues, "checks": checks, "issues": distinct_issues,
            "file_count": len(files), "expected": expected, "source_digest": _digest(files),
            "import_repairs": import_repairs, "line_repairs": line_repairs}


class ProjectWorkspace:
    def __init__(self, user_id: int, agent_id: int, session_id: int, run_id: str):
        if (type(user_id) is not int or user_id <= 0 or type(agent_id) is not int or agent_id <= 0
                or type(session_id) is not int or session_id <= 0 or not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id)):
            raise ProjectWorkspaceError("项目工作区缺少有效身份")
        self.session_id = session_id
        self.run_id = run_id
        self._base = Path(settings.AGENT_FILES_ROOT).resolve()
        self._relative = PurePosixPath("team-projects", run_id)
        workspace = Path(workspace_root(user_id, agent_id, session_id))
        account_path = workspace.relative_to(Path(os.path.abspath(settings.AGENT_FILES_ROOT)))
        self._components = (*account_path.parts, *self._relative.parts)
        self._lock = threading.RLock()

    def _source(self, *, create: bool = False) -> Path:
        if create:
            self._base.mkdir(parents=True, exist_ok=True)
        return _directory(self._base, (*self._components, "source"), create=create)

    def inspect(self) -> dict[str, str]:
        with self._lock:
            try:
                root = self._source()
            except FileNotFoundError:
                return {}
            files: dict[str, str] = {}
            casefolded: set[str] = set()
            size = 0
            for folder, subdirs, names in os.walk(root, followlinks=False):
                directory = Path(folder)
                for entry in subdirs:
                    _directory(directory, (entry,))
                for name in names:
                    target = directory / name
                    path = validate_project_path(target.relative_to(root).as_posix())
                    if path.casefold() in casefolded or len(files) >= MAX_PROJECT_FILES:
                        raise ProjectWorkspaceError("项目文件数超限或文件名存在跨平台冲突")
                    text = _read_regular(target)
                    size += len(text.encode())
                    if size > MAX_PROJECT_BYTES:
                        raise ProjectWorkspaceError("项目源码超过总大小限制")
                    files[path] = text
                    casefolded.add(path.casefold())
            validate_project_paths(files)
            return dict(sorted(files.items()))

    def read_file(self, path: str) -> str:
        parts = PurePosixPath(validate_project_path(path)).parts
        with self._lock:
            return _read_regular(_directory(self._source(), parts[:-1]) / parts[-1])

    def write_files(self, files: dict[str, str], *, allowed: Iterable[str]) -> list[dict]:
        patterns = {validate_project_path(path) for path in allowed}
        encoded = {validate_project_path(path): _encode(content) for path, content in files.items()}
        if not encoded.keys() <= patterns:
            raise ProjectWorkspaceError("成员只能写入自己的计划文件")
        with self._lock:
            existing = self.inspect()
            resulting = {**existing, **files}
            validate_project_paths(resulting)
            if len(resulting) > MAX_PROJECT_FILES or sum(len(content.encode()) for content in resulting.values()) > MAX_PROJECT_BYTES:
                raise ProjectWorkspaceError("项目源码超过限额")
            root = self._source(create=True)
            staging = _directory(root.parent, ("staging",), create=True)
            for path, data in encoded.items():
                if existing.get(path) == files[path]:
                    continue
                parts = PurePosixPath(path).parts
                target = _directory(root, parts[:-1], create=True) / parts[-1]
                _replace_bytes(target, staging, data)
            return [{"path": path, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                    for path, data in encoded.items()]

    def verify(self, *, expected: Iterable[str] = (), scope: ProjectScope = "fullstack") -> dict:
        if scope not in {"fullstack", "frontend", "backend"}:
            raise ProjectWorkspaceError("不支持的项目范围")
        return _verify_sources(self.inspect(), sorted({validate_project_path(path) for path in expected}), scope)

    @staticmethod
    def matches_snapshot(files: dict[str, str], source_digest: str | None) -> bool:
        return _digest(files) == source_digest

    def publish(self, title: str, verification: dict) -> dict:
        from app.api.v2.artifacts import make_artifact_id

        with self._lock:
            files = self.inspect()
            expected = verification["expected"]
            scope = verification["project_scope"]
            if _digest(files) != verification.get("source_digest"):
                raise ProjectWorkspaceError("项目文件在验收后发生变化，请重新验收")
            actual = _verify_sources(files, expected, scope)
            if runtime := verification.get("runtime_result"):
                actual = merge_runtime_report(actual, runtime, files)
                actual.pop("runtime_result")
            if coverage := verification.get("coverage_result"):
                actual = merge_coverage_report(actual, coverage, files)
            actual.pop("line_repairs", None)
            result = {"title": (title or "团队项目")[:140], "filename": "project.zip", "file_count": len(files),
                      "size": 0, "download_url": "", "state": "passed" if actual["passed"] else "failed",
                      "checks": actual["checks"], "issues": actual["issues"][:60], "verification_scope": actual["scope"]}
            if not files:
                return result
            directory = self._source().parent
            staging = _directory(directory, ("staging",), create=True)
            with tempfile.NamedTemporaryFile(dir=staging, delete=False) as temporary:
                staged = Path(temporary.name)
            try:
                with ZipFile(staged, "w", compression=ZIP_DEFLATED) as archive:
                    for path, content in files.items():
                        _archive_text(archive, path, content)
                    _archive_text(archive, "verification.json", json.dumps(actual, ensure_ascii=False, indent=2))
                os.replace(staged, directory / "project.zip")
            finally:
                staged.unlink(missing_ok=True)
            result["size"] = (directory / "project.zip").stat().st_size
            result["download_url"] = "/api/v2/artifacts/" + make_artifact_id(
                self.session_id, (self._relative / "project.zip").as_posix(),
            )
            return result


def _replace_bytes(target: Path, staging: Path, data: bytes) -> None:
    with tempfile.NamedTemporaryFile(dir=staging, delete=False) as temporary:
        staged = Path(temporary.name)
        try:
            temporary.write(data)
            temporary.flush()
            os.fsync(temporary.fileno())
        except BaseException:
            temporary.close()
            staged.unlink(missing_ok=True)
            raise
    try:
        os.replace(staged, target)
    finally:
        staged.unlink(missing_ok=True)


def _archive_text(archive: ZipFile, path: str, text: str) -> None:
    info = ZipInfo(path)
    info.compress_type = ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | 0o644) << 16
    archive.writestr(info, text.encode())
