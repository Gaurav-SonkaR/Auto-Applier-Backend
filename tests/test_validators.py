"""SEC-002 input validation and SEC-003 LaTeX sanitisation."""
import pytest

from backend.app.utils.validators import (
    MAX_SEARCH_TERM_LEN,
    ValidationError,
    escape_latex,
    neutralize_latex_commands,
    validate_search_term,
    validate_url,
)


class TestValidateUrl:
    @pytest.mark.parametrize("url", [
        "https://example.com/jobs/1",
        "http://careers.acme.co.uk/apply?id=7",
    ])
    def test_accepts_http_and_https(self, url):
        assert validate_url(url) == url

    def test_strips_surrounding_whitespace(self):
        assert validate_url("  https://example.com  ") == "https://example.com"

    @pytest.mark.parametrize("url", [
        "file:///etc/passwd",
        "javascript:alert(1)",
        "data:text/html,<script>",
        "ftp://example.com/x",
    ])
    def test_rejects_non_http_schemes(self, url):
        """These reach Playwright's page.goto — file:// would read local files
        straight into the scraped job description."""
        with pytest.raises(ValidationError):
            validate_url(url)

    @pytest.mark.parametrize("url", ["", "   ", "not-a-url", "https://"])
    def test_rejects_malformed(self, url):
        with pytest.raises(ValidationError):
            validate_url(url)

    def test_rejects_overlong(self):
        with pytest.raises(ValidationError):
            validate_url("https://example.com/" + "a" * 3000)


class TestValidateSearchTerm:
    def test_trims(self):
        assert validate_search_term("  Python Developer  ") == "Python Developer"

    def test_rejects_empty_when_required(self):
        with pytest.raises(ValidationError):
            validate_search_term("   ", field="job_title")

    def test_allows_empty_when_optional(self):
        assert validate_search_term("", required=False) == ""

    def test_rejects_overlong(self):
        with pytest.raises(ValidationError):
            validate_search_term("x" * (MAX_SEARCH_TERM_LEN + 1))


class TestLatexSanitisation:
    @pytest.mark.parametrize("cmd", [
        r"\input", r"\include", r"\write18", r"\immediate",
        r"\openout", r"\catcode", r"\csname", r"\directlua",
        r"\usepackage", r"\def", r"\expandafter",
    ])
    def test_dangerous_commands_are_defanged(self, cmd):
        out = neutralize_latex_commands(f"text {cmd}{{/etc/passwd}} more")
        assert cmd not in out
        assert r"\textbackslash{}" in out

    def test_defanging_is_case_insensitive(self):
        assert r"\INPUT" not in neutralize_latex_commands(r"\INPUT{x}")

    def test_benign_formatting_survives(self):
        """\\textbf must NOT be stripped — the injector relies on it rendering."""
        assert escape_latex(r"\textbf{Python}") == r"\textbf{Python}"

    @pytest.mark.parametrize("raw,expected", [
        ("R&D", r"R\&D"),
        ("50% faster", r"50\% faster"),
        ("C#", r"C\#"),
        ("$100k", r"\$100k"),
        ("snake_case", r"snake\_case"),
        ("2^10", r"2\^{}10"),
        ("~approx", r"\textasciitilde{}approx"),
    ])
    def test_breaking_characters_are_escaped(self, raw, expected):
        assert escape_latex(raw) == expected

    def test_neutralises_before_escaping(self):
        """Order matters: if escaping ran first the backslash would survive."""
        out = escape_latex(r"\input{secret}")
        assert r"\input" not in out
        assert out.startswith(r"\textbackslash{}input")

    def test_prompt_injected_jd_text_cannot_read_local_files(self):
        malicious = r"Great role! \input{/etc/passwd} Apply now."
        out = escape_latex(malicious)
        assert r"\input{" not in out
        assert "Apply now." in out
