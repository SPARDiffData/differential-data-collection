"""Secrets, meeting links and phone numbers (SCR-1, SCR-2): the finders in secrets.py and the
steps in text.py, with the must-refuse list from the plan on Issue #117. The end-to-end
items (held back, the rule file, nothing quoted) are in test_scrub.py.

Fake secrets are built here from parts and a seeded generator, so the repository never
holds a string shaped like a real key, and GitHub's secret scanning isn't set off. Phone
numbers are in fictional ranges (US 555-01xx, UK Ofcom drama numbers)."""

import random
import re
import string
import subprocess
import sys
import time
import tomllib

import pytest

from diffdata.scrub import secrets
from diffdata.scrub.people import People, Person
from diffdata.scrub.text import CHECKS, MAYBE_SECRET, Scrubber

RANDOM = random.Random(117)
ALNUM = string.ascii_letters + string.digits
URLSAFE = ALNUM + "_-"


@pytest.fixture(autouse=True)
def same_values_every_run():
    """Each test gets the same made-up values, however the tests are picked or ordered."""
    RANDOM.seed(117)


def rand(n: int, alphabet: str = ALNUM) -> str:
    return "".join(RANDOM.choice(alphabet) for _ in range(n))


def fake(kind: str) -> str:
    """A made-up value in the shape of a real secret, built from parts."""
    return {
        "aws key id": "AK" + "IA" + rand(16, string.ascii_uppercase + "234567"),
        "github": "gh" + "p_" + rand(36),
        "slack": "xo" + "xb-" + rand(12, string.digits) + "-" + rand(12, string.digits) + "-"
        + rand(24),
        "google": "AI" + "za" + rand(35, URLSAFE),
        "openai": "sk-" + "proj-" + rand(74, URLSAFE) + "T3Blbk" + "FJ" + rand(74, URLSAFE),
        "anthropic": "sk-" + "ant-" + "api03-" + rand(93, URLSAFE) + "AA",
        "jwt": "ey" + "J" + rand(30) + ".ey" + "J" + rand(40) + "." + rand(30),
        "pem": "-----BEGIN RSA PRIV" + "ATE KEY-----\n" + rand(64) + "\n" + rand(64)
        + "\n-----END RSA PRIV" + "ATE KEY-----",
        "password": rand(18),
    }[kind]  # fmt: skip


def scrub(text: str, people: People | None = None) -> str:
    return Scrubber(people or People()).scrub(text)[0]


def found(text: str) -> list[str]:
    return Scrubber(People()).leftovers(text)


# The rule file


def test_every_rule_compiles_and_the_file_is_the_pinned_release():
    raw = secrets.RULES_FILE.read_bytes()
    config = tomllib.loads(raw.decode("utf-8"))
    text_rules = [r for r in config["rules"] if "regex" in r]  # file-type rules included
    assert len(secrets.rules()) == len(text_rules) > 200
    assert {"nuget-config-password", "kubernetes-secret-yaml"} <= {r.id for r in secrets.rules()}
    assert "v8.30.1" in secrets.RULES_RELEASE
    assert "v8.30.1" in (secrets.RULES_FILE.parent / "README.md").read_text("utf-8")


@pytest.mark.parametrize(
    "go, python",
    [
        (r"(?i)abc", r"(?i)abc"),  # at the start: global, as in Python
        (r'x"(?i)ab"', r'x"(?i:ab")'),  # partway: to the end of its group
        (r'(?:"(?i)a"|b)c', r'(?:"(?i:a")|(?i:b))c'),  # across a | in the same group
        (r"a\zb", r"a\Zb"),
        (r"[[:alnum:]]{3}", r"[a-zA-Z0-9]{3}"),
        (r"[^]a]\[x", r"[^]a]\[x"),
    ],
)
def test_go_patterns_are_translated(go, python):
    assert secrets.go_to_python(go) == python


def test_a_mid_pattern_flag_matches_as_go_matches_it():
    pattern = re.compile(secrets.go_to_python(r'(?:"(?i)ab"|cd)'))
    assert pattern.fullmatch('"AB"') and pattern.fullmatch("CD")
    assert not re.compile(secrets.go_to_python(r'x(?:"(?i)ab")')).fullmatch('X"ab"')


