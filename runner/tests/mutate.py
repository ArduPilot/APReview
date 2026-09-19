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
PAGE = "runner/bin/make-runs-page.py"
PRB = "runner/bin/claude-usage-probe.sh"
MIG = "runner/bin/migrate-auth-root.sh"

# name -> (file(s), old, new)   old must appear exactly once in each file
M = [
 ('reresolve', RUN, 'd=$(role_config_dir "$tool" "$ROLE" "$d")',
                   'd=$(role_config_dir "$tool" "$ROLE")'),
 ('denyfsroot', RUN, '    if path == "/":', '    if False:'),
 ('denytilderoot', RUN, '    elif path == "~":', '    elif False:'),
 ('denydot', RUN, 'any(part in (".", "..") for part in path.split("/"))',
                  'False'),
 ('baretool', RUN, 'def denies_auth(rule):',
                  'def denies_auth(rule):\n    if rule == "Read":\n        return True'),
 ('denylist', RUN, 'if not isinstance(deny, list) or not all(isinstance(r, str) for r in deny):',
                  'if False:'),
 ('ruleunescape', RUN, 'path = rule[5:-4].replace("\\\\\\\\", "\\\\")',
                  'path = rule[5:-4]'),
 ('ruleescape', RUN, '    pattern = pattern.replace("\\\\\\\\", "\\\\\\\\\\\\\\\\")\n',
                  ''),
 ('loginescapelevel', AUT, 'auth = auth.replace("\\\\\\\\", "\\\\\\\\\\\\\\\\")\n',
                  ''),
 ('unusedprovider', ENV, '                if key == "model_providers" and name != "openai":',
                  '                if False:'),
 ('probebroken', PRB, 'if [ "$PROBE_RC" -eq 2 ]; then',
                  'if false; then'),
 ('probeownrun', PRB, ' && [ -z "${REVIEW_ROLE:-}" ]',
                  ''),
 ('quotaglob', PAGE, "glob.escape(directory), 'sessions'",
                  "directory, 'sessions'"),
 ('authglob', PAGE, "glob.escape(AUTH), tool + '-*'",
                  "AUTH, tool + '-*'"),
 ('claudeglob', PAGE, "os.path.join(glob.escape(r), sub) for r in account_dirs('claude')",
                  "os.path.join(r, sub) for r in account_dirs('claude')"),
 ('logglob', PAGE, "glob.escape(LOGS), 'reviewprs-*.log'",
                  "LOGS, 'reviewprs-*.log'"),
 ('quotadeleted', PAGE, '    samples = []',
                  '    samples = quota if directory and not os.path.isdir(directory) else []'),
 ('denysubstring', RUN, 'any(denies_auth(r) for r in deny)', 'any(auth in r or "review.auth" in r for r in deny)'),
 ('denyescaping', RUN, 'pattern = re.sub(r"([\\\\*?\\[\\]])", r"\\\\\\1", os.path.realpath(auth))', 'pattern = os.path.realpath(auth)'),
 ('loginescaping', AUT, 'auth = re.sub(r"([\\\\*?\\[\\]])", r"\\\\\\1", os.path.realpath(sys.argv[1]))', 'auth = os.path.realpath(sys.argv[1])'),
 ('permissionfinish', RUN, '    echo "finish=$(date -Is) status=preflight-failed"\n', ''),
 ('workfinish', RUN, '    echo "finish=$(date -Is) status=no-work-dir"\n', ''),
 ('pageterminal', PAGE, "elif re.search(r'status=(preflight-failed|no-work-dir)\\b', txt):", 'elif False:'),
 ('pagefailcount', PAGE, "'wrong-account', 'preflight-failed', 'no-work-dir'))", "'wrong-account'))"),
 ('denytool', RUN, 'not rule.startswith("Read(")', 'False'),
 ('denycoverage', RUN, 'os.path.commonpath([root, os.path.realpath(auth)]) == root', 'True'),
 ('denyancestor', RUN, 'os.path.commonpath([root, os.path.realpath(auth)]) == root', 'root == os.path.realpath(auth)'),
 ('denysuggestion', RUN, 'json.dumps("Read(/%s/**)" % pattern)', 'json.dumps("Read(%s/**)" % pattern)'),
 ('loginsettings', AUT, '"Bash(git push)", "Bash(git push:*)", "Read(/%s/**)" % auth', '"Bash(git push)", "Bash(git push:*)", "Read(%s/**)" % auth'),
 ('installsettings', "docs/review-box.md", '"Read(~/review.auth/**)"', '"Edit(~/review.auth/**)"'),
 ('readmesettings', "README.md", '"Read(~/review.auth/**)"', '"Edit(~/review.auth/**)"'),
 ('usetool', AUT, '    tool="$2"; role="$3"; acct="$4"\n    case "$tool" in claude|codex) ;; *) echo "unknown tool: $tool"; exit 2 ;; esac', '    tool="$2"; role="$3"; acct="$4"'),
 ('logintool', AUT, '    tool="$2"; acct="$3"\n    case "$tool" in claude|codex) ;; *) echo "unknown tool: $tool"; exit 2 ;; esac', '    tool="$2"; acct="$3"'),
 ('plainname', AUT, 'plain_name() {', 'plain_name() { return 0; }\nunused_plain_name() {'),
 ('rolealias', AUT, '{ [ -L "$dir" ] && [ "$(readlink -f "$dir")" = "$(readlink -f "$link")" ]; }', 'false'),
 ('quotaenv', PAGE, "_codex_pick = role_dir('codex')", "_codex_pick = os.environ.get('CODEX_HOME') or role_dir('codex')"),
 ('quotamixed', PAGE, 'samples = codex_quotas[directory][0]', 'samples = quota'),
 ('quotaunknown', PAGE, '    samples = []', '    samples = quota'),
 ('quotaidentity', PAGE, "codex_identity(directory) == r.get('codex_account')", 'True'),
 ('quotadangling', PAGE, "os.path.lexists(link) or role != 'default'", "role != 'default'"),
 ('quotaown', PAGE, "    return os.path.realpath(os.path.join(HOME, '.' + tool))", '    return None'),
 ('quotalabel', PAGE, 'html.escape(quota_role)', "''"),
 ('quotaspace', PAGE, 'home (.+)\\)$', 'home (\\S+)\\)$'),
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
 ("statuspartial", AUT, 'if sum(1 for k in keys if got.get(k)) != 3:', 'if False:'),
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
 ("nofallbackpin", ENV, '[ -n "$d" ] && [ "$d" = "$(readlink -f "$own" 2>/dev/null)" ] \\\n        && [ ! -L "$own" ] && d=""',
                        '[ -n "$d" ] && [ "$d" = "$(readlink -f "$own" 2>/dev/null)" ] && d=""'),
 ("nomethod", RUN, '    claude.ai|-) ;;', '    *) ;;'),
 ("noprovider", RUN, '    firstParty|-) ;;', '    *) ;;'),
 ("noclidir", RUN, 'if [ "$CLAUDE_CLI_DIR" != "-" ]', 'if false && [ "$CLAUDE_CLI_DIR" != "-" ]'),
 ("partialmeta", RUN, 'if [ "$CLAUDE_META" != 3 ]; then', 'if false; then'),
 ("codexprovider", RUN, 'if [ -n "$CODEX_PROVIDER" ]; then', 'if false; then'),
 ("codexapikey", ENV, '(mode or "").lower() in ("apikey", "api_key")', 'False'),
 ("implicitkey", ENV, 'd.get("OPENAI_API_KEY") and not mode', 'False'),
 ("nourlscan", ENV, '            if not safe_url(v):', '            if False:'),
 ("noenvkey", ENV, '        if key in ("env_key",',
                    '        if False and key in ("env_key",'),
 ("noreqauth", ENV, '        elif key == "requires_openai_auth" and v is not True:', '        elif False:'),
 ("noprofileprov", ENV, '        elif key == "model_provider" and v != "openai":', '        elif False:'),
 ("nounreadable", ENV, '        print("unreadable-config")\n        raise SystemExit',
                       '        raise SystemExit'),
 ("fieldsplit", RUN,
   '{ IFS= read -r CLAUDE_LOGGED; IFS= read -r CLAUDE_CLI_ACCOUNT; IFS= read -r CLAUDE_METHOD\n'
   '  IFS= read -r CLAUDE_PROVIDER; IFS= read -r CLAUDE_CLI_DIR; IFS= read -r CLAUDE_META; } <<EOS',
   'read -r CLAUDE_LOGGED CLAUDE_CLI_ACCOUNT CLAUDE_METHOD CLAUDE_PROVIDER '
   'CLAUDE_CLI_DIR CLAUDE_META <<EOS'),
 ('urlscheme', ENV, 'u.scheme == "https"', 'u.scheme in ("http", "https")'),
 ('urlport', ENV, 'u.port in (None, 443)', 'True'),
 ('mcpfalsepositive', ENV, '        elif provider and isinstance(v, (dict, list)):', '        elif key == "mcp_servers":\n            bad.append(setting)\n        elif provider and isinstance(v, (dict, list)):'),
 ('bearertoken', ENV, '"experimental_bearer_token", "auth", "aws", "query_params"', '"unused", "auth", "aws", "query_params"'),
 ('credstore', ENV, 'key == "cli_auth_credentials_store" and v != "file"', 'False'),
 ('forcedlogin', ENV, 'key == "forced_login_method" and v != "chatgpt"', 'False'),
 ('danglingconfig', ENV, 'os.path.lexists(path)', 'os.path.exists(path)'),
 ('statusdetail', AUT, '                other=$(check_codex_config "$dir")', '                other=$(check_codex_config "$dir"); other=${other%%:*}'),
 ('statusshape', AUT, '*) note="  MISMATCH: expected $want - runs will refuse" ;;', '*) note="  (recorded $want, reported differently)" ;;'),
 ('statusunsafeprobe', AUT, 'runs will refuse:$CLEARED_FAILED"\n    exit 1', 'runs will refuse:$CLEARED_FAILED"'),
 ('cliexit', RUN, 'reply=$(claude auth status --json 2>/dev/null) || reply=""', 'reply=$(claude auth status --json 2>/dev/null) || true'),
 ('statusexit', AUT, 'if out.returncode != 0:', 'if False:'),
 ('metacontrol', BOTH, 'any(ord(c) < 32 or ord(c) == 127 for c in v)', 'False'),
 ('metaempty', BOTH, 'if k != "email" and (not v or v == "-"):', 'if False:'),
 ('metaloggertype', RUN, 'type(d.get("loggedIn")) is not bool', 'False'),
 ('statusloggertype', AUT, 'type(got.get("loggedIn")) is not bool', 'False'),
 ('metawhitespace', RUN, 'IFS= read -r CLAUDE_CLI_DIR', 'read -r CLAUDE_CLI_DIR'),
 ('cliidentity', BOTH, 'raise ValueError("invalid email")', 'pass'),
 ('recordidentity', RUN, 'print("invalid-record")', 'print(email)'),
 ('staterecordidentity', AUT, '    print("invalid-record")\nelse:\n    print(email)', '    print(email)\nelse:\n    print(email)'),
 ('codextokens', ENV, '    if not all(isinstance(tok.get(k), str) and tok[k]', '    if False and not all(isinstance(tok.get(k), str) and tok[k]'),
 ('codexmode', ENV, 'if mode not in (None, "chatgpt"):', 'if False:'),
 ('codexidentity', ENV, 'raise ValueError("missing account id")', 'pass'),
 ("nometa", RUN, 'if [ "$CLAUDE_META" != 3 ]; then',
                 'if [ "$CLAUDE_META" != 0 ] && [ "$CLAUDE_META" != 3 ]; then'),
 ("statusnometa", AUT, 'if sum(1 for k in keys if got.get(k)) != 3:',
                      'if sum(1 for k in keys if got.get(k)) not in (0, 3):'),
 ("rolecase", RUN, 'case "$(printf %s "$MODE" | tr "[:upper:]" "[:lower:]")" in\n    rsync|rsyncproject',
                  'case "$MODE" in\n    rsync|rsyncproject'),
 ("roleurl", RUN, '|https://github.com/rsyncproject/rsync/pull/*', ''),
 ("mcpcallback", ENV, 'key == "mcp_oauth_callback_url" and not provider', 'False'),
 ("codexjwt", ENV, '    parts = tok["id_token"].split(".")', '    parts = ["e30", "e30", "stub"]'),
 ("urlparser", ENV, '        u = urlsplit(value)\n        return (isinstance(value, str) and not re.search(r"[\\s\\\\]", value)\n                and u.scheme == "https" and u.hostname in OK_HOSTS\n                and u.username is None and u.password is None\n                and u.port in (None, 443) and not u.query and not u.fragment)',
                    '        m = re.match(r"https?://([^/:]+)", value.strip())\n        return not m or m.group(1) in OK_HOSTS'),
 ("oneconfiglayer", ENV, 'paths += sorted(glob.glob(', 'paths += list((lambda *a: [])('),
 ("otelscan", ENV, '        elif isinstance(v, dict):\n            # Every other table too',
                   '        elif False:\n            # Every other table too'),
 # credentials moved inside the directory the reviewing agent is given
 ("nocreddeny", RUN, 'if auth and not any(denies_auth(r) for r in deny):',
                     'if False:'),
 ("credrealpath", PAGE, '_files.setdefault(os.path.realpath(_f), _f)',
                        '_files.setdefault(_f, _f)'),
 # the one-time move of the accounts
 ("migmerge", MIG, 'if [ -e "$NEW" ]; then', 'if false; then'),
 ("migstale", MIG, "or r == \"Read(~/review/**)\")", "or False)"),
 ("migdry", MIG, "    if dry:\n        continue", "    if False:\n        continue"),
 # the accounts live outside the directory the agent is handed
 ("authinsideroot", ENV, 'export REVIEW_AUTH="${REVIEW_AUTH:-$REVIEW_ROOT.auth}"',
                         'export REVIEW_AUTH="${REVIEW_AUTH:-$REVIEW_ROOT/auth}"'),
 # a run killed mid-flight never writes its own finish line
 ("nostall", PAGE, "        if last and now - last > grace and last > start:",
                   "        if False:"),
 ("stallgrace", PAGE, "STALL_GRACE = datetime.timedelta(minutes=30)",
                      "STALL_GRACE = datetime.timedelta(minutes=0)"),
 ("stalllockwait", PAGE, "        if w and 'lock acquired' not in txt:",
                         "        if False:"),
 # a refusal that nobody can see is a slot that goes quiet
 ("refusalbeforelog", RUN, 'export REVIEW_ROLE="$ROLE"\nif ! clear_inherited_credentials',
                           'export REVIEW_ROLE="$ROLE"\nexec >/dev/null\nif ! clear_inherited_credentials'),
 # the dashboard: it describes the accounts the roles select
 ("pageaccounts", PAGE, "sorted(glob.glob(os.path.join(glob.escape(AUTH), tool + '-*')))", "[]"),
 ("pageown", PAGE, "for d in [os.path.join(HOME, '.' + tool)] + \\", "for d in [] + \\"),
 ("pagestatus", PAGE, "elif re.search(r'status=wrong-\\w+-account', txt):",
                      "elif 'status=wrong-claude-account' in txt:"),
 ("probefollow", PRB, 'PROBE_DIR=$(role_config_dir claude default "$PROBE_DIR")',
                      'PROBE_DIR=""'),
 ("proberunenv", PRB, 'if [ -z "${CLAUDE_CONFIG_DIR:-}" ] && [ -z "${REVIEW_ROLE:-}" ]; then',
                      'if true; then'),
 ("earlyprobe", AUT, '    lockf="$AUTH/.$tool-$role.lock"',
                     '    got=$(account_of "$tool" "$dir")\n    lockf="$AUTH/.$tool-$role.lock"'),
]

