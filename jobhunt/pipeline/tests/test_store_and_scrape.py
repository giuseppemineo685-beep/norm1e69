from pipeline.models import Job
from pipeline.scrape import dedupe
from pipeline.store import LocalStore


def test_local_store_roundtrip(tmp_path):
    s = LocalStore(tmp_path / "jobs.json")
    j = Job(source="t", company="c", title="T", url="https://x/1")
    s.upsert([j])
    assert s.get_ids() == {j.id}
    s.update(j.id, status="accepted", score=77)
    assert s.list_by_status("accepted")[0].score == 77
    s.log_run("scrape", "t", 1, 1)
    s2 = LocalStore(tmp_path / "jobs.json")
    assert s2.get(j.id).status == "accepted"


def test_dedupe_keeps_the_copy_with_a_jd():
    a = Job(source="t", company="c", title="T", url="https://x/1")
    b = Job(source="t", company="c", title="T", url="https://x/1?utm_source=mail", jd="desc")
    out = dedupe([a, b])
    assert len(out) == 1 and out[0].jd == "desc"
