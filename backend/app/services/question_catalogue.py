"""The questions job portals ask on application forms.

Collected from what Wellfound, Naukri, LinkedIn Easy Apply, Indeed, Cutshort and
Instahyre put in front of an applicant. The user answers each once on the
Profile page; the applier types that answer whenever a form asks the same
question — in any of the listed wordings.

Deliberately NOT in here:

* The seven basics the profile already holds as structured fields (total
  experience, notice period, relocation, preferred locations, current and
  expected CTC, visa sponsorship). Those need typed handling for dropdown
  brackets; duplicating them here would give two answers to one question.
* Anything about gender, ethnicity, disability, veteran status, religion,
  marital status or age. Those are declined, never answered — see
  `form_choices.is_sensitive`.

`mode` decides how an answer is used:

* ``fixed``  — typed as written (after ``{company}`` / ``{role}`` placeholders
  are filled in).
* ``tailor`` — the user's answer is the substance; a model adapts the wording to
  the specific company and job description without adding claims to it.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class CatalogueQuestion:
    id: str
    category: str
    question: str
    mode: str = "fixed"
    hint: str = ""
    aliases: tuple[str, ...] = field(default_factory=tuple)


CATEGORIES = (
    "About you",
    "Motivation",
    "Experience",
    "Work setup",
    "Availability",
    "Compensation",
    "Education",
    "Eligibility & documents",
    "Other",
)


CATALOGUE: tuple[CatalogueQuestion, ...] = (
    # ── About you ───────────────────────────────────────────────────────────
    CatalogueQuestion(
        "about_yourself", "About you", "Tell us about yourself.",
        mode="tailor",
        hint="A short professional summary. It is adapted to each role, never extended.",
        aliases=("Introduce yourself", "Briefly describe yourself",
                 "Give a brief introduction about yourself", "Summary",
                 "Professional summary", "About me", "Describe yourself in a few lines"),
    ),
    CatalogueQuestion(
        "current_role_summary", "About you", "Describe your current role and responsibilities.",
        aliases=("What do you do in your current role?", "Current responsibilities",
                 "Describe your current job responsibilities",
                 "What are your key responsibilities in your current role?"),
    ),
    CatalogueQuestion(
        "strengths", "About you", "What are your key strengths?",
        aliases=("What are your strengths?", "Top strengths", "Key skills and strengths"),
    ),
    CatalogueQuestion(
        "proud_project", "About you", "Describe a project you are proud of.",
        aliases=("Tell us about a project you are most proud of",
                 "Describe your most challenging project",
                 "Share a project you have worked on",
                 "What is the most impactful project you have worked on?"),
    ),
    CatalogueQuestion(
        "portfolio_url", "About you", "Portfolio or personal website URL",
        aliases=("Portfolio", "Portfolio URL", "Personal website", "Website",
                 "Link to your portfolio", "Personal website or blog"),
    ),
    CatalogueQuestion(
        "languages_known", "About you", "Which languages can you speak?",
        aliases=("Languages known", "Languages spoken", "What languages do you speak?",
                 "Language proficiency"),
    ),

    # ── Motivation (adapted per company) ────────────────────────────────────
    CatalogueQuestion(
        "why_company", "Motivation", "What interests you most about this company?",
        mode="tailor",
        hint="What genuinely draws you to companies like this. It is adapted using each "
             "job description — don't name a company here, use {company}.",
        aliases=("Why do you want to work at this company?", "Why do you want to join us?",
                 "Why do you want to work here?", "Why are you interested in our company?",
                 "What excites you about this company?", "Why this company?",
                 "Why do you want to join {company}?", "What interests you about working here?",
                 "What attracts you to our company?"),
    ),
    CatalogueQuestion(
        "why_role", "Motivation", "Why are you interested in this role?",
        mode="tailor",
        aliases=("Why do you want this job?", "What interests you about this position?",
                 "Why are you applying for this role?", "Why this role?",
                 "What excites you about this opportunity?",
                 "Why are you a good fit for this role?"),
    ),
    CatalogueQuestion(
        "why_hire", "Motivation", "Why should we hire you?",
        mode="tailor",
        aliases=("What makes you a good fit?", "Why are you the right candidate?",
                 "What value will you bring to the team?", "Why should we consider you?"),
    ),
    CatalogueQuestion(
        "cover_letter", "Motivation", "Cover letter",
        mode="tailor",
        hint="Your standard cover note. It is adapted to each company and role.",
        aliases=("Cover note", "Add a cover letter", "Message to the hiring manager",
                 "Note to the hiring manager", "Write a note to the recruiter",
                 "Anything else you would like to share with the hiring team?",
                 "Additional information", "Add a note"),
    ),
    CatalogueQuestion(
        "reason_for_change", "Motivation", "Why are you looking for a change?",
        aliases=("Reason for job change", "Reason for leaving your current job",
                 "Why do you want to leave your current company?", "Reason for leaving",
                 "Why are you looking for a new opportunity?"),
    ),
    CatalogueQuestion(
        "next_role_goals", "Motivation", "What are you looking for in your next role?",
        aliases=("What are your career goals?", "Where do you see yourself in 5 years?",
                 "What kind of role are you looking for?", "Career aspirations"),
    ),

    # ── Experience ──────────────────────────────────────────────────────────
    CatalogueQuestion(
        "relevant_experience", "Experience", "Describe your experience relevant to this role.",
        mode="tailor",
        aliases=("Relevant experience", "What relevant experience do you have?",
                 "Describe your relevant work experience",
                 "Tell us about your experience with the technologies in this role"),
    ),
    CatalogueQuestion(
        "team_size_led", "Experience", "Have you led or managed a team? If so, how large?",
        aliases=("Team handling experience", "Have you managed a team?",
                 "Team size managed", "Do you have team leading experience?"),
    ),
    CatalogueQuestion(
        "startup_experience", "Experience", "Have you worked at a startup before?",
        aliases=("Do you have startup experience?", "Startup experience",
                 "Are you comfortable working in a startup environment?"),
    ),
    CatalogueQuestion(
        "client_facing", "Experience", "Do you have client-facing experience?",
        aliases=("Have you worked directly with clients?", "Client interaction experience"),
    ),

    # ── Work setup ──────────────────────────────────────────────────────────
    CatalogueQuestion(
        "work_mode", "Work setup", "Are you comfortable working from the office?",
        aliases=("Are you okay with work from office?", "Are you comfortable with WFO?",
                 "Can you work from our office?", "Are you open to working onsite?",
                 "Are you comfortable with a hybrid work model?",
                 "Preferred work mode", "Work from office, hybrid or remote?"),
    ),
    CatalogueQuestion(
        "shifts", "Work setup", "Are you comfortable working in rotational or night shifts?",
        aliases=("Are you okay with night shifts?", "Are you comfortable with rotational shifts?",
                 "Can you work in US shifts?", "Are you open to shift work?"),
    ),
    CatalogueQuestion(
        "travel", "Work setup", "Are you willing to travel for work?",
        aliases=("Are you open to travel?", "Willing to travel", "Can you travel if required?"),
    ),
    CatalogueQuestion(
        "remote_setup", "Work setup", "Do you have a laptop and a reliable internet connection?",
        aliases=("Do you have a personal laptop?", "Do you have a stable internet connection?",
                 "Do you have a home office setup?"),
    ),
    CatalogueQuestion(
        "contract_roles", "Work setup", "Are you open to contract or freelance roles?",
        aliases=("Are you open to a contract role?", "Are you okay with a contractual position?",
                 "Open to freelance work?", "Are you open to C2H roles?"),
    ),

    # ── Availability ────────────────────────────────────────────────────────
    CatalogueQuestion(
        "serving_notice", "Availability", "Are you currently serving your notice period?",
        aliases=("Are you serving notice period?", "Currently serving notice?",
                 "Have you resigned from your current job?"),
    ),
    CatalogueQuestion(
        "last_working_day", "Availability", "What is your last working day?",
        aliases=("Last working day", "LWD", "Last working date",
                 "When is your last day at your current company?"),
    ),
    CatalogueQuestion(
        "notice_buyout", "Availability", "Can your notice period be bought out or shortened?",
        aliases=("Is your notice period negotiable?", "Notice period buyout",
                 "Can you join earlier than your notice period?"),
    ),
    CatalogueQuestion(
        "offers_in_hand", "Availability", "Do you have any other offers in hand?",
        aliases=("Offers in hand", "Are you holding any offers?", "Do you have any offer?",
                 "Any other offers?"),
    ),

    # ── Compensation ────────────────────────────────────────────────────────
    CatalogueQuestion(
        "ctc_negotiable", "Compensation", "Is your expected salary negotiable?",
        aliases=("Is your expected CTC negotiable?", "Are you open to negotiation?",
                 "Salary negotiable"),
    ),
    CatalogueQuestion(
        "ctc_breakup", "Compensation", "What is the fixed and variable split of your current CTC?",
        aliases=("Current CTC breakup", "Fixed and variable component",
                 "Break-up of current salary"),
    ),

    # ── Education ───────────────────────────────────────────────────────────
    CatalogueQuestion(
        "highest_qualification", "Education", "What is your highest qualification?",
        aliases=("Highest education", "Highest degree", "Highest level of education",
                 "Education qualification", "Highest educational qualification"),
    ),
    CatalogueQuestion(
        "graduation_year", "Education", "What is your year of graduation?",
        aliases=("Year of passing", "Graduation year", "Passing year", "Batch",
                 "When did you graduate?"),
    ),
    CatalogueQuestion(
        "academic_score", "Education", "What was your CGPA or percentage?",
        aliases=("CGPA", "Percentage", "Graduation percentage", "GPA",
                 "Academic score", "Aggregate percentage"),
    ),
    CatalogueQuestion(
        "has_bachelors", "Education", "Have you completed a Bachelor's degree?",
        aliases=("Do you have a bachelor's degree?",
                 "Have you completed the following level of education: Bachelor's Degree?"),
    ),
    CatalogueQuestion(
        "education_gap", "Education", "Do you have any gap in your education or career?",
        aliases=("Any career gap?", "Do you have a career gap?", "Explain any gaps"),
    ),

    # ── Eligibility & documents ─────────────────────────────────────────────
    CatalogueQuestion(
        "authorized_india", "Eligibility & documents",
        "Are you legally authorized to work in India?",
        aliases=("Are you eligible to work in India?", "Do you have the right to work in India?",
                 "Work authorization for India"),
    ),
    CatalogueQuestion(
        "passport", "Eligibility & documents", "Do you have a valid passport?",
        aliases=("Valid passport", "Do you hold a valid passport?"),
    ),
    CatalogueQuestion(
        "background_check", "Eligibility & documents",
        "Are you comfortable with a background verification?",
        aliases=("Are you okay with background check?", "Background verification consent",
                 "Do you consent to a background check?"),
    ),
    CatalogueQuestion(
        "service_bond", "Eligibility & documents",
        "Are you comfortable signing a service agreement or bond?",
        aliases=("Are you okay with a bond?", "Service agreement", "Are you okay with a service bond?"),
    ),
    CatalogueQuestion(
        "driving_license", "Eligibility & documents", "Do you have a valid driving licence?",
        aliases=("Driving license", "Do you have a driving license?", "Do you own a vehicle?"),
    ),

    # ── Other ───────────────────────────────────────────────────────────────
    CatalogueQuestion(
        "heard_about_job", "Other", "How did you hear about this job?",
        aliases=("How did you hear about us?", "Source of application",
                 "Where did you find this job?", "How did you find this opportunity?",
                 "Referral source"),
    ),
    CatalogueQuestion(
        "referred_by", "Other", "Were you referred by an employee? If so, who?",
        aliases=("Referred by", "Employee referral", "Referrer name",
                 "Do you know anyone working at {company}?"),
    ),
    CatalogueQuestion(
        "previously_applied", "Other", "Have you applied to or worked at this company before?",
        aliases=("Have you previously applied here?", "Have you worked with us before?",
                 "Previously employed with {company}?"),
    ),
    CatalogueQuestion(
        "interview_availability", "Other", "When are you available for an interview?",
        aliases=("Interview availability", "Preferred interview time",
                 "When can you interview?"),
    ),
)
