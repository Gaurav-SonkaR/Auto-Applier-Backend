"""End-to-end test of LLM output -> injected LaTeX, against the real template.

`test_resume_template.py` checks the template still *matches* the injector's
regexes. This checks the step after: that injecting a realistic LLM response
actually replaces the right content and leaves a structurally valid document.

That gap matters because every injector failure is silent by design — the
regexes `.sub()` with no match, log a warning, and return the template
unchanged. A resume that quietly ships the generic seed content to an employer
looks exactly like a successful generation.

No pdflatex here (see the note at the bottom of the module), so this asserts
structural validity: balanced braces, closed environments, locked strings
intact, and prompt-injected commands defanged.
"""
import re
from pathlib import Path

import pytest

from backend.app.services.resume_generator import (
    locked_strings,
    SKILL_CATEGORIES,
    _inject_into_template,
    _validate_locked_fields,
)

TEMPLATE = Path(__file__).resolve().parents[1] / "templates" / "resume_template.tex"


@pytest.fixture(scope="module")
def template() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


@pytest.fixture
def llm_response() -> dict:
    """A well-formed response shaped like RESUME_SYSTEM_PROMPT demands."""
    return {
        "professional_summary": (
            "Backend engineer with production Django REST and FastAPI experience "
            "building containerised services on AWS with PostgreSQL tuning."
        ),
        "skills_row_order": [
            "Backend", "Cloud & DevOps", "Databases", "Languages",
            "AI / GenAI", "Automation", "Frontend", "ML / Data",
        ],
        "secondary_skills_to_include": ["Kubernetes"],
        "experience_bullets": {
            "job_0": [f"Experience bullet {i} with Django and AWS." for i in range(5)],
            "job_1": [f"Intern bullet {i} with Vue.js and MySQL." for i in range(2)],
        },
        "project_bullets": {
            "project_0": ["Agentic RAG bullet one.", "Agentic RAG bullet two."],
            "project_1": ["E-commerce bullet."],
            "project_2": ["Fraud detection bullet."],
            "project_3": ["Scraper bullet."],
        },
        "ats_keywords_used": ["Django", "FastAPI", "AWS"],
    }


# ── LaTeX structural helpers ─────────────────────────────────────────────────

def strip_comments(tex: str) -> str:
    return "\n".join(l for l in tex.split("\n") if not l.lstrip().startswith("%"))


def brace_balance(tex: str) -> int:
    """Net unmatched braces, ignoring escaped \\{ and \\}."""
    body = re.sub(r"\\[{}]", "", strip_comments(tex))
    return body.count("{") - body.count("}")


def environments(tex: str) -> list[tuple[str, int, int]]:
    body = strip_comments(tex)
    names = set(re.findall(r"\\begin\{(\w+)\}", body)) | set(re.findall(r"\\end\{(\w+)\}", body))
    return [
        (n, len(re.findall(rf"\\begin\{{{n}\}}", body)), len(re.findall(rf"\\end\{{{n}\}}", body)))
        for n in sorted(names)
    ]


class TestTemplateIsValidBefore:
    """Baseline — if the template itself is malformed the rest proves nothing."""

    def test_braces_balanced(self, template):
        assert brace_balance(template) == 0

    def test_environments_closed(self, template):
        for name, opens, closes in environments(template):
            assert opens == closes, f"\\begin{{{name}}} x{opens} vs \\end x{closes}"

    def test_has_document_wrapper(self, template):
        assert "\\begin{document}" in template
        assert "\\end{document}" in template


class TestInjectionProducesValidLatex:
    def test_braces_still_balanced(self, template, llm_response):
        assert brace_balance(_inject_into_template(template, llm_response)) == 0

    def test_environments_still_closed(self, template, llm_response):
        out = _inject_into_template(template, llm_response)
        for name, opens, closes in environments(out):
            assert opens == closes, f"\\begin{{{name}}} x{opens} vs \\end x{closes}"

    def test_document_wrapper_survives(self, template, llm_response):
        out = _inject_into_template(template, llm_response)
        assert out.count("\\begin{document}") == 1
        assert out.count("\\end{document}") == 1

    def test_preamble_untouched(self, template, llm_response):
        """Injection must never reach above \\begin{document}."""
        out = _inject_into_template(template, llm_response)
        assert template.split("\\begin{document}")[0] == out.split("\\begin{document}")[0]


