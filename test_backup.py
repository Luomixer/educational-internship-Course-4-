"""
Автотесты для утилиты резервного копирования.
Запуск: python -m unittest test_backup -v
Используется только стандартная библиотека.
"""

import csv
import json
import logging
import os
import sys
import tempfile
import unittest
import time
from pathlib import Path
from unittest import mock
import backup_tool as bt


# --------------------------------------------------------------------------- #
# Вспомогательные функции
# --------------------------------------------------------------------------- #

def make_config(tmp_root: Path, **overrides) -> dict:
    cfg = {
        "full_name": "Иванов Иван Иванович",
        "group": "ИУ7-11Б",
        "source_dir": str(tmp_root / "src"),
        "backup_dir": str(tmp_root / "backups"),
        "block_size": 4096,
        "exclude_patterns": [],
        "keep_copies": 3,
        "min_free_gb": 0.0,
        "passport_path": str(tmp_root / "passport.json"),
    }
    cfg.update(overrides)
    return cfg


def write_config(path: Path, cfg: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def read_log_rows(backup_sub: Path):
    log_path = backup_sub / bt.LOG_NAME
    with open(log_path, "r", newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f, delimiter=","))


def backup_dirs(cfg: dict):
    """Только папки копий с корректным именем (исключая state.json)."""
    root = Path(cfg["backup_dir"])
    if not root.is_dir():
        return []
    return sorted(
        p for p in root.iterdir()
        if p.is_dir() and bt.BACKUP_NAME_RE.match(p.name)
    )


def quiet_logger():
    lg = logging.getLogger("test_backup_quiet")
    lg.handlers.clear()
    lg.addHandler(logging.NullHandler())
    lg.setLevel(logging.CRITICAL)
    lg.propagate = False
    return lg


# --------------------------------------------------------------------------- #
# Базовый класс
# --------------------------------------------------------------------------- #

class BackupTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_root = Path(self._tmp.name)
        (self.tmp_root / "src").mkdir()
        (self.tmp_root / "backups").mkdir()

        self._orig_base = bt.BASE_DIR
        self._orig_identity = bt._CFG_IDENTITY
        bt.BASE_DIR = self.tmp_root
        bt._CFG_IDENTITY = None

        self.cfg = make_config(self.tmp_root)
        write_config(self.tmp_root / bt.USER_CONFIG_NAME, self.cfg)
        write_config(self.tmp_root / bt.DEFAULT_CONFIG_NAME, {})

        self.logger = quiet_logger()

    def tearDown(self):
        bt.BASE_DIR = self._orig_base
        bt._CFG_IDENTITY = self._orig_identity
        self._tmp.cleanup()

    def make_backup_dirs(self, count: int):
        root = Path(self.cfg["backup_dir"])
        dirs = []
        for i in range(count):
            name = f"2026-01-{i + 1:02d}_00-00-00"
            p = root / name
            p.mkdir(parents=True, exist_ok=True)
            dirs.append(p)
        return dirs


# --------------------------------------------------------------------------- #
# 1. Хэш с ФИО отличается от обычного
# --------------------------------------------------------------------------- #

class TestIdentityHash(BackupTestBase):
    def test_identity_hash_differs_from_plain(self):
        f = self.tmp_root / "sample.bin"
        f.write_bytes(b"hello backup world")

        plain = bt.file_hash(str(f))

        # identity читаем из конфига (кэш ещё пуст в этом тесте)
        identity = bt.get_identity()
        self.assertEqual(identity,
                         "Иванов Иван Иванович|ИУ7-11Б".encode("utf-8"))

        import hashlib
        h = hashlib.sha256()
        with open(f, "rb") as fh:
            while True:
                block = fh.read(bt.BLOCK_SIZE)
                if not block:
                    break
                h.update(block)
        h.update(identity)
        with_identity = h.hexdigest()

        self.assertNotEqual(plain, with_identity,
                            "хэш с ФИО должен отличаться от обычного")

# --------------------------------------------------------------------------- #
# 2. Кириллические имена и пробелы
# --------------------------------------------------------------------------- #

