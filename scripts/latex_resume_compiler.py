#!/usr/bin/env python3
"""
LaTeX Resume Compiler
Takes LaTeX code and compiles it into a single-page PDF resume.
Supports custom templates or a built-in professional template.
"""

import os
import sys
import shutil
import subprocess
import tempfile
import argparse
from pathlib import Path


# ─────────────────────────────────────────────
# BUILT-IN RESUME TEMPLATE
# ─────────────────────────────────────────────

RESUME_TEMPLATE = r"""
\documentclass[10pt, letterpaper]{article}

% ── Packages ──────────────────────────────────
\usepackage[
    top=0.5in, bottom=0.5in,
    left=0.6in, right=0.6in
]{geometry}
\usepackage{titlesec}
\usepackage{enumitem}
\usepackage{hyperref}
\usepackage{fontawesome5}
\usepackage{xcolor}
\usepackage{multicol}
\usepackage{array}
\usepackage{tabularx}
\usepackage{lmodern}
\usepackage[T1]{fontenc}
\usepackage[utf8]{inputenc}
\usepackage{microtype}

% ── Colors ────────────────────────────────────
\definecolor{accent}{RGB}{30, 90, 160}
\definecolor{lightgray}{RGB}{100, 100, 100}
\definecolor{rulecolor}{RGB}{180, 200, 230}

% ── Hyperlink style ───────────────────────────
\hypersetup{
    colorlinks=true,
    urlcolor=accent,
    linkcolor=accent
}

% ── No page numbers ───────────────────────────
\pagestyle{empty}

% ── Section formatting ────────────────────────
\titleformat{\section}
  {\bfseries\large\color{accent}}
  {}{0em}{}
  [\vspace{-4pt}\textcolor{rulecolor}{\rule{\linewidth}{1pt}}\vspace{2pt}]

\titlespacing{\section}{0pt}{8pt}{4pt}

% ── List settings ─────────────────────────────
\setlist[itemize]{
    leftmargin=1.2em,
    itemsep=1pt,
    parsep=0pt,
    topsep=2pt,
    label=\textbullet
}

% ── Custom Commands ───────────────────────────

% \resumeHeader{Name}{Phone}{Email}{LinkedIn}{GitHub}{Location}
\newcommand{\resumeHeader}[6]{%
    \begin{center}
        {\Huge\bfseries #1}\\[4pt]
        \small
        \textcolor{lightgray}{%
            \faPhone\ #2 \quad
            \faEnvelope\ \href{mailto:#3}{#3} \quad
            \faLinkedin\ \href{https://linkedin.com/in/#4}{#4} \quad
            \faGithub\ \href{https://github.com/#5}{#5} \quad
            \faMapMarker*\ #6
        }
    \end{center}
    \vspace{2pt}
}

% \resumeEntry{Title}{Company/Institution}{Location}{Date}
\newcommand{\resumeEntry}[4]{%
    \vspace{3pt}
    \noindent
    \begin{tabularx}{\linewidth}{@{}X r@{}}
        \textbf{#1} \textit{\textcolor{lightgray}{· #2}} & \textcolor{lightgray}{\small #4}\\
        \textit{\textcolor{lightgray}{\small #3}} & \\
    \end{tabularx}
    \vspace{-4pt}
}

% \resumeSimpleEntry{Title}{Date}
\newcommand{\resumeSimpleEntry}[2]{%
    \vspace{3pt}
    \noindent
    \begin{tabularx}{\linewidth}{@{}X r@{}}
        \textbf{#1} & \textcolor{lightgray}{\small #2}\\
    \end{tabularx}
    \vspace{-4pt}
}

% \skillRow{Category}{Skills}
\newcommand{\skillRow}[2]{%
    \noindent\textbf{#1:} \textcolor{lightgray}{#2}\\[2pt]
}

% ── Document ──────────────────────────────────
\begin{document}

% ════════════════════════════════════
% REPLACE CONTENT BELOW WITH YOUR OWN
% ════════════════════════════════════

\resumeHeader
    {John A. Doe}
    {+1 (555) 000-1234}
    {john.doe@email.com}
    {johndoe}
    {johndoe}
    {San Francisco, CA}

% ── SUMMARY ───────────────────────────────────
\section{Summary}
Results-driven Software Engineer with 5+ years of experience building scalable backend systems and
REST APIs. Passionate about clean code, distributed systems, and mentoring junior engineers.

% ── EXPERIENCE ────────────────────────────────
\section{Experience}

\resumeEntry{Senior Software Engineer}{Acme Corp}{San Francisco, CA}{Jan 2022 – Present}
\begin{itemize}
    \item Led redesign of payment processing pipeline, reducing latency by 40\% and handling 10k+ TPS
    \item Architected microservices migration from monolith using Kubernetes and Docker
    \item Mentored 4 junior engineers; conducted 50+ technical interviews
\end{itemize}

\resumeEntry{Software Engineer}{Beta Startup}{Remote}{Jun 2019 – Dec 2021}
\begin{itemize}
    \item Built RESTful APIs serving 2M+ daily active users using Python (FastAPI) and PostgreSQL
    \item Reduced CI/CD pipeline runtime by 35\% by parallelizing test suites in GitHub Actions
    \item Implemented real-time notifications with WebSockets and Redis Pub/Sub
\end{itemize}

\resumeEntry{Software Engineer Intern}{Gamma Tech}{New York, NY}{May 2018 – Aug 2018}
\begin{itemize}
    \item Developed internal dashboard in React + TypeScript tracking 15 key business KPIs
    \item Wrote integration tests achieving 92\% code coverage on core modules
\end{itemize}

% ── EDUCATION ─────────────────────────────────
\section{Education}

\resumeEntry{B.S. Computer Science}{State University}{New York, NY}{Aug 2015 – May 2019}
\begin{itemize}
    \item GPA: 3.8/4.0 · Dean's List (6 semesters) · Senior Thesis: \textit{Graph-based Anomaly Detection}
    \item Relevant coursework: Algorithms, Operating Systems, Database Systems, Machine Learning
\end{itemize}

% ── PROJECTS ──────────────────────────────────
\section{Projects}

\resumeSimpleEntry{OpenMetrics – Open Source Monitoring Tool}{github.com/johndoe/openmetrics}
\begin{itemize}
    \item Built a lightweight Prometheus-compatible metrics exporter in Go with 1.2k GitHub stars
    \item Supports 10+ exporters (MySQL, Redis, Nginx) and a built-in Grafana dashboard template
\end{itemize}

\resumeSimpleEntry{ML Price Predictor}{github.com/johndoe/price-predictor}
\begin{itemize}
    \item Trained XGBoost model on 500k records achieving 94\% accuracy; deployed via Flask on AWS Lambda
\end{itemize}

% ── SKILLS ────────────────────────────────────
\section{Skills}

\skillRow{Languages}{Python, Go, TypeScript, SQL, Bash, Java}
\skillRow{Frameworks \& Tools}{FastAPI, Django, React, Node.js, Docker, Kubernetes, Terraform}
\skillRow{Databases}{PostgreSQL, MySQL, Redis, MongoDB, Elasticsearch}
\skillRow{Cloud \& DevOps}{AWS (EC2, S3, Lambda, RDS), GCP, GitHub Actions, Datadog, Grafana}

% ── CERTIFICATIONS ────────────────────────────
\section{Certifications \& Awards}

\noindent
\textbf{AWS Certified Solutions Architect} \textcolor{lightgray}{– Amazon Web Services, 2023} \quad
\textbf{CKA} \textcolor{lightgray}{– Linux Foundation, 2022} \\[2pt]
\textbf{Hackathon Winner} \textcolor{lightgray}{– HackNY 2019 (Best Infrastructure Hack)}

\end{document}
"""


