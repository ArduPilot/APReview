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
 ("nofallbackpin", RUN, 'd=$(readlink -f "$HOME/.$tool" 2>/dev/null) || d=""\n           [ -n "$d" ] || { unset "$var"; return 0; } ;;',
                        'unset "$var"; return 0 ;;'),
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
 ("earlyprobe", AUT, '    lockf="$AUTH/.$tool-$role.lock"',
                     '    got=$(account_of "$tool" "$dir")\n    lockf="$AUTH/.$tool-$role.lock"'),
]

# An unrelated failure is not evidence for a particular guard. Each mutation
# must break its regression after the complete, unchanged suite has passed.
REGRESSION = {
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
    'nofallbackpin': 'Guard.test_a_symlinked_tool_home_is_pinned_even_with_no_role_link',
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
        print("all four test files pass", flush=True)
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
                test = "test_runner_guard.py" if case.startswith("Guard.") else "test_auth_roles.py"
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
