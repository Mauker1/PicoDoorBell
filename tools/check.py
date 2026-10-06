"""Run every check in the repository.

    python3 tools/check.py

Seven things, in increasing order of how long they take:

  1. Every .py file ends with exactly one newline.
  2. No em or en dashes in any .py or .md file.
  3. No spaced hyphen standing in for a dash, in any .md file.
  4. Every .py file byte-compiles.
  5. Firmware `global` declarations match what each function rebinds.
  6. ROADMAP.md is structurally sound (tools/check_roadmap.py).
  7. Every suite in tests/ passes.

Files covered: everything git does not ignore, tracked or not, so a new file
is checked before it is ever added, and local notes stay out by being listed
in .gitignore. Outside a git checkout, the whole tree.

Exits non-zero if anything fails, so it works as a pre-commit hook or a CI
step. Test output is captured and shown only for failures, since a passing
run prints several hundred lines nobody reads.
"""
import ast
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


def project_files():
    """Every file the checks cover, as sorted absolute paths.

    The files git does not ignore: tracked, staged, and new files not yet
    added (git ls-files --cached --others --exclude-standard). A new file is
    therefore checked the moment it exists, while local-only files stay out
    by being listed in .gitignore, which also keeps them from being committed
    by accident. Contents are read from the working tree, so a modified file
    is checked as it stands on disk. Outside a git checkout (an exported
    tarball, say) this falls back to walking the whole tree.
    """
    try:
        out = subprocess.run(
            ['git', 'ls-files', '-z', '--cached', '--others', '--exclude-standard'],
            cwd=ROOT, capture_output=True, check=True).stdout
        paths = set(os.path.join(ROOT, p) for p in out.decode('utf-8').split('\0') if p)
        # A tracked file deleted from disk is still listed until the deletion
        # is staged; there is nothing left to check.
        return sorted(p for p in paths if os.path.isfile(p))
    except (OSError, subprocess.CalledProcessError):
        notes.append(('file list', 'git unavailable here, so the whole tree was '
                      'scanned, ignored files included'))
        found = []
        for base, dirs, names in os.walk(ROOT):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            found.extend(os.path.join(base, n) for n in names)
        return sorted(found)


FILES = project_files()


def python_files():
    return [p for p in FILES if p.endswith('.py')]


def markdown_files():
    return [p for p in FILES if p.endswith('.md')]


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
for path in python_files() + markdown_files():
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
for path in markdown_files():
    for i, line in enumerate(open(path, encoding='utf-8').read().split('\n'), 1):
        if DELIM.match(line):
            continue          # table rule
        if line.startswith('|'):
            # A cell holding only '-' means 'none'; it is not a dash.
            hit = any(' - ' in c for c in line.split('|')
                      if c.strip() != '-')
        else:
            # Drop blockquote markers and a leading list marker, then look
            # for a spaced hyphen before anything a word can start with:
            # letters, but also code, quotes and emphasis, which the first
            # version of this check let through.
            body = re.sub(r'^\s*(>\s*)*', '', line)
            if body.startswith('- '):
                body = body[2:]
            hit = bool(re.search(r'\S - [\w`"\'*(\[]', body))
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

# --- 5. No implicit globals ------------------------------------------------
# A function that assigns a module-level name without `global` silently
# creates a local instead: the module value never changes, and nothing errors
# unless the name is also read first. E2 moved the loop into run_pass() for
# exactly this reason; this keeps the whole class out of the firmware. The
# converse fails too: a `global` for a name the function never binds is at
# best redundant (in-place mutation such as d[k] = v needs no declaration)
# and at worst misleading about who rebinds what. Scans the top-level modules
# (the files that go on the device), not tests/tools.
def module_level_names(tree):
    """Names bound at module level, including inside top-level try/if blocks
    (the guarded ntptime import), but never inside a function."""
    names = set()
    def visit(node):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.ClassDef)):
                names.add(child.name)
                continue
            if isinstance(child, ast.Lambda):
                continue
            if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store):
                names.add(child.id)
            if isinstance(child, (ast.Import, ast.ImportFrom)):
                for a in child.names:
                    names.add((a.asname or a.name).split('.')[0])
            visit(child)
    visit(tree)
    return names

def stores_in(fn):
    """Names a function body binds, not counting nested functions."""
    found = set()
    def visit(node):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.Lambda)):
                continue
            if isinstance(child, ast.Name) and isinstance(child.ctx, (ast.Store, ast.Del)):
                found.add(child.id)
            if isinstance(child, ast.ExceptHandler) and child.name:
                found.add(child.name)
            visit(child)
    visit(fn)
    return found

bad = []
for path in python_files():
    name = relative(path)
    if os.sep in name:
        continue              # tests/, tools/, boards/: not firmware
    tree = ast.parse(open(path, encoding='utf-8').read())
    top = module_level_names(tree)
    for fn in (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)):
        declared = set()
        for n in ast.walk(fn):
            if isinstance(n, ast.Global):
                declared |= set(n.names)
        params = {a.arg for a in fn.args.args + fn.args.kwonlyargs}
        bound = stores_in(fn)
        shadow = (bound & top) - declared - params
        if shadow:
            bad.append('%s:%d %s() assigns %s without global'
                       % (name, fn.lineno, fn.name, ', '.join(sorted(shadow))))
        idle = declared - bound
        if idle:
            bad.append('%s:%d %s() declares global %s but never rebinds it'
                       % (name, fn.lineno, fn.name, ', '.join(sorted(idle))))
report(not bad, 'globals match rebinding', '\n'.join(bad))

# --- 6. Roadmap structure --------------------------------------------------
checker = os.path.join(ROOT, 'tools', 'check_roadmap.py')
if os.path.exists(checker):
    r = subprocess.run([sys.executable, checker], capture_output=True, text=True)
    report(r.returncode == 0, 'roadmap structure',
           (r.stdout + r.stderr).strip())
else:
    report(False, 'roadmap structure', 'tools/check_roadmap.py is missing')

# --- 7. Test suites --------------------------------------------------------
# Each runs as its own process: the suites install stub modules into
# sys.modules and would contaminate each other in one interpreter.
tests_dir = os.path.join(ROOT, 'tests')
total = 0
# Same file list as everything else, so an ignored scratch test cannot fail
# the gate, while a new suite runs the moment it exists.
suites = sorted(os.path.basename(p) for p in python_files()
                if os.path.dirname(p) == tests_dir
                and os.path.basename(p).startswith('test_'))
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