# An unrelated failure is not evidence for a particular guard. Each mutation
# must break its regression after the complete, unchanged suite has passed.
REGRESSION = {
    'reresolve': 'Guard.test_a_switch_after_validation_does_not_resolve_the_role_again',
    'denyfsroot': 'Guard.test_read_denials_can_cover_the_home_or_filesystem_root',
    'denytilderoot': 'Guard.test_read_denials_can_cover_the_home_or_filesystem_root',
    'denydot': 'Guard.test_filesystem_normalization_does_not_prove_a_read_pattern',
    'baretool': 'Guard.test_disabling_read_alone_does_not_deny_the_path_to_other_readers',
    'denylist': 'Guard.test_a_deny_object_is_not_a_cli_permission_list',
    'ruleunescape': 'Guard.test_rule_escaping_and_pattern_escaping_are_separate',
    'ruleescape': 'Guard.test_rule_escaping_and_pattern_escaping_are_separate',
    'loginescapelevel': 'Guard.test_rule_escaping_and_pattern_escaping_are_separate',
    'unusedprovider': 'Guard.test_an_unused_provider_definition_does_not_route_requests',
    'probebroken': 'UsageProbe.test_a_broken_hourly_role_does_not_probe_the_fallback',
    'probeownrun': 'UsageProbe.test_a_run_using_the_tools_home_does_not_follow_a_later_switch',
    'quotaglob': 'Dashboard.test_glob_characters_in_an_account_name_cannot_select_another_account',
    'authglob': 'Dashboard.test_glob_characters_in_the_auth_root_do_not_hide_accounts',
    'claudeglob': 'Dashboard.test_glob_characters_in_a_claude_directory_do_not_hide_transcripts',
    'logglob': 'Dashboard.test_glob_characters_in_home_do_not_hide_run_logs',
    'quotadeleted': 'Dashboard.test_a_deleted_recorded_home_does_not_borrow_another_accounts_samples',
    'denysubstring': 'Guard.test_the_deny_rule_must_cover_reads_of_this_whole_auth_tree',
    'denyescaping': 'Guard.test_custom_auth_paths_are_escaped_in_login_and_refusal_instructions',
    'loginescaping': 'Guard.test_custom_auth_paths_are_escaped_in_login_and_refusal_instructions',
    'permissionfinish': 'Guard.test_a_permission_refusal_finishes_the_logged_run',
    'workfinish': 'Guard.test_a_missing_work_directory_finishes_the_logged_run',
    'pageterminal': 'Guard.test_a_permission_refusal_finishes_the_logged_run',
    'pagefailcount': 'Guard.test_a_permission_refusal_finishes_the_logged_run',
    'denytool': 'Guard.test_the_deny_rule_must_cover_reads_of_this_whole_auth_tree',
    'denycoverage': 'Guard.test_the_deny_rule_must_cover_reads_of_this_whole_auth_tree',
    'denyancestor': 'Guard.test_an_ancestor_read_denial_is_sufficient',
    'denysuggestion': 'Guard.test_the_suggested_read_rule_is_absolute_and_satisfies_the_preflight',
    'loginsettings': 'Guard.test_login_instructions_supply_working_settings',
    'installsettings': 'Guard.test_install_instructions_supply_working_settings',
    'readmesettings': 'Guard.test_readme_instructions_supply_working_settings',
    'usetool': 'Switching.test_unknown_tools_cannot_switch_roles',
    'logintool': 'Switching.test_unknown_tools_cannot_create_login_directories',
    'plainname': 'Switching.test_role_and_account_names_are_checked_before_paths_are_used',
    'rolealias': 'Switching.test_an_alias_of_the_current_role_is_refused_before_switching',
    'quotaenv': 'Dashboard.test_codex_card_follows_a_named_default_role_not_the_publisher',
    'quotamixed': 'Dashboard.test_codex_deltas_follow_each_logged_account_even_after_a_switch',
    'quotaunknown': 'Dashboard.test_codex_usage_without_matching_account_attribution_stays_unknown',
    'quotaidentity': 'Dashboard.test_codex_usage_without_matching_account_attribution_stays_unknown',
    'quotadangling': 'Dashboard.test_a_dangling_codex_role_does_not_show_the_fallbacks_meter',
    'quotaown': 'Dashboard.test_an_absent_codex_default_role_still_uses_the_tools_own_home',
    'quotalabel': 'Dashboard.test_codex_card_follows_a_named_default_role_not_the_publisher',
    'quotaspace': 'Dashboard.test_codex_deltas_follow_each_logged_account_even_after_a_switch',
    'rootowner': 'Guard.test_an_auth_root_we_do_not_own_stops_the_run',
    'unknownid': 'Guard.test_an_unknown_identity_with_a_record_stops_the_run',
    'rejectcodex': 'Guard.test_a_codex_account_file_that_disagrees_stops_the_run',
    'disagree': 'Guard.test_the_two_identity_sources_disagreeing_stops_the_run',
    'badrecord': 'Guard.test_an_unreadable_account_record_says_which_problem_it_is',
    'noflock': 'Switching.test_one_switch_at_a_time',
    'earlyrelease': 'Switching.test_the_lock_is_held_until_the_switch_has_been_validated',
    'noduddash': 'Switching.test_a_revert_to_an_option_shaped_target_is_not_claimed_falsely',
    'noswitchback': 'Switching.test_a_revert_to_an_option_shaped_target_is_not_claimed_falsely',
    'lyingrevert': 'Switching.test_a_revert_that_fails_says_so',
    'nolocksymlink': 'Switching.test_a_symlinked_lock_is_refused_rather_than_written_through',
    'norootfirst': 'Switching.test_an_unsafe_root_is_refused_before_the_lock_is_opened',
    'nostatusrec': 'StatusView.test_a_record_the_runner_cannot_read_is_flagged',
    'noconflict': 'StatusView.test_a_directory_that_disagrees_with_the_cli_is_flagged',
    'alwaysconflict': 'StatusView.test_a_matching_identity_is_not_flagged_as_a_conflict',
    'nomismatch': 'StatusView.test_a_record_naming_another_account_is_flagged',
    'noidunknown': 'StatusView.test_an_identity_that_cannot_be_checked_is_flagged',
    'nodefaultrow': 'StatusView.test_a_missing_default_role_is_shown_as_the_tools_own_account',
    'statusapikey': 'StatusView.test_a_codex_api_key_is_not_shown_as_an_account',
    'statusmethod': 'StatusView.test_a_token_login_is_not_shown_as_the_subscription',
    'statusprovider': 'StatusView.test_a_cloud_provider_is_not_shown_as_the_subscription',
    'statusdir': 'StatusView.test_credentials_read_from_another_directory_are_flagged',
    'statuspartial': 'StatusView.test_a_partial_answer_from_the_cli_is_flagged',
    'statusfailedunset': 'StatusView.test_a_credential_variable_that_cannot_be_unset_is_reported',
    'statusstderr': 'StatusView.test_a_warning_on_stderr_is_not_part_of_the_directory',
    'blindaccount': 'StatusView.test_it_reports_the_address_a_signed_in_role_will_run_as',
    'lazyswitch': 'Switching.test_switching_to_the_account_already_in_place_is_still_checked',
    'noenvsweep': 'Guard.test_an_alternate_credential_store_does_not_reach_the_cli',
    'nounsetcheck': 'Guard.test_a_credential_variable_that_cannot_be_unset_stops_the_run',
    'skipunsets': 'Guard.test_a_codex_credential_variable_is_cleared',
    'suffixgap': 'Guard.test_a_token_variable_with_a_suffix_does_not_reach_the_cli',
    'noopenai': 'Guard.test_an_openai_key_does_not_reach_the_cli',
    'nocodex': 'Guard.test_a_codex_credential_variable_is_cleared',
    'nomethod': 'Guard.test_a_token_login_stops_the_run',
    'noprovider': 'Guard.test_a_cloud_provider_stops_the_run',
    'noclidir': 'Guard.test_credentials_read_from_another_directory_stop_the_run',
    'partialmeta': 'Guard.test_a_cli_that_reports_none_of_those_is_refused',
    'codexprovider': 'Guard.test_a_config_that_cannot_be_parsed_stops_the_run',
    'codexapikey': 'Guard.test_a_codex_api_key_stops_the_run',
    'implicitkey': 'Guard.test_an_api_key_with_no_auth_mode_stops_the_run',
    'nourlscan': 'Guard.test_a_redefined_openai_provider_stops_the_run',
    'noenvkey': 'Guard.test_a_provider_key_variable_stops_the_run',
    'noreqauth': 'Guard.test_a_provider_that_waives_openai_auth_stops_the_run',
    'noprofileprov': 'Guard.test_a_provider_named_in_a_profile_stops_the_run',
    'nounreadable': 'Guard.test_a_config_that_cannot_be_parsed_stops_the_run',
    'fieldsplit': 'Guard.test_an_account_directory_whose_path_has_a_space',
    'urlscheme': 'Guard.test_endpoint_urls_are_parsed_as_urls',
    'urlport': 'Guard.test_endpoint_urls_are_parsed_as_urls',
    'mcpfalsepositive': 'Guard.test_mcp_routing_and_documentation_do_not_select_inference_credentials',
    'bearertoken': 'Guard.test_provider_credential_overrides_are_refused',
    'credstore': 'Guard.test_an_alternate_codex_credential_store_is_refused',
    'forcedlogin': 'Guard.test_a_forced_api_login_is_refused',
    'danglingconfig': 'Guard.test_a_dangling_codex_config_is_not_absent',
    'statusdetail': 'Guard.test_a_forced_api_login_is_refused',
    'statusshape': 'Guard.test_status_refuses_different_identity_shapes_like_the_runner',
    'statusunsafeprobe': 'StatusView.test_a_credential_variable_that_cannot_be_unset_is_reported',
    'cliexit': 'Guard.test_a_failed_cli_exit_is_not_authentication',
    'statusexit': 'Guard.test_a_failed_cli_exit_is_not_authentication',
    'metacontrol': 'Guard.test_invalid_metadata_is_refused_instead_of_treated_as_absent',
    'metaempty': 'Guard.test_invalid_metadata_is_refused_instead_of_treated_as_absent',
    'metaloggertype': 'Guard.test_invalid_metadata_is_refused_instead_of_treated_as_absent',
    'statusloggertype': 'Guard.test_invalid_metadata_is_refused_instead_of_treated_as_absent',
    'metawhitespace': 'Guard.test_a_directory_with_trailing_space_is_preserved',
    'cliidentity': 'Guard.test_raw_cli_and_directory_identities_do_not_leak_into_logs',
    'recordidentity': 'Guard.test_raw_cli_and_directory_identities_do_not_leak_into_logs',
    'staterecordidentity': 'Guard.test_raw_cli_and_directory_identities_do_not_leak_into_logs',
    'codextokens': 'Guard.test_incomplete_codex_tokens_are_not_a_login',
    'codexmode': 'Guard.test_an_unsupported_codex_auth_mode_is_not_a_subscription',
    'codexidentity': 'Guard.test_a_codex_identity_cannot_contain_a_secret',
    'nometa': 'Guard.test_a_cli_that_reports_none_of_those_is_refused',
    'statusnometa': 'Guard.test_no_cli_metadata_is_also_refused_by_status',
    'rolecase': 'Guard.test_github_reference_case_and_url_form_do_not_change_the_role',
    'roleurl': 'Guard.test_github_reference_case_and_url_form_do_not_change_the_role',
    'mcpcallback': 'Guard.test_mcp_routing_and_documentation_do_not_select_inference_credentials',
    'codexjwt': 'Guard.test_an_unparseable_codex_id_token_is_not_a_login',
    'urlparser': 'Guard.test_endpoint_urls_are_parsed_as_urls',
    'oneconfiglayer': 'Guard.test_a_profile_layer_beside_the_config_is_checked_too',
    'otelscan': 'Guard.test_a_telemetry_exporter_pointing_elsewhere_stops_the_run',
    'nocreddeny': 'Guard.test_credentials_inside_the_granted_directory_must_be_denied',
    'credrealpath': 'Dashboard.test_it_does_not_count_one_transcript_reached_two_ways',
    'migmerge': 'AuthRootMove.test_it_refuses_to_merge_two_account_roots',
    'migstale': 'AuthRootMove.test_an_ancestor_rule_that_no_longer_covers_them_is_replaced',
    'migdry': 'AuthRootMove.test_a_dry_run_changes_nothing_but_names_every_edit',
    'authinsideroot': 'Guard.test_the_accounts_are_not_inside_the_directory_the_agent_is_given',
    'nostall': 'Dashboard.test_a_run_that_stopped_writing_is_not_still_running',
    'stallgrace': 'Dashboard.test_a_run_still_writing_is_left_alone',
    'stalllockwait': 'Dashboard.test_a_run_queued_on_the_lock_is_not_called_dead',
    'refusalbeforelog': 'Guard.test_a_refusal_is_written_to_the_run_log',
    'pageaccounts': 'Dashboard.test_it_counts_the_account_a_role_selects',
    'pageown': 'Dashboard.test_it_counts_the_tools_own_directory',
    'pagestatus': 'Dashboard.test_a_refused_codex_run_is_shown_as_refused',
    'probefollow': 'UsageProbe.test_the_hourly_probe_reads_the_account_the_role_selects',
    'nofallbackpin': 'Guard.test_a_symlinked_tool_home_is_pinned_even_with_no_role_link',
    'proberunenv': 'UsageProbe.test_a_run_probe_keeps_the_account_the_run_selected',
    'earlyprobe': 'Switching.test_an_unsafe_account_is_refused_before_the_cli_reads_it',
}

