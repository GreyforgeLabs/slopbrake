#!/usr/bin/env python3
"""PreToolUse guard (rule H5): block destructive git commands in agent sessions.

The Bash command is parsed like a shell would (quotes, control operators, substitutions,
heredocs, env prefixes and exports, wrappers such as env/sudo/xargs, nested `bash -c` and
`eval` payloads, scripts fed to a shell from echo/printf/cat or a process substitution, and
$( )/backtick substitutions whose echo/printf/cat output becomes the command or a git argument),
then every git invocation is judged on its argv after git's global options and `-c` config.
Text inside the arguments of other commands (commit messages, grep patterns, heredocs
written to files) is never matched. A command that does not parse falls back to a
conservative check of the raw string.

Commands that move an existing ref (branch -f, checkout -B, switch -C, update-ref, a forced fetch
refspec into a local branch or tag) are judged by what they would lose: the guard runs one
read-only `git rev-list` and blocks only when a commit on the ref would be reachable from no other
branch, remote-tracking ref or tag, nor from the new value. When git can't answer it blocks; where no
repository exists yet (git init or clone earlier in the command) there is no branch to lose.
Every other command is judged on its argv alone, without running anything.

Hook protocol: JSON on stdin (`tool_input.command`, or grok's camelCase `toolInput.command`,
and `cwd`); exit 2 with the reason on stderr blocks, exit 0 allows. `--scope always` judges
every command (the project hook); `--scope managed` acts only when the git invocation's
effective directory is inside a repo whose toplevel has .claude/slopbrake.json (the
user-level hook). Importable API:
check_command(command, cwd, scope) -> reason or None. Stdlib only.

Known limits (data flow and runners the parser does not model; the guard is a seatbelt, not a
sandbox): values that come from variables (`X=push; git $X -f`, `git ${X:-push} -f`) or from
substitutions other than echo/printf/cat; multi-stage pipelines and scripts written to a file then
run; wrappers it does not know, such as watch, ionice, chrt, taskset, flock, unbuffer, parallel.
"""
from __future__ import annotations

import json
import os
import posixpath
import re
import shlex
import sys

MAX_DEPTH = 12
LOST_LIMIT = 101  # count orphaned commits up to 100; more is "100+"
LOCATING = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY",
            "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE", "GIT_PREFIX")
