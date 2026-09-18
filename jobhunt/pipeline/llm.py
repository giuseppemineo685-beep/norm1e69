"""All Claude API calls: scoring a posting against the profile and tailoring documents."""
from __future__ import annotations

import os
from typing import Optional

import anthropic
from pydantic import BaseModel, Field

from .models import Job

MODEL = os.environ.get("JOBHUNT_MODEL", "claude-opus-5")


class Fit(BaseModel):
    score: int = Field(ge=0, le=100, description="0-100 fit of the candidate for this posting")
    matching: list[str] = Field(description="3-6 concrete skills or experiences the candidate has that the posting asks for")
    missing: list[str] = Field(description="0-4 requirements the candidate lacks or only partly covers")
    summary: str = Field(description="One or two sentences on why to apply or not, in the candidate's language")
    seniority_ok: bool = Field(description="False when the level is clearly too junior or too senior")
    language_ok: bool = Field(description="False when the posting requires a language the candidate does not have")


class Tailored(BaseModel):
    cv_markdown: str = Field(description="The complete CV in markdown, same structure as the template")
    cover_letter_markdown: str = Field(description="The complete cover letter in markdown")
    changes: list[str] = Field(description="Short list of what was changed versus the templates")


def _client() -> anthropic.Anthropic:
    return anthropic.Anthropic()


SCORE_SYSTEM = """You screen job postings for one candidate. You get the candidate profile and one posting.
Judge the fit honestly: a high score means the candidate would be a credible shortlist for this exact role.
Weigh hard requirements (years, domain, languages, work permit, location) more than nice-to-haves.
Answer in the language the profile is written in."""


def score_job(job: Job, profile: str, client: Optional[anthropic.Anthropic] = None) -> Fit:
    client = client or _client()
    posting = f"Company: {job.company}\nTitle: {job.title}\nLocation: {job.location}\nURL: {job.url}\n\n{job.jd or '(no description available, judge from the title)'}"
    resp = client.messages.parse(
        model=MODEL,
        max_tokens=4000,
        output_config={"effort": "medium"},
        system=[
            {"type": "text", "text": SCORE_SYSTEM},
            # The profile is identical across the run, so it is the cache breakpoint.
            {"type": "text", "text": f"<candidate_profile>\n{profile}\n</candidate_profile>",
             "cache_control": {"type": "ephemeral"}},
        ],
        messages=[{"role": "user", "content": f"<posting>\n{posting[:12000]}\n</posting>"}],
        output_format=Fit,
    )
    if resp.stop_reason == "refusal" or resp.parsed_output is None:
        raise RuntimeError(f"scoring stopped: {resp.stop_reason}")
    return resp.parsed_output


TAILOR_SYSTEM = """You adapt a candidate's CV and cover letter to one specific job posting.

Rules:
- Never invent experience, employers, dates, degrees or numbers. Only reorder, reword, select and emphasise what the profile and templates already contain.
- Keep the CV template's structure and section order. Rewrite the summary and reorder or rephrase skills and bullet points so the most relevant ones come first and use the posting's vocabulary where it is truthful.
- The cover letter must be specific to this company and role: name the company, the role, and two or three concrete reasons the candidate fits, taken from the profile. No generic filler.
- Keep the candidate's language and tone. Follow the writing rules exactly.
- Return complete documents, ready to send, not diffs."""


def tailor_documents(job: Job, profile: str, cv_template: str, cover_template: str,
                     writing_rules: str, client: Optional[anthropic.Anthropic] = None) -> Tailored:
    client = client or _client()
    user = (
        f"<posting>\nCompany: {job.company}\nTitle: {job.title}\nLocation: {job.location}\n\n{job.jd[:15000]}\n</posting>\n\n"
        f"<cv_template>\n{cv_template}\n</cv_template>\n\n"
        f"<cover_letter_template>\n{cover_template}\n</cover_letter_template>"
    )
    with client.messages.stream(
        model=MODEL,
        max_tokens=16000,
        system=[
            {"type": "text", "text": TAILOR_SYSTEM},
            {"type": "text", "text": f"<writing_rules>\n{writing_rules}\n</writing_rules>\n\n<candidate_profile>\n{profile}\n</candidate_profile>",
             "cache_control": {"type": "ephemeral"}},
        ],
        messages=[{"role": "user", "content": user}],
        output_format=Tailored,
    ) as stream:
        resp = stream.get_final_message()
    if resp.stop_reason == "refusal" or resp.parsed_output is None:
        raise RuntimeError(f"tailoring stopped: {resp.stop_reason}")
    return resp.parsed_output
