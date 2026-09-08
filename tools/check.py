"""Run every check in the repository.

    python3 tools/check.py

Four things, in increasing order of how long they take:

  1. Every .py file ends with exactly one newline.
  2. No em or en dashes in any .py or .md file.
  3. No spaced hyphen standing in for a dash, in any .md file.
  4. Every .py file byte-compiles.
  5. ROADMAP.md is structurally sound (tools/check_roadmap.py).
  6. Every suite in tests/ passes.

Exits non-zero if anything fails, so it works as a pre-commit hook or a CI
step. Test output is captured and shown only for failures, since a passing
run prints several hundred lines nobody reads.
"""
import os
import py_compile
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKIP_DIRS = {'.git', '__pycache__', '.mypy_cache'}

failures = []
notes = []


def report(ok, label, detail=''):
    print('%-5s %s' % ('ok' if ok else 'FAIL', label))
    if not ok:
        failures.append(label)
        if detail:
            notes.append((label, detail))


def python_files():
    for base, dirs, names in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in sorted(names):
            if name.endswith('.py'):
                yield os.path.join(base, name)


def relative(path):
    return os.path.relpath(path, ROOT)


# --- 1. Trailing newline ---------------------------------------------------
# One newline, no more. Keeps diffs from tagging the last line every time it
# moves, and it is the kind of rule that only holds if something enforces it.
bad = []
for path in python_files():
    data = open(path, 'rb').read()
    if not data:
        bad.append(relative(path) + ' (empty)')
    elif not data.endswith(b'\n'):
        bad.append(relative(path) + ' (no trailing newline)')
    elif data.endswith(b'\n\n'):
        bad.append(relative(path) + ' (more than one)')
report(not bad, 'trailing newlines', '\n'.join(bad))

# --- 2. No em or en dashes -------------------------------------------------
# Plain hyphens only, in every tracked .py and .md. The roadmap's item
# headings depend on it structurally, and check_roadmap.py matches on it.
DASHES = {'\u2014': 'em dash', '\u2013': 'en dash'}
bad = []
for base, dirs, names in os.walk(ROOT):
    dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
    for name in sorted(names):
        if not name.endswith(('.py', '.md')):
            continue
        path = os.path.join(base, name)
        text = open(path, encoding='utf-8').read()
        for i, line in enumerate(text.split('\n'), 1):
            for ch, label in DASHES.items():
                if ch in line:
                    bad.append('%s:%d %s: %s'
                               % (relative(path), i, label, line.strip()[:70]))
report(not bad, 'no em or en dashes', '\n'.join(bad[:20]))

# --- 3. No spaced hyphen standing in for a dash ----------------------------
# The rule is about construction, not the character: swapping an em dash for
# a hyphen keeps the habit and changes only its spelling. A hyphen inside a
# compound word never has spaces around it, so a spaced one in prose is a
# dash in disguise. Markdown only; in Python a spaced hyphen is subtraction.
DELIM = re.compile(r'^\|(\s*:?-+:?\s*\|)+$')
bad = []
for base, dirs, names in os.walk(ROOT):
    dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
    for name in sorted(names):
        if not name.endswith('.md'):
            continue
        path = os.path.join(base, name)
        for i, line in enumerate(open(path, encoding='utf-8').read().split('\n'), 1):
            if DELIM.match(line):
                continue          # table rule
            if line.startswith('|'):
                # A cell holding only '-' means 'none'; it is not a dash.
                hit = any(' - ' in c for c in line.split('|')
                          if c.strip() != '-')
            else:
                hit = bool(re.search(r'\S - \w', line))
            if hit:
                bad.append('%s:%d %s' % (relative(path), i, line.strip()[:70]))
report(not bad, 'no spaced hyphens in prose', '\n'.join(bad[:20]))

# --- 4. Byte-compiles ------------------------------------------------------
# Catches a syntax error before it reaches a board, where the only symptom is
# a device that will not start.
bad = []
with tempfile.TemporaryDirectory() as cache:
    for path in python_files():
        target = os.path.join(cache, relative(path).replace(os.sep, '_') + 'c')
        try:
            py_compile.compile(path, cfile=target, doraise=True)
        except py_compile.PyCompileError as e:
            bad.append(str(e).strip())
report(not bad, 'byte-compiles', '\n'.join(bad))

# --- 5. Roadmap structure --------------------------------------------------
checker = os.path.join(ROOT, 'tools', 'check_roadmap.py')
if os.path.exists(checker):
    r = subprocess.run([sys.executable, checker], capture_output=True, text=True)
    report(r.returncode == 0, 'roadmap structure',
           (r.stdout + r.stderr).strip())
else:
    report(False, 'roadmap structure', 'tools/check_roadmap.py is missing')

# --- 6. Test suites --------------------------------------------------------
# Each runs as its own process: the suites install stub modules into
# sys.modules and would contaminate each other in one interpreter.
tests_dir = os.path.join(ROOT, 'tests')
total = 0
suites = sorted(n for n in os.listdir(tests_dir)
                if n.startswith('test_') and n.endswith('.py'))
for name in suites:
    path = os.path.join(tests_dir, name)
    r = subprocess.run([sys.executable, path], capture_output=True, text=True,
                       timeout=300)
    tail = [l for l in r.stdout.strip().split('\n') if 'passed' in l]
    summary = tail[-1] if tail else '(no summary)'
    if r.returncode == 0 and tail:
        total += int(summary.split('/')[0])
    report(r.returncode == 0, 'tests/' + name + '  ' + summary,
           '\n'.join(l for l in r.stdout.split('\n') if l.startswith('FAIL'))
           or r.stderr.strip())

print()
for label, detail in notes:
    print('--- ' + label + ' ---')
    print(detail)
    print()

if failures:
    print('%d check(s) failed' % len(failures))
    sys.exit(1)

print('all checks passed  (%d assertions across %d suites)' % (total, len(suites)))
