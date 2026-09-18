from pipeline.config import Search
from pipeline.models import Job
from pipeline.prefilter import passes

S = Search(include=["strategy", "portfolio"], exclude=["intern"], locations=["zurich"], remote_ok=True)


def j(title, loc="Zurich", jd=""):
    return Job(source="t", company="c", title=title, url="https://x/" + title, location=loc, jd=jd)


def test_include_and_location():
    assert passes(j("Strategy Manager"), S)[0]
    assert not passes(j("Strategy Manager", loc="Berlin"), S)[0]
    assert passes(j("Strategy Manager", loc="Remote, Europe"), S)[0]


def test_exclude_wins():
    ok, why = passes(j("Strategy Intern"), S)
    assert not ok and "intern" in why


def test_include_can_match_jd():
    assert not passes(j("Manager"), S)[0]
    assert passes(j("Manager", jd="You own the product portfolio"), S)[0]


def test_unknown_location_is_allowed():
    assert passes(j("Strategy Manager", loc=""), S)[0]