# ─────────────────────────────────────────────
# COMPILER
# ─────────────────────────────────────────────

def check_latex_installed() -> str:
    """Check for an available LaTeX engine. Returns engine name or raises."""
    for engine in ("pdflatex", "xelatex", "lualatex"):
        if shutil.which(engine):
            return engine
    raise EnvironmentError(
        "No LaTeX engine found. Install TeX Live or MiKTeX:\n"
        "  Ubuntu/Debian : sudo apt-get install texlive-full\n"
        "  macOS         : brew install --cask mactex\n"
        "  Windows       : https://miktex.org/download"
    )


def compile_latex(latex_code: str, output_path: str, engine: str = "pdflatex") -> str:
    """
    Compile LaTeX source to PDF.

    Args:
        latex_code  : Raw LaTeX source as a string.
        output_path : Destination path for the generated PDF.
        engine      : LaTeX engine to use (pdflatex / xelatex / lualatex).

    Returns:
        Absolute path to the generated PDF.
    """
    output_path = Path(output_path).resolve()

    with tempfile.TemporaryDirectory() as tmpdir:
        tex_file = Path(tmpdir) / "resume.tex"
        tex_file.write_text(latex_code, encoding="utf-8")

        # Run twice so references / TOC are resolved
        for run in range(2):
            result = subprocess.run(
                [
                    engine,
                    "-interaction=nonstopmode",
                    "-halt-on-error",
                    f"-output-directory={tmpdir}",
                    str(tex_file),
                ],
                capture_output=True,
                text=True,
                cwd=tmpdir,
            )

            if result.returncode != 0:
                # Show the relevant part of the log on failure
                log_file = Path(tmpdir) / "resume.log"
                log_text = log_file.read_text(encoding="utf-8", errors="replace") if log_file.exists() else ""
                _print_latex_errors(log_text, result.stderr)
                raise RuntimeError(
                    f"LaTeX compilation failed (exit {result.returncode}). "
                    "See error messages above."
                )

        pdf_src = Path(tmpdir) / "resume.pdf"
        if not pdf_src.exists():
            raise FileNotFoundError("Compilation succeeded but no PDF was produced.")

        # Ensure output directory exists and copy PDF to destination
        output_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(pdf_src, output_path)

    print(f"✅  Resume generated → {output_path}")
    return str(output_path)


