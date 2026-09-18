import json
from pathlib import Path

from pipeline.sources import greenhouse, gmail_alerts, generic
from pipeline.jd import html_to_text

FIX = Path(__file__).parent / "fixtures"


def test_greenhouse_parse():
    jobs = greenhouse.parse(json.loads((FIX / "greenhouse.json").read_text()), "Acme")
    assert len(jobs) == 2
    lead = jobs[0]
    assert lead.title == "Strategy & Operations Lead"
    assert "- 5 years" in lead.jd and "<" not in lead.jd
    assert lead.external_id == "123"
    assert lead.location == "Zurich, Switzerland"
    assert lead.posted_at == "2026-09-10"


def test_linkedin_alert_parse():
    jobs = gmail_alerts.parse_alert_html((FIX / "linkedin_alert.html").read_text())
    by_title = {j.title: j for j in jobs}
    assert set(by_title) == {"Strategy Manager", "Portfolio Manager", "Business Analyst Operations"}
    sm = by_title["Strategy Manager"]
    assert sm.url == "https://www.linkedin.com/jobs/view/4012345678/"
    assert sm.company == "Zurich Insurance"
    assert sm.location == "Zurich, Switzerland"
    assert sm.source == "gmail:linkedin"
    pm = by_title["Portfolio Manager"]
    assert pm.external_id == "4098765432" and pm.company == "UBS"
    ba = by_title["Business Analyst Operations"]
    assert ba.source == "gmail:jobs.ch" and ba.company == "Swisscom" and ba.location == "Zurich"


def test_alert_dedupes_by_external_id():
    html = (FIX / "linkedin_alert.html").read_text()
    jobs = gmail_alerts.parse_alert_html(html + html)
    assert len(jobs) == 3


def test_generic_html():
    html = """<ul>
      <li><a href="/careers/job/42">Strategy Lead</a><span class="loc">Zurich</span></li>
      <li><a href="/careers/job/42">Strategy Lead</a></li>
      <li><a href="/about">About us</a></li>
      <li><a href="/careers/job/43"><img src="x"></a></li>
    </ul>"""
    jobs = generic.parse(html, {"name": "Acme", "url": "https://acme.com/careers",
                                "link_pattern": "/job/", "location_selector": ".loc"})
    assert [(j.title, j.url, j.location) for j in jobs] == [("Strategy Lead", "https://acme.com/careers/job/42", "Zurich")]


def test_html_to_text_keeps_bullets():
    t = html_to_text("<main><h1>Role</h1><script>x()</script><ul><li>One</li><li>Two</li></ul></main>")
    assert "Role" in t and "- One" in t and "x()" not in t
