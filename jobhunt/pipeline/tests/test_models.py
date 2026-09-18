from pipeline.models import canonical_url, make_job_id, Job


def test_canonical_url_strips_tracking_and_www():
    a = canonical_url("https://www.linkedin.com/jobs/view/123/?trackingId=x&refId=y")
    b = canonical_url("http://linkedin.com/jobs/view/123")
    assert a == b == "https://linkedin.com/jobs/view/123"


def test_id_prefers_external_id():
    assert make_job_id("lever", "abc", "https://x/1") == make_job_id("lever", "abc", "https://x/2")
    assert make_job_id("lever", None, "https://x/1") != make_job_id("lever", None, "https://x/2")


def test_job_roundtrip():
    j = Job(source="html", company=" Acme ", title="  Strategy   Lead ", url="https://a/b ")
    rec = j.to_record()
    back = Job.from_record(rec)
    assert back.company == "Acme" and back.title == "Strategy Lead" and back.id == j.id
    assert back.status == "new"
