"""The LaTeX template and the resume generator are coupled by regex.

`resume_generator._inject_into_template` finds its three injection points by
matching the template's exact byte shape. Editing either side alone silently
degrades output — injection just no-ops and the generic seed resume ships to
the employer. These tests fail loudly instead.

Imports only the constants from resume_generator, not the module's runtime
dependencies, so they run without groq/playwright installed.
"""
import re
from pathlib import Path

import pytest

TEMPLATE = Path(__file__).resolve().parents[1] / "templates" / "resume_template.tex"

# Kept in sync with resume_generator.SKILL_CATEGORIES / LOCKED_STRINGS.
# Duplicated deliberately: importing resume_generator would pull in groq,
# and a test that asserts "these two lists agree" is worth more than one
# that imports the value it is checking.
SKILL_CATEGORIES = [
    "Languages", "Backend", "AI / GenAI", "Frontend",
    "Databases", "Cloud & DevOps", "Automation", "ML / Data",
]
LOCKED_STRINGS = [
    "Gaurav Sonkar", "Divyal Technology Pvt. Ltd.", "Tracsis PLC",
    "Banaras Hindu University", "Jun 2025", "Feb 2025", "May 2025",
]

SUMMARY_RE = re.compile(r"(\\section\{Professional Summary\}\n\{\\small\n)(.*?)(\n\})", re.DOTALL)
SKILLS_RE = re.compile(r"(\\section\{Skills\}\n\{\\small\n)(.*?)(\n\})", re.DOTALL)
ITEMIZE_RE = re.compile(r"(\\begin\{itemize\})(.*?)(\\end\{itemize\})", re.DOTALL)


@pytest.fixture(scope="module")
def tex() -> str:
    assert TEMPLATE.exists(), f"template missing at {TEMPLATE}"
    return TEMPLATE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def code(tex) -> str:
    """Template with LaTeX comments stripped."""
    return "\n".join(l for l in tex.split("\n") if not l.lstrip().startswith("%"))


def test_uses_lf_line_endings():
    """Every injection regex is anchored on \\n. CRLF silently breaks all three."""
    assert b"\r\n" not in TEMPLATE.read_bytes()


class TestSummaryInjectionPoint:
    def test_matches(self, tex):
        assert SUMMARY_RE.search(tex) is not None

    def test_has_seed_text(self, tex):
        assert SUMMARY_RE.search(tex).group(2).strip()

    def test_replacement_round_trips(self, tex):
        out = SUMMARY_RE.sub(lambda m: m.group(1) + "REPLACED" + m.group(3), tex)
        assert "REPLACED" in out
        assert out != tex


class TestSkillsInjectionPoint:
    def test_matches(self, tex):
        assert SKILLS_RE.search(tex) is not None

    def test_has_one_row_per_category(self, tex):
        rows = [l for l in SKILLS_RE.search(tex).group(2).split("\n") if l.strip()]
        assert len(rows) == len(SKILL_CATEGORIES)

    def test_row_terminators(self, tex):
        """All rows but the last end in \\\\; the last must not, or LaTeX emits
        a stray blank line."""
        rows = [l for l in SKILLS_RE.search(tex).group(2).split("\n") if l.strip()]
        assert all(r.rstrip().endswith("\\\\") for r in rows[:-1])
        assert not rows[-1].rstrip().endswith("\\\\")

    @pytest.mark.parametrize("category", SKILL_CATEGORIES)
    def test_category_present(self, tex, category):
        block = SKILLS_RE.search(tex).group(2)
        assert "\\textbf{" + category.replace("&", "\\&") + ":}" in block

    @pytest.mark.parametrize("category", SKILL_CATEGORIES)
    def test_category_matches_after_backslash_normalisation(self, tex, category):
        """Mirrors resume_generator.row_priority's comparison. 'Cloud & DevOps'
        is stored escaped as 'Cloud \\& DevOps', so a naive substring test fails
        and that row would sort to the bottom regardless of JD relevance."""
        block = SKILLS_RE.search(tex).group(2)
        norm = lambda s: s.replace("\\", "").lower()
        assert norm(category) in norm(block)


class TestItemizeBlocks:
    def test_exactly_six_blocks(self, tex):
        """Blocks are addressed positionally: 0-1 experience, 2-5 projects.
        An extra itemize anywhere above shifts every mapping."""
        assert len(ITEMIZE_RE.findall(tex)) == 6

    def test_every_block_has_items(self, tex):
        for i, m in enumerate(ITEMIZE_RE.finditer(tex)):
            assert m.group(2).count("\\item") > 0, f"block {i} is empty"

    def test_education_and_achievements_use_no_itemize(self, tex):
        """They sit after the six mapped blocks; using itemize there would add
        blocks 6+ which the injector ignores — harmless today, but it makes the
        contract fragile the moment a section is reordered."""
        tail = tex[tex.index("\\section{Education}"):]
        assert "\\begin{itemize}" not in tail


class TestLockedFields:
    @pytest.mark.parametrize("locked", LOCKED_STRINGS)
    def test_present_verbatim(self, tex, locked):
        assert locked in tex


class TestAtsRules:
    """FR-008: ATS-friendly formatting — no tables, standard fonts."""

    @pytest.mark.parametrize("banned", ["tabular", "tabularx", "multicol", "longtable"])
    def test_no_tables(self, code, banned):
        assert banned not in code

    def test_no_custom_glyph_fonts(self, code):
        assert "fontawesome" not in code

    def test_single_column(self, code):
        assert "twocolumn" not in code

    def test_suppresses_page_numbers(self, code):
        """Named for what it checks. It used to be called
        `test_compiles_to_one_page_by_construction`, which asserted nothing of
        the kind — and was false besides: the template compiles to two pages,
        which is why generation failed every attempt until MAX_RESUME_PAGES
        was raised to 2."""
        assert "\\pagestyle{empty}" in code