def main():
    only = sys.argv[1:]
    work = tempfile.mkdtemp(prefix="mutate-", dir=WORK)
    home = os.path.join(work, "h"); os.makedirs(home)
    tree = os.path.join(work, "r")
    bad = 0
    selected = [m for m in M if not only or m[0] in only]
    test_env = {"HOME": home, "PATH": "/usr/bin:/bin", "TMPDIR": work,
                "PYTHONDONTWRITEBYTECODE": "1"}
    tests = sorted(t for t in os.listdir(os.path.join(SRC, "runner/tests"))
                   if t.startswith("test_") and t.endswith(".py"))
    try:
        # A broken baseline is not evidence that a mutation was caught.
        for t in tests:
            result = subprocess.run(["python3", os.path.join(SRC, "runner/tests", t)],
                                    capture_output=True, text=True, env=test_env)
            if result.returncode:
                print("baseline failed: " + t)
                print(result.stdout + result.stderr)
                return 1
        print("%d test files pass" % len(tests), flush=True)
        for name, files, old, new in selected:
            shutil.rmtree(tree, ignore_errors=True)
            shutil.copytree(SRC, tree, ignore=shutil.ignore_patterns(
                ".git", ".test-work", "__pycache__"))
            for f in ((files,) if isinstance(files, str) else files):
                path = os.path.join(tree, f)
                s = io.open(path).read()
                if s.count(old) != 1:
                    print("  %-16s NOT APPLIED (%d matches in %s)"
                          % (name, s.count(old), f)); bad += 1; break
                io.open(path, "w").write(s.replace(old, new, 1))
            else:
                case = REGRESSION[name]
                # find the class rather than guessing from two filenames: a
                # third test file silently sent every case to the wrong one,
                # which reads as NOT CAUGHT whatever the mutation did
                cls = case.split(".")[0]
                test = next((t for t in tests
                             if re.search(r"^class %s\(" % re.escape(cls),
                                          io.open(os.path.join(tree, "runner/tests", t)).read(),
                                          re.M)), None)
                if test is None:
                    print("  %-16s NO SUCH TEST CLASS: %s" % (name, cls))
                    bad += 1
                    continue
                r = subprocess.run(["python3", os.path.join(tree, "runner/tests", test), case, "-f"],
                                   capture_output=True, text=True, env=test_env)
                output = r.stdout + r.stderr
                failures = re.findall(r"^FAIL: (.+)$", output, re.M)
                if failures and not re.search(r"^ERROR:", output, re.M):
                    print("  %-16s caught: %s" % (name, failures[0]))
                else:
                    print("  %-16s NOT CAUGHT" % name); bad += 1
                    if r.returncode:
                        print(output)
                sys.stdout.flush()
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print("%d of %d mutations unaccounted for" % (bad, len(selected)))
    return 1 if bad else 0

sys.exit(main())
