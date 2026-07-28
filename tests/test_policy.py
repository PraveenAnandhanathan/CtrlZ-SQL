"""The rulebook: evaluation, risk, and the strictness of the loader."""

from __future__ import annotations

import pytest

from ctrlz.errors import ConfigError
from ctrlz.policy import (
    ALLOW,
    BLOCK,
    WARN,
    Context,
    PolicyEngine,
    evaluate_sql,
    find_policy_file,
    load_defaults,
    load_policy,
    parse,
)

TRACKED = ["public.users", "public.orders", "public.audit"]


def decide(sql, tracked=TRACKED, **kwargs):
    return evaluate_sql(sql, tracked=tracked, **kwargs)


# -- the shipped defaults --------------------------------------------------


@pytest.mark.parametrize(
    "sql,outcome,rule",
    [
        # The disaster the whole tool exists for.
        ("DELETE FROM users", BLOCK, "unfiltered-write"),
        ("UPDATE users SET salary = 0", BLOCK, "unfiltered-write"),
        # A WHERE that belongs to a subquery is not a filter on the delete.
        ("DELETE FROM audit USING (SELECT id FROM staging WHERE ok) q", BLOCK,
         "unfiltered-write"),
        # Properly filtered writes are silent.
        ("UPDATE users SET salary = 0 WHERE id = 1", ALLOW, None),
        ("DELETE FROM orders WHERE id = 1", ALLOW, None),
        ("INSERT INTO users (name) VALUES ('ada')", ALLOW, None),
        ("SELECT * FROM users", ALLOW, None),
        # Uncapturable and unundoable things.
        ("TRUNCATE users", BLOCK, "truncate"),
        ("DROP TABLE users", WARN, "destructive-ddl"),
        ("ALTER TABLE users DROP COLUMN salary", WARN, "destructive-ddl"),
        # Filters that filter nothing.
        ("UPDATE users SET x = 1 WHERE 1 = 1", WARN, "tautology-filter"),
        ("DELETE FROM users WHERE true", WARN, "tautology-filter"),
        # No safety net on this table.
        ("UPDATE payments SET x = 1 WHERE id = 1", WARN, "untracked-table"),
    ],
)
def test_default_rules(sql, outcome, rule):
    decision = decide(sql)
    assert decision.outcome == outcome, decision.explain()
    if rule:
        assert decision.decided_by is not None
        assert decision.decided_by.name == rule


def test_defaults_do_not_block_on_risk_alone():
    """Decision D-3: a fresh install never refuses work merely for looking risky."""
    policy = load_defaults()
    assert policy.block_on_risk is False

    # Scores at the threshold, but its rule only warns.
    decision = decide("ALTER TABLE users DROP COLUMN salary")
    assert decision.risk >= policy.risk_threshold
    assert decision.outcome == WARN


# -- risk aggregation ------------------------------------------------------


RULEBOOK = {
    "version": 1,
    "defaults": {"risk_threshold": 70, "block_on_risk": False},
    "rules": [
        {"name": "low", "when": {"kind": ["write"]}, "action": "warn", "risk": 10,
         "message": "low"},
        {"name": "mid", "when": {"statement": ["DELETE"]}, "action": "warn", "risk": 40,
         "message": "mid"},
        {"name": "high", "when": {"unfiltered": True}, "action": "warn", "risk": 80,
         "message": "high"},
    ],
}


def test_risk_is_the_highest_matching_rule_not_a_sum():
    """A summed score cannot be explained, so it gets ignored. max() can."""
    decision = PolicyEngine(parse(RULEBOOK)).evaluate_sql("DELETE FROM users")
    assert {m.name for m in decision.matched} == {"low", "mid", "high"}
    assert decision.risk == 80          # not 10 + 40 + 80
    assert decision.scored_by.name == "high"


def test_explain_names_the_rule_that_decided_and_the_rule_that_scored():
    book = dict(RULEBOOK)
    book["rules"] = RULEBOOK["rules"] + [
        {"name": "veto", "when": {"statement": ["DELETE"]}, "action": "block",
         "risk": 5, "message": "veto"}
    ]
    decision = PolicyEngine(parse(book)).evaluate_sql("DELETE FROM users")

    assert decision.outcome == BLOCK
    assert decision.decided_by.name == "veto"   # decided by the blocking rule
    assert decision.scored_by.name == "high"    # scored by the riskiest one

    explanation = decision.explain()
    assert "veto" in explanation and "high" in explanation


def test_block_on_risk_is_opt_in_and_needs_no_code_change():
    """The same statement, the same code, two rulebooks, two outcomes."""
    warn_only = dict(RULEBOOK)
    decision = PolicyEngine(parse(warn_only)).evaluate_sql("DELETE FROM users")
    assert decision.outcome == WARN
    assert decision.risk == 80

    strict = {**RULEBOOK, "defaults": {"risk_threshold": 70, "block_on_risk": True}}
    decision = PolicyEngine(parse(strict)).evaluate_sql("DELETE FROM users")
    assert decision.outcome == BLOCK
    assert decision.decided_by.name == "high"
    assert "block_on_risk" not in decision.explain() or decision.blocked


