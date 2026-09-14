#!/usr/bin/env python3
"""Self-checks for the fix-issue skill. Run from the repo root.

Exits non-zero on any failure so this can gate a commit or a CI job.
Covers what a reader cannot verify by eye: link resolution, tier naming,
owner-split accounting, agent-definition/tier-table agreement, shell syntax,
and the token budget against the post-compaction re-attachment cap.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SKILL = Path(".claude/skills/fix-issue")
AGENTS = Path(".claude/agents")
REATTACH_CAP = 5000
# Coordinator-owned references. ALWAYS_COORD is the subset a normal run reads,
# which is what the resident-token figure is about; autonomous-mode is
# coordinator-owned but only read when the user signals that mode.
# Role -> tier. Checked against each definition's `model` AND against SKILL.md's
# role table, so a change to either alone is caught.
ROLE_TIER = {
    "fix-issue-investigator": "LIGHT",
    "fix-issue-verifier": "LIGHT",
    "fix-issue-renderer": "LIGHT",
    "fix-issue-simplifier": "MID",
    "fix-issue-gate-specialist": "MID",
    "fix-issue-diagnostician": "HIGH",
    "fix-issue-writer": "HIGH",
    "fix-issue-visual-reviewer": "HIGH",
    "fix-issue-reviewer": "HIGH",
    "fix-issue-merge-assessor": "HIGH",
}

# Tools each role cannot do its briefed job without.
REQUIRED_TOOLS = {
    "fix-issue-investigator": {"Bash"},
    "fix-issue-verifier": {"Bash"},
    "fix-issue-renderer": {"Bash", "Skill"},
    "fix-issue-diagnostician": {"Bash", "Read"},
    "fix-issue-writer": {"Read", "Edit", "Write", "Bash"},
    "fix-issue-simplifier": {"Skill"},
    "fix-issue-gate-specialist": {"Bash"},
    "fix-issue-visual-reviewer": {"Bash", "Read"},
    "fix-issue-reviewer": {"Bash", "Read"},
    "fix-issue-merge-assessor": {"Bash"},
}

ALWAYS_COORD = {"coordinator.md", "agent-types.md", "scope-discipline.md", "merge-and-cleanup.md"}
COORD_REFS = ALWAYS_COORD | {"autonomous-mode.md"}

failures: list[str] = []


def fail(msg: str) -> None:
    failures.append(msg)


def logical_lines(block: str) -> list[str]:
    """Shell continuations make a guard span lines; join them before checking."""
    return re.sub(r"\\\n\s*", " ", block).splitlines()


def md_files() -> list[Path]:
    return [SKILL / "SKILL.md", *sorted((SKILL / "references").glob("*.md"))]


def check_links() -> None:
    for f in md_files() + sorted(AGENTS.glob("*.md")):
        for _, target in re.findall(r"\[([^\]]+)\]\(([^)]+)\)", f.read_text()):
            if target.startswith(("http", "#")):
                continue
            if not (f.parent / target.split("#")[0]).resolve().exists():
                fail(f"broken link in {f.name}: {target}")


def check_steps() -> None:
    body = (SKILL / "SKILL.md").read_text()
    spine = set(re.findall(r"^(\d{1,2})\. \*\*", body, re.M))
    for f in md_files():
        for n in set(re.findall(r"\bStep (\d+)\b", f.read_text())):
            if n not in spine:
                fail(f"{f.name} references Step {n}, absent from the spine")


def check_tiers_named() -> None:
    """Every spawn instruction must name a tier; the table is the contract."""
    tiers = ("LIGHT", "MID", "HIGH")
    for f in md_files():
        lines = f.read_text().splitlines()
        for i, line in enumerate(lines, 1):
            # Match any spawn phrasing: the role nouns AND the agent type names,
            # since a brief may name either. Omitting a noun here is how a
            # missing tier slipped through before.
            nouns = (r"worker|reviewer|verifier|specialist|investigator|assessor"
                     r"|diagnostician|writer|renderer|simplifier|diagnostic")
            verbs = r"[Aa]ssign|[Ss]pawn|[Ll]aunch|[Bb]rief|[Rr]oute to"
            new_spawn = rf"\b({verbs})\b\s+(a|an|one)\s+(fresh\s+)?"
            if not re.search(new_spawn, line):
                continue
            if not (re.search(rf"{new_spawn}.*\b({nouns})\b", line)
                    or re.search(rf"{new_spawn}.*`fix-issue-[a-z-]+`", line)):
                continue
            if re.search(r"`fix-issue-[a-z-]+`", line):
                continue   # the agent type carries its tier; checked against the table
            ctx = " ".join(lines[max(0, i - 2) : i + 2])
            if not any(t in ctx for t in tiers) and "sole writer" not in ctx:
                fail(f"{f.name}:{i} assigns a worker without naming a tier")


def table_tier_for(table: str, stem: str) -> str:
    """The tier cell of the role-table row naming this agent type exactly."""
    for line in table.splitlines():
        if line.startswith("|") and f"`{stem}`" in line:
            cells = [c.strip() for c in line.split("|")]
            return cells[4] if len(cells) > 4 else ""
    return ""


def check_agent_definitions() -> None:
    """Definitions must parse, and their model must agree with the tier table."""
    tier_model = {"LIGHT": "haiku", "MID": "sonnet", "HIGH": "claude-opus-4-8"}
    table = (SKILL / "SKILL.md").read_text()
    for a in sorted(AGENTS.glob("fix-issue-*.md")):
        head = a.read_text().split("---")[1]
        fields = dict(re.findall(r"^(\w+):\s*(.+)$", head, re.M))
        for required in ("name", "description", "model", "tools", "effort"):
            if required not in fields:
                fail(f"{a.name} missing frontmatter field '{required}'")
        if fields.get("name") != a.stem:
            fail(f"{a.name} name '{fields.get('name')}' != filename")
        if fields.get("model") not in tier_model.values():
            fail(f"{a.name} model '{fields.get('model')}' is not a tier model")
        if fields.get("effort") not in {"low", "medium", "high", "xhigh", "max"}:
            fail(f"{a.name} effort '{fields.get('effort')}' is not a documented value")
        if a.stem != "fix-issue-writer":
            for forbidden in {"Edit", "Write", "NotebookEdit"} & {
                h.split("(")[0] for h in {t.strip() for t in fields.get("tools", "").split(",")}
            }:
                fail(f"{a.name} is read-only but holds `{forbidden}`")
        needs = REQUIRED_TOOLS.get(a.stem, set())
        have = {t.strip() for t in fields.get("tools", "").split(",")}
        for tool in needs - {h.split("(")[0] for h in have}:
            fail(f"{a.name} cannot do its job without `{tool}` in tools")
        expected = ROLE_TIER.get(a.stem)
        if expected is None:
            fail(f"{a.name} has no entry in ROLE_TIER; add it so drift is detectable")
        elif fields.get("model") != tier_model[expected]:
            fail(f"{a.name} is {fields.get('model')} but its role tier is {expected}"
                 f" ({tier_model[expected]})")
        elif expected not in table_tier_for(table, a.stem):
            fail(f"{a.name}: SKILL.md's role table does not give it tier {expected}")
        if "Agent" in fields.get("tools", "") and "Agent(" not in fields.get("tools", ""):
            fail(f"{a.name} grants unrestricted Agent; use Agent(<type>)")
        if a.stem not in table:
            fail(f"{a.name} is not named in SKILL.md")


def check_no_dangling_names() -> None:
    body = (SKILL / "SKILL.md").read_text()
    defined = {p.stem for p in AGENTS.glob("fix-issue-*.md")}
    for named in set(re.findall(r"`(fix-issue-[a-z-]+)`", body)):
        if named not in defined:
            fail(f"SKILL.md names `{named}` but no agent definition exists")
    linked = set()
    for f in md_files():
        linked |= {t.split("#")[0].split("/")[-1] for _, t in re.findall(r"\[([^\]]+)\]\(([^)]+)\)", f.read_text())}
    rows = "\n".join(l for l in (SKILL / "SKILL.md").read_text().splitlines()
                      if l.startswith("|") and "references/" in l)
    for ref in (SKILL / "references").glob("*.md"):
        if ref.name not in linked:
            fail(f"references/{ref.name} is orphaned: nothing links to it")
        if ref.name not in rows:
            fail(f"references/{ref.name} is missing from SKILL.md's owner table")


def check_prose_tool_claims() -> None:
    """Prose in a coord-owned file that names an agent and a backticked tool must
    agree with that agent's definition. This is how a removed `Skill` grant kept
    being described as present."""
    tools = {}
    for a in AGENTS.glob("fix-issue-*.md"):
        head = a.read_text().split("---")[1]
        m = re.search(r"^tools:\s*(.+)$", head, re.M)
        tools[a.stem] = {t.strip().split("(")[0] for t in (m.group(1) if m else "").split(",")}
    for f in md_files():
        for line in f.read_text().splitlines():
            for agent in re.findall(r"`(fix-issue-[a-z-]+)`", line):
                if agent not in tools:
                    continue
                for claimed in re.findall(r"`(Skill|Agent|Write|Edit)`", line):
                    if claimed not in tools[agent] and "no " not in line.lower():
                        fail(f"{f.name}: says `{agent}` has `{claimed}`, but it does not")


def check_repo_paths() -> None:
    """A command pointed at a path that does not exist is the class that produced
    the build_gallery.py lock grep and the fabricated expected_aborts key."""
    for f in md_files():
        for m in re.finditer(r"```bash\n(.*?)```", f.read_text(), re.S):
            for path in re.findall(r"(?<![\w/.-])((?:scripts|tests|src|examples|docs|\.github|\.claude)/[\w./-]+)", m.group(1)):
                if "<" in path or path.endswith("/"):
                    continue
                if not Path(path).exists():
                    fail(f"{f.name}: command references missing path {path}")


def check_prose_paths() -> None:
    """A fabricated path in prose misdirects just as effectively as one in a
    command; both have happened."""
    for f in md_files():
        body = re.sub(r"```.*?```", "", f.read_text(), flags=re.S)
        for path in re.findall(r"`((?:scripts|tests|src|examples|docs|\.github|\.claude)/[\w./-]+)`", body):
            if "<" in path or path.endswith("/"):
                continue
            if not Path(path).exists():
                fail(f"{f.name}: prose references missing path {path}")


def check_numeric_consistency() -> None:
    """The same measured quantity must carry the same figure everywhere. Two files
    disagreed on the Explore saving ($0.24 vs "a few dollars") and nothing caught
    it."""
    # Distinctive subjects only, and sentence scope: a paragraph mentioning several
    # quantities produced false conflicts.
    subjects = ("explore", "fork")
    figures: dict[str, set[str]] = {}
    for f in md_files():
        for sentence in re.split(r"(?<=[.;])\s+", f.read_text()):
            amounts = {a.rstrip(".") for a in re.findall(r"\$([\d.,]+)", sentence)}
            low = sentence.lower()
            for subject in subjects:
                if amounts and subject in low:
                    figures.setdefault(subject, set()).update(amounts)
    for subject, vals in sorted(figures.items()):
        if len(vals) > 1:
            fail(f"'{subject}' carries conflicting figures {sorted(vals)}; "
                 "one measured quantity, one number")


def check_owner_split() -> None:
    """The coordinator must not be told to read worker-facing references."""
    body = (SKILL / "SKILL.md").read_text()
    rows = re.findall(r"^\| \[`([^`]+)`\][^|]*\| ([^|]+)\|", body, re.M)
    if not rows:
        fail("reference table has no owner column")
    for name, owner in rows:
        declared_coord = "coord" in owner
        if declared_coord != (name in COORD_REFS):
            fail(f"{name}: owner column says '{owner.strip()}', COORD_REFS says {name in COORD_REFS}")


def check_shell_blocks() -> None:
    for f in md_files():
        for m in re.finditer(r"```bash\n(.*?)```", f.read_text(), re.S):
            code = re.sub(r"<[^<>\n]{1,40}>", "PLACEHOLDER", m.group(1))
            with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as fh:
                fh.write(code)
                path = fh.name
            for shell in ("bash", "zsh"):
                r = subprocess.run([shell, "-n", path], capture_output=True, text=True)
                if r.returncode:
                    fail(f"{shell} syntax error in {f.name}: {r.stderr.strip()[:120]}")


def check_guards_self_fail() -> None:
    """`set -e` is inert here: the Bash tool runs zsh and evals the block, so
    ERREXIT never fires. Any bare `test ...` line is a decorative guard."""
    for f in md_files():
        for m in re.finditer(r"```bash\n(.*?)```", f.read_text(), re.S):
            block = m.group(1)
            if re.search(r"^\s*set -[a-z]*e", block, re.M):
                fail(f"{f.name}: shell block relies on `set -e`, which does not fire in this harness")
            for line in logical_lines(block):
                stripped = line.strip()
                if re.match(r"(test |\[ |\[\[ )", stripped) and "||" not in stripped:
                    if not re.match(r"(if|while|until)\b", stripped):
                        fail(f"{f.name}: bare guard `{stripped[:60]}` cannot fail the "
                             "block; add `|| die ...`")
                        continue
                if re.match(r"if (\[|\[\[|test )", stripped) and "; then" in stripped \
                        and not re.search(r"\b(exit|die|return|continue|break)\b", stripped):
                    fail(f"{f.name}: bare guard `{stripped[:60]}` cannot fail the block; add `|| die ...`")


def check_recurring_defect_classes() -> None:
    """Three defects recurred across reviews; each is now a check.

    1. An `nf_metro` invocation without PYTHONPATH either raises
       ModuleNotFoundError or, worse, silently reads an installed snapshot
       instead of the worktree under test.
    2. `curl` without `-f` exits 0 on a 404 and saves the error page, so a
       missing preview reads as an empty result rather than a failure.
    3. A `git worktree add -b` without `--no-track` takes main as upstream, and
       a later bare push suggests pushing to main.
    """
    for f in md_files():
        for m in re.finditer(r"```bash\n(.*?)```", f.read_text(), re.S):
            block = m.group(1)
            for line in logical_lines(block):
                if re.search(r"\b(python -m nf_metro|python -m pytest|probe_layout\.py|inspect_layout\.py|routing_gate_coverage\.py|test_guard_registry_golden\.py)", line):
                    exported = re.search(r"^\s*export PYTHONPATH=", block, re.M)
                    if "PYTHONPATH" not in line and not exported:
                        fail(f"{f.name}: `{line.strip()[:50]}` runs without PYTHONPATH")
                if re.search(r"\bcurl\b", line) and not re.search(r"-[a-zA-Z]*f", line):
                    fail(f"{f.name}: curl without -f will exit 0 on a 404: {line.strip()[:50]}")
                if "worktree add" in line and "-b " in line and "--no-track" not in line:
                    fail(f"{f.name}: `worktree add -b` without --no-track sets upstream to main")
            for m2 in re.finditer(r"^\s*if \[.*?\]; then\s*$", block, re.M):
                tail = block[m2.end():m2.end() + 200]
                if not re.search(r"\b(exit|die|return)\b", tail.split("fi")[0]):
                    fail(f"{f.name}: `if [ ... ]` guard whose body never exits is decorative")
            if re.search(r"ART=[^\n]*\$\$", block):
                fail(f"{f.name}: `$$` in a shared artifact path - it differs per Bash "
                     "call, so later blocks cannot find it")
            if re.search(r"\$ART/", block) and "mkdir -p" not in block:
                fail(f"{f.name}: block writes into $ART without creating it")
            if "die " in block and "die() {" not in block:
                fail(f"{f.name}: block calls `die` without defining it; the guard is decorative")


def check_grep_strings_exist() -> None:
    """A script greps the CI comment for specific wordings. Assert those strings
    are what the workflow actually emits, so a self-consistent rename of both the
    grep and its own test fixture cannot pass."""
    wf = Path(".github/workflows/pr-render-publish.yml")
    if not wf.exists():
        fail("pr-render-publish.yml missing; the sticky-comment wordings cannot be checked")
        return
    emitted = wf.read_text()
    script = SKILL / "scripts" / "visual_preview.sh"
    if not script.exists():
        return
    for phrase in re.findall(r'grep -q[xE]? "([^"$]{12,})"', script.read_text()):
        for alt in phrase.split("|"):
            if alt and alt not in emitted:
                fail(f"visual_preview.sh greps for '{alt}', which pr-render-publish.yml "
                     "does not emit")


def check_scripts() -> None:
    """The bundled scripts are the part that used to be prose. Parse them in both
    shells and run their self-tests, so a defect in them fails here rather than
    in a run. This is what a documented block could never offer."""
    scripts = sorted((SKILL / "scripts").glob("*.sh"))
    if not scripts:
        fail("no scripts/*.sh found; the shell chains should live in scripts")
    for sh_file in scripts:
        for shell in ("bash", "zsh"):
            r = subprocess.run([shell, "-n", str(sh_file)], capture_output=True, text=True)
            if r.returncode:
                fail(f"{sh_file.name}: {shell} parse error: {r.stderr.strip()[:120]}")
        # shellcheck catches what a parse check cannot: a variable referenced but
        # never assigned, which is exactly the defect class that kept shipping.
        if shutil.which("shellcheck"):
            r = subprocess.run(["shellcheck", "-S", "warning", str(sh_file)],
                               capture_output=True, text=True)
            if r.returncode:
                first = next((l for l in r.stdout.splitlines() if "SC" in l), r.stdout[:100])
                fail(f"{sh_file.name}: shellcheck: {first.strip()[:120]}")
        if "--self-test" not in sh_file.read_text():
            fail(f"{sh_file.name} has no --self-test; its logic cannot be checked here")
            continue
        r = subprocess.run(["bash", str(sh_file), "--self-test"], capture_output=True, text=True)
        if r.returncode:
            fail(f"{sh_file.name} --self-test failed: "
                 f"{(r.stdout + r.stderr).strip().splitlines()[-1][:120]}")
    # Python helpers get the same treatment. Run them from a foreign cwd with an
    # explicit repo argument: a positional-argument bug in one of these was
    # invisible while everything happened to run from the repo root.
    repo = Path.cwd().resolve()
    for py_file in sorted((SKILL / "scripts").glob("*.py")):
        if py_file.name == Path(__file__).name:
            continue
        if "--self-test" not in py_file.read_text():
            fail(f"{py_file.name} has no --self-test; its logic cannot be checked here")
            continue
        r = subprocess.run([sys.executable, str(py_file.resolve()), "--self-test", str(repo)],
                           capture_output=True, text=True, cwd="/")
        if r.returncode:
            last = (r.stdout + r.stderr).strip().splitlines()
            fail(f"{py_file.name} --self-test failed from a foreign cwd: "
                 f"{last[-1][:120] if last else 'no output'}")

    # Every script must be referenced, or it is dead weight nobody will run.
    body = "\n".join(f.read_text() for f in md_files())
    for script in scripts + sorted((SKILL / "scripts").glob("*.py")):
        if script.name == Path(__file__).name:
            continue
        if script.name not in body:
            fail(f"{script.name} is not referenced from any skill file")


def check_token_budget() -> None:
    """Gate SKILL.md against the post-compaction re-attachment cap.

    The correct instrument is Anthropic's count_tokens endpoint: token counts are
    model-specific and only it knows Claude's tokenizer. Use it when the SDK and a
    credential are present. Otherwise fall back to tiktoken, which is OpenAI's
    encoding and is documented to **undercount Claude tokens by 15-20%**, more on
    code. So the fallback is not compared to the cap directly: it is scaled by
    that factor first. A raw proxy reading under 5,000 means nothing.
    """
    body_text = (SKILL / "SKILL.md").read_text()

    exact = None
    try:
        import anthropic  # noqa: PLC0415

        exact = (
            anthropic.Anthropic()
            .messages.count_tokens(
                model="claude-opus-5",
                messages=[{"role": "user", "content": body_text}],
            )
            .input_tokens
        )
    except Exception:
        exact = None  # no SDK, no credential, or no network: fall back below

    if exact is not None:
        margin = REATTACH_CAP - exact
        print(f"  SKILL.md {exact} tokens (count_tokens, exact); {margin} headroom "
              f"under the {REATTACH_CAP} cap")
        if margin < 0:
            fail(f"SKILL.md is {exact} tokens, over the {REATTACH_CAP} re-attachment cap")
        return

    try:
        import tiktoken  # noqa: PLC0415
    except ImportError:
        fail("neither the anthropic SDK nor tiktoken is available, so the token "
             "budget cannot be checked at all")
        return

    enc = tiktoken.get_encoding("o200k_base")
    proxy = len(enc.encode(body_text))
    # Documented undercount is 15-20%; take the pessimistic end.
    UNDERCOUNT = 1.20
    projected = int(proxy * UNDERCOUNT)
    coord = proxy + sum(len(enc.encode((SKILL / "references" / n).read_text()))
                        for n in sorted(ALWAYS_COORD))
    print(f"  SKILL.md {proxy} o200k tokens -> ~{projected} Claude tokens at the "
          f"documented {int((UNDERCOUNT - 1) * 100)}% undercount (cap {REATTACH_CAP}); "
          f"coordinator-resident ~{int(coord * UNDERCOUNT)}")
    # 5% slack on top of the pessimistic projection: the 15-20% band is itself a
    # range, and the docs say the undercount is worse on code-heavy content.
    ceiling = int(REATTACH_CAP * 0.95)
    if projected > ceiling:
        fail(f"SKILL.md projects to ~{projected} Claude tokens, over the {ceiling} "
             f"working ceiling ({REATTACH_CAP} cap less 5% slack). Install the "
             "anthropic SDK with a credential for an exact count, or cut the file.")


def main() -> int:
    for check in (
        check_links,
        check_steps,
        check_tiers_named,
        check_agent_definitions,
        check_owner_split,
        check_numeric_consistency,
        check_no_dangling_names,
        check_repo_paths,
        check_prose_tool_claims,
        check_prose_paths,
        check_shell_blocks,
        check_scripts,
        check_grep_strings_exist,
        check_guards_self_fail,
        check_recurring_defect_classes,
        check_token_budget,
    ):
        check()
    if failures:
        print(f"FAIL: {len(failures)} problem(s)")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("OK: fix-issue skill self-checks pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