CONTROL = ("&&", "||", ";;", "|&", ";", "&", "|", "(", ")", "\n")
REDIRECTS = ("<<<", "<<-", "&>>", "<<", ">>", "&>", ">&", "<&", ">|", "<>", "<", ">")
OPERATORS = sorted(CONTROL + REDIRECTS, key=len, reverse=True)
RESERVED = {"!", "{", "}", "if", "then", "else", "elif", "fi", "do", "done", "while", "until", "time", "coproc"}
SHELLS = {"bash", "sh", "zsh", "dash", "ksh", "mksh", "ash"}
ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
FALSE = ("false", "no", "off", "0", "")
VARIABLE = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*|[0-9@*#?$!-])?")
FALLBACK = re.compile(
    r"\bgit\b.*?(--force|--mirror|--delete|--prune|--no-verify|--hard|--merge|--discard-changes|hookspath"
    r"|\bpush\b.*\s[-+:]|\bclean\b|\bbranch\b.*\s-\w*[DMCdf]|\bcheckout\b|\brestore\b|\bstash\s+(drop|clear)"
    r"|\bupdate-ref\b|\breflog\s+(expire|delete)|\bprune\b|\bcommit\b.*\s-\w*n|\bswitch\b.*\s-\w*C"
    r"|\btag\b.*\s(-\w*d|--delete)|\bworktree\b.*\bremove\b|\bfilter-(branch|repo)\b|(?<=git)\s+rm\b.*\s-\w*f"
    r"|\bfetch\b.*\s\+)",
    re.DOTALL | re.IGNORECASE)


class Unparseable(Exception):
    pass


class Word:
    def __init__(self):
        self.text, self.quote_at, self.subs = "", None, []

    def add(self, chars, quoted=False):
        if quoted and self.quote_at is None:
            self.quote_at = len(self.text)
        self.text += chars


class Heredoc:
    def __init__(self, delim, quoted, strip):
        self.delim, self.quoted, self.strip, self.body, self.subs = delim, quoted, strip, "", []


class Lexer:
    """Splits a command into words, operators and heredocs; substitutions are kept for re-checking."""

    def __init__(self, src):
        self.s, self.i, self.pending = src, 0, []

    def tokens(self, stop=False):
        s, toks, word, depth = self.s, [], None, 0

        def end():
            nonlocal word
            if word is not None:
                if toks and toks[-1] in ("<<", "<<-"):
                    doc = Heredoc(word.text, word.quote_at is not None, toks[-1] == "<<-")
                    self.pending.append(doc)
                    toks.append(doc)
                else:
                    toks.append(word)
            word = None

        while self.i < len(s):
            c = s[self.i]
            if c in " \t":
                end()
                self.i += 1
            elif c == "\n":
                end()
                toks.append("\n")
                self.i += 1
                self.read_heredocs()
            elif c == "#" and word is None:
                while self.i < len(s) and s[self.i] != "\n":
                    self.i += 1
            elif c == "\\":
                if s.startswith("\\\n", self.i):
                    self.i += 2
                    continue
                word = word or Word()
                word.add(s[self.i + 1:self.i + 2], quoted=True)
                self.i += 2
            elif c == "'":
                close = s.find("'", self.i + 1)
                if close < 0:
                    raise Unparseable
                word = word or Word()
                word.add(s[self.i + 1:close], quoted=True)
                self.i = close + 1
            elif c == '"':
                word = word or Word()
                self.i += 1
                self.double_quoted(word)
            elif c == "$" or c == "`":
                word = word or Word()
                self.expansion(word, quoted=False)
            elif c in "<>" and s.startswith("(", self.i + 1):
                word = word or Word()
                start = self.i
                self.i += 2
                word.subs.append(self.substitution())
                word.add(s[start:self.i])
            elif c in ";&|()<>":
                op = next(o for o in OPERATORS if s.startswith(o, self.i))
                if op[0] in "<>" and word is not None and word.quote_at is None and word.text.isdigit():
                    word = None  # an io number such as the 2 in 2>&1
                end()
                self.i += len(op)
                if op == "(":
                    depth += 1
                elif op == ")":
                    if stop and depth == 0:
                        return toks
                    depth -= 1
                toks.append(op)
            else:
                word = word or Word()
                word.add(c)
                self.i += 1
        if stop:
            raise Unparseable
        end()
        return toks

    def double_quoted(self, word):
        s = self.s
        while self.i < len(s):
            c = s[self.i]
            if c == '"':
                self.i += 1
                return
            if c == "\\" and s[self.i + 1:self.i + 2] in ('"', "\\", "$", "`", "\n"):
                word.add(s[self.i + 1].replace("\n", ""), quoted=True)
                self.i += 2
            elif c in "$`":
                self.expansion(word, quoted=True)
            else:
                word.add(c, quoted=True)
                self.i += 1
        raise Unparseable

    def expansion(self, word, quoted):
        s, start = self.s, self.i
        if s.startswith("$((", self.i):
            self.i = self.balanced(self.i + 3, "(", ")", 2)
        elif s.startswith("$(", self.i):
            self.i += 2
            word.subs.append(self.substitution())
        elif s.startswith("${", self.i):
            self.i = self.balanced(self.i + 2, "{", "}", 1)
        elif s.startswith("$'", self.i) and not quoted:
            end = self.i + 2
            while end < len(s) and s[end] != "'":
                end += 2 if s[end] == "\\" else 1
            if end >= len(s):
                raise Unparseable
            text = s[self.i + 2:end]
            try:
                text = text.encode("latin-1", "backslashreplace").decode("unicode_escape")
            except UnicodeDecodeError:
                pass
            word.add(text, quoted=True)
            self.i = end + 1
            return
        elif s.startswith("`", self.i):
            end = self.i + 1
            while end < len(s) and s[end] != "`":
                end += 2 if s[end] == "\\" else 1
            if end >= len(s):
                raise Unparseable
            word.subs.append(re.sub(r"\\([`$\\])", r"\1", s[self.i + 1:end]))
            self.i = end + 1
        else:
            self.i = VARIABLE.match(s, self.i).end()
        word.add(s[start:self.i], quoted=quoted)

    def substitution(self):
        """Consume up to the `)` closing a `$(` or `<(`; return the inner command."""
        start = self.i
        self.tokens(stop=True)
        return self.s[start:self.i - 1]

    def balanced(self, i, open_, close, depth):
        while i < len(self.s):
            depth += {open_: 1, close: -1}.get(self.s[i], 0)
            i += 1
            if depth == 0:
                return i
        raise Unparseable

    def read_heredocs(self):
        s = self.s
        for doc in self.pending:
            lines = []
            while self.i < len(s):
                end = s.find("\n", self.i)
                end = len(s) if end < 0 else end
                line = s[self.i:end]
                self.i = end + 1
                if (line.lstrip("\t") if doc.strip else line) == doc.delim:
                    break
                lines.append(line)
            doc.body = "\n".join(lines)
            if not doc.quoted:  # an unquoted delimiter still expands $( ) and backticks in the body
                doc.subs = expansions(doc.body)
        self.pending = []


def expansions(text):
    """Command substitutions inside text that is expanded like a double-quoted string."""
    found, i = [], 0
    while i < len(text):
        if text[i] == "\\":
            i += 2
        elif text[i] in "$`":
            lexer, word = Lexer(text), Word()
            lexer.i = i
            lexer.expansion(word, quoted=True)
            found += word.subs
            i = max(lexer.i, i + 1)
        else:
            i += 1
    return found


def here_texts(items):
    """Heredoc bodies and here-strings among a simple command's items."""
    texts = []
    for i, item in enumerate(items):
        if isinstance(item, Heredoc):
            texts.append(item.body)
        elif item == "<<<" and i + 1 < len(items) and isinstance(items[i + 1], Word):
            texts.append(items[i + 1].text)
    return texts


def outputs(items):
    """What a simple command writes to stdout, when that is knowable: echo/printf args, cat's heredoc."""
    words, i = [], 0
    while i < len(items):
        if items[i] in REDIRECTS:
            i += 1  # skip the redirect target
        elif isinstance(items[i], Word):
            words.append(items[i].text)
        i += 1
    name = basename(words[0]) if words else ""
    if name in ("echo", "printf"):
        text = [w for w in words[1:] if not re.fullmatch(r"-[neE]+", w)]
        return [" ".join(text), *text]
    return here_texts(items) if name == "cat" else []


def emitted(command):
    """Everything the simple commands of command (a process substitution) knowably print."""
    try:
        toks = Lexer(command).tokens()
    except Unparseable:
        return []
    found, current = [], []
    for tok in [*toks, "\n"]:
        if isinstance(tok, str) and tok not in REDIRECTS:
            found += outputs(current) if current else []
            current = []
        else:
            current.append(tok)
    return found


def whole_substitution(word):
    """True when word is nothing but one $( ) or backtick substitution."""
    return len(word.subs) == 1 and (word.text == f"$({word.subs[0]})" or (
        len(word.text) > 1 and word.text[0] == word.text[-1] == "`"))


def split_words(text):
    """shlex word splitting for env -S strings and alias values; whitespace when quotes don't balance."""
    try:
        return shlex.split(text)
    except ValueError:
        return text.split()


def basename(word):
    return word.rsplit("/", 1)[-1]


def resolve(base, path):
    """Join a cd/-C target onto the current directory; None when it can't be known statically."""
    if base is None or path is None:
        return None
    path = os.path.expandvars(os.path.expanduser(path))
    if "$" in path or "`" in path:
        return None
    return os.path.normpath(os.path.join(base, path))


def managed(path):
    """True when path is inside a git repo whose toplevel has .claude/slopbrake.json."""
    if path is None:
        return True  # unknown directory: judge it rather than let it through (callers pass the session cwd)
    path = os.path.abspath(path)
    while not os.path.exists(path) and os.path.dirname(path) != path:
        path = os.path.dirname(path)
    while True:
        if os.path.exists(os.path.join(path, ".git")):
            return os.path.isfile(os.path.join(path, ".claude", "slopbrake.json"))
        if os.path.dirname(path) == path:
            return False
        path = os.path.dirname(path)


def parse(args, short_arg="", short_opt="", long_arg=()):
    """Split subcommand args into ([(option, value)], positionals) the way git's parse-options does."""
    opts, pos, i = [], [], 0
    while i < len(args):
        a = args[i]
        if a == "--":
            pos += args[i + 1:]
            break
        if a.startswith("--"):
            name, eq, value = a.partition("=")
            if not eq and name in long_arg and i + 1 < len(args):
                i += 1
                value = args[i]
            opts.append((name, value))
        elif a.startswith("-") and len(a) > 1:
            for j, ch in enumerate(a[1:], 1):
                if ch in short_arg or ch in short_opt:
                    value = a[j + 1:]
                    if not value and ch in short_arg and i + 1 < len(args):
                        i += 1
                        value = args[i]
                    opts.append(("-" + ch, value))
                    break
                opts.append(("-" + ch, ""))
        else:
            pos.append(a)
        i += 1
    return opts, pos


def has(opts, *names):
    """Any of names given; long options also match git's unambiguous abbreviations (--forc)."""
    for name, _ in opts:
        if name in names or (name.startswith("--") and len(name) > 3 and any(n.startswith(name) for n in names)):
            return True
    return False


def whole_tree(spec):
    """A pathspec that covers the whole tree (or everything under the current directory)."""
    if spec.startswith(":("):
        magic, _, spec = spec[2:].partition(")")
        if "exclude" in {m.strip() for m in magic.split(",")}:
            return True
    elif spec.startswith(":"):
        spec = spec[1:]
        while spec[:1] in ("/", "!", "^"):
            if spec[0] != "/":
                return True  # only-negative pathspecs mean "everything except"
            spec = spec[1:]
        spec = spec.removeprefix(":")
    spec = posixpath.normpath(spec) if spec else "."
    return all(p in (".", "..") for p in spec.split("/") if p) or spec.strip("/") in ("*", "**")


def toplevel(path):
    """The work tree root of the repo that path is in (the nearest directory with a .git), or None."""
    path = os.path.realpath(path)
    while not os.path.exists(os.path.join(path, ".git")):
        if os.path.dirname(path) == path:
            return None
        path = os.path.dirname(path)
    return path


def repo_root(base, spec, tree=None):
    """True when the path spec, read from base, is the root of the work tree (tree when --work-tree names one,
    else the toplevel of base's repo) or a directory above it."""
    path, top = resolve(base, spec), os.path.realpath(tree) if tree else base and toplevel(base)
    if path is None or top is None:
        return False
    path = os.path.realpath(path)
    return top == path or top.startswith(path.rstrip("/") + "/")


def spell_out(arg, longs, unambiguous):
    """arg with an unambiguous abbreviation of one of longs (it starts with one of unambiguous) written in full."""
    name, eq, value = arg.partition("=")
    full = next((o for o in longs if o.startswith(name) and name.startswith(unambiguous)), None) if name[:2] == "--" else None
    return full + eq + value if full else arg


def local_ref(dst):
    """The local ref a fetch refspec's destination names (git's get_local_ref)."""
    if dst.startswith("refs/"):
        return dst
    return "refs/" + dst if dst.startswith(("heads/", "tags/", "remotes/")) else "refs/heads/" + dst


def plural(count):
    return f"{count} commit" + ("" if count == 1 else "s") if count < LOST_LIMIT else f"{LOST_LIMIT - 1}+ commits"


class Uncheckable(Exception):
    """The guard can't tell what a command would lose: git failed, or the arguments make no sense."""


class NoRepository(Uncheckable):
    """git found no repository: none exists yet (a git init or clone earlier in the command makes it)."""


class Judge:
    def __init__(self, cwd, scope, depth=0, env=None, session=None):
        self.dir, self.scope, self.depth, self.env = cwd, scope, depth, env or {}
        self.session = session or cwd  # the hook's cwd: stands in for directories that can't be known
        self.procsubs, self.cmdsubs = {}, {}

    def string(self, text, cwd=False):
        if cwd is not False:
            return Judge(cwd, self.scope, self.depth + 1, dict(self.env), self.session).string(text)
        if self.depth > MAX_DEPTH:
            return self.unscoped("the command nests shells too deeply to check")
        try:
            toks = Lexer(text).tokens()
        except Unparseable:
            match = FALLBACK.search(text)
            return self.unscoped(f"the command does not parse and looks like a destructive git command "
                                 f"({match.group(0)!r})") if match else None
        return self.tokens(toks)

    def unscoped(self, reason):
        """A reason tied to no git invocation: in managed scope it counts only in a known managed directory or
        from a managed session cwd (an unknown directory falls back to the session cwd)."""
        known = self.dir is not None and managed(self.dir)
        return reason if self.scope != "managed" or known or managed(self.session) else None

    def nested(self, text):
        return self.string(text, cwd=self.dir)

    def tokens(self, toks):
        saved, current, previous, piped = [], [], None, False
        for tok in [*toks, "\n"]:
            if not isinstance(tok, str) or tok in REDIRECTS:
                current.append(tok)
                continue
            if current:
                reason = self.simple(current, previous if piped else None)
                if reason:
                    return reason
                previous = current
            current, piped = [], tok in ("|", "|&")
            if tok == "(":
                saved.append(self.dir)
            elif tok == ")" and saved:
                self.dir = saved.pop()
        return None

    def simple(self, items, piped_from):
        words, stdin, subs, i = [], [], [], 0
        while i < len(items):
            item = items[i]
            if isinstance(item, Word):
                words.append(item)
                subs += item.subs
            elif isinstance(item, Heredoc):
                stdin.append(item.body)
                subs += item.subs
            elif item in REDIRECTS and i + 1 < len(items) and isinstance(items[i + 1], Word):
                i += 1
                subs += items[i].subs
                if item == "<<<":
                    stdin.append(items[i].text)
                elif item == "<" and items[i].text.startswith("<("):
                    stdin += [text for sub in items[i].subs for text in emitted(sub)]
            i += 1
        for sub in subs:
            reason = self.nested(sub)
            if reason:
                return reason
        env = {}
        self.procsubs = {w.text: w.subs for w in words if w.text.startswith("<(")}
        self.cmdsubs = {w.text: (w.subs[0], w.quote_at == 0) for w in words if whole_substitution(w)}
        while words and words[0].text in RESERVED - {"time"}:
            if words.pop(0).text == "coproc" and len(words) > 1 and words[1].text == "{":
                words.pop(0)  # coproc NAME { ...; }
        while words and ASSIGNMENT.match(words[0].text) and (
                words[0].quote_at is None or words[0].quote_at >= ASSIGNMENT.match(words[0].text).end()):
            name, _, value = words.pop(0).text.partition("=")
            env[name] = value
        return self.argv([w.text for w in words], env, stdin, piped_from)

    def argv(self, argv, env, stdin, piped_from):
        cwd = self.dir
        while argv:
            name = basename(argv[0])
            if argv[0] in RESERVED:
                argv = argv[1:]
                if name == "time":
                    argv = argv[1:] if argv[:1] == ["-p"] else argv
            elif name in ("command", "builtin"):
                argv = argv[1:]
                while argv and argv[0].startswith("-"):
                    if argv[0] in ("-v", "-V"):
                        return None  # a lookup, not a run
                    argv = argv[1:]
            elif name == "exec":
                argv = skip_options(argv[1:], takes_value=("-a",))
            elif name in ("nohup", "setsid"):
                argv = skip_options(argv[1:])
            elif name == "stdbuf":
                argv = skip_options(argv[1:], takes_value=("-i", "-o", "-e"))
            elif name in ("sudo", "doas"):
                argv, chdir = sudo_options(argv[1:])
                cwd = resolve(cwd, chdir) if chdir else cwd
            elif name == "nice":
                argv = skip_options(argv[1:], takes_value=("-n", "--adjustment"))
            elif name == "timeout":
                argv = skip_options(argv[1:], takes_value=("-s", "-k", "--signal", "--kill-after"))[1:]
            elif name == "xargs":
                argv = skip_options(argv[1:], takes_value=("-I", "-n", "-P", "-L", "-d", "-E", "-s", "-a"))
            elif name == "env":
                argv, chdir, split = env_options(argv[1:], env)
                cwd = resolve(cwd, chdir) if chdir else cwd
                if split is not None:
                    argv = split + argv
            else:
                break
        if not argv:
            return None
        if argv[0] in self.cmdsubs:  # the shell runs what the substitution prints, e.g. $(echo git push -f)
            rest = " ".join(map(shlex.quote, self.expand(argv[1:])))
            return self.scripts([f"{out} {rest}" for out in emitted(self.cmdsubs[argv[0]][0])], cwd)
        name = basename(argv[0])
        if name in ("cd", "pushd"):
            target = next((a for a in argv[1:] if not a.startswith("-") or a == "-"), "~")
            self.dir = None if target == "-" else resolve(self.dir, target)
        elif name == "popd":
            self.dir = None
        elif name == "export" or (name in ("declare", "typeset", "local") and any(
                a.startswith("-") and "x" in a for a in argv[1:])):
            self.env.update(a.split("=", 1) for a in argv[1:] if ASSIGNMENT.match(a))
        elif name == "eval":
            return self.string(" ".join(argv[1:]), cwd=cwd)
        elif name in ("source", "."):
            return self.scripts(self.substituted(argv[1:2]), cwd)
        elif name in SHELLS:
            return self.shell(argv[1:], stdin, piped_from, cwd)
        elif name == "git":
            return Git(self, cwd, {**self.env, **env}).run(self.expand(argv[1:]))
        return None

    def expand(self, args):
        """args with each knowable command substitution replaced by its output (word-split unless quoted)."""
        expanded = []
        for a in args:
            sub, quoted = self.cmdsubs.get(a, (None, False))
            texts = emitted(sub) if sub else []
            expanded += ([texts[0]] if quoted else texts[0].split()) if texts else [a]
        return expanded

    def substituted(self, args):
        """The scripts printed by process-substitution arguments such as <(echo ...)."""
        return [text for a in args for sub in self.procsubs.get(a, ()) for text in emitted(sub)]

    def scripts(self, scripts, cwd):
        for script in scripts:
            reason = self.string(script.replace("\\n", "\n"), cwd=cwd)
            if reason:
                return reason
        return None

    def shell(self, args, stdin, piped_from, cwd):
        # Like bash/sh, options (and -o/-O values) keep being read after -c; the first operand is the payload.
        i, reads_stdin, command = 0, False, False
        while i < len(args):
            a = args[i]
            if a == "--":
                i += 1
                break
            if a in ("--rcfile", "--init-file"):
                i += 2
            elif a[:1] in "-+" and not a.startswith("--") and len(a) > 1:
                command = command or (a[0] == "-" and "c" in a)
                reads_stdin = reads_stdin or "s" in a
                i += 1 + a.count("o") + a.count("O")
            elif a.startswith("--"):
                i += 1
            else:
                break
        if command:
            return self.string(args[i], cwd=cwd) if i < len(args) else None
        if i < len(args) and not reads_stdin and args[i] != "-":
            return self.scripts(self.substituted(args[i:i + 1]), cwd)  # a script file: checkable only as <(...)
        return self.scripts([*stdin, *(outputs(piped_from) if piped_from else [])], cwd)


def skip_options(argv, takes_value=()):
    while argv and argv[0].startswith("-") and argv[0] != "-":
        if argv[0] == "--":
            return argv[1:]
        argv = argv[2:] if argv[0] in takes_value else argv[1:]
    return argv


def sudo_options(argv):
    chdir = None
    while argv and argv[0].startswith("-"):
        a = argv.pop(0)
        if a == "--":
            break
        if a in ("-D", "--chdir") and argv:
            chdir = argv.pop(0)
        elif a.startswith("--chdir="):
            chdir = a.partition("=")[2]
        elif a in ("-u", "-g", "-h", "-p", "-C", "-r", "-t", "-U", "-T", "-R", "--user", "--group", "--host",
                   "--prompt", "--close-from", "--role", "--type", "--other-user", "--command-timeout", "--chroot"):
            argv = argv[1:]
    return argv, chdir


def env_options(argv, env):
    chdir = split = None
    while argv:
        a = argv[0]
        if a == "--":
            argv = argv[1:]
            break
        if a in ("-u", "--unset", "-C", "--chdir", "-S", "--split-string") and len(argv) > 1:
            if a in ("-C", "--chdir"):
                chdir = argv[1]
            elif a in ("-S", "--split-string"):
                split = split_words(argv[1])
            argv = argv[2:]
        elif a.startswith(("--chdir=", "--split-string=", "-S")):
            value = a[2:] if a.startswith("-S") else a.partition("=")[2]
            chdir, split = (value, split) if a.startswith("--chdir") else (chdir, split_words(value))
            argv = argv[1:]
        elif a.startswith("-"):
            argv = argv[1:]
        elif ASSIGNMENT.match(a):
            name, _, value = a.partition("=")
            env[name] = value
            argv = argv[1:]
        else:
            break
    return argv, chdir, split


class Git:
    """One git invocation: global options, then the subcommand's rule."""

    def __init__(self, judge, cwd, env):
        self.judge, self.cwd, self.env, self.config, self.nested = judge, cwd, env, [], False
        self.git_dir, self.work_tree = env.get("GIT_DIR"), env.get("GIT_WORK_TREE")

    def run(self, args):
        i = 0
        while i < len(args) and args[i].startswith("-"):
            a = args[i]
            name, _, value = a.partition("=")
            if a in ("-C", "-c", "--config-env", "--git-dir", "--work-tree", "--namespace", "--attr-source") \
                    and i + 1 < len(args):
                i += 1
                name, value = a, args[i]
            if name == "-C":
                self.cwd = resolve(self.cwd, value)
            elif name in ("-c", "--config-env"):
                self.config.append(value)
            elif name == "--git-dir":
                self.git_dir = value
            elif name == "--work-tree":
                self.work_tree = value
            i += 1
        if i >= len(args):
            return None
        reason = self.config_reason() or self.subcommand(args[i], args[i + 1:])
        target = self.effective_dir()
        if reason and not self.nested and self.judge.scope == "managed" and \
                not managed(self.judge.session if target is None else target):
            return None
        return reason

    def effective_dir(self):
        if self.work_tree:
            return resolve(self.cwd, self.work_tree)
        if self.git_dir:
            git_dir = resolve(self.cwd, self.git_dir)
            return os.path.dirname(git_dir) if git_dir and basename(git_dir) == ".git" else git_dir
        return self.cwd

    def inner(self, command):
        """Judge a command git itself runs; its reason already carries its own managed-scope verdict."""
        reason = self.judge.string(command, cwd=self.cwd)
        self.nested = self.nested or bool(reason)
        return reason

    def setting(self, pattern):
        """True/False for the last -c entry whose key matches pattern, None when unset."""
        found = None
        for entry in self.config:
            key, eq, value = entry.partition("=")
            if re.fullmatch(pattern, key.lower()):
                found = value.lower() not in FALSE if eq else True
        return found

    def config_reason(self):
        keys = [entry.partition("=")[0].lower() for entry in self.config]
        keys += [value.lower() for name, value in self.env.items() if name.startswith("GIT_CONFIG")]
        if any("core.hookspath" in key for key in keys):
            return "overriding core.hooksPath switches off the repo's git hooks"
        return None

    def subcommand(self, sub, rest):
        for entry in self.config:
            key, _, value = entry.partition("=")
            if key.lower() == f"alias.{sub.lower()}" and value:
                self.config.remove(entry)
                if value.startswith("!"):
                    return self.inner(" ".join([value[1:], *map(shlex.quote, rest)]))
                words = split_words(value)
                return self.subcommand(words[0], words[1:] + rest) if words else None
        rule = getattr(self, "rule_" + sub.replace("-", "_"), None)
        why = rule(rest) if rule else None
        return f"{why} ({' '.join(['git', sub, *rest])})" if why else None

    def rule_push(self, rest):
        opts, pos = parse(rest, short_arg="o", long_arg=("--repo", "--receive-pack", "--exec", "--push-option"))
        if has(opts, "-f", "--force", "--force-with-lease", "--force-if-includes"):
            return "force push rewrites remote history"
        if has(opts, "--mirror", "-d", "--delete", "--prune") or self.setting(r"remote\..+\.mirror"):
            return "this push deletes or overwrites remote refs"
        if has(opts, "--no-verify"):
            return "--no-verify skips the pre-push gate"
        if any(p.startswith("+") for p in pos):
            return "a +refspec is a force push, which rewrites remote history"
        if any(p.startswith(":") and len(p) > 1 for p in pos):
            return "a :ref refspec deletes the remote branch"
        return None

    def rule_reset(self, rest):
        return "reset --hard/--merge discards uncommitted work" if has(parse(rest)[0], "--hard", "--merge") else None

    def rule_clean(self, rest):
        opts, _ = parse(rest, short_arg="e", long_arg=("--exclude",))
        if (has(opts, "-f", "--force") or self.setting("clean.requireforce") is False) and not has(
                opts, "-n", "--dry-run"):
            return "clean -f deletes untracked files"
        return None

    def judged(self):
        """Whether a reason from this invocation counts: always in "always" scope, else only in a managed repo.
        Checked before running git, so the managed hook never spawns git for repos it ignores."""
        target = self.effective_dir()
        return self.judge.scope != "managed" or managed(self.judge.session if target is None else target)

    def git(self, *args):
        """Run a read-only git command against this invocation's repo; its stdout, or Uncheckable."""
        import subprocess  # deferred: only the commands that move or overwrite a ref pay for it

        if self.cwd is None:
            raise Uncheckable("the directory it runs in isn't known")
        cwd = self.cwd
        while not (self.git_dir or self.work_tree or os.path.isdir(cwd)) and os.path.dirname(cwd) != cwd:
            cwd = os.path.dirname(cwd)  # made by an earlier command (mkdir, clone): judge the repo it will be in
        command = ["git", "-C", cwd]
        command += ["--git-dir", self.git_dir] if self.git_dir else []
        command += ["--work-tree", self.work_tree] if self.work_tree else []
        env = {k: v for k, v in os.environ.items() if k not in LOCATING}
        env["LC_ALL"] = "C"
        try:
            done = subprocess.run([*command, *args], capture_output=True, text=True, env=env, timeout=10,
                                  stdin=subprocess.DEVNULL, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            raise Uncheckable(f"git did not run: {exc}") from exc
        if done.returncode:
            lines = done.stderr.strip().splitlines()
            message = lines[0].removeprefix("fatal: ") if lines else f"git exited {done.returncode}"
            raise (NoRepository if message.startswith("not a git repository") else Uncheckable)(message)
        return done.stdout.strip()

    def orphaned(self, ref, keep=()):
        """How many commits reachable from ref no other branch, remote-tracking ref or tag reaches, nor any of
        keep (the ref's new value). 0 when ref doesn't exist. Counts at most LOST_LIMIT."""
        kind, _, name = ref.removeprefix("refs/").partition("/")
        groups = []
        for group in ("heads", "remotes", "tags"):
            if kind == group and name:
                groups.append(f"--exclude={name}")  # the ref itself is no other copy of its commits
            groups.append("--branches" if group == "heads" else f"--{group}")
        try:
            return int(self.git("rev-list", "--ignore-missing", "--count", f"--max-count={LOST_LIMIT}", ref, "--not",
                                *groups, *keep) or 0)
        except NoRepository:
            return 0  # no repository yet, so no branch: git init or clone runs first in the same command

    def start_point(self, start):
        """start as one revision: branch, checkout -B and switch -C read A...B as the merge base of A and B (an empty
        side is HEAD), so it is resolved here; a range or an option can't be checked."""
        sides = start.split("...") if "..." in start else [start]
        if len(sides) > 2 or any(side.startswith(("-", "^")) or ".." in side for side in sides):
            raise Uncheckable(f"start point {start!r}")
        return self.git("merge-base", sides[0] or "HEAD", sides[1] or "HEAD") if len(sides) == 2 else start

    def reset_branch(self, verb, name, start):
        """The reason a forced create (branch -f, checkout -B, switch -C) loses work, or None.
        A new branch, or one whose every commit another ref or the start point still reaches, is allowed."""
        if not self.judged():
            return None
        try:
            if not name or name in ("-", "@", "HEAD") or "@{" in name:
                raise Uncheckable(f"no plain branch name ({name!r})" if name else "no branch name")
            lost = self.orphaned(f"refs/heads/{name}", [self.start_point(start)])
        except Uncheckable as exc:
            return (f"{verb} resets the branch if it already exists, and the guard could not check whether that "
                    f"orphans commits ({exc}); run it from inside the repository with a plain branch name, or "
                    "create a branch under a new name instead")
        if lost:
            return (f"{verb} would orphan {plural(lost)} on branch {name!r} that no other branch, remote-tracking "
                    f"ref or tag reaches, nor {start}; create a branch under a new name instead, or push or tag "
                    f"{name!r} first so its commits stay reachable")
        return None

    def rule_branch(self, rest):
        opts, pos = parse(rest, short_arg="u", short_opt="t", long_arg=(
            "--set-upstream-to", "--contains", "--no-contains", "--merged", "--no-merged", "--points-at",
            "--format", "--sort"))
        if has(opts, "-D", "-M", "-C") or (has(opts, "-f", "--force") and has(
                opts, "-d", "--delete", "-m", "--move", "-c", "--copy")):
            return "force-deleting or overwriting a branch loses its commits"
        if has(opts, "-f", "--force") and not has(  # these list or configure branches; none moves one
                opts, "-l", "--list", "-a", "--all", "-r", "--remotes", "--contains", "--no-contains", "--merged",
                "--no-merged", "--points-at", "-u", "--set-upstream-to", "--set-upstream", "--unset-upstream",
                "--show-current", "--edit-description"):
            return self.reset_branch("branch -f", pos[0] if pos else "", pos[1] if len(pos) > 1 else "HEAD")
        return None

    def rule_checkout(self, rest):
        opts, pos = parse(rest, short_arg="bB", long_arg=("--orphan", "--pathspec-from-file"))
        if has(opts, "-f", "--force"):
            return "checkout --force discards uncommitted changes"
        if has(opts, "--pathspec-from-file"):
            return "pathspecs read from a file can't be checked and may cover the whole tree"
        if any(whole_tree(p) for p in pos):
            return "checking out the whole tree discards uncommitted changes"
        reset = [value for name, value in opts if name == "-B"]
        if reset:
            return self.reset_branch("checkout -B", reset[-1], pos[0] if pos else "HEAD")
        return None

    def rule_switch(self, rest):
        # git accepts unambiguous abbreviations such as --force-c; spell them out so their value parses
        rest = [spell_out(a, ("--force-create", "--create", "--orphan"), ("--force-", "--cr", "--or")) for a in rest]
        opts, pos = parse(rest, short_arg="cC", long_arg=("--create", "--force-create", "--orphan"))
        if has(opts, "-f", "--force", "--discard-changes"):
            return "switch --force discards uncommitted changes"
        reset = [value for name, value in opts if name in ("-C", "--force-create")]
        if reset:
            return self.reset_branch("switch -C", reset[-1], pos[0] if pos else "HEAD")
        return None

    def rule_tag(self, rest):
        opts, _ = parse(rest, short_arg="mFu", long_arg=(
            "--message", "--file", "--local-user", "--sort", "--contains", "--no-contains", "--points-at",
            "--merged", "--no-merged", "--format", "--cleanup", "--trailer"))
        if has(opts, "-d", "--delete"):
            return ("deleting a tag removes a release marker that other clones, tools and people rely on; leave it "
                    "in place and create a tag under a new name instead")
        return None

    def rule_worktree(self, rest):
        sub = next((a for a in rest if not a.startswith("-")), "")
        if sub == "remove" and has(parse(rest[rest.index(sub) + 1:])[0], "-f", "--force"):
            return ("worktree remove --force deletes a worktree's uncommitted and untracked changes (or a locked "
                    "worktree); commit or stash the work in it, then remove it without --force")
        return None

    def rule_filter_branch(self, rest):
        return ("filter-branch rewrites every commit of the branches it touches; ask the operator to run it on a "
                "fresh clone")

    def rule_filter_repo(self, rest):
        return ("filter-repo rewrites the whole history and drops the origin remote; ask the operator to run it on "
                "a fresh clone")

    def rule_rm(self, rest):
        opts, pos = parse(rest, long_arg=("--pathspec-from-file",))
        if not has(opts, "-f", "--force") or has(opts, "--cached", "-n", "--dry-run"):
            return None
        if has(opts, "--pathspec-from-file"):
            return "pathspecs read from a file can't be checked and may cover the whole tree"
        tree = resolve(self.cwd, self.work_tree) if self.work_tree else None
        if any(whole_tree(p) or repo_root(self.cwd, p, tree) for p in pos):
            return ("rm -f over the whole tree deletes every tracked file together with its uncommitted changes; "
                    "remove the specific paths instead, or use rm --cached to untrack files without deleting them")
        return None

    def rule_checkout_index(self, rest):
        opts, _ = parse(rest, long_arg=("--prefix", "--stage"))
        prefix = [value for name, value in opts if name == "--prefix"]
        elsewhere = (prefix and posixpath.normpath(prefix[-1] or ".") != ".") or has(opts, "--temp")
        if has(opts, "-f", "--force") and has(opts, "-a", "--all", "--stdin") and not elsewhere:
            return ("checkout-index -a -f overwrites every file in the working tree with the index, discarding "
                    "uncommitted changes; restore the specific files instead (git checkout-index -f -- <file>)")
        return None

    def forced_refspecs(self, verb, opts, pos):
        """The reason a forced fetch/pull refspec (+src:dst, or --force) overwrites a local branch or tag and orphans
        commits only it has, or None. The fetched value isn't known yet, so it doesn't count as keeping them."""
        force = has(opts, "-f", "--force")
        # --refmap and -c remote.<name>.fetch map the fetched refs like a command-line refspec, with --all too
        specs = [value for name, value in opts if name == "--refmap"]
        specs += [value for key, _, value in (entry.partition("=") for entry in self.config)
                  if re.fullmatch(r"remote\..+\.fetch", key.lower())]
        # with --all or --multiple every operand is a remote; refspecs in its config files aren't modelled
        i = len(pos) if has(opts, "--all", "--multiple", "-m") and verb == "fetch" else 1
        while i < len(pos):
            if pos[i] == "tag" and i + 1 < len(pos):
                specs.append(f"refs/tags/{pos[i + 1]}:refs/tags/{pos[i + 1]}")
                i += 1
            else:
                specs.append(pos[i])
            i += 1
        if force and has(opts, "--stdin"):
            specs.append("+<stdin>:<stdin>")
        for spec in specs:
            plus = spec.startswith("+")
            src, colon, dst = spec.removeprefix("+").partition(":")
            if not (plus or force) or not colon or not dst or spec.startswith("^"):
                continue  # a fast-forward only update, or no local destination
            ref = local_ref(dst)
            if not ref.startswith(("refs/heads/", "refs/tags/")) or not self.judged():
                continue  # remote-tracking refs are the remote's copy, overwritten on every fetch
            try:
                if src == "<stdin>":
                    raise Uncheckable("its refspecs come from stdin")
                if "*" in ref:
                    raise Uncheckable(f"{spec} can overwrite many local refs at once")
                lost = self.orphaned(ref)
            except Uncheckable as exc:
                return (f"a forced {verb} into a local branch or tag can't be checked for lost commits ({exc}); "
                        f"fetch into a new branch name, or drop the + and --force so only a fast-forward happens")
            if lost:
                return (f"{verb} {spec} would overwrite {ref} and orphan {plural(lost)} that no other ref reaches; fetch into "
                        f"a new branch name ({src}:<new-name>), or drop the + and --force so only a fast-forward "
                        "happens")
        return None

    def rule_fetch(self, rest):
        opts, pos = parse(rest, short_arg="jo", long_arg=(
            "--jobs", "--server-option", "--depth", "--deepen", "--shallow-since", "--shallow-exclude",
            "--upload-pack", "--refmap", "--negotiation-tip", "--recurse-submodules-default", "--submodule-prefix",
            "--filter"))
        return self.forced_refspecs("fetch", opts, pos)

    def rule_restore(self, rest):
        opts, pos = parse(rest, short_arg="s", long_arg=("--source", "--pathspec-from-file"))
        worktree = has(opts, "-W", "--worktree") or not has(opts, "-S", "--staged")
        if worktree and has(opts, "--pathspec-from-file"):
            return "pathspecs read from a file can't be checked and may cover the whole tree"
        if worktree and any(whole_tree(p) for p in pos):
            return "restoring the whole working tree discards uncommitted changes"
        return None

    def rule_stash(self, rest):
        sub = next((a for a in rest if not a.startswith("-")), "")
        return "stash drop/clear deletes stashed work" if sub in ("drop", "clear") else None

    def rule_commit(self, rest):
        opts, _ = parse(rest, short_arg="mFcCt", short_opt="uS", long_arg=(
            "--message", "--file", "--reuse-message", "--reedit-message", "--fixup", "--squash", "--author",
            "--date", "--template", "--trailer", "--cleanup"))
        return "--no-verify skips the pre-commit gate" if has(opts, "-n", "--no-verify") else None

    def no_verify(self, opts):
        return "--no-verify skips the repo's git hooks" if has(opts, "--no-verify") else None

    def rule_merge(self, rest):
        return self.no_verify(parse(rest, short_arg="mFsX", long_arg=("--message", "--file", "--strategy"))[0])

    def rule_pull(self, rest):
        opts, pos = parse(rest, short_arg="sXoj", long_arg=(
            "--strategy", "--strategy-option", "--jobs", "--server-option", "--depth", "--deepen", "--shallow-since",
            "--shallow-exclude", "--upload-pack", "--negotiation-tip"))
        return self.no_verify(opts) or self.forced_refspecs("pull", opts, pos)

    def rule_am(self, rest):
        return self.no_verify(parse(rest)[0])

    def rule_rebase(self, rest):
        opts, _ = parse(rest, short_arg="sXx", long_arg=("--exec", "--onto", "--strategy", "--strategy-option"))
        for name, value in opts:
            if name in ("-x", "--exec"):
                reason = self.inner(value)
                if reason:
                    return f"rebase --exec runs it: {reason}"
        return self.no_verify(opts)

    def rule_submodule(self, rest):
        if "foreach" not in rest:
            return None
        command = rest[rest.index("foreach") + 1:]
        while command and command[0].startswith("-"):
            command = command[1:]
        reason = self.inner(" ".join(command))
        return f"submodule foreach runs it: {reason}" if reason else None

    def rule_config(self, rest):
        opts, pos = parse(rest, short_arg="f", long_arg=(
            "--file", "--blob", "--type", "--default", "--comment", "--value"))
        action = pos.pop(0) if pos and pos[0] in (
            "set", "unset", "get", "list", "edit", "rename-section", "remove-section") else None
        key = pos[0].lower() if pos else ""
        sections = action in ("rename-section", "remove-section") or has(opts, "--rename-section", "--remove-section")
        if sections and key == "core":
            return "removing the core section drops core.hooksPath"
        if key == "core.hookspath" and (
                action in ("set", "unset") or has(opts, "--unset", "--unset-all", "--replace-all", "--add")
                or (action is None and len(pos) > 1 and not has(opts, "--get", "--get-all", "--get-regexp"))):
            return "changing core.hooksPath switches off the repo's git hooks"
        if key.startswith("alias.") and len(pos) > 1 and not has(opts, "--get", "--get-all", "--get-regexp"):
            value = pos[1]
            words = split_words(value)
            reason = self.inner(value[1:]) if value.startswith("!") else (
                Git(self.judge, self.cwd, self.env).subcommand(words[0], words[1:]) if words else None)
            return f"the alias would run a blocked command: {reason}" if reason else None
        return None

    def rule_update_ref(self, rest):
        opts, pos = parse(rest, short_arg="m")
        if has(opts, "-d") or (len(pos) > 1 and re.fullmatch(r"0{40}|0{64}", pos[1])):
            return "update-ref -d deletes a ref"
        if has(opts, "--stdin"):
            return ("update-ref --stdin reads its updates from stdin, which the guard can't check; run one "
                    "git update-ref <ref> <new> per ref instead")
        if len(pos) < 2 or not self.judged():
            return None  # git refuses the command itself
        ref, new = pos[0], pos[1]
        try:
            if new.startswith(("-", "^")) or ".." in new:
                raise Uncheckable(f"new value {new!r}")
            if ref == "HEAD" and not has(opts, "--no-deref"):
                ref = self.git("rev-parse", "--symbolic-full-name", "HEAD") or "HEAD"  # moves the checked-out branch
            lost = self.orphaned(ref, [new])
        except Uncheckable as exc:
            return (f"update-ref overwrites {ref}, and the guard could not check whether that orphans commits ({exc}); "
                    "create a new ref instead")
        if lost:
            return (f"update-ref would orphan {plural(lost)} on {ref} that no other branch, remote-tracking ref or "
                    f"tag reaches, nor {new}; create a new ref instead, or push or tag the old commits first")
        return None

    def rule_reflog(self, rest):
        sub = next((a for a in rest if not a.startswith("-")), "")
        return "expiring or deleting reflog entries destroys the recovery trail" if sub in ("expire", "delete") else None

    def rule_gc(self, rest):
        prune = {value.lower() for name, value in parse(rest)[0] if name == "--prune"}
        return "gc --prune=now deletes unreachable objects immediately" if prune & {"now", "all"} else None

    def rule_prune(self, rest):
        return None if has(parse(rest)[0], "-n", "--dry-run") else "git prune deletes unreachable objects immediately"


def check_command(command: str, cwd: str, scope: str) -> str | None:
    """The reason to block command run from cwd, or None. scope is "always" or "managed"."""
    if scope not in ("always", "managed"):
        raise ValueError(f"unknown scope {scope!r}")
    return Judge(os.path.abspath(cwd or os.getcwd()), scope).string(command)


USAGE = "usage: git_guard.py [--scope always|managed]  (the hook JSON on stdin)"


def main(argv=None):
    """The hook. Its one option is parsed by hand: argparse costs more than the check itself on every Bash call.
    Bad usage exits 2 (blocks), like the guard's other failures in always scope."""
    args = list(sys.argv[1:] if argv is None else argv)
    if args in (["-h"], ["--help"]):
        print(f"{USAGE}\n\n{__doc__.splitlines()[0]}")
        return 0
    if len(args) == 1 and args[0].startswith("--scope="):
        args = ["--scope", args[0].partition("=")[2]]
    scope = "always" if not args else args[1] if len(args) == 2 and args[0] == "--scope" else None
    if scope not in ("always", "managed"):
        print(USAGE, file=sys.stderr)
        return 2
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8", errors="replace"))  # UTF-8 whatever the locale
        command = (payload.get("tool_input") or payload.get("toolInput") or {}).get("command")  # grok: camelCase
        cwd = payload.get("cwd") or os.getcwd()
        reason = check_command(command, str(cwd), scope) if isinstance(command, str) and command.strip() else None
    except Exception as exc:  # noqa: BLE001 - fail closed for the project hook, never break other sessions
        if scope == "managed":
            return 0
        reason = f"the guard could not check this command ({type(exc).__name__}: {exc})"
    if reason:
        print(f"BLOCKED by the destructive-git guard (H5): {reason}. The operator has not granted this in agent "
              "sessions; ask them to run it.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
