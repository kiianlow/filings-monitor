"""
Guards for the rules that have each broken the live bot at least once.
These read the repo's own files; they do not run anything.
"""

import re
from pathlib import Path

import pytest

import halts
import monitor

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
STATE_WRITERS = ["monitor.yml", "halts.yml", "congress.yml", "trump.yml",
                 "earnings.yml", "events.yml"]


def wf(name):
    return (ROOT / ".github" / "workflows" / name).read_text()


def test_the_workflow_files_kick_gs_starts_still_exist():
    """Kick.gs dispatches these by file name. A rename makes every kick 404."""
    for name in ("monitor.yml", "halts.yml", "congress.yml", "earnings.yml", "events.yml"):
        assert (ROOT / ".github" / "workflows" / name).exists(), name
        assert "workflow_dispatch:" in wf(name), name


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_every_workflow_keeps_its_pins_and_a_timeout(path):
    text = path.read_text()
    assert "runs-on: ubuntu-24.04" in text and "ubuntu-latest" not in text
    assert 'python-version: "3.11"' in text
    assert "timeout-minutes:" in text
    assert "pip install -r requirements.txt" in text


@pytest.mark.parametrize("name", STATE_WRITERS)
def test_a_state_commit_can_never_fail_a_job(name):
    text = wf(name)
    step = text[text.index("- name: Commit state"):]
    assert "continue-on-error: true" in step
    assert step.rstrip().endswith("exit 0")
    assert "git pull --rebase --autostash" in step
    assert "for i in 1 2 3 4 5" in step
    assert "cancel-in-progress: false" in text
    assert re.search(r"concurrency:\s*\n\s*group: \S+", text)


def test_each_workflow_has_its_own_concurrency_group():
    groups = [re.search(r"group: (\S+)", wf(n)).group(1) for n in STATE_WRITERS]
    assert len(set(groups)) == len(groups)


def test_heartbeat_workflow_cannot_write_to_the_repo():
    text = wf("heartbeat.yml")
    assert "contents: read" in text and "contents: write" not in text
    assert "git push" not in text


def test_tests_workflow_reads_no_secrets_and_cannot_write():
    text = wf("tests.yml")
    assert "${{ secrets." not in text and "contents: read" in text
    assert "paths:" in text


def test_digest_workflows_pass_force_only_when_asked():
    for name, script in (("earnings.yml", "earnings.py"), ("events.yml", "events.py")):
        text = wf(name)
        assert f"run: python {script} ${{{{ inputs.force && '--force' || '' }}}}" in text
        assert "default: false" in text
        assert text.count("- cron:") == 3


def test_dependencies_are_pinned_exactly():
    lines = [ln for ln in (ROOT / "requirements.txt").read_text().splitlines()
             if ln.strip() and not ln.startswith("#")]
    assert len(lines) == 4
    assert all(re.fullmatch(r"[A-Za-z0-9_.-]+==[0-9][0-9A-Za-z.]*", ln) for ln in lines), lines


def test_no_credentials_in_the_repo():
    token = re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b")           # Telegram bot token
    gh = re.compile(r"\b(ghp|gho|ghs|github_pat)_[A-Za-z0-9_]{20,}")
    for path in list(ROOT.glob("*.py")) + list(ROOT.glob("*.md")) + WORKFLOWS + \
            [ROOT / ".env.example"]:
        if not path.exists():
            continue
        text = path.read_text()
        assert not token.search(text), f"something shaped like a bot token in {path.name}"
        assert not gh.search(text), f"something shaped like a GitHub token in {path.name}"


def test_secrets_come_only_from_the_environment():
    text = (ROOT / "monitor.py").read_text()
    for name in ("TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "SEC_USER_AGENT", "FINNHUB_KEY"):
        assert f'{name} = os.environ.get("{name}", "")' in text


def test_status_contract_with_the_apps_script_card():
    """Status.gs reads these two files from GitHub by name and by field."""
    assert halts.STATE_FILE.name == "halts_state.json"
    assert monitor.HEALTH_FILE.name == "health.json"
    src = (ROOT / "halts.py").read_text()
    assert src.count('stamp("halts_state.json"') == 3
    assert 'health[f] = stamp' in (ROOT / "monitor.py").read_text()


def test_message_format_rules():
    assert monitor.PREVIEW_HEAD == "CONGRESS" and monitor.PREVIEW_SPACER == 56
    assert monitor.SOURCE_TAG == "\U0001F3DB️"
    assert monitor.BOX_LANG == "plaintext"
    assert '"disable_notification": False' in (ROOT / "monitor.py").read_text()


def test_one_writer_per_state_file():
    """A state file written by two workflows collides on git push."""
    owners = {}
    for mod in ("monitor", "halts", "congress", "trump", "earnings", "events"):
        src = (ROOT / f"{mod}.py").read_text()
        for fname in set(re.findall(r'"([a-z_]+\.json)"', src)):
            if re.search(rf'(stamp\(\s*"{fname}"|STATE_DIR / "{fname}")', src):
                owners.setdefault(fname, set()).add(mod)
    shared = {f: m for f, m in owners.items() if len(m) > 1}
    # monitor.py only READS these two (heartbeat, and the read-only company list).
    assert set(shared) <= {"congress_health.json", "sec_companies.json"}, shared


def test_monitor_job_limit_leaves_room_after_the_run_budget():
    """monitor.py stops starting work at RUN_BUDGET_SECONDS; the job limit
    must leave room for a slow filing in progress, setup and the commit."""
    limit = int(re.search(r"timeout-minutes: (\d+)", wf("monitor.yml")).group(1))
    assert limit * 60 - monitor.RUN_BUDGET_SECONDS >= 8 * 60


def test_halts_history_has_one_writer_and_is_committed():
    for mod in ("monitor", "congress", "trump", "earnings", "events"):
        assert "halts.csv" not in (ROOT / f"{mod}.py").read_text(), mod
    assert halts.HALTS_CSV.name == "halts.csv"
    yml = wf("halts.yml")
    # On its own line, so a missing file cannot stop state being added.
    assert "git add state || true" in yml
    assert "git add data/halts.csv 2>/dev/null || true" in yml


def test_commit_steps_take_the_branch_from_env():
    for name in STATE_WRITERS:
        text = wf(name)
        assert "BRANCH: ${{ github.ref_name }}" in text, name
        assert 'origin "$BRANCH"' in text and 'HEAD:"$BRANCH"' in text, name
        # once for the commit step, once for the checkout below
        assert text.count("${{ github.ref_name }}") == 2, name


@pytest.mark.parametrize("name", STATE_WRITERS)
def test_state_writers_check_out_the_branch_tip_and_never_leave_a_rebase(name):
    """A queued run must start from the newest state, not the state that was
    current when it was queued, or it repeats the previous run's alerts."""
    text = wf(name)
    co = text.index("actions/checkout@v4")
    assert "ref: ${{ github.ref_name }}" in text[co:co + 500], name
    assert "git rebase --abort 2>/dev/null || true" in text, name
