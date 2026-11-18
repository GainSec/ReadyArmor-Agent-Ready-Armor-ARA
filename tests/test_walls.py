from pathlib import Path

from ara.walls import find_match, matches, parse


WALLS_FIXTURE = """
# comment line, ignored
## deny_path
~/.ssh/**
/etc/shadow
/tmp/blocked/**

## other_section
~/.not-applied
"""


def test_parse_extracts_only_deny_path_rules(tmp_path: Path):
    walls = parse(WALLS_FIXTURE, home=tmp_path)
    assert len(walls.path_rules) == 3
    patterns = [r.pattern for r in walls.path_rules]
    assert patterns[0] == str(tmp_path / ".ssh/**")
    assert patterns[1] == "/etc/shadow"
    assert patterns[2] == "/tmp/blocked/**"


def test_rule_ids_are_stable_and_unique(tmp_path: Path):
    walls = parse(WALLS_FIXTURE, home=tmp_path)
    ids = [r.rule_id for r in walls.path_rules]
    assert ids == ["deny_path:1", "deny_path:2", "deny_path:3"]


def test_matches_recursive_glob(tmp_path: Path):
    walls = parse("## deny_path\n~/.ssh/**\n", home=tmp_path)
    (rule,) = walls.path_rules
    assert matches(rule, str(tmp_path / ".ssh/id_rsa"))
    assert matches(rule, str(tmp_path / ".ssh/subdir/key"))
    assert not matches(rule, str(tmp_path / "not-ssh/id_rsa"))


def test_find_match_first_rule_wins(tmp_path: Path):
    walls = parse(
        "## deny_path\n/etc/**\n~/.ssh/**\n",
        home=tmp_path,
    )
    hit = find_match(walls, "/etc/passwd")
    assert hit is not None
    assert hit.rule_id == "deny_path:1"


def test_find_match_returns_none_for_allowed(tmp_path: Path):
    walls = parse("## deny_path\n~/.ssh/**\n", home=tmp_path)
    assert find_match(walls, str(tmp_path / "safe/file.txt")) is None


def test_absolute_path_rule(tmp_path: Path):
    walls = parse("## deny_path\n/etc/shadow\n", home=tmp_path)
    (rule,) = walls.path_rules
    assert matches(rule, "/etc/shadow")
    assert not matches(rule, "/etc/shadow.bak")


def test_regex_path_rule(short_tmp):
    import pathlib
    from ara.walls import parse, find_match, find_approval_match
    home = pathlib.Path("/home/test")
    w = parse(
        "## deny_path\n"
        "regex: ^/tmp/gen-[0-9]+\\.so$\n"
        "## approval_path\n"
        "regex: \\.env$\n",
        home=home,
    )
    # deny_path regex matches
    assert find_match(w, "/tmp/gen-42.so") is not None
    assert find_match(w, "/tmp/gen-a.so") is None  # not a digit
    # approval_path regex uses search (not anchored)
    assert find_approval_match(w, "fs.read", path="/etc/foo/.env") is not None
    assert find_approval_match(w, "fs.read", path="/etc/env.txt") is None


def test_regex_command_rule():
    from ara.walls import parse, find_command_match
    w = parse("## deny_command\nregex: ^curl|^wget$\n")
    assert find_command_match(w, "curl") is not None
    assert find_command_match(w, "wget") is not None
    assert find_command_match(w, "fetch") is None


def test_malformed_regex_falls_back_to_literal():
    from ara.walls import parse, find_match
    w = parse("## deny_path\nregex: [unclosed\n")
    # Should not raise; literal match on exact pattern.
    assert find_match(w, "[unclosed") is not None
    assert find_match(w, "other") is None


def test_deny_env_parse():
    from ara.walls import parse
    w = parse("## deny_env\nOPENAI_API_KEY\n*_TOKEN\n")
    assert len(w.env_rules) == 2
    assert w.env_rules[0].pattern == "OPENAI_API_KEY"


def test_find_env_match_and_scrub():
    from ara.walls import parse, find_env_match, scrub_env
    w = parse("## deny_env\nOPENAI_API_KEY\n*_TOKEN\n")
    assert find_env_match(w, "OPENAI_API_KEY") is not None
    assert find_env_match(w, "GITHUB_TOKEN") is not None
    assert find_env_match(w, "PATH") is None

    env = {
        "PATH": "/bin",
        "OPENAI_API_KEY": "sk-xxx",
        "GITHUB_TOKEN": "ghs_yyy",
        "ARA_SOCKET": "/tmp/ara/guard.sock",
        "ARA_SESSION_TOKEN": "control-plane-token",
        "ARA_SESSION_ID": "sid-123",
        "HOME": "/home/u",
    }
    clean, stripped = scrub_env(env, w)
    assert "OPENAI_API_KEY" not in clean
    assert "GITHUB_TOKEN" not in clean
    assert clean["ARA_SOCKET"] == "/tmp/ara/guard.sock"
    assert clean["ARA_SESSION_TOKEN"] == "control-plane-token"
    assert clean["ARA_SESSION_ID"] == "sid-123"
    assert clean["PATH"] == "/bin"
    names = sorted(k for k, _ in stripped)
    assert names == ["GITHUB_TOKEN", "OPENAI_API_KEY"]


def test_tags_expand_into_deny_path(short_tmp):
    import pathlib
    from ara.walls import parse, find_match
    home = pathlib.Path("/home/test")
    w = parse(
        "## tags\n"
        "secrets  ~/.ssh/**\n"
        "secrets  ~/.aws/**\n"
        "scratch  /tmp/**\n"
        "\n"
        "## deny_path\n"
        "@secrets\n"
        "/etc/shadow\n",
        home=home,
    )
    assert len(w.path_rules) == 3  # 2 secrets + 1 etc
    assert find_match(w, "/home/test/.ssh/id_rsa") is not None
    assert find_match(w, "/home/test/.aws/credentials") is not None
    assert find_match(w, "/etc/shadow") is not None
    # scratch tag wasn't referenced — no rule.
    assert find_match(w, "/tmp/foo") is None


def test_tags_in_command_and_approval_sections():
    from ara.walls import parse, find_command_match, find_approval_match
    w = parse(
        "## tags\n"
        "dangerous curl\n"
        "dangerous wget\n"
        "asks git\n"
        "\n"
        "## deny_command\n"
        "@dangerous\n"
        "\n"
        "## approval_command\n"
        "@asks\n",
    )
    assert find_command_match(w, "curl") is not None
    assert find_command_match(w, "wget") is not None
    assert find_approval_match(w, "exec.spawn", command="git") is not None


def test_undefined_tag_yields_no_rule():
    from ara.walls import parse
    w = parse("## deny_path\n@missing\n")
    assert len(w.path_rules) == 0