def test_dropping_a_rules_lazy_lead_finds_the_same_secrets():
    """The leading [\\w.-]{0,50}? only moves where a match starts, never its secret."""
    config = tomllib.loads(secrets.RULES_FILE.read_text("utf-8"))
    leads = [r for r in config["rules"] if "regex" in r and "path" not in r]
    leads = [r for r in leads if secrets.drop_lead(secrets.go_to_python(r["regex"])) != (
        secrets.go_to_python(r["regex"]))]  # fmt: skip
    assert len(leads) > 50
    value = rand(40, "0123456789abcdef")
    for rule in leads:
        keyword = rule.get("keywords", ["key"])[0]
        texts = [
            f"my_{keyword}_token = {value}\n",
            f"{keyword} {keyword}: '{value}' and {keyword}={value} ",
            f'x.y-z{keyword}_API_KEY := "{value}"',
        ]
        full = re.compile(secrets.go_to_python(rule["regex"]), re.ASCII)
        lean = re.compile(secrets.drop_lead(secrets.go_to_python(rule["regex"])), re.ASCII)
        for text in texts:
            spans = [
                secrets.secret_span(m, rule.get("secretGroup", 0)) for m in full.finditer(text)
            ]
            lean_spans = [
                secrets.secret_span(m, rule.get("secretGroup", 0)) for m in lean.finditer(text)
            ]
            assert spans == lean_spans, rule["id"]


WORST = ("a", "A", "0", "-", "=", "/", " ", "\t", "\n", ".", "_", "'", '"', ":", "ey")
WORST += ("a+", "x+%40", "key=", "curl -H ", "https://a:", "sk-ant-api03-", "-----BEGIN ")


