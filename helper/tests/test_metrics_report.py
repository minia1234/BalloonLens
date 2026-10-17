import json

from balloonlens import metrics_report as mr


def row(**kw):
    base = {"ts": 1, "status": "done", "model": "claude-haiku-5-5", "segments": 12, "queue_ms": 5, "gate_ms": 1, "decode_ms": 30,
            "memory_ms": 4, "ocr_lock_wait_ms": 0, "detect_ms": 200, "recognize_ms": 600, "ocr_retries": 0, "first_read_ms": 500,
            "first_step_ms": 510, "first_result_ms": 3600, "ocr_done_ms": 900, "translate_ms": 6000, "steps": 2, "total_ms": 7000}
    base.update(kw)
    return base


def write(tmp_path, rows, extra=()):
    path = tmp_path / "metrics.jsonl"
    path.write_text("\n".join([json.dumps(r) for r in rows] + list(extra)) + "\n", encoding="utf-8")
    return path


def test_rows_that_are_not_finished_jobs_or_not_json_are_skipped(tmp_path):
    path = write(tmp_path, [row(), row(status="failed"), row(status="cancelled"), row(status="partial", model="qwen3.5:9b")],
                 extra=["not json", "[1, 2]", ""])
    assert len(mr.load_rows(path)) == 2
    assert [r["model"] for r in mr.load_rows(path, model="qwen3.5:9b")] == ["qwen3.5:9b"]
    assert mr.load_rows(tmp_path / "missing.jsonl") == []


def test_last_keeps_the_newest_jobs(tmp_path):
    path = write(tmp_path, [row(ts=i, total_ms=i) for i in range(10)])
    assert [r["ts"] for r in mr.load_rows(path, last=3)] == [7, 8, 9]
    assert len(mr.load_rows(path, last=0)) == 10


def test_median_p90_and_max_per_stage_use_only_the_rows_that_have_the_stage():
    rows = [row(detect_ms=v) for v in (100, 200, 300, 400, 1000)] + [{"status": "done", "total_ms": 1}]
    s = mr.summarize(rows)
    assert s["detect_ms"] == {"n": 5, "median": 300, "p90": 1000, "max": 1000}
    assert "visible_read_ms" not in s and s["total_ms"]["n"] == 6


def test_the_slowest_reading_stage_gets_its_own_advice():
    for stage, word in (("recognize_ms", "Manga OCR"), ("detect_ms", "CRAFT"), ("ocr_lock_wait_ms", "OCR 잠금")):
        rows = [row(**{"detect_ms": 100, "recognize_ms": 100, "ocr_lock_wait_ms": 0, stage: 5000})]
        notes = mr.diagnose(rows, mr.summarize(rows))
        assert word in notes[0] and stage in notes[0]


def test_a_long_ocr_done_with_small_detect_and_recognize_points_at_the_stages_around_them():
    rows = [row(detect_ms=100, recognize_ms=100, ocr_done_ms=6000)]
    assert any("다른 구간" in n for n in mr.diagnose(rows, mr.summarize(rows)))
    rows = [row(detect_ms=300, recognize_ms=600, ocr_done_ms=1000)]
    assert not any("다른 구간" in n for n in mr.diagnose(rows, mr.summarize(rows)))


def test_translation_dominating_is_reported_with_the_first_line_time():
    rows = [row()]
    notes = " ".join(mr.diagnose(rows, mr.summarize(rows)))
    assert "Claude 호출 시간이 병목" in notes and "첫 번역문까지 3600 ms" in notes and "2번" in notes


def test_re_reads_that_rarely_help_are_pointed_out():
    rows = [row(ocr_retries=10, ocr_retry_spans=5, ocr_retry_fixed=1, ocr_retry_ms=200) for _ in range(3)]
    note = next(n for n in mr.diagnose(rows, mr.summarize(rows)) if "의심 판독" in n)
    assert "15개를 다시 읽었고" in note and "3개(20%)" in note and "줄일 여지" in note
    rows = [row(ocr_retries=5, ocr_retry_spans=5, ocr_retry_fixed=5)]
    assert "효과가 있어요" in next(n for n in mr.diagnose(rows, mr.summarize(rows)) if "의심 판독" in n)


def test_pages_taken_from_the_cache_and_resumed_pages_are_counted():
    rows = [row(ocr_cache=True), row(ocr_resumed=7), row()]
    notes = " ".join(mr.diagnose(rows, mr.summarize(rows)))
    assert "3개 중 1개는 OCR 캐시" in notes and "7개" in notes


def test_the_report_and_the_command(tmp_path, capsys):
    path = write(tmp_path, [row(), row(recognize_ms=800)])
    assert mr.main(["--file", str(path)]) == 0
    out = capsys.readouterr().out
    assert "recognize_ms" in out and "first_result_ms" in out and "진단" in out
    assert mr.main(["--file", str(path), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["jobs"] == 2 and data["stages"]["recognize_ms"]["median"] == 700
    assert mr.main(["--file", str(tmp_path / "none.jsonl")]) == 1
    assert mr.diagnose([], {}) == ["기록된 작업이 없어요."]