class TestCyrillicAndSpaces(BackupTestBase):
    def test_cyrillic_names_and_spaces_roundtrip(self):
        src = Path(self.cfg["source_dir"])
        name = "документ с пробелами.txt"
        content = "Привет, мир!\n".encode("utf-8")
        (src / name).write_bytes(content)

        rc = bt.run_backup(self.cfg, dry_run=False, logger=self.logger)
        self.assertEqual(rc, 0)

        dirs = backup_dirs(self.cfg)          # <- только папки копий
        self.assertEqual(len(dirs), 1)
        backup_sub = dirs[0]

        copied = backup_sub / name
        self.assertTrue(copied.is_file(), "кириллический файл не скопирован")
        self.assertEqual(copied.read_bytes(), content)

        rows = read_log_rows(backup_sub)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["Имя файла"], name)
        self.assertEqual(rows[0]["Хэш"], bt.file_hash(str(copied)))


# --------------------------------------------------------------------------- #
# 3. Проверка обнаруживает изменение
# --------------------------------------------------------------------------- #

class TestVerifyDetectsCorruption(BackupTestBase):
    def test_verify_detects_changed_file(self):
        src = Path(self.cfg["source_dir"])
        (src / "a.txt").write_text("original", encoding="utf-8")

        rc = bt.run_backup(self.cfg, dry_run=False, logger=self.logger)
        self.assertEqual(rc, 0)

        backup_sub = backup_dirs(self.cfg)[-1]
        (backup_sub / "a.txt").write_text("tampered", encoding="utf-8")

        with mock.patch("builtins.print"):
            ok = bt.verify_backup(str(backup_sub))
        self.assertFalse(ok, "verify должен обнаружить подмену")


# --------------------------------------------------------------------------- #
# 4. Повторный запуск не копирует неизменённое
# --------------------------------------------------------------------------- #

class TestIncremental(BackupTestBase):
    def test_second_run_skips_unchanged(self):
        src = Path(self.cfg["source_dir"])
        (src / "keep.txt").write_text("same", encoding="utf-8")
        (src / "change.txt").write_text("v1", encoding="utf-8")

        with mock.patch("builtins.print"):
            rc1 = bt.run_backup(self.cfg, dry_run=False, logger=self.logger)
        self.assertEqual(rc1, 0)

        dirs_after_first = backup_dirs(self.cfg)
        self.assertEqual(len(dirs_after_first), 1)
        first_backup = dirs_after_first[0]
        self.assertTrue((first_backup / "keep.txt").is_file())

        (src / "change.txt").write_text("v2-changed", encoding="utf-8")

        # Логгер с выводом на stderr — увидим реальное исключение
        dbg = logging.getLogger("test_backup_dbg")
        dbg.handlers.clear()
        h = logging.StreamHandler(sys.stderr)
        h.setFormatter(logging.Formatter("[TEST] %(levelname)s: %(message)s"))
        dbg.addHandler(h)
        dbg.setLevel(logging.DEBUG)
        dbg.propagate = False

        with mock.patch("builtins.print"):
            rc2 = bt.run_backup(self.cfg, dry_run=False, logger=dbg)

        # Сначала проверим state — если ошибка, будет видно по логу
        state = bt.load_state(Path(self.cfg["backup_dir"]))
        h_keep = bt.file_hash(str(src / "keep.txt"))
        h_change = bt.file_hash(str(src / "change.txt"))
        self.assertIn("keep.txt", state["files"])
        self.assertIn("change.txt", state["files"])
        self.assertEqual(state["files"]["keep.txt"]["hash"], h_keep)
        self.assertEqual(state["files"]["change.txt"]["hash"], h_change)

        self.assertEqual(rc2, 0, "второй запуск не должен возвращать ошибку")

        dirs_after_second = backup_dirs(self.cfg)
        self.assertEqual(len(dirs_after_second), 2)
        second_backup = dirs_after_second[-1]
        self.assertTrue((second_backup / "keep.txt").is_file())
        self.assertTrue((second_backup / "change.txt").is_file())

