from __future__ import annotations

import argparse
import ast
import fnmatch
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import py_compile
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
import zipfile

from . import __version__


CONFIG_NAME = "devgram-builder.json"
STATE_DIR = ".devgrambuilder"
STATE_CONFIG = "config.json"
BUILDS_DIR = "builds"
DEV_SERVER_PORT = 42690
PLUGIN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
MODULE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")
DEFAULT_IGNORES = [
    ".git/**",
    ".idea/**",
    ".vscode/**",
    ".venv/**",
    "venv/**",
    "env/**",
    "builds/**",
    "dist/**",
    "**/__pycache__/**",
    "**/*.pyc",
    "**/*.pyo",
    "**/*.log",
]


class BuilderError(Exception):
    pass


def _json_load(path: Path) -> dict:
    try:
        value = json.loads(path.read_text("utf-8"))
    except FileNotFoundError as error:
        raise BuilderError(f"не найден {path.name}") from error
    except json.JSONDecodeError as error:
        raise BuilderError(f"ошибка JSON в {path}: {error}") from error
    if not isinstance(value, dict):
        raise BuilderError(f"{path.name} должен содержать JSON-объект")
    return value


def _json_write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", "utf-8")


def _project_root(start: Path | None = None) -> Path:
    current = (start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        if (candidate / CONFIG_NAME).is_file():
            return candidate
    raise BuilderError(f"{CONFIG_NAME} не найден; запустите dgb new в каталоге проекта")


def _state(root: Path) -> dict:
    path = root / STATE_DIR / STATE_CONFIG
    if not path.exists():
        value = {
            "source": "src",
            "ignoreAll": list(DEFAULT_IGNORES),
            "optionalAssets": [],
            "compilationIgnore": [],
        }
        _json_write(path, value)
        return value
    value = _json_load(path)
    value.setdefault("source", "src")
    value.setdefault("ignoreAll", list(DEFAULT_IGNORES))
    value.setdefault("optionalAssets", [])
    value.setdefault("compilationIgnore", [])
    return value


def _save_state(root: Path, value: dict) -> None:
    _json_write(root / STATE_DIR / STATE_CONFIG, value)


def _safe_relative(value: str, label: str) -> PurePosixPath:
    value = str(value or "").replace("\\", "/").strip("/")
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts:
        raise BuilderError(f"небезопасный путь {label}: {value!r}")
    return path


def _matches(path: str, patterns: list[str]) -> bool:
    normalized = path.replace("\\", "/")
    return any(fnmatch.fnmatch(normalized, pattern.replace("\\", "/")) for pattern in patterns)


def _normalize_requirement(value: str) -> str:
    value = str(value or "").strip()
    if not value or len(value) > 128 or value.startswith(("-", ".", "/")) or "://" in value:
        return ""
    chars = []
    for char in value:
        if char.isalnum() or char in "._-":
            chars.append(char)
        else:
            break
    return "".join(chars).replace("_", "-").lower()


def _validate_config(config: dict, root: Path, state: dict) -> tuple[Path, PurePosixPath]:
    required = ("id", "name", "version", "author")
    missing = [key for key in required if not str(config.get(key, "")).strip()]
    if missing:
        raise BuilderError("не заполнены поля: " + ", ".join(missing))
    plugin_id = str(config["id"]).strip()
    if not PLUGIN_ID_RE.fullmatch(plugin_id):
        raise BuilderError("id: только латинские буквы, цифры, '.', '_' и '-', максимум 64 символа")
    source = _safe_relative(state.get("source", "src"), "source")
    source_dir = root.joinpath(*source.parts)
    if not source_dir.is_dir():
        raise BuilderError(f"каталог исходников не найден: {source}")
    main = _safe_relative(config.get("main", "main.py"), "main")
    main_path = source_dir.joinpath(*main.parts)
    if not main_path.is_file():
        raise BuilderError(f"точка входа не найдена: {source}/{main}")
    requirements = config.get("requirements", [])
    if requirements is None:
        requirements = []
    if not isinstance(requirements, list) or len(requirements) > 32:
        raise BuilderError("requirements должен быть списком максимум из 32 зависимостей")
    for requirement in requirements:
        if not _normalize_requirement(requirement):
            raise BuilderError(f"небезопасная зависимость: {requirement!r}")
    return source_dir, main


def _iter_tree(base: Path, prefix: str, root: Path, ignore: list[str], optional: list[str], no_assets: bool):
    if not base.exists():
        return
    for path in sorted(base.rglob("*")):
        if path.is_dir():
            continue
        if path.is_symlink():
            raise BuilderError(f"символические ссылки не поддерживаются: {path.relative_to(root)}")
        project_rel = path.relative_to(root).as_posix()
        if path.name == ".gitkeep" or _matches(project_rel, ignore):
            continue
        if no_assets and _matches(project_rel, optional):
            continue
        rel = path.relative_to(base).as_posix()
        archive_name = f"{prefix}/{rel}" if prefix else rel
        yield path, archive_name


def _collect_files(root: Path, config: dict, state: dict, no_assets: bool) -> tuple[dict[str, Path], Path, PurePosixPath]:
    source_dir, main = _validate_config(config, root, state)
    ignore = [str(x) for x in state.get("ignoreAll", [])]
    optional = [str(x) for x in state.get("optionalAssets", [])]
    result: dict[str, Path] = {}
    for path, archive_name in _iter_tree(source_dir, "", root, ignore, optional, no_assets):
        result[archive_name] = path
    for dirname in ("assets", "locales", "wheels"):
        base = root / dirname
        for path, archive_name in _iter_tree(base, dirname, root, ignore, optional, no_assets):
            result[archive_name] = path
    if len(result) + 2 > 4096:
        raise BuilderError("в пакете больше 4096 файлов")
    if main.as_posix() not in result:
        raise BuilderError(f"точка входа исключена ignore-правилом: {main}")
    return result, source_dir, main


def _check_sources(files: dict[str, Path]) -> None:
    for archive_name, path in files.items():
        if archive_name.endswith(".py"):
            try:
                ast.parse(path.read_text("utf-8"), filename=archive_name)
            except (SyntaxError, UnicodeDecodeError) as error:
                raise BuilderError(f"ошибка Python в {archive_name}: {error}") from error
        if archive_name.startswith("locales/") and archive_name.endswith(".json"):
            try:
                locale = json.loads(path.read_text("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as error:
                raise BuilderError(f"ошибка локализации {archive_name}: {error}") from error
            if not isinstance(locale, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in locale.items()):
                raise BuilderError(f"{archive_name}: ожидается JSON-объект строк")


def _python311() -> list[str] | None:
    candidates = [[sys.executable], ["python3.11"], ["python3"], ["python"]]
    if os.name == "nt":
        candidates.insert(1, ["py", "-3.11"])
    seen: set[tuple[str, ...]] = set()
    for command in candidates:
        key = tuple(command)
        if key in seen:
            continue
        seen.add(key)
        try:
            result = subprocess.run(command + ["-c", "import sys;print('%d.%d'%sys.version_info[:2])"],
                                    capture_output=True, text=True, timeout=10)
            if result.returncode == 0 and result.stdout.strip() == "3.11":
                return command
        except (OSError, subprocess.SubprocessError):
            pass
    return None


def _source_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _compile_sources(root: Path, files: dict[str, Path], state: dict, level: int, reset: bool,
                     verbose: bool) -> tuple[dict[str, bytes | Path], dict]:
    command = _python311()
    if command is None:
        raise BuilderError("для -c нужен Python 3.11 (на Windows: py -3.11)")
    cache_dir = root / STATE_DIR / "cache"
    manifest_path = cache_dir / "manifest.json"
    if reset and cache_dir.exists():
        shutil.rmtree(cache_dir)
    cache = _json_load(manifest_path) if manifest_path.exists() else {}
    new_cache: dict[str, dict] = {}
    ignore = [str(x) for x in state.get("compilationIgnore", [])]
    output: dict[str, bytes | Path] = {}
    compiled = cached = 0
    for archive_name, path in files.items():
        project_rel = path.relative_to(root).as_posix()
        if not archive_name.endswith(".py") or _matches(project_rel, ignore):
            output[archive_name] = path
            continue
        digest = _source_hash(path)
        pyc_name = archive_name[:-3] + ".pyc"
        pyc_path = cache_dir.joinpath(*PurePosixPath(pyc_name).parts)
        entry = cache.get(archive_name, {})
        if entry.get("sha256") == digest and entry.get("optimize") == level and pyc_path.is_file():
            cached += 1
        else:
            pyc_path.parent.mkdir(parents=True, exist_ok=True)
            script = (
                "import py_compile;"
                f"py_compile.compile({str(path)!r},cfile={str(pyc_path)!r},"
                f"dfile={archive_name!r},doraise=True,optimize={level})"
            )
            result = subprocess.run(command + ["-c", script], capture_output=True, text=True)
            if result.returncode != 0:
                raise BuilderError(result.stderr.strip() or f"не удалось скомпилировать {archive_name}")
            compiled += 1
        new_cache[archive_name] = {"sha256": digest, "optimize": level, "output": pyc_name}
        output[pyc_name] = pyc_path
        if verbose:
            print(f"  compile: {archive_name} -> {pyc_name}")
    _json_write(manifest_path, new_cache)
    return output, {"compiled": compiled, "cached": cached, "python": "3.11", "level": level}


def _manifest(config: dict, main: str, build_info: dict | None) -> dict:
    result = {
        "id": str(config["id"]).strip(),
        "name": str(config["name"]).strip(),
        "version": str(config["version"]).strip(),
        "author": str(config["author"]).strip(),
        "main": main,
    }
    aliases = {
        "description": "description",
        "icon": "icon",
        "min_app_version": "min_app_version",
        "minAppVersion": "min_app_version",
        "min_devgram": "min_devgram",
        "minDevGram": "min_devgram",
        "min_sdk": "min_sdk",
        "permissions": "permissions",
        "requirements": "requirements",
    }
    for source, target in aliases.items():
        if source in config and target not in result:
            result[target] = config[source]
    if build_info is not None:
        result["devgram_builder"] = build_info
    return result


def _validate_wheels(manifest: dict, files: dict[str, bytes | Path]) -> None:
    wheel_names = [PurePosixPath(name).name.lower().replace("_", "-")
                   for name in files if name.startswith("wheels/") and name.endswith(".whl")]
    for requirement in manifest.get("requirements", []) or []:
        package = _normalize_requirement(requirement)
        if not any(name.startswith(package + "-") for name in wheel_names):
            raise BuilderError(f"для зависимости {package!r} нет wheel в wheels/")


def _zip_write(archive: zipfile.ZipFile, name: str, data: bytes) -> None:
    info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o100644 << 16
    archive.writestr(info, data)


def _increment_stat(root: Path, key: str) -> None:
    path = root / STATE_DIR / "stats.json"
    stats = _json_load(path) if path.exists() else {}
    stats[key] = int(stats.get(key, 0)) + 1
    _json_write(path, stats)


def build_project(args: argparse.Namespace, quiet: bool = False) -> Path:
    root = _project_root()
    config = _json_load(root / CONFIG_NAME)
    state = _state(root)
    files, _source_dir, main = _collect_files(root, config, state, args.no_assets)
    if args.ast or args.compile is not None:
        _check_sources(files)
    compile_info = None
    package_files: dict[str, bytes | Path] = dict(files)
    package_main = main.as_posix()
    if args.compile is not None:
        package_files, compile_info = _compile_sources(root, files, state, args.compile, args.reset, args.verbose)
        if package_main.endswith(".py") and package_main[:-3] + ".pyc" in package_files:
            package_main = package_main[:-3] + ".pyc"
    elif args.reset:
        raise BuilderError("--reset используется только вместе с --compile")

    build_info = None if args.no_info else {
        "version": __version__,
        "compiled": args.compile is not None,
        "python": "3.11" if args.compile is not None else f"{sys.version_info.major}.{sys.version_info.minor}",
    }
    if compile_info:
        build_info.update({"optimize": compile_info["level"]})
    if args.static_version:
        build_info = build_info or {}
        build_info["static_version"] = args.static_version[0]
    if args.static_client:
        build_info = build_info or {}
        build_info["client"] = args.static_client[0]
    manifest = _manifest(config, package_main, build_info)
    _validate_wheels(manifest, package_files)

    if not args.no_folder:
        package_files[f"{STATE_DIR}/{STATE_CONFIG}"] = root / STATE_DIR / STATE_CONFIG
    package_files["manifest.json"] = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")

    suffixes = []
    if args.static_version and len(args.static_version) > 1 and args.static_version[1].lower() == "true":
        suffixes.append(args.static_version[0])
    if args.static_client and len(args.static_client) > 1:
        suffixes.append(args.static_client[1])
    base_name = f"{manifest['id']}-{manifest['version']}"
    if suffixes:
        base_name += "-" + "-".join(re.sub(r"[^A-Za-z0-9._-]+", "-", x) for x in suffixes)
    output = Path(args.output).expanduser().resolve() if args.output else root / BUILDS_DIR / f"{base_name}.dgplugin"
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_suffix(output.suffix + ".tmp")
    try:
        with zipfile.ZipFile(temp, "w") as archive:
            for name in sorted(package_files):
                path = PurePosixPath(name)
                if path.is_absolute() or ".." in path.parts or name.startswith("/"):
                    raise BuilderError(f"небезопасный путь в архиве: {name}")
                value = package_files[name]
                data = value if isinstance(value, bytes) else value.read_bytes()
                _zip_write(archive, name, data)
        temp.replace(output)
        latest = output.parent / "latest.dgplugin"
        if latest.resolve() != output.resolve():
            shutil.copy2(output, latest)
        _increment_stat(root, "successful")
    except Exception:
        temp.unlink(missing_ok=True)
        _increment_stat(root, "failed")
        raise

    if not quiet:
        print(f"Собрано: {output}")
        print(f"Файлов: {len(package_files)}, размер: {output.stat().st_size} байт")
        if compile_info:
            print(f"Python 3.11: {compile_info['compiled']} скомпилировано, {compile_info['cached']} из кэша")
    return output


def _prompt(label: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{label}{suffix}: ").strip()
    return value or default


def _normalize_id(author: str, name: str) -> str:
    author = re.sub(r"[^A-Za-z0-9]+", ".", author.lstrip("@")).strip(".") or "author"
    plugin = re.sub(r"[^A-Za-z0-9]+", ".", name).strip(".") or "plugin"
    return (author + "." + plugin).lower()[:64].rstrip(".")


def new_project(args: argparse.Namespace) -> None:
    root = Path(args.directory or ".").expanduser().resolve()
    if (root / CONFIG_NAME).exists() and not args.force:
        raise BuilderError(f"{CONFIG_NAME} уже существует")
    if args.gen:
        if not args.name or not args.author:
            raise BuilderError("для --gen нужны --name и --author")
        name, author = args.name, args.author
    else:
        print("DevGramBuilder / Новый плагин")
        name = args.name or _prompt("Название", root.name or "My Plugin")
        author = args.author or _prompt("Автор", "@username")
    if args.gen:
        plugin_id = args.plugin_id or _normalize_id(author, name)
        version = args.plugin_version or "1.0.0"
    else:
        plugin_id = args.plugin_id or _prompt("ID", _normalize_id(author, name))
        version = args.plugin_version or _prompt("Версия", "1.0.0")
    description = args.description if args.description is not None else (
        "Описание плагина" if args.gen else _prompt("Описание", "Описание плагина")
    )
    icon = args.icon if args.icon is not None else ("" if args.gen else _prompt("HTTPS-ссылка на иконку", ""))
    root.mkdir(parents=True, exist_ok=True)
    config = {
        "id": plugin_id,
        "name": name,
        "version": version,
        "author": author,
        "description": description,
        "icon": icon,
        "main": "main.py",
        "min_app_version": "12.10.3",
        "min_devgram": "3",
        "requirements": [],
    }
    _json_write(root / CONFIG_NAME, config)
    _save_state(root, {
        "source": "src",
        "ignoreAll": list(DEFAULT_IGNORES),
        "optionalAssets": [],
        "compilationIgnore": [],
    })
    main = f'''from devgram import BasePlugin


class Plugin(BasePlugin):
    id = {plugin_id!r}
    name = {name!r}
    version = {version!r}
    author = {author!r}
    description = {description!r}
    icon = {icon!r}

    def on_load(self):
        self.bulletin(f"{{self.name}} загружен", kind="success")

    def on_unload(self):
        pass
'''
    (root / "src").mkdir(exist_ok=True)
    (root / "src" / "main.py").write_text(main, "utf-8")
    for directory in ("assets", "locales", "wheels"):
        (root / directory).mkdir(exist_ok=True)
        (root / directory / ".gitkeep").touch()
    _json_write(root / "locales" / "ru.json", {"plugin.name": name})
    _json_write(root / "locales" / "en.json", {"plugin.name": name})
    print(f"Проект создан: {root}")
    print("Сборка: dgb build -a -v -nf")


def cached(args: argparse.Namespace) -> None:
    root = _project_root()
    config = _json_load(root / CONFIG_NAME)
    state = _state(root)
    files, _, _ = _collect_files(root, config, state, False)
    manifest_path = root / STATE_DIR / "cache" / "manifest.json"
    cache = _json_load(manifest_path) if manifest_path.exists() else {}
    changed = []
    for name, path in files.items():
        if name.endswith(".py") and cache.get(name, {}).get("sha256") != _source_hash(path):
            changed.append(name)
    deleted = sorted(set(cache) - {name for name in files if name.endswith(".py")})
    if not changed and not deleted:
        print("Кэш актуален")
        return
    for name in sorted(changed):
        print("изменён:", name)
    for name in deleted:
        print("удалён:", name)


def edit_ignore(args: argparse.Namespace, delete: bool) -> None:
    root = _project_root()
    state = _state(root)
    key = {"all": "ignoreAll", "no_assets": "optionalAssets", "compile": "compilationIgnore"}[args.target]
    values = state.setdefault(key, [])
    if delete:
        try:
            index = int(args.value)
            removed = values.pop(index)
        except (ValueError, IndexError):
            raise BuilderError("неверный индекс ignore-правила")
        print("Удалено:", removed)
    else:
        value = args.value.replace("\\", "/")
        if value not in values:
            values.append(value)
        print("Добавлено:", value)
    _save_state(root, state)
    for index, value in enumerate(values):
        print(f"  {index}: {value}")


def stats(args: argparse.Namespace) -> None:
    root = _project_root()
    if args.kind == "builds":
        path = root / STATE_DIR / "stats.json"
        value = _json_load(path) if path.exists() else {}
        print("Успешных сборок:", value.get("successful", 0))
        print("Неудачных сборок:", value.get("failed", 0))
        return
    config = _json_load(root / CONFIG_NAME)
    state = _state(root)
    files, _, _ = _collect_files(root, config, state, False)
    if args.all:
        extra = [root / CONFIG_NAME, root / STATE_DIR / STATE_CONFIG]
        for directory in args.additional or []:
            base = (root / directory).resolve()
            if base.is_dir():
                extra.extend(path for path in base.rglob("*") if path.is_file())
        for path in extra:
            if path.is_file():
                files[path.relative_to(root).as_posix()] = path
    if args.kind == "files":
        counts: dict[str, int] = {}
        for name in files:
            suffix = PurePosixPath(name).suffix.lower() or "без расширения"
            counts[suffix] = counts.get(suffix, 0) + 1
        for suffix, count in sorted(counts.items()):
            print(f"{suffix}: {count}")
        print("Всего:", len(files))
    elif args.kind == "size":
        total = sum(path.stat().st_size for path in files.values())
        print(f"Размер исходников: {total} байт")
    else:
        total = 0
        for name, path in files.items():
            try:
                total += len(path.read_text("utf-8").splitlines())
            except (UnicodeDecodeError, OSError):
                continue
        print(f"Строк: {total}")


def watch(args: argparse.Namespace) -> None:
    build_args = shlex.split(args.build_args)
    parser = make_parser()
    parsed = parser.parse_args(["build", *build_args])
    root = _project_root()

    def snapshot() -> dict[str, tuple[int, int]]:
        result = {}
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(root).as_posix()
            if rel.startswith((BUILDS_DIR + "/", STATE_DIR + "/cache/")) or "/__pycache__/" in "/" + rel:
                continue
            stat = path.stat()
            result[rel] = (stat.st_mtime_ns, stat.st_size)
        return result

    previous = snapshot()
    print(f"Наблюдение запущено ({args.interval} с). Ctrl+C — выход.")
    try:
        while True:
            time.sleep(args.interval)
            current = snapshot()
            if current != previous:
                previous = current
                try:
                    build_project(parsed)
                except BuilderError as error:
                    print("Ошибка сборки:", error, file=sys.stderr)
    except KeyboardInterrupt:
        print("\nНаблюдение остановлено")


def _adb_forward() -> None:
    subprocess.run(["adb", "forward", f"tcp:{DEV_SERVER_PORT}", f"tcp:{DEV_SERVER_PORT}"], check=True)


def _upload(path: Path, token: str) -> str:
    boundary = "----DevGram" + uuid.uuid4().hex
    payload = (
        f"--{boundary}\r\n".encode("ascii")
        + f'Content-Disposition: form-data; name="postData"; filename="{path.name.replace(chr(34), "_")}"\r\n'.encode("utf-8")
        + b"Content-Type: application/octet-stream\r\n\r\n"
        + path.read_bytes()
        + f"\r\n--{boundary}--\r\n".encode("ascii")
    )
    request = urllib.request.Request(
        f"http://127.0.0.1:{DEV_SERVER_PORT}/upload",
        data=payload,
        headers={"X-DevGram-Token": token, "Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8", "replace")


def upload(args: argparse.Namespace) -> None:
    token = args.token or os.environ.get("DEVGRAM_TOKEN", "")
    if not token:
        raise BuilderError("укажите --token или переменную DEVGRAM_TOKEN")
    if args.archive:
        archive = Path(args.archive).expanduser().resolve()
    else:
        build_args = argparse.Namespace(
            no_assets=False, no_folder=True, verbose=args.verbose, reset=False,
            ast=True, compile=None, no_info=False, static_version=None,
            static_client=None, output=None,
        )
        archive = build_project(build_args)
    if not archive.is_file() or archive.suffix != ".dgplugin":
        raise BuilderError(f"архив не найден: {archive}")
    if not args.no_forward:
        _adb_forward()
    print(_upload(archive, token))


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dgb", description="DevGram plugin builder")
    parser.add_argument("--version", action="version", version=f"DevGramBuilder {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    new = commands.add_parser("new", help="создать проект плагина")
    new.add_argument("directory", nargs="?", help="каталог проекта (по умолчанию текущий)")
    new.add_argument("-g", "--gen", action="store_true", help="создать без интерактивных вопросов")
    new.add_argument("-n", "--name", help="название плагина")
    new.add_argument("-a", "--author", help="автор")
    new.add_argument("--id", dest="plugin_id", help="ID плагина")
    new.add_argument("--plugin-version", default=None, help="версия плагина")
    new.add_argument("--description", help="описание")
    new.add_argument("--icon", help="HTTPS-ссылка на иконку")
    new.add_argument("--force", action="store_true", help="перезаписать конфигурацию")
    new.set_defaults(handler=new_project)

    build = commands.add_parser("build", help="собрать .dgplugin")
    build.add_argument("--no-assets", action="store_true", help="исключить optionalAssets")
    build.add_argument("-nf", "--no-folder", action="store_true", help="не добавлять .devgrambuilder")
    build.add_argument("-v", "--verbose", action="store_true", help="подробный вывод")
    build.add_argument("-r", "--reset", action="store_true", help="очистить кэш компиляции")
    build.add_argument("-ni", "--no-info", action="store_true", help="не добавлять сведения о Builder")
    build.add_argument("-sv", "--static-version", nargs="+", metavar=("VERSION", "APPEND"))
    build.add_argument("-sc", "--static-client", nargs="+", metavar=("PACKAGE", "NAME"))
    modes = build.add_mutually_exclusive_group()
    modes.add_argument("-a", "--ast", action="store_true", help="проверить синтаксис Python")
    modes.add_argument("-c", "--compile", nargs="?", type=int, choices=(0, 1, 2), const=1,
                       help="скомпилировать Python 3.11 с уровнем оптимизации 0-2")
    build.add_argument("-o", "--output", help="путь выходного .dgplugin")
    build.set_defaults(handler=build_project)

    cache = commands.add_parser("cached", help="показать изменения относительно кэша")
    cache.set_defaults(handler=cached)

    watcher = commands.add_parser("watch", help="пересобирать при изменениях")
    watcher.add_argument("interval", type=float, help="интервал проверки в секундах")
    watcher.add_argument("-a", "--args", dest="build_args", default="", help="аргументы dgb build")
    watcher.set_defaults(handler=watch)

    for name, delete, help_text in (("add-ignore", False, "добавить ignore-правило"),
                                    ("del-ignore", True, "удалить ignore-правило по индексу")):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("value", help="путь или индекс")
        group = command.add_mutually_exclusive_group(required=True)
        group.add_argument("--all", action="store_const", const="all", dest="target")
        group.add_argument("--no-assets", action="store_const", const="no_assets", dest="target")
        group.add_argument("--compile", action="store_const", const="compile", dest="target")
        command.set_defaults(handler=lambda args, delete=delete: edit_ignore(args, delete))

    statistic = commands.add_parser("stats", help="статистика проекта")
    statistic.add_argument("kind", choices=("builds", "lines", "size", "files"))
    statistic.add_argument("--all", action="store_true", help="учесть конфигурацию и дополнительные каталоги")
    statistic.add_argument("--additional", nargs="*", default=[])
    statistic.set_defaults(handler=stats)

    up = commands.add_parser("upload", help="собрать и загрузить через Dev Server")
    up.add_argument("archive", nargs="?", help="готовый .dgplugin; без него проект будет собран")
    up.add_argument("--token", help="токен Dev Server (или DEVGRAM_TOKEN)")
    up.add_argument("--no-forward", action="store_true", help="не выполнять adb forward")
    up.add_argument("-v", "--verbose", action="store_true")
    up.set_defaults(handler=upload)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = make_parser()
    args = parser.parse_args(argv)
    try:
        result = args.handler(args)
        return int(result) if isinstance(result, int) else 0
    except BuilderError as error:
        print(f"Ошибка: {error}", file=sys.stderr)
        return 1
    except urllib.error.HTTPError as error:
        details = error.read().decode("utf-8", "replace").strip()
        print(f"Ошибка загрузки: HTTP {error.code}" + (f": {details}" if details else ""), file=sys.stderr)
        return 1
    except (OSError, subprocess.SubprocessError, urllib.error.URLError) as error:
        print(f"Ошибка: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