def test_risk_below_threshold_never_blocks_even_when_opted_in():
    strict = {**RULEBOOK, "defaults": {"risk_threshold": 90, "block_on_risk": True}}
    decision = PolicyEngine(parse(strict)).evaluate_sql("DELETE FROM users")
    assert decision.risk == 80
    assert decision.outcome == WARN


# -- conditions ------------------------------------------------------------


def book(**condition):
    return parse(
        {
            "version": 1,
            "rules": [
                {"name": "t", "when": condition, "action": "block", "risk": 50,
                 "message": "matched"}
            ],
        }
    )


@pytest.mark.parametrize(
    "pattern,sql,matches",
    [
        ("payments", "UPDATE payments SET x = 1 WHERE id = 1", True),
        ("payments", "UPDATE public.payments SET x = 1 WHERE id = 1", True),
        ("public.payments", "UPDATE public.payments SET x = 1 WHERE id = 1", True),
        ("pay*", "UPDATE payments SET x = 1 WHERE id = 1", True),
        ("PAYMENTS", "UPDATE payments SET x = 1 WHERE id = 1", True),
        ("payments", "UPDATE users SET x = 1 WHERE id = 1", False),
        ("payments", "SELECT * FROM payments", True),
    ],
)
def test_table_patterns_match_qualified_and_bare_names(pattern, sql, matches):
    decision = PolicyEngine(book(tables=[pattern])).evaluate_sql(sql)
    assert (decision.outcome == BLOCK) is matches


def test_writes_untracked_ignores_reads_and_unknown_targets():
    engine = PolicyEngine(book(writes_untracked=True))
    context = Context.build(tracked=["public.users"])

    assert engine.evaluate_sql(
        "UPDATE payments SET x = 1 WHERE id = 1", context
    ).outcome == BLOCK
    assert engine.evaluate_sql(
        "UPDATE users SET x = 1 WHERE id = 1", context
    ).outcome == ALLOW
    # Reading an untracked table is not a risk to anything.
    assert engine.evaluate_sql("SELECT * FROM payments", context).outcome == ALLOW
    # Nor is a write whose target we could not read -- crying wolf about
    # statements we did not understand would train people to ignore us.
    assert engine.evaluate_sql("SOME UNPARSEABLE THING", context).outcome == ALLOW


def test_confidence_bounds_gate_rules():
    strict = PolicyEngine(book(unfiltered=True, min_confidence=0.8))
    assert strict.evaluate_sql("DELETE FROM t", prefer="sqlglot").outcome == BLOCK
    assert strict.evaluate_sql("DELETE FROM t", prefer="regex").outcome == ALLOW


def test_scope_limits_a_rule_to_an_environment():
    policy = parse(
        {
            "version": 1,
            "rules": [
                {"name": "prod-only", "when": {"kind": ["write"]}, "action": "block",
                 "risk": 50, "message": "no", "scope": {"environments": ["prod*"]}}
            ],
        }
    )
    engine = PolicyEngine(policy)
    sql = "UPDATE users SET x = 1 WHERE id = 1"
    assert engine.evaluate_sql(sql, Context.build(environment="production")).outcome == BLOCK
    assert engine.evaluate_sql(sql, Context.build(environment="staging")).outcome == ALLOW


def test_scope_limits_a_rule_to_actors():
    policy = parse(
        {
            "version": 1,
            "rules": [
                {"name": "interns", "when": {"kind": ["write"]}, "action": "block",
                 "risk": 50, "message": "no", "scope": {"actors": ["intern-*"]}}
            ],
        }
    )
    engine = PolicyEngine(policy)
    sql = "DELETE FROM users WHERE id = 1"
    assert engine.evaluate_sql(sql, Context.build(actor="intern-sam")).outcome == BLOCK
    assert engine.evaluate_sql(sql, Context.build(actor="praveen")).outcome == ALLOW


# -- scripts ---------------------------------------------------------------


def test_a_script_is_as_dangerous_as_its_worst_statement():
    decision = decide("UPDATE users SET x = 1 WHERE id = 1; DELETE FROM orders")
    assert decision.outcome == BLOCK
    assert decision.decided_by.name == "unfiltered-write"


def test_a_repeated_rule_is_reported_once_per_script():
    decision = decide("DELETE FROM users; DELETE FROM orders")
    names = [m.name for m in decision.matched]
    assert names.count("unfiltered-write") == 1


# -- the loader is strict on purpose ---------------------------------------


def test_unknown_condition_field_is_an_error_not_a_shrug():
    """A typo in a safety rule must not silently disable it."""
    with pytest.raises(ConfigError) as exc:
        parse({"version": 1, "rules": [
            {"name": "typo", "when": {"has_filtr": False}, "action": "block"}
        ]})
    assert "has_filtr" in str(exc.value)
    assert "has_filter" in str(exc.value)   # suggests the intended field