# --------------------------------------------------------------------------- #
# 5. Очистка оставляет N копий
# --------------------------------------------------------------------------- #

class TestCleanOld(BackupTestBase):
    def test_clean_keeps_n_latest(self):
        self.cfg["keep_copies"] = 2
        write_config(self.tmp_root / bt.USER_CONFIG_NAME, self.cfg)

        created = self.make_backup_dirs(5)
        self.assertEqual(len(backup_dirs(self.cfg)), 5)

        with mock.patch("builtins.input", return_value="y"), \
             mock.patch("builtins.print"):
            rc = bt.clean_old(self.cfg, dry_run=False, logger=self.logger)
        self.assertEqual(rc, 0)

        remaining = [p.name for p in backup_dirs(self.cfg)]
        expected = [p.name for p in created[-2:]]
        self.assertEqual(sorted(remaining), sorted(expected))


# --------------------------------------------------------------------------- #
# 6. Ошибка конфигурации
# --------------------------------------------------------------------------- #

class TestConfigError(BackupTestBase):
    def test_missing_required_key_exits_with_2(self):
        bad = make_config(self.tmp_root)
        del bad["source_dir"]
        write_config(self.tmp_root / bt.USER_CONFIG_NAME, bad)

        # ВАЖНО: load_config сливает default и user. Если в default
        # есть source_dir, ошибки не будет. Поэтому кладём и в default
        # пустой объект (уже сделано в setUp) — тогда ключа нет нигде.
        with self.assertRaises(SystemExit) as cm:
            bt.load_config(str(self.tmp_root / bt.USER_CONFIG_NAME))
        self.assertEqual(cm.exception.code, 2)

    def test_broken_json_exits_with_2(self):
        p = self.tmp_root / bt.USER_CONFIG_NAME
        p.write_text("{ not valid json ", encoding="utf-8")
        with self.assertRaises(SystemExit) as cm:
            bt.load_config(str(p))
        self.assertEqual(cm.exception.code, 2)

    def test_missing_default_config_exits_with_2(self):
        (self.tmp_root / bt.DEFAULT_CONFIG_NAME).unlink()
        with self.assertRaises(SystemExit) as cm:
            bt.load_config(str(self.tmp_root / bt.USER_CONFIG_NAME))
        self.assertEqual(cm.exception.code, 2)


# --------------------------------------------------------------------------- #
# 7. Скрытые файлы (только Windows)
# --------------------------------------------------------------------------- #

@unittest.skipIf(sys.platform != "win32",
                 "скрытые атрибуты проверяются только на Windows")
class TestHiddenFilesWindows(BackupTestBase):
    def test_hidden_file_is_skipped(self):
        import ctypes
        src = Path(self.cfg["source_dir"])
        hidden = src / "hidden.txt"
        hidden.write_text("secret", encoding="utf-8")
        visible = src / "visible.txt"
        visible.write_text("public", encoding="utf-8")

        FILE_ATTRIBUTE_HIDDEN = 0x2
        ok = ctypes.windll.kernel32.SetFileAttributesW(
            str(hidden), FILE_ATTRIBUTE_HIDDEN
        )
        if not ok:
            self.skipTest("не удалось выставить атрибут HIDDEN")

        try:
            with mock.patch("builtins.print"):
                rc = bt.run_backup(self.cfg, dry_run=False,
                                   logger=self.logger)
            self.assertEqual(rc, 0)

            backup_sub = backup_dirs(self.cfg)[-1]
            self.assertFalse((backup_sub / "hidden.txt").exists())
            self.assertTrue((backup_sub / "visible.txt").is_file())

            rows = read_log_rows(backup_sub)
            hidden_rows = [r for r in rows if r["Имя файла"] == "hidden.txt"]
            self.assertEqual(len(hidden_rows), 1)
            self.assertTrue(hidden_rows[0]["Хэш"].startswith("ПРОПУЩЕН"))
        finally:
            # Снять атрибут, чтобы tmp-каталог удалился без проблем
            ctypes.windll.kernel32.SetFileAttributesW(str(hidden), 0x80)


if __name__ == "__main__":
    unittest.main(verbosity=2)