#!/usr/bin/env python3
"""Drive the interviewer service the way the main app will: buy a pack, book, answer, finish, refund, erase.

Standard library only, so it runs anywhere. Use it against a local container or the staging Fly app:

    python scripts/scripts/staging_smoke.py --base-url http://localhost:8080 --api-key <key>
    python scripts/scripts/staging_smoke.py --base-url http://interviewer-ms.flycast:8080 --api-key <key> --audio
    python scripts/scripts/staging_smoke.py --base-url ... --api-key ... --students 10     # small load test

The key can also come from INTERVIEWER_API_KEY. Every student is a throwaway `smoke-...` id and is erased at the end
(use --keep to leave the data for inspection). Exit code 0 means every check passed.

It uses real AI providers when the service has keys, so a run costs a few cents per student. Without keys the service
degrades to its deterministic scoring and the run still passes; check the "ai" line to see which one you tested.
"""
import argparse
import http.client
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone

ANSWERS = [
    "Situation: our checkout API was timing out under load. My task was to bring p95 under 300 ms. I profiled the "
    "queries, added a Redis cache and moved slow work to a queue. As a result p95 dropped from 1.2 s to 240 ms.",
    "Situation: a data pipeline kept failing at night. I owned the fix. I added retries with backoff, alerts and a "
    "dashboard. The result was zero missed runs in the next quarter and on-call pages down by 70 percent.",
    "Situation: two teams disagreed on an API contract. My task was to unblock the release. I wrote a one page "
    "proposal, ran a short review and we shipped two days early. The result was that both teams adopted the template.",
]
RESUME = "Senior backend engineer. Python, FastAPI, PostgreSQL, Redis. Reduced API latency by 80 percent."
JD = "Backend engineer. Python, Kafka, Kubernetes, 5+ years experience, strong communication."


class Failure(Exception):
    pass


class Client:
    def __init__(self, base_url: str, api_key: str, timeout: float) -> None:
        self.base = base_url.rstrip("/") + "/api/v1"
        self.key = api_key
        self.timeout = timeout

    def call(self, method: str, path: str, body=None, form=None, expect=(200,)):
        headers = {"X-API-Key": self.key}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        elif form is not None:
            boundary = uuid.uuid4().hex
            data = _multipart(form, boundary)
            headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        request = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                status, raw, response_headers = response.status, response.read(), response.headers
        except urllib.error.HTTPError as error:
            status, raw, response_headers = error.code, error.read(), error.headers
        except (urllib.error.URLError, http.client.HTTPException, OSError) as error:
            raise Failure(f"{method} {path} could not reach the service: {error}") from None
        if status not in expect:
            if status == 502 and b"all_providers_failed" in raw:
                raise Failure(
                    f"{method} {path} -> 502: no AI provider answered. Are the provider API keys set on the service? "
                    f"({raw[:200]!r})"
                )
            raise Failure(f"{method} {path} -> {status}, expected {expect}: {raw[:300]!r}")
        kind = response_headers.get("Content-Type", "")
        return status, (json.loads(raw) if raw and "json" in kind else raw)


def _multipart(fields: dict, boundary: str) -> bytes:
    parts = []
    for name, value in fields.items():
        if isinstance(value, tuple):  # (filename, bytes, content_type)
            filename, content, content_type = value
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                f"Content-Type: {content_type}\r\n\r\n".encode() + content + b"\r\n"
            )
        else:
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
            )
    return b"".join(parts) + f"--{boundary}--\r\n".encode()


