import hashlib
import csv
import shutil
import os
import stat
import subprocess
import sys
import time
import json
import argparse
import logging
import fnmatch
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
LOG_NAME = "backup_log.csv"          
STATE_NAME = "state.json"            
LOG_FILE_NAME = "backup.log"         
BLOCK_SIZE = 65536                   
DEFAULT_CONFIG_NAME = "config_default.json"
USER_CONFIG_NAME = "config.json"
# Регулярка имени папки копии: 2026-01-31_12-34-56
BACKUP_NAME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}(_\d{2})?$"
)
_CFG_IDENTITY = None


def _read_json_file(path: Path, required: bool) -> dict:
    if not path.is_file():
        if required:
            print(f"ОШИБКА конфигурации: файл не найден: {path}", file=sys.stderr)
            sys.exit(2)
        return {}
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        print(
            f"ОШИБКА конфигурации: {path}: "
            f"строка {e.lineno}, столбец {e.colno}: {e.msg}",
            file=sys.stderr,
        )
        sys.exit(2)
    except OSError as e:
        print(f"ОШИБКА чтения конфигурации: {e}", file=sys.stderr)
        sys.exit(2)

    if not isinstance(data, dict):
        print(
            f"ОШИБКА конфигурации: {path}: корневой элемент должен быть объектом",
            file=sys.stderr,
        )
        sys.exit(2)
    return data

def load_config(path: str) -> dict:
    default_path = BASE_DIR / DEFAULT_CONFIG_NAME
    cfg = _read_json_file(default_path, required=True)

    user_path = Path(path)
    if user_path.is_file() and user_path.resolve() != default_path.resolve():
        user_data = _read_json_file(user_path, required=True)
        for key, value in user_data.items():
            cfg[key] = value

    # Обязательные ключи — проверяем после слияния.
    required_keys = (
        "full_name", "group", "source_dir", "backup_dir",
        "block_size", "exclude_patterns", "keep_copies", "min_free_gb",
    )
    missing = [k for k in required_keys if k not in cfg]
    if missing:
        print(
            f"ОШИБКА конфигурации: в {default_path} отсутствуют ключи: "
            f"{', '.join(missing)}",
            file=sys.stderr,
        )
        sys.exit(2)

    # Приведение относительных путей к абсолютным относительно BASE_DIR.
    for key in ("source_dir", "backup_dir", "passport_path"):
        if key in cfg and cfg[key]:
            p = Path(cfg[key])
            if not p.is_absolute():
                p = BASE_DIR / p
            cfg[key] = str(p.resolve())

    # Типы и значения по умолчанию для необязательных полей.
    cfg.setdefault("passport_path", str((BASE_DIR / "passport.json").resolve()))
    cfg["block_size"] = int(cfg["block_size"])
    cfg["keep_copies"] = int(cfg["keep_copies"])
    cfg["min_free_gb"] = float(cfg["min_free_gb"])
    if not isinstance(cfg["exclude_patterns"], list):
        cfg["exclude_patterns"] = list(cfg["exclude_patterns"])

    return cfg

def get_identity() -> bytes:
    if _CFG_IDENTITY is not None:
        return _CFG_IDENTITY
    cfg = load_config(str(BASE_DIR / USER_CONFIG_NAME))
    return (cfg["full_name"] + "|" + cfg["group"]).encode("utf-8")

def is_hidden(path) -> bool:
    try:
        attrs = os.stat(path).st_file_attributes
    except (AttributeError, OSError):
        return False
    return bool(attrs & stat.FILE_ATTRIBUTE_HIDDEN)

