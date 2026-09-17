#!/usr/bin/env python3
"""Break one thing at a time in a copy of the tree; every break must turn a test red.

A test suite can pass because it asserts what happens to be true rather than what
the code is for. This runs each listed mutation against a fresh copy of the tree
and reports any that no test objects to. Run it after changing a guard:

    runner/tests/mutate.py            # every mutation
    runner/tests/mutate.py noflock    # one, by name
"""
import io, os, re, shutil, subprocess, sys, tempfile

SRC = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
# somewhere with room for a copy of the tree per mutation; not /tmp, which is
# RAM on the review box
WORK = os.environ.get("REVIEW_MUTATE_DIR") or tempfile.gettempdir()
RUN = "runner/bin/run-reviewprs.sh"
AUT = "runner/bin/review-auth.sh"
ENV = "runner/bin/review-env.sh"
BOTH = (RUN, AUT)

# name -> (file(s), old, new)   old must appear exactly once in each file
M = [
 ("rootowner", ENV, ' -o ! -user "$(id -u)"', ''),
 ("unknownid", RUN, 'if [ -z "$ACCOUNT" ]; then', 'if false; then'),
 ("rejectcodex", RUN, '[ "$CODEX_ACCOUNT" != "$WANT" ]', 'false'),
 ("disagree", RUN, '[ "$CLAUDE_CLI_ACCOUNT" != "$CLAUDE_REC_ACCOUNT" ]', 'false'),
 ("badrecord", RUN, 'WANT=$(read_account_file "$CLAUDE_DIR/ACCOUNT")',
                    'WANT=$(read_account_file "$CLAUDE_DIR/ACCOUNT" || true)'),
 ("noflock", AUT, 'flock 9 ||', 'true 9 ||'),
 ("earlyrelease", AUT, 'was=$(readlink "$link"', 'exec 9>&-; was=$(readlink "$link"'),
 ("noduddash", AUT, 'ln -sfn -- "$was"', 'ln -sfn "$was"'),
 ("noswitchback", AUT, 'ln -sfn -- "$was" "$tmp2"', 'true "$was" "$tmp2"'),
 ("lyingrevert", AUT, 'if [ "$back" = 1 ]; then', 'if true; then'),
 ("nolocksymlink", AUT, '[ ! -L "$lockf" ] ||', '[ 1 = 1 ] ||'),
 ("norootfirst", AUT, '    auth_root_ok || { echo "refusing to switch"; exit 1; }\n', ''),
 ("nostatusrec", AUT, 'note="  BAD ACCOUNT RECORD - runs will refuse"', 'true'),
 ("noconflict", AUT, '&& note="  CONFLICT: directory records $rec - runs will refuse"', '&& true'),
 ("alwaysconflict", AUT, '[ -n "$rec" ] && [ "$rec" != "$got" ]', '[ -n "$rec" ]'),
 ("nomismatch", AUT, '&& note="  MISMATCH: expected $want - runs will refuse" ;;', '&& true ;;'),
 ("noidunknown", AUT, '*:"signed in")', '*:"never happens")'),
 ("nodefaultrow", AUT, 'if [ "$rc" -eq 1 ]; then', 'if false; then'),
 ("statusapikey", AUT, 'elif [ "$got" = api-key ]; then', 'elif false; then'),
 ("statusmethod", AUT, '(got.get("authMethod") or "claude.ai") != "claude.ai"', 'False'),
 ("statusprovider", AUT, '(got.get("apiProvider") or "firstParty") != "firstParty"', 'False'),
 ("statusdir", AUT, 'print("wrong-directory"); raise SystemExit', 'pass'),
 ("statuspartial", AUT, 'if sum(1 for k in keys if got.get(k)) not in (0, 3):', 'if False:'),
 ("statusfailedunset", AUT, 'if ! clear_inherited_credentials; then',
                            'clear_inherited_credentials; if false; then'),
 ("statusstderr", AUT, 'dir=$(review_auth "$tool" "$role" 2>/dev/null); rc=$?',
                       'dir=$(review_auth "$tool" "$role" 2>&1); rc=$?'),
 ("blindaccount", AUT, 'account_of() {', 'account_of() { echo ""; return 0; }\nunused_of() {'),
 ("lazyswitch", AUT, 'mv -T "$tmp" "$link" ||',
                     '[ "$(readlink "$link")" = "$tool-$acct" ] && exit 0; mv -T "$tmp" "$link" ||'),
 ("noenvsweep", ENV, 'for v in $CLEARED_VARS; do', 'for v in ; do'),
 ("nounsetcheck", ENV, '        [ -z "${!v+x}" ] || CLEARED_FAILED="$CLEARED_FAILED $v"\n', ''),
 ("skipunsets", ENV, '        unset "$v" 2>/dev/null || true\n',
                     '        case "$v" in OPENAI_*|CODEX_*) ;; *) unset "$v" 2>/dev/null || true ;; esac\n'),
 ("suffixgap", ENV, '\\|USE_[A-Z0-9_]\\{1,\\}\\)[A-Z0-9_]*', '\\|USE_[A-Z0-9_]\\{1,\\}\\)'),
 ("noopenai", ENV, '\\(ANTHROPIC\\|OPENAI\\)', '\\(ANTHROPIC\\)'),
 ("nocodex", ENV, '\\(CLAUDE\\|CODEX\\)_[A-Z0-9_]*\\(', '\\(CLAUDE\\)_[A-Z0-9_]*\\('),
 ("nofallbackpin", RUN, 'd=$(readlink -f "$HOME/.$tool" 2>/dev/null) || d=""\n           [ -n "$d" ] || { unset "$var"; return 0; } ;;',
                        'unset "$var"; return 0 ;;'),
 ("nomethod", RUN, '    claude.ai|-) ;;', '    *) ;;'),
 ("noprovider", RUN, '    firstParty|-) ;;', '    *) ;;'),
 ("noclidir", RUN, 'if [ "$CLAUDE_CLI_DIR" != "-" ]', 'if false && [ "$CLAUDE_CLI_DIR" != "-" ]'),
 ("partialmeta", RUN, 'if [ "$CLAUDE_META" != 0 ] && [ "$CLAUDE_META" != 3 ]; then', 'if false; then'),
 ("codexprovider", RUN, 'if [ -n "$CODEX_PROVIDER" ]; then', 'if false; then'),
 ("codexapikey", RUN, '(d.get("auth_mode") or "").lower() in ("apikey", "api_key")', 'False'),
 ("implicitkey", RUN, 'd.get("OPENAI_API_KEY") and not d.get("auth_mode")', 'False'),
 ("nourlscan", BOTH, '        if m and m.group(1) not in OK_HOSTS:', '        if False:'),
 ("noenvkey", BOTH, '            if key in ("env_key", "api_key", "env_http_headers", "http_headers"):',
                    '            if False:'),
 ("noreqauth", BOTH, '            elif key == "requires_openai_auth" and v is False:', '            elif False:'),
 ("noprofileprov", BOTH, '            elif key == "model_provider" and v != "openai":', '            elif False:'),
 ("nounreadable", BOTH, '    print("unreadable-config")\n    raise SystemExit', '    raise SystemExit'),
 ("fieldsplit", RUN,
   '{ read -r CLAUDE_LOGGED; read -r CLAUDE_CLI_ACCOUNT; read -r CLAUDE_METHOD\n'
   '  read -r CLAUDE_PROVIDER; read -r CLAUDE_CLI_DIR; read -r CLAUDE_META; } <<EOS',
   'read -r CLAUDE_LOGGED CLAUDE_CLI_ACCOUNT CLAUDE_METHOD CLAUDE_PROVIDER '
   'CLAUDE_CLI_DIR CLAUDE_META <<EOS'),
]