def test_no_rule_is_slow_on_long_worst_case_text():
    """Python's regex engine can be slow where Go's can't. A slow rule fails here, in CI,
    not on a producer's machine."""
    slow = []
    for rule in secrets.rules():
        lead = " ".join(rule.keywords) + " "
        for piece in WORST:
            text = lead + piece * (20_000 // len(piece))
            started = time.perf_counter()
            list(rule.pattern.finditer(text))
            if (took := time.perf_counter() - started) > 1:
                slow.append((rule.id, piece, round(took, 2)))
    assert slow == []


@pytest.mark.parametrize("piece", WORST)
def test_every_step_and_the_last_check_are_quick_on_long_worst_case_text(piece):
    text = piece * (20_000 // len(piece))
    started = time.perf_counter()
    scrubbed = scrub(text)
    found(scrubbed)
    assert time.perf_counter() - started < 5


# Known secret shapes


@pytest.mark.parametrize(
    "kind", ["aws key id", "github", "slack", "google", "openai", "anthropic", "jwt", "pem"]
)
def test_a_known_secret_shape_becomes_secret(kind):
    value = fake(kind)
    assert scrub(f"Here it is: {value} (don't share)") == "Here it is: [secret] (don't share)"


def test_an_aws_secret_key_and_a_password_line_become_secret_and_only_the_value_goes():
    key, password = rand(40, ALNUM + "+/"), fake("password")
    text = f'aws_secret_access_key = {key}\npassword = "{password}"\nDB_PASSWORD: {password}'
    scrubbed = scrub(text)
    assert key not in scrubbed and password not in scrubbed
    # The words before each value stay. (gitleaks' Terraform rule counts a password's quotes
    # as part of it, so they may go too.)
    lines = scrubbed.splitlines()
    assert lines[0] == "aws_secret_access_key = [secret]"
    assert lines[1] in ('password = "[secret]"', "password = [secret]")
    assert lines[2] == "DB_PASSWORD: [secret]"
    assert found(scrubbed) == []


def test_a_password_in_a_link_becomes_secret_and_the_link_stays_readable():
    password = fake("password")
    assert (
        scrub(f"use postgres://admin:{password}@db.example.com/app")
        == "use postgres://admin:[secret]@db.example.com/app"
    )
    query = f"https://example.com/login?user=a&password={password}"
    assert scrub(query) == "https://example.com/login?user=a&password=[secret]"
    # The last check doesn't read the placeholder as a password, so the copy is kept.
    for scheme in ("postgres", "mysql", "ftp", "https"):
        scrubbed = scrub(f"{scheme}://admin:{password}@db.example.com/app")
        assert scrubbed == f"{scheme}://admin:[secret]@db.example.com/app"
        assert found(scrubbed) == []


def test_a_settings_line_with_no_secret_in_it_isnt_replaced():
    """The rules' entropy settings: the generic key rule needs a value that looks random."""
    text = (
        "timeout = 30\nkey: value\napi_version = v2\nauth = enabled\naccess: read-only-mode\n"
        "token_name = my_test_token_here\npassword: ********\nsecret = 'not-a-real-one'"
    )
    assert scrub(text) == text


def test_the_same_secret_twice_is_replaced_both_times_even_where_no_rule_sees_it():
    password = fake("password")
    text = f"password = {password}\nand again later: {password}."
    assert scrub(text) == "password = [secret]\nand again later: [secret]."


# A secret split or disguised (must-refuse 17 and "split or disguised")


@pytest.mark.parametrize(
    "hidden",
    ["\u200b", "\u00ad", "\u200b\u2060", "\ufeff\u00ad"],
    ids=["zero-width space", "soft hyphen", "two different", "two others"],
)
def test_must_refuse_17_a_secret_split_by_invisible_characters_is_replaced_whole(hidden):
    value = fake("github")
    split = value[:10] + hidden[0] + value[10:20] + hidden[-1] + value[20:]
    assert scrub(f"token: {split} ok") == "token: [secret] ok"


def test_must_refuse_17_the_last_check_finds_half_a_split_secret():
    value = fake("anthropic")
    split = value[:54] + "\u200b" + value[54:]
    assert found(f"left: {split}") == ["a secret"]
    # A broken replacement that stops at the invisible character leaves half a key.
    assert found(f"left: [secret]\u200b{value[54:]}") == ["a secret"]


@pytest.mark.parametrize(
    "wrap",
    ["`{}`", "```\n{}\n```", "<https://api.example.com/v1?token={}|link>", "'{}'", "({})"],
    ids=["backticks", "code block", "inside a link", "quotes", "brackets"],
)
def test_a_secret_wrapped_in_formatting_is_found(wrap):
    value = fake("github")
    assert value not in scrub(wrap.format(value))
    assert "[secret]" in scrub(wrap.format(value))


def test_a_secret_in_wide_letters_is_found():
    value = fake("github")
    wide = "".join(chr(ord(c) + 0xFEE0) if "!" <= c <= "~" else c for c in value)
    assert scrub(f"key {wide} end") == "key [secret] end"


# The randomness check


def test_a_random_string_becomes_secret_maybe_and_is_flagged_without_its_value():
    value = rand(40)
    text, flags = Scrubber(People()).scrub(f"the value {value} works")
    assert text == "the value [secret?] works"
    assert [(f.kind, f.text) for f in flags] == [(MAYBE_SECRET, "")]


def test_a_long_hex_string_like_a_commit_hash_becomes_secret_maybe():
    assert scrub("fixed in " + rand(40, "0123456789abcdef")) == "fixed in [secret?]"


@pytest.mark.parametrize(
    "text",
    [
        "internationalization and characterization2024",
        "Thesentencewithoutanyspacesatall",
        "MyProjectNotes2024Final ExportSlackChannelHistory2 getUserAccountSettings2",
        "README-2026-11-23-final and snake_case_variable_name_2",
        "[Person 12] [secret?] [meeting link] [phone] [email]",
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa 1111111111111111111111111111",
    ],
)
def test_ordinary_words_and_placeholders_are_left_alone(text):
    assert scrub(text) == text


def test_slack_and_google_links_come_through_whole():
    doc = "https://docs.google.com/document/d/" + rand(44, URLSAFE) + "/edit?usp=sharing"
    drive = "https://drive.google.com/file/d/" + rand(33, URLSAFE) + "/view"
    message = "https://example.slack.com/archives/C00000001/p1790864040000100?thread_ts=1790864040.000100&cid=C00000001"
    canvas = "https://example.slack.com/docs/T00000000/F00000003"
    text = f"See <{doc}|the doc>, {drive}, {message} and [canvas]({canvas})."
    assert scrub(text) == text


# Meeting links


@pytest.mark.parametrize(
    "link",
    [
        "https://us02web.zoom.us/j/12345678901?pwd={code}",
        "https://zoom.us/my/example.room",
        "https://meet.google.com/abc-defg-hij",
        "meet.google.com/abc-defg-hij?authuser=0",
        "https://teams.microsoft.com/l/meetup-join/19%3ameeting_{code}%40thread.v2/0",
        "https://teams.live.com/meet/9876543210?p={code}",
        "https://example.webex.com/example/j.php?MTID={code}",
        "https://whereby.com/example-room",
        "https://meet.jit.si/ExampleRoom{code}",
        "https://calendly.com/example/30min",
        "https://gotomeet.me/ExampleRoom",
        "https://global.gotomeeting.com/join/123456789",
        "https://example.zoomgov.com/j/1612345678?pwd={code}",
    ],
)
def test_a_meeting_link_becomes_meeting_link_passcode_and_all(link):
    link = link.format(code=rand(24))
    assert scrub(f"Join here: {link} at 10") == "Join here: [meeting link] at 10"
    assert scrub(f"<{link}|Join here>") == "<[meeting link]|Join here>"


def test_other_links_stay_even_if_they_mention_a_meeting_service():
    text = "https://example.com/zoom/notes and https://teams.microsoft.com/settings and zoom.us"
    assert scrub(text) == text


# Phone numbers


@pytest.mark.parametrize(
    "number",
    [
        "(201) 555-0123",
        "201-555-0123",
        "201.555.0123",
        "+1 202 555 0142",
        "+1-800-555-0199",
        "020 7946 0958",
        "+44 20 7946 0958",
        "+44 (0)20 7946 0958",
        "+33 1 99 00 12 34",  # ARCEP's range for fiction
    ],
)
def test_a_phone_number_becomes_phone(number):
    assert scrub(f"Call me on {number} tomorrow.") == "Call me on [phone] tomorrow."


def test_a_tel_link_is_replaced_whole():
    assert scrub("<tel:+12025550142|call>") == "<[phone]|call>"


@pytest.mark.parametrize(
    "text",
    [
        "On 2026-11-23 at 10:30, and 23/11/2026, 11/23/2026.",
        "Version 1.2.3 and v2026.11.0 cost $1,234.56 or £12,345.",
        "ts 1790864040.000100 and p1790864040000100",
        "[Person 12] and [Person 345] met at 14:00-15:30.",
        "See https://example.com/p/2015550123 and https://docs.google.com/d/2015550123/edit",
        "https://example.slack.com/archives/C00000001/p1790864040000100",
    ],
)
def test_dates_times_versions_money_ids_and_links_arent_phone_numbers(text):
    assert scrub(text) == text


# Order, placeholders and the last check


def test_new_checks_come_first_in_order():
    assert CHECKS[:4] == ("secrets", "meeting links", "phone numbers", "emails")
    assert CHECKS[-1] == "last check"


@pytest.mark.parametrize("name", ["Secret", "Phone", "Meeting", "Link", "Meeting Link"])
def test_a_person_named_like_a_placeholder_doesnt_change_it(name):
    people = People(people=[Person("[Person 1]", ("U00000001",), (name,), ())])
    text = f"{name} said call {'(201) 555-0123'} or https://meet.google.com/abc-defg-hij"
    scrubbed = scrub(text, people)
    assert scrubbed == "[Person 1] said call [phone] or [meeting link]"
    assert found(scrubbed) == []


@pytest.mark.parametrize(
    "left, kind",
    [
        (lambda: fake("github"), "a secret"),
        (lambda: rand(40), "a secret"),
        (lambda: "https://meet.google.com/abc-defg-hij", "a meeting link"),
        (lambda: "+44 20 7946 0958", "a phone number"),
    ],
)
def test_the_last_check_names_the_kind_never_the_value(left, kind):
    value = left()
    result = found(f"something left: {value}")
    assert result == [kind]
    assert value not in " ".join(result)


# Review fixes (#126)


def test_the_curl_rules_take_time_in_step_with_the_line_not_its_square():
    """Go reads a line once; Python's engine tried every curl again to the end of it."""
    rules = {rule.id: rule for rule in secrets.rules()}
    for rule_id in ("curl-auth-header", "curl-auth-user"):
        took = []
        for n in (20_000, 80_000):
            text = "curl -H " * (n // 8)
            started = time.perf_counter()
            list(rules[rule_id].pattern.finditer(text))
            took.append(time.perf_counter() - started)
        assert took[1] < 8 * max(took[0], 0.01), rule_id  # four times the text: about 4x


def test_a_curl_header_or_user_is_still_found():
    token, password = rand(32), rand(16)
    assert scrub(f'curl -H "Authorization: Bearer {token}" https://api.example.com') == (
        'curl -H "Authorization: Bearer [secret]" https://api.example.com'
    )
    assert scrub(f"curl -u admin:{password} https://api.example.com") == (
        "curl -u [secret] https://api.example.com"  # gitleaks' secret is user:password
    )


@pytest.mark.parametrize(
    "alphabet",
    [
        string.ascii_lowercase + string.digits,
        string.ascii_uppercase + string.digits,
        string.ascii_uppercase + "234567",  # base32, as in a 2FA seed
    ],
    ids=["small letters and digits", "capitals and digits", "base32"],
)
def test_a_random_run_in_one_case_becomes_secret_maybe(alphabet):
    value = rand(32, alphabet)
    while not (any(c.isdigit() for c in value) and any(c.isalpha() for c in value)):
        value = rand(32, alphabet)
    assert scrub(f"seed {value} end") == "seed [secret?] end"


@pytest.mark.parametrize(
    "text",
    [
        "123e4567-e89b-12d3-a456-426614174000",  # a UUID: an ID, left alone
        "my_project_notes_2024_final and q4_planning_2026_draft_v2",
        "export-slack-channel-history-2 and MAX_UPLOAD_SIZE_BYTES_2048",
        "differentialdatacollection2026 and weeklyupdate20261123alexsample",
        "CHANGELOG2026NOVEMBERDRAFT and thequickbrownfox2024jumpsoverthelazydog",
    ],
)
def test_names_in_one_case_are_left_alone(text):
    assert scrub(text) == text


@pytest.mark.parametrize(
    "number", ["Office 020.7946.0958/ext 12", "Call 201.555.0123/201.555.0124"]
)
def test_a_phone_number_with_dots_before_a_slash_isnt_read_as_a_link(number):
    scrubbed = scrub(number)
    assert "[phone]" in scrubbed and not re.search(r"\d{4}", scrubbed.replace("[phone]", ""))


def test_a_random_value_in_a_google_links_query_is_still_checked():
    doc = "https://docs.google.com/document/d/" + rand(44, URLSAFE) + "/edit"
    assert scrub(f"{doc}?x={rand(40)}") == f"{doc}?x=[secret?]"


# Review fixes, second round (#126: the second /review-pr and Codex)


def run_with_deadline(code: str, seconds: float = 20) -> None:
    """Run Python code in its own process and fail if it takes longer than `seconds`, so a
    rule that backtracks without end fails the test instead of hanging it."""
    subprocess.run([sys.executable, "-c", code], check=True, timeout=seconds)


def test_no_rule_backtracks_without_end_on_runs_of_line_breaks():
    run_with_deadline(
        "from diffdata.scrub import secrets\n"
        "for piece in ('\\r', '\\n', '\\r\\n', '\\t'):\n"
        "    text = 'curl' + piece * 50 + ' x -H x -u x'\n"
        "    for rule in secrets.rules():\n"
        "        list(rule.pattern.finditer(text * 200))\n"
    )


def test_a_long_curl_command_still_loses_its_credentials_and_nothing_is_flagged():
    token = rand(14)
    text = "curl " + "a" * 2050 + f' -H "Authorization: Bearer @{token}" https://api.example.com'
    scrubbed, flags = Scrubber(People()).scrub(text)
    assert token not in scrubbed and "Bearer [secret]" in scrubbed
    assert flags == [] and found(scrubbed) == []


@pytest.mark.parametrize(
    "link, scrubbed",
    [
        ("https://meet.google.com./abc-defg-hij", "[meeting link]"),
        ("meet.google.com./abc-defg-hij", "[meeting link]"),
        ("https://zoom.us/j/12345678901?note[]=test&pwd=Demo5 ok", "[meeting link] ok"),
        ("[join](https://zoom.us/j/12345678901?pwd=Demo5) now", "[join]([meeting link]) now"),
    ],
)
def test_a_meeting_link_with_a_trailing_dot_or_brackets_goes_whole(link, scrubbed):
    assert scrub(link) == scrubbed
    assert found(scrub(link)) == []


def test_a_rule_for_one_kind_of_file_runs_on_pasted_text():
    text = '<add key="ClearTextPassword" value="D0g.Frog!" />'
    scrubbed = scrub(text)
    assert "D0g.Frog!" not in scrubbed and found(scrubbed) == []


def test_mixed_case_letters_alone_at_24_or_more_become_secret_maybe():
    value = "".join(RANDOM.choice(string.ascii_letters) for _ in range(28))
    assert scrub(f"key {value} end") == "key [secret?] end"
    assert scrub("ExportSlackChannelHistoryNotes") == "ExportSlackChannelHistoryNotes"


def test_a_random_value_in_a_slack_link_path_is_checked_and_a_sites_name_stays():
    assert scrub("https://example.slack.com/api/demo/" + rand(30)) == (
        "https://example.slack.com/api/demo/[secret?]"
    )
    value = rand(30)
    assert scrub(f"https://example.com/a/{value}/b and example.com/{value}") == (
        "https://example.com/a/[secret?]/b and example.com/[secret?]"
    )


def test_a_tel_link_goes_with_its_extension():
    assert scrub("<tel:+12025550142;ext=7899|call>") == "<[phone]|call>"


def test_digits_in_an_email_are_left_for_the_email_step_and_nothing_is_flagged():
    scrubbed, flags = Scrubber(People()).scrub("mail 2025550142@example.org today")
    assert scrubbed == "mail [email] today" and flags == []


def test_an_at_after_secret_outside_a_link_is_still_a_mention():
    assert scrub("[secret]@private.handle") == "[secret]@[name?]"
    assert scrub("x://u:[secret]@host.example") == "x://u:[secret]@host.example"


def test_must_refuse_17_the_last_check_reads_past_invisible_characters_in_what_survives():
    """Every raw piece of the surviving half is under 20 characters, so only reading past
    the invisible characters finds it."""
    half = fake("anthropic")[54:]
    spread = "\u200b".join(half[i : i + 15] for i in range(0, len(half), 15))
    assert all(len(piece) < 20 for piece in spread.split("\u200b"))
    assert found(f"left: [secret]\u200b{spread}") == ["a secret"]


def test_the_rule_file_is_checked_again_on_every_scrub(monkeypatch, tmp_path):
    secrets.rules()  # compiled and cached
    monkeypatch.setattr(secrets, "RULES_FILE", tmp_path / "missing.toml")
    with pytest.raises(secrets.RulesError, match="is missing"):
        secrets.rules()


# Review fixes, Codex's second run


@pytest.mark.parametrize(
    "text, scrubbed",
    [
        ("curl -u admin:{p}; echo done", "curl -u [secret]; echo done"),
        ("curl --user=admin:{p} && ls", "curl --user=[secret] && ls"),
        (
            "https://example.test/login?password={p}&next=/home",
            "https://example.test/login?password=[secret]&next=/home",
        ),
        (
            "https://api.example.test/v1?api_key={p}#top",
            "https://api.example.test/v1?api_key=[secret]#top",
        ),
    ],
)
def test_a_credential_followed_by_a_shell_or_link_separator_is_replaced(text, scrubbed):
    result = scrub(text.format(p=fake("password")))
    assert result == scrubbed
    assert found(result) == []  # the placeholder itself never fails the last check


def test_a_placeholder_in_a_query_passes_the_last_check():
    assert found("https://example.test/x?key=[secret?]&token=[secret]") == []


def test_only_ids_in_a_google_links_path_are_kept():
    value = rand(24)
    redirect = f"https://docs.google.com/document/d/public-doc/edit?redirect=/d/{value}"
    assert value not in scrub(redirect)
    doc_id = rand(33, URLSAFE)
    assert scrub(f"https://drive.google.com/open?id={doc_id}") == (
        f"https://drive.google.com/open?id={doc_id}"
    )


def test_a_query_value_is_checked_whole_even_with_a_slash_in_it():
    value = rand(12) + "/" + rand(12)
    assert (
        scrub(f"https://example.com/cb?state={value}") == "https://example.com/cb?state=[secret?]"
    )


@pytest.mark.parametrize(
    "text, scrubbed",
    [
        ("https://TEAMS.MICROSOFT.COM./meet/1234?p=Demo5", "[meeting link]"),
        ("https://user:pw@zoom.us:443/j/123?pwd=Demo5 x", "[meeting link] x"),
        (
            "[join](https://zoom.us/j/12345678901?note=(demo)&pwd=Demo5) after",
            "[join]([meeting link]) after",
        ),
        ("https://zoom.us/j/1?ret=(https://meet.google.com/abc)&pwd=Demo5 x", "[meeting link] x"),
        ("(see https://zoom.us/j/1?pwd=Demo5) after", "(see [meeting link]) after"),
    ],
)
def test_meeting_links_are_read_by_their_real_host_and_bracket_balance(text, scrubbed):
    assert scrub(text) == scrubbed
    assert found(scrub(text)) == []


def test_a_tel_extension_after_a_closing_bracket_goes_too():
    assert scrub("<tel:+1-(202-555-0142);ext=7899|call>") == "<[phone]|call>"


@pytest.mark.parametrize(
    "email",
    ["2025550142@[192.0.2.1]", "{r}@private.example", "{r}@[IPv6:2001:db8::1]"],
)
def test_an_email_is_left_whole_for_the_email_step(email):
    scrubbed, flags = Scrubber(People()).scrub(f"mail {email.format(r=rand(24))} today")
    assert scrubbed == "mail [email] today" and flags == []


def test_many_links_are_read_in_step_with_their_number():
    took = []
    for n in (2_000, 8_000):
        text = "https://example.com/aaaaaaaaaaaaaaaaaaaaaaaa " * n
        started = time.perf_counter()
        found(scrub(text))
        took.append(time.perf_counter() - started)
    assert took[1] < 8 * max(took[0], 0.01)  # four times the links: about 4x


# Review fixes, third round (the final /review-pr at dba7601)


@pytest.mark.parametrize(
    "host", ["zoom​.us", "ｚｏｏｍ.us"], ids=["zero-width space", "wide letters"]
)
def test_a_secret_in_a_meeting_link_with_a_disguised_host_still_goes(host):
    for value in (fake("github"), rand(40)):
        scrubbed = scrub(f"join https://{host}/j/1?x={value} now")
        assert scrubbed == "join [meeting link] now"
        assert found(scrubbed) == []
    # The last check finds a disguised meeting link too.
    assert found(f"https://{host}/j/1?pwd=x") == ["a meeting link"]


def test_the_kubernetes_rule_is_quick_on_a_long_run_of_spaces():
    run_with_deadline(
        "from diffdata.scrub.text import Scrubber\n"
        "from diffdata.scrub.people import People\n"
        "scrubber = Scrubber(People())\n"
        "text = 'kind: Secret\\ndata:\\n  demo: |' + ' ' * 20000 + '!'\n"
        "scrubber.leftovers(scrubber.scrub(text)[0])\n",
        seconds=10,
    )


@pytest.mark.parametrize(
    "text",
    [
        '<add key="Password" value="{p}" />',
        'password = "{p}"',
        "kind: Secret\ndata:\n  token: {p}",
    ],
)
def test_a_rules_secret_thats_only_a_placeholder_passes_the_last_check(text):
    scrubbed = scrub(text.format(p=fake("password")))
    assert "[secret]" in scrubbed
    assert found(scrubbed) == []


def test_a_random_run_just_before_an_email_is_replaced_and_the_email_too():
    scrubbed = scrub(f"{rand(40)}/x@example.org")
    assert scrubbed == "[secret?]/[email]"
    assert found(scrubbed) == []


def test_a_plain_word_in_a_query_isnt_replaced_all_through_the_copy():
    text = "https://example.com/page?key=features and the features list, auth=required"
    assert (
        scrub(text) == "https://example.com/page?key=[secret] and the features list, auth=required"
    )


def test_the_same_password_in_curl_and_a_link_leaves_the_links_host_alone():
    password = fake("password")
    scrubbed, flags = Scrubber(People()).scrub(
        f"curl -u admin:{password}; psql postgres://admin:{password}@db.internal.test/app"
    )
    assert scrubbed == "curl -u [secret]; psql postgres://[secret]@db.internal.test/app"
    assert flags == [] and found(scrubbed) == []
