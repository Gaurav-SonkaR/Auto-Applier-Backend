from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from pypdf import PdfReader


class LaTeXCompileError(Exception):
    """Raised when pdflatex returns a non-zero exit code."""

    def __init__(self, message: str, log: str = "") -> None:
        super().__init__(message)
        self.log = log


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

        result = subprocess.run(
            [
                "pdflatex",
                "-interaction=nonstopmode",
                "-halt-on-error",
                "-no-shell-escape",  # belt-and-suspenders: block \write18 even if the
                                     # local TeX distro's default config allows it
                "resume.tex",
            ],
            cwd=tmpdir,
            capture_output=True,
            text=True,
        )

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


def _check_page_count(pdf_path: Path) -> None:
    reader = PdfReader(str(pdf_path))
    pages = len(reader.pages)
    if pages > 1:
        raise LaTeXCompileError(
            f"Compiled resume is {pages} pages — must be exactly 1 page. "
            "Reduce content or tighten spacing."
        )


def _extract_error(log: str) -> str:
    lines = log.splitlines()
    error_lines = [ln for ln in lines if ln.startswith("!") or "Error" in ln]
    return " | ".join(error_lines[:5]) if error_lines else "(see log)"