def main():
    only = sys.argv[1:]
    work = tempfile.mkdtemp(prefix="mutate-", dir=WORK)
    home = os.path.join(work, "h"); os.makedirs(home)
    tree = os.path.join(work, "r")
    bad = 0
    try:
        for name, files, old, new in M:
            if only and name not in only:
                continue
            shutil.rmtree(tree, ignore_errors=True)
            shutil.copytree(SRC, tree)
            for f in ((files,) if isinstance(files, str) else files):
                path = os.path.join(tree, f)
                s = io.open(path).read()
                if s.count(old) != 1:
                    print("  %-16s NOT APPLIED (%d matches in %s)"
                          % (name, s.count(old), f)); bad += 1; break
                io.open(path, "w").write(s.replace(old, new, 1))
            else:
                fails = 0
                for t in sorted(os.listdir(os.path.join(tree, "runner/tests"))):
                    if not t.startswith("test_"):
                        continue
                    r = subprocess.run(["python3", os.path.join(tree, "runner/tests", t)],
                                       capture_output=True, text=True,
                                       env={"HOME": home, "PATH": "/usr/bin:/bin"})
                    fails += len(re.findall(r"^(?:FAIL|ERROR):", r.stdout + r.stderr, re.M))
                if fails:
                    print("  %-16s caught (%d)" % (name, fails))
                else:
                    print("  %-16s SURVIVES" % name); bad += 1
                sys.stdout.flush()
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print("%d of %d mutations unaccounted for" % (bad, len(M)))
    return 1 if bad else 0

sys.exit(main())
