"""Тесты tools/check_public.py: что уйдёт в git, то и проверяем; шаблоны денег и телефонов."""
import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import check_public as cp  # noqa: E402


def write(root, rel, text):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def kinds(found):
    return {(rel, kind) for rel, _, kind, _ in found}


class PatternsTest(unittest.TestCase):
    def hits(self, kind, line):
        return [m.group(0) for m in cp.PATTERNS[kind].finditer(line)]

    def test_money(self):
        for s in ("1 500 ₸", "200 руб.", "99₽", "5 €", "$25", "$ 3.50"):  # public-ok выдуманное
            self.assertTrue(self.hits("деньги", s), s)
        for s in ('case "$1" in', "volume $1", "$(( a + 1 ))"):
            self.assertFalse(self.hits("деньги", s), s)

    def test_phones(self):
        for s in ("+7 900 000-00-01", "+44 20 123 45 67", "8 (900) 000-00-02", "8 700 000 00 03", "89000000004"):  # public-ok
            self.assertTrue(self.hits("телефон", s), s)
        for s in ("v1.8.900.000", "300000", "2026-10-08"):
            self.assertFalse(self.hits("телефон", s), s)


class BlindSpotsTest(unittest.TestCase):
    """Номер и ключ через перенос строки; имя стоп-листа внутри слитного имени."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def found(self, text, deny=()):
        write(self.root, "f.py", text)
        return [(n, kind, s) for _, n, kind, s in cp.scan(self.root, [cp.Deny(w) for w in deny])]

    def test_phone_and_key_split_by_newline(self):
        split = ('x = ("звони +7 900 "\n     "000-00-07")\n',              # строка Python в две
                 "звони +7 900\n000-00-08\n",                             # перенос в тексте
                 'k = "sk-' + "a" * 12 + '" +\n    "' + "b" * 12 + '"\n')    # ключ склейкой
        for text in split:
            got = self.found(text)
            self.assertEqual(len(got), 1, (text, got))
            self.assertTrue(got[0][1].endswith("через перенос"), got)
            self.assertEqual(got[0][0], 1)
        ok = 'x = ("звони +7 900 "\n     "000-00-07")  # public-ok\n'
        self.assertEqual(self.found(ok), [])
        for text in ("версия 1.8\n900 000 00 00 строк\n", "a = 1\nb = 2\n", "+7 900 000-00-09\nдальше\n"):  # public-ok
            self.assertEqual([k for k in self.found(text) if "перенос" in k[1]], [], text)

    def test_deny_inside_glued_name(self):
        deny = ("Глаша", "Макс", "Tester")
        for text in ("автор ГлашаПетрова", "папка МаксТестов", "repo TesterShop", "бот my_tester_bot",
                     "ник supertester", "ГлашиныЗаметки"):
            self.assertTrue([k for k in self.found(text + "\n", deny) if k[1] == "стоп-лист"], text)
        for text in ("Максимум и максимально", "контроллер", "автор TesterShop  # public-ok"):
            self.assertEqual(self.found(text + "\n", deny), [], text)


@unittest.skipUnless(shutil.which("git"), "нужен git")
class GitScopeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self.tmp.name)
        subprocess.run(["git", "init", "-q", self.root], check=True)
        write(self.root, ".gitignore", "secret.txt\nstate/\n")
        write(self.root, "tracked.txt", "ключ sk-" + "a" * 24 + "\n")
        write(self.root, "secret.txt", "ключ sk-" + "b" * 24 + "\n")        # игнорируется — не в git
        write(self.root, "state/x.json", "+7 900 000 00 01\n")             # игнорируется  # public-ok
        write(self.root, "inbox/q.jsonl", "{}\n")                          # рабочая папка, не игнорируется
        write(self.root, ".env", "A=1\n")
        subprocess.run(["git", "-C", self.root, "add", "tracked.txt"], check=True)
        write(self.root, "new.md", "звони 8 900 000 00 02\n")              # неотслеживаемое неигнорируемое  # public-ok

    def tearDown(self):
        self.tmp.cleanup()

    def test_only_what_goes_to_git(self):
        got = kinds(cp.scan(self.root, []))
        self.assertIn(("tracked.txt", "ключ"), got)
        self.assertIn(("new.md", "телефон"), got)
        self.assertIn(("inbox", "рабочая папка в выгрузке"), got)
        self.assertIn((".env", "файл ключей"), got)
        self.assertFalse([k for k in got if k[0].startswith(("secret.txt", "state"))], got)

    def test_plain_folder_walked(self):
        shutil.rmtree(os.path.join(self.root, ".git"))
        got = kinds(cp.scan(self.root, []))
        self.assertIn(("secret.txt", "ключ"), got)
        self.assertIn(("state", "рабочая папка в выгрузке"), got)

    def test_inside_foreign_repo_that_ignores_it(self):
        sub = os.path.join(self.root, "export")
        write(self.root, ".gitignore", "secret.txt\nstate/\nexport/\n")
        write(sub, "leak.md", "ключ sk-" + "c" * 24 + "\n")
        self.assertIn(("leak.md", "ключ"), kinds(cp.scan(sub, [])))   # обошли сами, а не 0 от ls-files

    def test_nothing_checked_is_failure(self):
        empty = os.path.join(self.root, "empty")
        os.makedirs(empty)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cp.main([empty])
        self.assertEqual(code, 1)
        self.assertIn("Проверено файлов: 0", out.getvalue())
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cp.main([os.path.join(self.root, "tracked.txt")]), 1)   # находка — 1

    def test_deny_without_file_is_usage_error(self):
        for argv in (["--deny"], [self.root, "--deny"], ["--deny", "--x", self.root]):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(cp.main(argv), 2, argv)
            self.assertIn("--deny без файла", err.getvalue())


if __name__ == "__main__":
    unittest.main()