class TestContentActuallyReplaced:
    """The failure mode this file exists for: injection silently no-ops and the
    generic seed resume ships as if it were tailored."""

    def test_summary_replaced(self, template, llm_response):
        out = _inject_into_template(template, llm_response)
        assert llm_response["professional_summary"] in out
        seed = re.search(
            r"\\section\{Professional Summary\}\n\{\\small\n(.*?)\n\}", template, re.DOTALL
        ).group(1)
        assert seed.strip() not in out

    def test_experience_bullets_replaced(self, template, llm_response):
        out = _inject_into_template(template, llm_response)
        for bullet in llm_response["experience_bullets"]["job_0"]:
            assert bullet in out
        assert "Designed and deployed 25+ production REST APIs" not in out

    def test_project_bullets_replaced(self, template, llm_response):
        out = _inject_into_template(template, llm_response)
        for key in ("project_0", "project_1", "project_2", "project_3"):
            for bullet in llm_response["project_bullets"][key]:
                assert bullet in out

    def test_bullet_counts_match_the_response(self, template, llm_response):
        out = _inject_into_template(template, llm_response)
        blocks = re.findall(r"\\begin\{itemize\}(.*?)\\end\{itemize\}", out, re.DOTALL)
        assert len(blocks) == 6
        expected = [5, 2, 2, 1, 1, 1]  # job_0, job_1, project_0..3
        assert [b.count("\\item") for b in blocks] == expected

    def test_skills_reordered_to_requested_priority(self, template, llm_response):
        out = _inject_into_template(template, llm_response)
        block = re.search(r"\\section\{Skills\}\n\{\\small\n(.*?)\n\}", out, re.DOTALL).group(1)
        rows = [r for r in block.split("\n") if r.strip()]
        assert "Backend" in rows[0], "most JD-relevant category must lead"
        assert len(rows) == len(SKILL_CATEGORIES)

    def test_escaped_category_still_sorts(self, template, llm_response):
        """Regression for D8: 'Cloud & DevOps' is stored LaTeX-escaped as
        'Cloud \\& DevOps' and used to never match, sinking to the bottom
        regardless of the job description."""
        out = _inject_into_template(template, llm_response)
        block = re.search(r"\\section\{Skills\}\n\{\\small\n(.*?)\n\}", out, re.DOTALL).group(1)
        rows = [r for r in block.split("\n") if r.strip()]
        position = next(i for i, r in enumerate(rows) if "DevOps" in r)
        assert position == 1, f"Cloud & DevOps was asked for 2nd, landed {position + 1}th"

    def test_secondary_skill_appended_to_top_row(self, template, llm_response):
        out = _inject_into_template(template, llm_response)
        block = re.search(r"\\section\{Skills\}\n\{\\small\n(.*?)\n\}", out, re.DOTALL).group(1)
        assert "Kubernetes" in block.split("\n")[0]

    def test_row_terminators_preserved(self, template, llm_response):
        """Every row but the last needs a trailing \\\\, or the rows run together."""
        out = _inject_into_template(template, llm_response)
        block = re.search(r"\\section\{Skills\}\n\{\\small\n(.*?)\n\}", out, re.DOTALL).group(1)
        rows = [r for r in block.split("\n") if r.strip()]
        assert all(r.rstrip().endswith("\\\\") for r in rows[:-1])
        assert not rows[-1].rstrip().endswith("\\\\")


# The facts the template ships with as placeholder content. The locked list is
# derived from a *profile* now, so this is the profile that matches the template.
TEMPLATE_PROFILE = {
    "personal": {"name": "Gaurav Sonkar"},
    "experience": [
        {"title": "Software Developer", "company": "Divyal Technology Pvt. Ltd.",
         "client": "Tracsis PLC", "start_date": "Jun 2025", "end_date": "Present"},
        {"title": "Software Developer Intern", "company": "Divyal Technology Pvt. Ltd.",
         "start_date": "Feb 2025", "end_date": "May 2025"},
    ],
    "education": {"institution": "Banaras Hindu University",
                  "start_date": "Oct 2023", "end_date": "Jun 2025"},
}
TEMPLATE_LOCKED = locked_strings(TEMPLATE_PROFILE)