def run_student(client: Client, index: int, args, log) -> dict:
    student = f"smoke-{uuid.uuid4().hex[:10]}"
    payment = f"pay-{uuid.uuid4().hex[:10]}"
    result = {"student": student, "ok": False}
    plans_on = client.call("GET", "/plans", expect=(200, 404))[0] == 200
    try:
        if plans_on:
            now = datetime.now(timezone.utc).isoformat()
            _, pack = client.call(
                "POST", "/packs/activate",
                body={"external_ref": student, "plan": args.plan, "payment_id": payment, "purchased_at": now},
            )
            log(index, f"pack activated: {pack}")
            _, again = client.call(
                "POST", "/packs/activate",
                body={"external_ref": student, "plan": args.plan, "payment_id": payment, "purchased_at": now},
            )
            check(again.get("applied") is False, "replaying the same payment must not add minutes twice")

        started = time.monotonic()
        _, created = client.call(
            "POST", "/interviews",
            body={
                "role": "Backend Engineer", "candidate_name": "Smoke Test", "resume_text": RESUME, "jd_text": JD,
                "external_ref": student, "plan": args.plan if plans_on else None,
                "consent_to_ai_processing": True,
                "config": {"question_count": 3, "duration_minutes": 15} if plans_on else {"question_count": 3},
            },
            expect=(201,),
        )
        interview_id = created["interview"]["id"]
        questions = created["questions"]
        result["create_s"] = round(time.monotonic() - started, 1)
        log(index, f"created {interview_id} with {len(questions)} questions in {result['create_s']} s")

        for position, question in enumerate(questions):
            text = ANSWERS[position % len(ANSWERS)]
            if args.audio and position == 0:
                _, wav = client.call("POST", "/speech/synthesize", form={"text": text})
                check(wav[:4] == b"RIFF", "speech synthesis did not return a WAV file")
                _, answer = client.call(
                    "POST", f"/interviews/{interview_id}/answers/audio",
                    form={"question_index": question["index"], "audio": ("answer.wav", wav, "audio/wav")},
                )
                spoken = answer["transcript"] if "transcript" in answer else ""
                log(index, f"audio answer transcribed ({len(spoken)} characters)")
                check(len(spoken) > 40, "audio answer came back with an empty or tiny transcript")
            else:
                client.call(
                    "POST", f"/interviews/{interview_id}/answers",
                    body={"question_index": question["index"], "transcript": text, "duration_seconds": 40},
                )

        started = time.monotonic()
        status, first = client.call("POST", f"/interviews/{interview_id}/finish", expect=(200, 202))
        result["finish_call_s"] = round(time.monotonic() - started, 1)
        if status == 202:
            check(first["status"] == "processing" and first["report"] is None, "202 must carry status processing")
            _, duplicate = client.call("POST", f"/interviews/{interview_id}/finish", expect=(202, 200))
            deadline = time.monotonic() + args.report_timeout
            report = None
            while time.monotonic() < deadline:
                _, body = client.call("GET", f"/interviews/{interview_id}/report")
                if body["status"] == "failed":
                    raise Failure(f"report build failed: {body.get('error')}")
                if body["report"]:
                    report = body["report"]
                    break
                time.sleep(2.5)
            check(report is not None, f"report not ready after {args.report_timeout} s")
            result["report_ready_s"] = round(time.monotonic() - started, 1)
            check(result["finish_call_s"] < 5, f"async finish took {result['finish_call_s']} s, expected an instant 202")
        else:
            report = first["report"]
            result["report_ready_s"] = result["finish_call_s"]
            log(index, "finish answered 200 (synchronous mode: FINISH_ASYNC is off on this service)")
        check(report.get("overall_score") is not None, "report has no overall_score")
        routing = report.get("routing_trace") or {}
        result["ai"] = "providers" if routing else "deterministic fallback (no AI provider answered)"
        log(index, f"report ready: score {report['overall_score']} in {result['report_ready_s']} s; ai={result['ai']}")

        status, saved = client.call("POST", f"/interviews/{interview_id}/finish", expect=(200,))
        check(saved["report"]["overall_score"] == report["overall_score"], "finish after completion must return the saved report")

        if plans_on:
            _, balance = client.call("GET", f"/packs/{student}")
            log(index, f"balance after one interview: {balance['packs']}")
            _, refund = client.call("POST", "/packs/revoke",
                                    body={"external_ref": student, "plan": args.plan, "payment_id": payment},
                                    expect=(200, 409))
            log(index, f"refund attempt: {refund}")
        result["ok"] = True
    finally:
        if not args.keep:
            try:
                client.call("DELETE", f"/interviews/by-ref/{student}", expect=(200,))
            except Failure as error:
                log(index, f"cleanup failed: {error}")
                result["ok"] = False
    return result


def check(condition: bool, message: str) -> None:
    if not condition:
        raise Failure(message)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default="http://localhost:8080")
    parser.add_argument("--api-key", default=os.environ.get("INTERVIEWER_API_KEY", ""))
    parser.add_argument("--plan", default="economy")
    parser.add_argument("--students", type=int, default=1, help="run this many students at once (load test)")
    parser.add_argument("--audio", action="store_true", help="answer the first question by voice (checks Piper and Whisper)")
    parser.add_argument("--keep", action="store_true", help="do not erase the test students afterwards")
    parser.add_argument("--timeout", type=float, default=60, help="per request, seconds")
    parser.add_argument("--report-timeout", type=float, default=180, help="how long to wait for a report, seconds")
    args = parser.parse_args()
    if not args.api_key:
        print("Pass --api-key or set INTERVIEWER_API_KEY", file=sys.stderr)
        return 2

    client = Client(args.base_url, args.api_key, args.timeout)
    lock = threading.Lock()

    def log(index: int, message: str) -> None:
        with lock:
            print(f"[student {index}] {message}", flush=True)

    try:
        client.call("GET", "/health")
        client.call("GET", "/ready")
        _, details = client.call("GET", "/ready/details")
    except Failure as error:
        print(f"service is not reachable or not ready: {error}", file=sys.stderr)
        return 1
    print(f"service: database={details['database']} redis={details['redis']} voice={details['voice']}")
    print(f"providers configured: {details['llm_providers']}")
    if args.audio:
        check_voice = details["voice"]["tts_binary_available"]
        if not check_voice:
            print("FAIL: --audio requested but Piper is not available on this service", file=sys.stderr)
            return 1

    results: list[dict] = []

    def worker(index: int) -> None:
        try:
            results.append(run_student(client, index, args, log))
        except Exception as error:  # noqa: BLE001
            log(index, f"FAILED: {error}")
            results.append({"ok": False, "error": str(error)})

    threads = [threading.Thread(target=worker, args=(i + 1,)) for i in range(max(1, args.students))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    passed = sum(1 for item in results if item["ok"])
    print(f"\n{passed}/{len(results)} students passed")
    times = sorted(item["report_ready_s"] for item in results if "report_ready_s" in item)
    if times:
        print(f"time to report: min {times[0]} s, median {times[len(times) // 2]} s, max {times[-1]} s")
    finish_calls = sorted(item["finish_call_s"] for item in results if "finish_call_s" in item)
    if finish_calls:
        print(f"finish call itself: median {finish_calls[len(finish_calls) // 2]} s, max {finish_calls[-1]} s")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