def _print_latex_errors(log: str, stderr: str) -> None:
    """Print human-readable errors extracted from the LaTeX log."""
    errors = [l for l in log.splitlines() if l.startswith("!") or "Error" in l]
    print("\n──── LaTeX Errors ────")
    if errors:
        for line in errors[:20]:
            print(" ", line)
    elif stderr:
        print(stderr[:2000])
    print("──────────────────────\n")


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def load_latex_from_file(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def get_template(template_name: str = "default") -> str:
    """Return built-in template or load from file."""
    if template_name == "default":
        return RESUME_TEMPLATE
    return load_latex_from_file(template_name)


# ─────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────

def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Compile a LaTeX resume to a single-page PDF.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Compile a custom .tex file
  python latex_resume_compiler.py --input my_resume.tex --output resume.pdf

  # Generate PDF from the built-in template
  python latex_resume_compiler.py --template default --output resume.pdf

  # Use a specific LaTeX engine
  python latex_resume_compiler.py --input my_resume.tex --engine xelatex

  # Save built-in template to disk for editing
  python latex_resume_compiler.py --save-template my_template.tex
        """,
    )
    p.add_argument("--input",         metavar="FILE",    help="Path to your .tex source file")
    p.add_argument("--template",      metavar="NAME",    help='Use a template: "default" or path to a .tex file')
    p.add_argument("--output",        metavar="FILE",    default="resume.pdf", help="Output PDF path (default: resume.pdf)")
    p.add_argument("--engine",        metavar="ENGINE",  default=None,         help="LaTeX engine: pdflatex | xelatex | lualatex")
    p.add_argument("--save-template", metavar="FILE",    help="Write the built-in template to a file and exit")
    return p


def main():
    parser = build_arg_parser()
    args = parser.parse_args()

    # ── Save template and exit ──────────────────
    if args.save_template:
        dest = Path(args.save_template)
        dest.write_text(RESUME_TEMPLATE, encoding="utf-8")
        print(f"📄  Template saved → {dest}")
        sys.exit(0)

    # ── Determine LaTeX source ──────────────────
    if args.input:
        latex_code = load_latex_from_file(args.input)
        print(f"📂  Loaded LaTeX source from: {args.input}")
    elif args.template:
        latex_code = get_template(args.template)
        print(f"📋  Using template: {args.template}")
    else:
        print("ℹ️   No --input or --template given. Using built-in template.")
        latex_code = RESUME_TEMPLATE

    # ── Detect engine ──────────────────────────
    if args.engine:
        engine = args.engine
        if not shutil.which(engine):
            print(f"❌  Engine '{engine}' not found in PATH.")
            sys.exit(1)
    else:
        try:
            engine = check_latex_installed()
        except EnvironmentError as e:
            print(f"❌  {e}")
            sys.exit(1)

    print(f"⚙️   Engine : {engine}")
    print(f"📄  Output : {args.output}")

    # ── Compile ────────────────────────────────
    try:
        compile_latex(latex_code, args.output, engine)
    except (RuntimeError, FileNotFoundError) as e:
        print(f"\n❌  {e}")
        sys.exit(1)


# ─────────────────────────────────────────────
# PROGRAMMATIC API EXAMPLE
# ─────────────────────────────────────────────

def compile_resume_from_string(latex_code: str, output_pdf: str = "resume.pdf") -> str:
    """
    Convenience function for use in other Python scripts.

    Usage:
        from latex_resume_compiler import compile_resume_from_string
        compile_resume_from_string(my_latex_string, "output/resume.pdf")
    """
    engine = check_latex_installed()
    return compile_latex(latex_code, output_pdf, engine)


if __name__ == "__main__":
    main()