class TestLockedFieldsAreThisAccountsOwn:
    """The list used to be seven string literals of one person's details, so it
    protected nothing for anyone else — and named a stranger in their errors."""

    def test_it_is_built_from_the_profile(self):
        assert "Gaurav Sonkar" in TEMPLATE_LOCKED
        assert "Divyal Technology Pvt. Ltd." in TEMPLATE_LOCKED
        assert "Banaras Hindu University" in TEMPLATE_LOCKED

    def test_a_different_account_gets_its_own(self):
        other = locked_strings({
            "personal": {"name": "Asha Verma"},
            "experience": [{"title": "Backend Engineer", "company": "Northwind"}],
            "education": {"institution": "IIT Bombay"},
        })
        assert other[0] == "Asha Verma"
        assert "Northwind" in other and "IIT Bombay" in other
        assert not any("Gaurav" in v or "Divyal" in v for v in other)

    def test_present_is_not_treated_as_a_fact(self):
        assert "Present" not in TEMPLATE_LOCKED

    def test_duplicates_are_collapsed(self):
        """Both roles share an employer; guarding it twice buys nothing."""
        assert len(TEMPLATE_LOCKED) == len(set(TEMPLATE_LOCKED))

    def test_an_empty_profile_yields_nothing_to_guard(self):
        assert locked_strings({}) == []


class TestLockedFieldsSurvive:
    @pytest.mark.parametrize("locked", TEMPLATE_LOCKED)
    def test_present_after_injection(self, template, llm_response, locked):
        if locked in ("Oct 2023",):        # education dates are not in the body
            pytest.skip("not part of the injected sections")
        assert locked in _inject_into_template(template, llm_response)

    def test_validator_rejects_a_mangled_employer(self):
        with pytest.raises(ValueError, match="locked field"):
            _validate_locked_fields(
                {"summary": "Worked at Divyal Tech on APIs"}, TEMPLATE_LOCKED
            )

    def test_validator_accepts_the_full_name(self):
        _validate_locked_fields(
            {"summary": "Worked at Divyal Technology Pvt. Ltd."}, TEMPLATE_LOCKED
        )

    def test_validator_guards_the_other_account_instead(self):
        """Proof the guard follows the profile: the same sentence is harmless
        for one account and a violation for another.

        The employer is deliberately multi-word — the rule is "first word
        present without the full string", so only a truncatable value can be
        truncated.
        """
        asha = locked_strings({
            "personal": {"name": "Asha Verma"},
            "experience": [{"title": "x", "company": "Northwind Systems Ltd"}],
        })

        # Mangling *Gaurav's* employer means nothing to Asha's resume.
        _validate_locked_fields({"summary": "Worked at Divyal Tech"}, asha)

        # Mangling hers does.
        with pytest.raises(ValueError, match="locked field"):
            _validate_locked_fields({"summary": "Worked at Northwind on APIs"}, asha)

        # And the full name is accepted.
        _validate_locked_fields({"summary": "Worked at Northwind Systems Ltd"}, asha)


class TestHostileLlmOutput:
    """LLM output derives from scraped job descriptions, so it is untrusted."""

    def test_prompt_injected_file_read_is_defanged(self, template, llm_response):
        llm_response["professional_summary"] = "Engineer. \\input{/etc/passwd} Hire me."
        out = _inject_into_template(template, llm_response)
        assert "\\input{/etc/passwd}" not in out
        assert "\\textbackslash{}input" in out
        assert brace_balance(out) == 0

    def test_shell_escape_is_defanged(self, template, llm_response):
        llm_response["experience_bullets"]["job_0"] = ["Ran \\write18{rm -rf /} in prod."]
        out = _inject_into_template(template, llm_response)
        assert "\\write18{" not in out

    def test_special_characters_are_escaped(self, template, llm_response):
        llm_response["professional_summary"] = "Cut costs 50% & raised R&D spend by $2M."
        out = _inject_into_template(template, llm_response)
        assert "50\\%" in out
        assert "\\$2M" in out
        assert brace_balance(out) == 0

    def test_missing_keys_leave_the_template_intact(self, template):
        """A partial LLM response must degrade, not corrupt the document."""
        out = _inject_into_template(template, {"professional_summary": "Only a summary."})
        assert brace_balance(out) == 0
        assert out.count("\\end{document}") == 1
        for name, opens, closes in environments(out):
            assert opens == closes

    def test_empty_response_returns_usable_template(self, template):
        out = _inject_into_template(template, {})
        assert brace_balance(out) == 0
        assert "\\end{document}" in out


# NOTE: none of this proves the document *compiles*. There is no TeX engine in
# this environment, so `pdflatex` — and with it the one-page assertion in
# latex_compiler._check_page_count — remains unverified. Run one real
# generation (or `docker compose build`, which installs the TeX collections)
# before relying on the output.