def file_hash(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            block = f.read(BLOCK_SIZE)
            if not block:
                break
            h.update(block)
    #h.update(get_identity())
    return h.hexdigest()

def copy_file(src: str, dst_dir: str) -> str:
    os.makedirs(dst_dir, exist_ok=True)
    dst = os.path.join(dst_dir, os.path.basename(src))
    shutil.copy2(src, dst)
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def write_log_row(log, name, size, copied_at, digest) -> None:
    log.writerow([name, size, copied_at, digest])
LOG_HEADER = ["Имя файла", "Размер", "Время копирования", "Хэш"]

def make_backup() -> str:
    cfg = load_config(str(BASE_DIR / USER_CONFIG_NAME))
    src_dir = cfg["source_dir"]
    bak_root = Path(cfg["backup_dir"])
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    backup_sub = _unique_backup_dir(bak_root, stamp)
    backup_sub.mkdir(parents=True, exist_ok=True)
    log_path = backup_sub / LOG_NAME

    if not os.path.isdir(src_dir):
        print(f"ОШИБКА: исходная папка не найдена: {src_dir}")
        return str(backup_sub)

    files = [
        f for f in os.listdir(src_dir)
        if os.path.isfile(os.path.join(src_dir, f))
    ]

    with open(log_path, "w", newline="", encoding="utf-8-sig") as lf:
        w = csv.writer(lf, delimiter=",")
        w.writerow(LOG_HEADER)

        if not files:
            print("ВНИМАНИЕ: исходная папка пуста, копировать нечего.")
            return str(backup_sub)

        for name in files:
            src = os.path.join(src_dir, name)
            try:
                copied_at = copy_file(src, str(backup_sub))
                size = os.path.getsize(src)
                digest = file_hash(str(backup_sub / name))
                write_log_row(w, name, size, copied_at, digest)
                print(f"OK: {name} -> {digest[:16]}...")
            except Exception as e:
                print(f"ОШИБКА при обработке '{name}': {e}")
                write_log_row(w, name, "-", "-", f"ОШИБКА: {e}")
    return str(backup_sub)

def verify_backup(backup_dir: str) -> bool:
    backup_path = Path(backup_dir)
    log_path = backup_path / LOG_NAME
    if not log_path.is_file():
        print(f"ОШИБКА: журнал не найден: {log_path}")
        return False

    state = load_state(backup_path.parent)

    errors = 0
    total = 0
    with open(log_path, "r", newline="", encoding="utf-8-sig") as lf:
        reader = csv.DictReader(lf, delimiter=",")
        for row in reader:
            name = row.get("Имя файла")
            expected = row.get("Хэш", "")
            if not name:
                continue
            if expected.startswith("ПРОПУЩЕН"):
                print(f"SKIP: {name} (скрытый)")
                continue
            total += 1

            copy_path = backup_path / name
            if not copy_path.is_file():
                st = state.get("files", {}).get(name)
                if st and st.get("path"):
                    alt = backup_path.parent / st["path"]
                    if alt.is_file():
                        copy_path = alt
            if not copy_path.is_file():
                print(f"FAIL: {name} — копия отсутствует")
                errors += 1
                continue

            try:
                actual = file_hash(str(copy_path))
            except Exception as e:
                print(f"FAIL: {name} — ошибка чтения: {e}")
                errors += 1
                continue

            if actual == expected:
                print(f"OK: {name}")
            else:
                print(f"FAIL: {name} — хэш не совпадает")
                errors += 1

    print(f"\nИтог: проверено {total}, ошибок {errors}")
    return errors == 0

def parse_args():
    parser = argparse.ArgumentParser(
        prog="backup_tool.py",
        description=(
            "Утилита резервного копирования с контролем целостности "
            "(вариант 2, дизайн)."
        ),
        epilog=(
            "Коды завершения:\n"
            "  0 — успех\n"
            "  1 — проверка нашла повреждения\n"
            "  2 — ошибка конфигурации или аргументов\n"
            "  3 — часть файлов не удалось скопировать"
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument(
        "--config",
        default=str(BASE_DIR / USER_CONFIG_NAME),
        help="путь к config.json (по умолчанию: ./config.json)",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="подробный вывод в консоль",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="ничего не менять, только показать план",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("backup", help="создать резервную копию (инкрементально)")

    sp_v = sub.add_parser("verify", help="проверить целостность копии")
    sp_v.add_argument(
        "backup_dir", nargs="?", default=None,
        help="папка копии (по умолчанию — последняя)",
    )

    sp_r = sub.add_parser("report", help="отчёт по последней копии")
    sp_r.add_argument(
        "--format", choices=["txt", "html"], default="txt",
        help="формат отчёта (по умолчанию: txt)",
    )

    sub.add_parser("clean", help="удалить старые копии (оставить N последних)")

    return parser.parse_args()

def setup_logging(verbose):
    logger = logging.getLogger("backup")
    logger.setLevel(logging.DEBUG)

    for handler in list(logger.handlers):
        try:
            handler.close()
        finally:
            logger.removeHandler(handler)

    log_path = BASE_DIR / LOG_FILE_NAME

    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.touch(exist_ok=True)

    fh = logging.FileHandler(log_path, mode="a", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
    ))
    logger.addHandler(fh)

    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO if verbose else logging.WARNING)
    ch.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    logger.addHandler(ch)

    logger.info("Логирование инициализировано, файл: %s", log_path)
    return logger

def is_excluded(rel_path, patterns) -> bool:
    rel = str(rel_path).replace("\\", "/")
    name = rel.rsplit("/", 1)[-1]
    for pat in patterns:
        pat_norm = str(pat).replace("\\", "/")
        if fnmatch.fnmatch(name, pat_norm):
            return True
        if fnmatch.fnmatch(rel, pat_norm):
            return True
        if "/" not in pat_norm:
            for part in rel.split("/"):
                if fnmatch.fnmatch(part, pat_norm):
                    return True
    return False

def load_state(backups_dir) -> dict:
    path = Path(backups_dir) / STATE_NAME
    if not path.is_file():
        return {"files": {}}
    try:
        with open(path, "r", encoding="utf-8") as f:
            state = json.load(f)
        if not isinstance(state, dict) or "files" not in state:
            return {"files": {}}
        return state
    except (OSError, json.JSONDecodeError) as e:
        print(f"state.json повреждён ({e}), начинаю с пустого состояния")
        return {"files": {}}

def save_state(backups_dir, state) -> None:
    path = Path(backups_dir) / STATE_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def needs_copy(rel_path, digest, state) -> bool:
    prev = state.get("files", {}).get(rel_path)
    return prev is None or prev.get("hash") != digest

def _unique_backup_dir(bak_root: Path, stamp: str) -> Path:
    """Вернуть несуществующий путь bak_root/<stamp>[_NN]."""
    candidate = bak_root / stamp
    if not candidate.exists():
        return candidate
    i = 1
    while True:
        candidate = bak_root / f"{stamp}_{i:02d}"
        if not candidate.exists():
            return candidate
        i += 1


def run_backup(cfg: dict, dry_run: bool, logger: logging.Logger) -> int:
    src_root = Path(cfg["source_dir"])
    bak_root = Path(cfg["backup_dir"])
    patterns = cfg.get("exclude_patterns", [])

    if not src_root.is_dir():
        logger.error("Исходная папка не найдена: %s", src_root)
        return 3

    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    # Защита от коллизии: два запуска в одну секунду не должны
    # перезаписывать друг друга.
    backup_sub = _unique_backup_dir(bak_root, stamp)
    log_path = backup_sub / LOG_NAME

    state = load_state(bak_root)
    state_files = state.setdefault("files", {})

    stats = {
        "copied": 0, "unchanged": 0, "hidden": 0,
        "excluded": 0, "errors": 0, "bytes": 0,
        "files": [], "started_at": time.monotonic(),
        "backup_dir": str(backup_sub),
    }

    files = []
    for root, _dirs, names in os.walk(src_root):
        for n in names:
            full = Path(root) / n
            rel = full.relative_to(src_root).as_posix()
            files.append((full, rel))

    if not files:
        logger.warning("Исходная папка пуста: %s", src_root)
        if not dry_run:
            backup_sub.mkdir(parents=True, exist_ok=True)
            with open(log_path, "w", newline="", encoding="utf-8-sig") as lf:
                csv.writer(lf, delimiter=",").writerow(LOG_HEADER)
        return 0

    if dry_run:
        logger.info("DRY-RUN: план для %d файлов", len(files))

    if not dry_run:
        backup_sub.mkdir(parents=True, exist_ok=True)

    log_fh = None
    writer = None
    if not dry_run:
        log_fh = open(log_path, "w", newline="", encoding="utf-8-sig")
        writer = csv.writer(log_fh, delimiter=",")
        writer.writerow(LOG_HEADER)

    try:
        for full, rel in files:
            try:
                if is_excluded(rel, patterns):
                    stats["excluded"] += 1
                    logger.info("Исключён: %s", rel)
                    if dry_run:
                        print(f"[DRY] исключён: {rel}")
                    continue

                if is_hidden(str(full)):
                    stats["hidden"] += 1
                    logger.info("Пропуск (скрытый): %s", rel)
                    if dry_run:
                        print(f"[DRY] скрытый (пропуск): {rel}")
                    elif writer:
                        writer.writerow(
                            [rel, full.stat().st_size, "-",
                             "ПРОПУЩЕН (скрытый)"]
                        )
                    continue

                src_hash = file_hash(str(full))
                dst = backup_sub / rel

                if not dry_run and needs_copy(rel, src_hash, state):
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    t0 = time.monotonic()
                    copied_at = copy_file(str(full), str(dst.parent))
                    dur = time.monotonic() - t0
                    logger.info("Скопирован %s за %.3f c", rel, dur)

                    copy_hash = file_hash(str(dst))
                    if writer:
                        writer.writerow(
                            [rel, full.stat().st_size, copied_at, copy_hash]
                        )

                    state_files[rel] = {
                        "hash": src_hash,
                        "path": str(dst.relative_to(bak_root)),
                    }
                    stats["copied"] += 1
                    stats["bytes"] += full.stat().st_size
                    stats["files"].append({
                        "rel": rel, "size": full.stat().st_size,
                        "hash": src_hash, "copied": True,
                    })
                    print(f"OK: {rel}")
                else:
                    stats["unchanged"] += 1
                    if dry_run:
                        print(f"[DRY] без изменений (был бы пропущен): {rel}")
                        stats["files"].append({
                            "rel": rel, "size": full.stat().st_size,
                            "hash": src_hash, "copied": False,
                        })
                        continue

                    # Гарантируем, что файл присутствует в НОВОЙ папке копии.
                    # Если dst уже существует и корректен — ничего не делаем.
                    # Если dst отсутствует — пытаемся hard link на prev_path,
                    # но в ЛЮБОМ случае падения копируем ИЗ ИСТОЧНИКА.
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    prev = state_files.get(rel, {}) or {}
                    prev_path = (
                        bak_root / prev["path"] if prev.get("path") else None
                    )

                    already_ok = False
                    if dst.is_file():
                        try:
                            already_ok = file_hash(str(dst)) == src_hash
                        except OSError:
                            already_ok = False

                    if not already_ok:
                        linked = False
                        if (prev_path is not None
                                and prev_path.is_file()
                                and not _same_file(prev_path, dst)):
                            try:
                                os.link(prev_path, dst)
                                linked = True
                            except OSError:
                                linked = False
                        if not linked:
                            # Копируем из ИСТОЧНИКА — он точно существует
                            # и не совпадает с dst.
                            try:
                                shutil.copy2(str(full), str(dst))
                            except Exception as e:
                                raise RuntimeError(
                                    "не удалось материализовать "
                                    f"неизменённый файл: {e}"
                                )

                    copy_hash = file_hash(str(dst))
                    if writer:
                        writer.writerow([
                            rel, full.stat().st_size,
                            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            copy_hash,
                        ])

                    state_files[rel] = {
                        "hash": src_hash,
                        "path": str(dst.relative_to(bak_root)),
                    }
                    stats["files"].append({
                        "rel": rel, "size": full.stat().st_size,
                        "hash": src_hash, "copied": False,
                    })
                    print(f"=  {rel} (без изменений)")
            except Exception as e:
                stats["errors"] += 1
                logger.error("Ошибка при обработке %s: %s", rel, e,
                             exc_info=True)
                if not dry_run and writer:
                    writer.writerow([rel, "-", "-", f"ОШИБКА: {e}"])
    finally:
        if log_fh:
            log_fh.close()

    if not dry_run:
        save_state(bak_root, state)

    stats["duration"] = time.monotonic() - stats["started_at"]
    stats["mb_per_s"] = (
        stats["bytes"] / 1048576.0 / stats["duration"]
        if stats["duration"] > 0 else 0.0
    )
    stats["finished_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    print(
        f"\nИтог: скопировано {stats['copied']}, "
        f"без изменений {stats['unchanged']}, "
        f"скрытых {stats['hidden']}, исключено {stats['excluded']}, "
        f"ошибок {stats['errors']}; "
        f"{stats['bytes'] / 1048576:.1f} МБ за {stats['duration']:.2f} c "
        f"({stats['mb_per_s']:.2f} МБ/с)"
    )

    if dry_run:
        return 0
    return 3 if stats["errors"] else 0


def _same_file(a: Path, b: Path) -> bool:
    """True, если a и b — один и тот же файл (или оба не существуют)."""
    try:
        return os.path.samefile(str(a), str(b))
    except (OSError, FileNotFoundError):
        return False

def clean_old(cfg: dict, dry_run: bool, logger: logging.Logger) -> int:
    bak_root = Path(cfg["backup_dir"]).resolve()
    keep = int(cfg["keep_copies"])
    if not bak_root.is_dir():
        logger.warning("Папка копий не найдена: %s", bak_root)
        return 0

    dirs = []
    for p in bak_root.iterdir():
        if not p.is_dir():
            continue
        if not BACKUP_NAME_RE.match(p.name):
            continue
        try:
            p.resolve().relative_to(bak_root)
        except ValueError:
            logger.warning("Пропущена папка вне backup_dir: %s", p)
            continue
        dirs.append(p)

    dirs.sort(key=lambda p: p.name)
    to_delete = dirs[:-keep] if keep > 0 else dirs

    if not to_delete:
        print("Нечего удалять.")
        return 0

    print("К удалению:")
    for p in to_delete:
        print(f"  {p}")
    if dry_run:
        print("[DRY-RUN] Удаление пропущено.")
        return 0

    ans = input("Удалить перечисленные папки? [y/N]: ").strip().lower()
    if ans not in ("y", "yes", "д", "да"):
        print("Отмена.")
        return 0

    for p in to_delete:
        try:
            shutil.rmtree(p)
            logger.info("Удалена старая копия: %s", p)
            print(f"Удалено: {p}")
        except OSError as e:
            logger.error("Не удалось удалить %s: %s", p, e, exc_info=True)
            print(f"Ошибка удаления {p}: {e}")
    return 0

def find_duplicates(entries):
    groups = defaultdict(list)
    for e in entries:
        groups[e["hash"]].append(e["rel"])
    return {h: names for h, names in groups.items() if len(names) > 1}

def check_disk_space(cfg: dict, passport_path: str,
                     logger: logging.Logger) -> bool:
    try:
        total = 0
        for root, _dirs, names in os.walk(cfg["source_dir"]):
            for n in names:
                total += (Path(root) / n).stat().st_size
    except OSError:
        total = 0

    free_gb = shutil.disk_usage(cfg["source_dir"]).free / (1024 ** 3)
    need_gb = (total * 2) / (1024 ** 3)
    min_free = float(cfg.get("min_free_gb", 0))

    warnings = []
    if free_gb < need_gb:
        warnings.append(
            f"свободно {free_gb:.1f} ГБ < удвоенного объёма данных "
            f"{need_gb:.1f} ГБ"
        )
    if free_gb < min_free:
        warnings.append(
            f"свободно {free_gb:.1f} ГБ < порога {min_free:.1f} ГБ"
        )

    # Читаем passport.json, если он есть (R15).
    try:
        with open(passport_path, "r", encoding="utf-8-sig") as f:
            json.load(f)
    except (OSError, json.JSONDecodeError):
        logger.warning(
            "passport.json не найден или повреждён — пропускаю сверку"
        )

    for w in warnings:
        logger.warning("R15: %s", w)
        print(f"ВНИМАНИЕ (R15): {w}")
    return not warnings

def build_report(cfg: dict, stats: dict, fmt: str = "txt",
                 logger: logging.Logger | None = None) -> int:
    dups = find_duplicates(stats.get("files", []))
    top3 = sorted(
        stats.get("files", []),
        key=lambda x: x["size"], reverse=True,
    )[:3]

    lines = [
        "ОТЧЁТ О РЕЗЕРВНОМ КОПИРОВАНИИ",
        "=" * 40,
        f"Папка копии: {stats.get('backup_dir')}",
        f"Завершено: {stats.get('finished_at')}  "
        f"Длительность: {stats.get('duration', 0):.2f} c",
        f"Скорость: {stats.get('mb_per_s', 0):.2f} МБ/с",
        "",
        "Счётчики:",
        f"  скопировано:     {stats.get('copied', 0)}",
        f"  без изменений:   {stats.get('unchanged', 0)}",
        f"  скрытых:         {stats.get('hidden', 0)}",
        f"  исключено:       {stats.get('excluded', 0)}",
        f"  с ошибками:      {stats.get('errors', 0)}",
        f"  общий объём:     {stats.get('bytes', 0) / 1048576:.1f} МБ",
        "",
        "Три самых больших файла:",
    ]
    for f in top3:
        lines.append(f"  {f['rel']} — {f['size'] / 1048576:.2f} МБ")
    lines.append("")
    lines.append("Группы дубликатов (одинаковый хэш):")
    if dups:
        for h, names in dups.items():
            lines.append(f"  {h[:16]}...: {', '.join(names)}")
    else:
        lines.append("  дубликатов не найдено")

    if fmt == "html":
        html = [
            "<!DOCTYPE html><html lang='ru'><head>"
            "<meta charset='utf-8'><title>Отчёт backup</title></head><body>",
            "<pre>",
            "\n".join(lines),
            "</pre></body></html>",
        ]
        out_name = "report.html"
        with open(BASE_DIR / out_name, "w", encoding="utf-8") as f:
            f.write("\n".join(html))
    else:
        out_name = "report.txt"
        with open(BASE_DIR / out_name, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

    print(f"Отчёт сохранён: {BASE_DIR / out_name}")
    if logger:
        logger.info("Отчёт сохранён: %s", BASE_DIR / out_name)
    return 0

def _last_backup_dir(cfg: dict):
    root = Path(cfg["backup_dir"])
    if not root.is_dir():
        return None
    dirs = [
        p for p in root.iterdir()
        if p.is_dir() and BACKUP_NAME_RE.match(p.name)
    ]
    return str(max(dirs)) if dirs else None

def _collect_stats_for(backup_dir: str) -> dict:

    log_path = Path(backup_dir) / LOG_NAME
    files, total = [], 0
    if log_path.is_file():
        with open(log_path, "r", newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f, delimiter=","):
                h = row.get("Хэш", "") or ""
                if h.startswith("ПРОПУЩЕН") or h.startswith("ОШИБКА"):
                    continue
                raw_size = row.get("Размер") or "0"
                sz = int(raw_size) if raw_size.isdigit() else 0
                total += sz
                files.append({
                    "rel": row["Имя файла"], "size": sz, "hash": h,
                })
    return {
        "backup_dir": backup_dir, "files": files, "bytes": total,
        "copied": len(files), "unchanged": 0, "hidden": 0,
        "excluded": 0, "errors": 0, "duration": 0.0, "mb_per_s": 0.0,
        "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

def main() -> int:
    args = parse_args()
    logger = setup_logging(args.verbose)
    try:
        if args.command == "backup":
            cfg = load_config(args.config)
            return run_backup(cfg, args.dry_run, logger)

        if args.command == "verify":
            cfg = load_config(args.config)
            target = args.backup_dir or _last_backup_dir(cfg)
            if not target:
                print("ОШИБКА: копии не найдены", file=sys.stderr)
                return 2
            ok = verify_backup(target)
            return 0 if ok else 1

        if args.command == "report":
            cfg = load_config(args.config)
            target = _last_backup_dir(cfg)
            if not target:
                print("ОШИБКА: копии не найдены", file=sys.stderr)
                return 2
            stats = _collect_stats_for(target)
            return build_report(cfg, stats, args.format, logger)

        if args.command == "clean":
            cfg = load_config(args.config)
            return clean_old(cfg, args.dry_run, logger)

        print(f"ОШИБКА: неизвестная команда {args.command}", file=sys.stderr)
        return 2
    except SystemExit:
        raise
    except Exception:
        logger.exception("Необработанная ошибка")
        print("КРИТИЧЕСКАЯ ОШИБКА — подробности в backup.log", file=sys.stderr)
        return 3

if __name__ == "__main__":
    sys.exit(main())