def test_unknown_top_level_key_is_rejected():
    with pytest.raises(ConfigError, match="rulez"):
        parse({"version": 1, "rulez": []})


def test_unsupported_version_is_rejected():
    with pytest.raises(ConfigError, match="version"):
        parse({"version": 99, "rules": []})


def test_invalid_action_is_rejected():
    with pytest.raises(ConfigError, match="action"):
        parse({"version": 1, "rules": [
            {"name": "r", "when": {}, "action": "explode"}
        ]})


def test_risk_outside_the_range_is_rejected():
    with pytest.raises(ConfigError, match="risk"):
        parse({"version": 1, "rules": [
            {"name": "r", "when": {}, "action": "warn", "risk": 900}
        ]})


def test_duplicate_rule_names_are_rejected():
    """A shadowed rule is a rule that silently stopped working."""
    with pytest.raises(ConfigError, match="more than once"):
        parse({"version": 1, "rules": [
            {"name": "same", "when": {}, "action": "warn"},
            {"name": "same", "when": {}, "action": "block"},
        ]})


def test_rule_without_a_name_is_rejected():
    with pytest.raises(ConfigError, match="name"):
        parse({"version": 1, "rules": [{"when": {}, "action": "warn"}]})


def test_non_boolean_condition_value_is_rejected():
    with pytest.raises(ConfigError, match="true or false"):
        parse({"version": 1, "rules": [
            {"name": "r", "when": {"has_filter": "yes"}, "action": "warn"}
        ]})


# -- loading from disk -----------------------------------------------------


POLICY_FILE = """
version: 1
defaults:
  risk_threshold: 50
  block_on_risk: true
rules:
  - name: house-rule
    when: {tables: [ledger]}
    action: block
    risk: 99
    message: nobody touches the ledger
"""


def test_policy_is_loaded_from_a_file(tmp_path, monkeypatch):
    path = tmp_path / "ctrlz.policy.yaml"
    path.write_text(POLICY_FILE)
    monkeypatch.setenv("CTRLZ_POLICY", str(path))

    policy = load_policy()
    assert policy.block_on_risk is True
    assert policy.rule("house-rule") is not None

    decision = PolicyEngine(policy).evaluate_sql("UPDATE ledger SET x = 1 WHERE id = 1")
    assert decision.outcome == BLOCK
    assert "ledger" in decision.messages[0]


def test_policy_file_is_found_by_walking_up(tmp_path, monkeypatch):
    monkeypatch.delenv("CTRLZ_POLICY", raising=False)
    (tmp_path / "ctrlz.policy.yaml").write_text(POLICY_FILE)
    nested = tmp_path / "a" / "b" / "c"
    nested.mkdir(parents=True)

    assert find_policy_file(str(nested)) == tmp_path / "ctrlz.policy.yaml"


def test_missing_policy_file_falls_back_to_the_defaults(tmp_path, monkeypatch):
    monkeypatch.delenv("CTRLZ_POLICY", raising=False)
    monkeypatch.chdir(tmp_path)
    policy = load_policy()
    assert policy.rule("unfiltered-write") is not None


def test_pointing_at_a_missing_policy_file_is_an_error(tmp_path, monkeypatch):
    monkeypatch.setenv("CTRLZ_POLICY", str(tmp_path / "nope.yaml"))
    with pytest.raises(ConfigError, match="does not exist"):
        load_policy()


def test_malformed_yaml_is_reported_clearly(tmp_path, monkeypatch):
    path = tmp_path / "ctrlz.policy.yaml"
    path.write_text("version: 1\nrules: [ unclosed")
    monkeypatch.setenv("CTRLZ_POLICY", str(path))
    with pytest.raises(ConfigError, match="not valid YAML"):
        load_policy()


# -- message rendering -----------------------------------------------------


def test_messages_are_filled_in_from_the_analysis():
    decision = decide("DELETE FROM users")
    assert "DELETE" in decision.messages[0]
    assert "users" in decision.messages[0]


def test_a_broken_message_template_does_not_break_the_check():
    """A typo in a message must not disable the rule that carries it."""
    policy = parse({"version": 1, "rules": [
        {"name": "r", "when": {"unfiltered": True}, "action": "block", "risk": 10,
         "message": "bad placeholder {nonexistent}"}
    ]})
    decision = PolicyEngine(policy).evaluate_sql("DELETE FROM users")
    assert decision.outcome == BLOCK
    assert "nonexistent" in decision.messages[0]


def test_evaluation_never_raises_on_hostile_input():
    for sql in ["", "   ", "\x00\x01", "'" * 500, "(" * 300, None]:
        decision = evaluate_sql(sql if isinstance(sql, str) else "")
        assert decision.outcome in (ALLOW, WARN, BLOCK)
