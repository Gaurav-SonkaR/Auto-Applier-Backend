from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from pypdf import PdfReader


class LaTeXCompileError(Exception):
    """Raised when the resume cannot be turned into a PDF."""

    def __init__(self, message: str, log: str = "") -> None:
        super().__init__(message)
        self.log = log


# What the user has to install, said once. Resume generation is the core of the
# app and it cannot work without a TeX distribution, so the failure has to name
# the fix — the bare OSError says only "[WinError 2] The system cannot find the
# file specified", which tells nobody anything.
PDFLATEX_MISSING = (
    "pdflatex was not found on PATH, so no resume can be built. Install a TeX "
    "distribution and reopen the terminal: MiKTeX (https://miktex.org/download) "
    "on Windows, TeX Live (`apt install texlive-latex-recommended`) on Linux, "
    "MacTeX on macOS."
)


def pdflatex_available() -> bool:
    """Whether a resume can be compiled at all, without compiling one."""
    return shutil.which("pdflatex") is not None


def compile_resume_from_file(tex_path: str, output_path: str) -> str:
    """Compile a .tex file to PDF. Returns the absolute path to the written PDF."""
    source = Path(tex_path)
    if not source.exists():
        raise FileNotFoundError(f"LaTeX source not found: {tex_path}")
    latex = source.read_text(encoding="utf-8")
    return compile_resume_from_string(latex, output_path)


def compile_resume_from_string(latex: str, output_path: str) -> str:
    """Compile a LaTeX string to PDF. Returns the absolute path to the written PDF."""
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        tex_file = tmpdir / "resume.tex"
        tex_file.write_text(latex, encoding="utf-8")

        if not pdflatex_available():
            # Checked before spawning, so the message is the same whether the
            # binary is absent or the PATH lookup fails mid-run.
            raise LaTeXCompileError(PDFLATEX_MISSING)

        try:
            result = subprocess.run(
                [
                    "pdflatex",
                "-interaction=nonstopmode",
                "-halt-on-error",
                    "-no-shell-escape",  # belt-and-suspenders: block \write18 even
                                         # if the local TeX distro allows it
                    "resume.tex",
                ],
                cwd=tmpdir,
                capture_output=True,
                text=True,
            )
        except OSError as exc:
            # Raced with an uninstall, or PATH points at something unrunnable.
            raise LaTeXCompileError(PDFLATEX_MISSING) from exc

        log_path = tmpdir / "resume.log"
        log_content = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""

        if result.returncode != 0:
            error_excerpt = _extract_error(log_content)
            raise LaTeXCompileError(
                f"pdflatex failed (exit {result.returncode}): {error_excerpt}",
                log=log_content,
            )

        pdf_tmp = tmpdir / "resume.pdf"
        if not pdf_tmp.exists():
            raise LaTeXCompileError("pdflatex exited 0 but no PDF was produced", log=log_content)

        _check_page_count(pdf_tmp)
        shutil.copy2(pdf_tmp, out)

    return str(out.resolve())


# A resume longer than this is a formatting failure, not a long career.
#
# This was 1, which made generation impossible rather than strict: the template
# carries two roles, four projects and eight skill rows, and injection rewrites
# the text inside those blocks without ever removing one. Measured on the real
# template, only a *single* project fits on one page — so enforcing one page
# meant dropping three of four projects from every resume, and until then it
# simply failed all three attempts every time.
MAX_RESUME_PAGES = 2


def _check_page_count(pdf_path: Path) -> None:
    reader = PdfReader(str(pdf_path))
    pages = len(reader.pages)
    if pages > MAX_RESUME_PAGES:
        raise LaTeXCompileError(
            f"Compiled resume is {pages} pages — must be at most "
            f"{MAX_RESUME_PAGES}. Reduce content or tighten spacing."
        )


def _extract_error(log: str) -> str:
    lines = log.splitlines()
    error_lines = [ln for ln in lines if ln.startswith("!") or "Error" in ln]
    return " | ".join(error_lines[:5]) if error_lines else "(see log)"
