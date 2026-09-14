"""Black-box contract tests. Run: python3 -m unittest discover -s tests -v

Only the standard library is used. Each test gets a fresh root and invokes the
real CLI in separate processes. Missing CLI implementation is a failure, not a
skip. No Codex tasks, background services, or external agents are created.
"""

import copy
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest


CLI = Path(__file__).resolve().parents[1] / "scripts" / "navigator.py"


class NavigatorContractTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(CLI.is_file(), f"CLI implementation is missing: {CLI}")
        temporary = tempfile.TemporaryDirectory(prefix="navigator-contract-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.serial = 0
        initialized = self.invoke("init", root=self.root)
        self.assertEqual(Path(initialized["db"]), self.root / ".navigator/state.sqlite")
        self.assertEqual(Path(initialized["access"]), self.root / ".navigator/main.json")
        self.assertTrue(Path(initialized["db"]).is_file())
        self.main = Path(initialized["access"])
        self.assertTrue(self.main.is_file())
        self.blueprint = {"goal": "Deliver a locally verifiable result",
                          "acceptance": ["Artifacts satisfy the independent review"]}
        created = self.invoke("create", {"blueprint": self.blueprint})
        self.task = created["task"]
        self.revision = created["revision"]

    def raw(self, op, data=None, access=None, root=None, data_file=None):
        command = [sys.executable, str(CLI), op]
        payload = None
        if op == "init":
            command += ["--root", str(root)]
        else:
            command += ["--access", str(access or self.main),
                        "--data", str(data_file) if data_file else "-"]
            if data_file is None:
                payload = json.dumps(data if data is not None else {})
        result = subprocess.run(command, input=payload, text=True,
                                capture_output=True, cwd=self.root, timeout=30)
        try:
            output = json.loads(result.stdout)
        except (ValueError, TypeError) as exc:
            self.fail(f"{op}: expected one JSON object, exit={result.returncode}, "
                      f"stdout={result.stdout!r}, stderr={result.stderr!r}: {exc}")
        self.assertIsInstance(output, dict)
        return result.returncode, output

    def invoke(self, op, data=None, *, access=None, root=None, data_file=None,
               success=True):
        code, output = self.raw(op, data, access, root, data_file)
        if success:
            self.assertEqual(code, 0, (op, output))
            self.assertIs(output.get("ok"), True, (op, output))
        else:
            self.assertNotEqual(code, 0, (op, output))
            self.assertIs(output.get("ok"), False, (op, output))
            self.assertIn("error", output)  # Error wording is deliberately free.
        return output

    def main_op(self, op, *, success=True, **data):
        return self.invoke(op, {"task": self.task, **data}, success=success)

    @staticmethod
    def loop(loop_id, deps=(), phase=1, kind="work"):
        return {"id": loop_id, "title": f"Loop {loop_id}", "phase": phase,
                "goal": f"Produce {loop_id}", "acceptance": [f"Review {loop_id}"],
                "deps": list(deps), "scope": [f"outputs/{loop_id}"], "kind": kind,
                "inputs": {"input_marker": f"input-{loop_id}"}}

    def plan(self, loops, success=True, revision=None):
        response = self.main_op("plan", revision=(self.revision if revision is None
                                                 else revision),
                                loops=loops, success=success)
        if success:
            self.revision = response["revision"]
        return response

    def graph(self):
        self.specs = [self.loop("build"), self.loop("check", ["build"], 2, "check"),
                      self.loop("accept", ["check", "other"], 3, "acceptance"),
                      self.loop("other", phase=2)]
        self.plan(self.specs)

    def ready(self):
        loops = self.main_op("ready")["loops"]
        self.assertIsInstance(loops, list)
        # The contract fixes the outer field, but not the ready-item encoding.
        return [entry if isinstance(entry, str) else entry["id"] for entry in loops]

    def file(self, text, suffix=".txt"):
        self.serial += 1
        path = self.root / f"file-{self.serial}{suffix}"
        path.write_text(text, encoding="utf-8")
        return path

    def packet_file(self, packet, role, run):
        for key in ("protocol", "db", "role", "token", "task", "loop", "run"):
            self.assertIn(key, packet)
        self.assertEqual(packet["protocol"], "navigator/v1")
        self.assertEqual(packet["role"], role)
        self.assertEqual(packet["task"], self.task)
        self.assertEqual(packet["run"], run)
        self.assertTrue(Path(packet["db"]).is_absolute())
        self.assertTrue(packet["token"])
        return self.file(json.dumps(packet), ".json")

    def claim(self, loop_id, request_id=None, bound=True):
        self.serial += 1
        request_id = request_id or f"request-{self.serial}"
        claimed = self.main_op("claim", loop=loop_id, request_id=request_id)
        self.assertIs(claimed["new"], True)
        self.assertIsInstance(claimed["prompt"], str)
        self.assertEqual(claimed["packet"]["loop"], loop_id)
        access = self.packet_file(claimed["packet"], "worker", claimed["run"])
        binding = {"run": claimed["run"], "worker_id": f"worker-{claimed['run']}",
                   "host_id": "test-host", "workdir": str(self.root)}
        if bound:
            self.main_op("bind", **binding)
        return {**claimed, "access": access, "binding": binding,
                "request_id": request_id}

    def submit(self, attempt):
        artifact = self.file(f"artifact for {attempt['run']}")
        payload = {"summary": f"PRIVATE_IMPLEMENTER_SUMMARY_{attempt['run']}",
                   "artifacts": [str(artifact)], "conditions": {"environment": "local"}}
        self.invoke("submit", payload, access=attempt["access"])
        attempt.update(artifact=artifact, submission=payload)
        return attempt

    def review(self, attempt):
        response = self.invoke("review-packet", access=attempt["access"])
        self.assertIsInstance(response["prompt"], str)
        packet = response["packet"]
        self.assertNotEqual(packet["token"], attempt["packet"]["token"])
        attempt["verifier"] = self.packet_file(packet, "verifier", attempt["run"])
        attempt["review_response"] = response
        return attempt

    def verify(self, attempt, passed=True, success=True, **overrides):
        payload = {"passed": passed, "report": str(self.file("Independent review")),
                   "reviewer_id": f"reviewer-{attempt['run']}"}
        payload.update(overrides)
        return self.invoke("verify", payload, access=attempt["verifier"], success=success)

    def complete(self, loop_id):
        attempt = self.review(self.submit(self.claim(loop_id)))
        self.verify(attempt)
        return attempt

    def reconcile(self, attempt, stopped=True):
        return self.main_op("reconcile", run=attempt["run"], stopped=stopped,
                            note="Observed host execution status")

    def assert_retry_reserved(self, attempt):
        self.main_op("claim", loop=attempt["packet"]["loop"],
                     request_id="premature-retry", success=False)

    def test_full_main_worker_independent_verifier_workflow(self):
        self.plan([self.loop("build"), self.loop("check", ["build"], 2, "check"),
                   self.loop("accept", ["check"], 3, "acceptance")])
        self.assertEqual(self.ready(), ["build"])
        self.main_op("finish", success=False)
        build = self.submit(self.claim("build"))
        self.assertEqual(self.ready(), [])  # Submission is not verification.
        self.review(build)
        self.verify(build)
        self.assertEqual(self.ready(), ["check"])
        self.complete("check")
        self.assertEqual(self.ready(), ["accept"])
        self.complete("accept")
        self.main_op("finish")
        status = self.main_op("status")
        for field in ("task", "blueprint", "revision", "state", "loops", "runs"):
            self.assertIn(field, status)
        self.assertEqual(status["task"], self.task)
        self.assertEqual(status["blueprint"], self.blueprint)
        self.assertEqual(self.ready(), [])

    def test_finish_requires_acceptance_kind_even_when_work_passed(self):
        self.plan([self.loop("work")])
        self.complete("work")
        self.main_op("finish", success=False)

    def test_ready_order_and_dependency_gate(self):
        self.plan([self.loop("z", phase=2), self.loop("b"), self.loop("a"),
                   self.loop("dependent", ["a"], 1)])
        self.assertEqual(self.ready(), ["a", "b", "z"])
        self.main_op("claim", loop="dependent", request_id="too-early", success=False)
        self.complete("a")
        self.assertEqual(self.ready(), ["b", "dependent", "z"])

    def test_concurrent_distinct_requests_reserve_one_attempt(self):
        self.plan([self.loop("work")])
        barrier = threading.Barrier(8)

        def compete(index):
            barrier.wait(timeout=15)
            return self.raw("claim", {"task": self.task, "loop": "work",
                                      "request_id": f"concurrent-{index}"})

        with ThreadPoolExecutor(max_workers=8) as pool:
            responses = list(pool.map(compete, range(8)))
        winners = [(code, body) for code, body in responses if code == 0]
        self.assertEqual(len(winners), 1, responses)
        self.assertIs(winners[0][1]["ok"], True)
        self.assertIs(winners[0][1]["new"], True)
        for code, body in responses:
            if code:
                self.assertIs(body["ok"], False)
                self.assertIn("error", body)

    def test_concurrent_same_request_is_idempotent_and_cannot_retarget(self):
        self.plan([self.loop("a"), self.loop("b")])
        barrier = threading.Barrier(6)

        def compete(_):
            barrier.wait(timeout=15)
            return self.invoke("claim", {"task": self.task, "loop": "a",
                                         "request_id": "one-dispatch"})

        with ThreadPoolExecutor(max_workers=6) as pool:
            responses = list(pool.map(compete, range(6)))
        self.assertEqual(len({body["run"] for body in responses}), 1)
        self.assertEqual(sum(body["new"] is True for body in responses), 1)
        for body in responses:
            self.assertEqual(body["packet"], responses[0]["packet"])
        self.main_op("claim", loop="b", request_id="one-dispatch", success=False)
        self.assertEqual(self.ready(), ["b"])

    def test_unbound_dispatch_recovery_requires_reconciliation(self):
        self.plan([self.loop("work")])
        attempt = self.claim("work", bound=False)
        recovered = self.main_op("claim", loop="work", request_id=attempt["request_id"])
        self.assertEqual(recovered["run"], attempt["run"])
        self.assertIs(recovered["new"], False)
        self.assertEqual(recovered["packet"], attempt["packet"])
        status = self.main_op("status")
        self.assertIn(str(attempt["run"]), json.dumps(status["runs"]))
        self.invoke("submit", {"summary": "unbound", "artifacts": [], "conditions": {}},
                    access=attempt["access"], success=False)
        self.assert_retry_reserved(attempt)
        self.reconcile(attempt, stopped=False)
        self.assert_retry_reserved(attempt)
        self.reconcile(attempt)
        replacement = self.claim("work")
        self.assertNotEqual(replacement["run"], attempt["run"])
        self.invoke("submit", {"summary": "late", "artifacts": [], "conditions": {}},
                    access=attempt["access"], success=False)

    def test_binding_is_idempotent_but_cannot_be_replaced(self):
        self.plan([self.loop("work")])
        attempt = self.claim("work")
        self.main_op("bind", **attempt["binding"])
        for field, value in (("worker_id", "other-worker"), ("host_id", "other-host"),
                             ("workdir", str(self.root / "different"))):
            with self.subTest(field=field):
                self.main_op("bind", **{**attempt["binding"], field: value}, success=False)
        self.submit(attempt)

    def test_submission_idempotence_and_immutable_result(self):
        self.plan([self.loop("work")])
        attempt = self.submit(self.claim("work"))
        self.invoke("submit", attempt["submission"], access=attempt["access"])
        for field, value in (("summary", "changed"), ("conditions", {"environment": "changed"}),
                             ("artifacts", [str(self.file("new artifact"))])):
            with self.subTest(field=field):
                self.invoke("submit", {**attempt["submission"], field: value},
                            access=attempt["access"], success=False)
        self.review(attempt)
        self.verify(attempt)

    def test_verification_packet_excludes_implementer_summary(self):
        self.plan([self.loop("work")])
        attempt = self.review(self.submit(self.claim("work")))
        context = self.invoke("context", access=attempt["verifier"])
        for value in (attempt["review_response"], context):
            encoded = json.dumps(value)
            self.assertNotIn(attempt["submission"]["summary"], encoded)
            self.assertIn(str(attempt["artifact"]), encoded)
            self.assertIn("local", encoded)
            self.assertIn("Review work", encoded)
        self.verify(attempt)

    def test_worker_context_is_scoped_to_own_loop_and_dependencies(self):
        self.graph()
        upstream = self.complete("build")
        unrelated = self.complete("other")
        attempt = self.claim("check")
        context = self.invoke("context", access=attempt["access"])
        self.assertEqual(context["blueprint"], self.blueprint)
        encoded = json.dumps(context)
        self.assertIn("Review check", encoded)
        self.assertIn(str(upstream["artifact"]), encoded)
        self.assertNotIn(str(unrelated["artifact"]), encoded)
        self.assertNotIn("input-other", encoded)
        self.assertNotIn("input-accept", encoded)

    def test_role_guards_and_no_recursive_verification(self):
        self.plan([self.loop("work")])
        attempt = self.review(self.submit(self.claim("work")))
        main_requests = [("claim", {"loop": "work", "request_id": "wrong-role"}),
                         ("bind", attempt["binding"]),
                         ("change", {"loop": "work", "reason": "wrong role"}),
                         ("reconcile", {"run": attempt["run"], "stopped": True, "note": "x"}),
                         ("pause", {"note": "x"}), ("resume", {"note": "x"}),
                         ("finish", {}), ("history", {}), ("status", {}), ("ready", {}),
                         ("plan", {"revision": self.revision, "loops": []}),
                         ("blueprint", {"revision": self.revision,
                                        "blueprint": self.blueprint, "reason": "x"}),
                         ("create", {"blueprint": self.blueprint})]
        for access in (attempt["access"], attempt["verifier"]):
            for op, data in main_requests:
                with self.subTest(role=str(access), operation=op):
                    self.invoke(op, {"task": self.task, **data}, access=access, success=False)
        verification = {"passed": True, "report": str(self.file("review")),
                        "reviewer_id": "independent"}
        for access in (self.main, attempt["access"]):
            self.invoke("verify", verification, access=access, success=False)
        for access in (self.main, attempt["verifier"]):
            self.invoke("submit", attempt["submission"], access=access, success=False)
            self.invoke("block", {"reason": "wrong role"}, access=access, success=False)
            self.invoke("review-packet", access=access, success=False)
        self.verify(attempt)

    def test_forged_packet_cannot_change_capability_scope(self):
        self.plan([self.loop("a"), self.loop("b")])
        attempt = self.claim("a")
        for field, value in (("role", "main"), ("loop", "b"), ("token", "forged-token"),
                             ("run", "nonexistent-run"), ("task", "nonexistent-task")):
            with self.subTest(field=field):
                forged = self.file(json.dumps({**attempt["packet"], field: value}), ".json")
                self.invoke("context", access=forged, success=False)

    def test_reviewer_must_differ_from_worker_and_report_must_exist(self):
        self.plan([self.loop("work")])
        attempt = self.review(self.submit(self.claim("work")))
        self.verify(attempt, reviewer_id=attempt["binding"]["worker_id"], success=False)
        self.verify(attempt, report=str(self.root / "missing-report"), success=False)
        self.verify(attempt)

    def test_missing_artifact_and_remote_url_rejected(self):
        self.plan([self.loop("work")])
        attempt = self.claim("work")
        for artifact in (str(self.root / "missing"), "relative.txt",
                         "https://example.invalid/artifact"):
            with self.subTest(artifact=artifact):
                self.invoke("submit", {"summary": "invalid", "artifacts": [artifact],
                                       "conditions": {}}, access=attempt["access"], success=False)
        self.submit(attempt)

    def test_artifact_drift_rejects_old_verification(self):
        self.plan([self.loop("work")])
        attempt = self.review(self.submit(self.claim("work")))
        attempt["artifact"].write_text("changed after submission", encoding="utf-8")
        self.verify(attempt, success=False)
        self.assert_retry_reserved(attempt)
        self.reconcile(attempt)
        fresh = self.complete("work")
        self.assertNotEqual(fresh["run"], attempt["run"])
        self.verify(attempt, success=False)

    def test_deleted_artifact_rejects_verification(self):
        self.plan([self.loop("work")])
        attempt = self.review(self.submit(self.claim("work")))
        attempt["artifact"].unlink()
        self.verify(attempt, success=False)

    def test_failed_review_keeps_reservation_until_reconciled(self):
        self.plan([self.loop("work"), self.loop("accept", ["work"], 2, "acceptance")])
        failed = self.review(self.submit(self.claim("work")))
        self.verify(failed, passed=False)
        self.assertNotIn("accept", self.ready())
        self.assert_retry_reserved(failed)
        self.reconcile(failed, stopped=False)
        self.assert_retry_reserved(failed)
        self.reconcile(failed)
        fresh = self.complete("work")
        self.assertNotEqual(fresh["run"], failed["run"])
        self.verify(failed, success=False)
        self.assertEqual(self.ready(), ["accept"])

    def test_worker_block_requires_reconciliation_before_retry(self):
        self.plan([self.loop("work")])
        attempt = self.claim("work")
        self.invoke("block", {"reason": "Missing input"}, access=attempt["access"])
        self.assert_retry_reserved(attempt)
        self.reconcile(attempt)
        self.assertNotEqual(self.claim("work")["run"], attempt["run"])

    def test_change_invalidates_passed_transitive_results_only(self):
        self.graph()
        for loop_id in ("build", "check", "other", "accept"):
            self.complete(loop_id)
        self.main_op("change", loop="build", reason="Input requirement changed")
        self.assertEqual(self.ready(), ["build"])
        self.main_op("finish", success=False)
        self.complete("build")
        self.assertEqual(self.ready(), ["check"])
        self.complete("check")
        self.assertEqual(self.ready(), ["accept"])
        self.complete("accept")
        self.main_op("finish")  # Unrelated 'other' result was preserved.

    def test_change_makes_running_consumer_stale_and_keeps_reservation(self):
        self.graph()
        self.complete("build")
        consumer = self.review(self.submit(self.claim("check")))
        unrelated = self.review(self.submit(self.claim("other")))
        self.main_op("change", loop="build", reason="Dependency changed")
        self.verify(consumer, success=False)
        self.invoke("submit", consumer["submission"], access=consumer["access"], success=False)
        self.verify(unrelated)
        self.complete("build")
        self.assert_retry_reserved(consumer)
        self.reconcile(consumer)
        fresh = self.complete("check")
        self.assertNotEqual(fresh["run"], consumer["run"])
        self.assertEqual(self.ready(), ["accept"])

    def test_file_drift_invalidates_transitive_running_consumer(self):
        self.graph()
        source = self.complete("build")
        consumer = self.review(self.submit(self.claim("check")))
        other = self.review(self.submit(self.claim("other")))
        source["artifact"].write_text("external source edit", encoding="utf-8")
        self.verify(consumer, success=False)
        self.verify(other)
        self.main_op("finish", success=False)
        self.assert_retry_reserved(consumer)
        self.reconcile(consumer)
        self.assertEqual(self.ready(), ["build"])
        self.complete("build")
        self.assertEqual(self.ready(), ["check"])

    def test_dependency_file_drift_rejects_running_worker_submission(self):
        self.plan([self.loop("build"), self.loop("consumer", ["build"], 2)])
        source = self.complete("build")
        consumer = self.claim("consumer")
        source["artifact"].write_text("external edit", encoding="utf-8")
        self.invoke("submit", {"summary": "outdated result",
                               "artifacts": [str(self.file("consumer output"))],
                               "conditions": {}}, access=consumer["access"], success=False)

    def test_pause_resume_requires_fresh_observations_for_every_active_run(self):
        self.plan([self.loop("a"), self.loop("b"), self.loop("c")])
        a, b = self.claim("a"), self.claim("b", bound=False)
        self.reconcile(a, stopped=False)  # Before-pause evidence is insufficient.
        self.main_op("pause", note="Pause dispatch")
        self.assertEqual(self.ready(), [])
        self.main_op("claim", loop="c", request_id="paused-claim", success=False)
        self.main_op("resume", note="No observations yet", success=False)
        self.reconcile(a, stopped=False)
        self.main_op("resume", note="Only one observation", success=False)
        self.reconcile(b)
        self.main_op("resume", note="All active hosts observed")
        self.assertEqual(self.ready(), ["b", "c"])
        self.assert_retry_reserved(a)
        self.main_op("pause", note="Second pause")
        self.main_op("resume", note="Old observations cannot be reused", success=False)
        self.reconcile(a)
        self.main_op("resume", note="Observed after second pause")
        self.assertEqual(self.ready(), ["a", "b", "c"])

    def test_pause_without_active_attempts_can_resume(self):
        self.plan([self.loop("a")])
        self.main_op("pause", note="Idle pause")
        self.assertEqual(self.ready(), [])
        self.main_op("resume", note="Continue")
        self.assertEqual(self.ready(), ["a"])

    def test_invalid_plans_are_atomic_and_revision_guarded(self):
        original = [self.loop("a"), self.loop("b", ["a"], 2)]
        self.plan(original)
        before = self.main_op("status")
        invalid = [[self.loop("a", ["b"]), self.loop("b", ["a"])],
                   [self.loop("a", ["missing"])], [self.loop("a", ["a"])],
                   [self.loop("a"), self.loop("a")],
                   [{**self.loop("a"), "acceptance": []}],
                   [{**self.loop("a"), "phase": 0}],
                   [{**self.loop("a"), "kind": "unsupported"}]]
        for loops in invalid:
            with self.subTest(loops=loops):
                self.plan(loops, success=False)
                after = self.main_op("status")
                self.assertEqual(after["revision"], before["revision"])
                self.assertEqual(after["loops"], before["loops"])
                self.assertEqual(self.ready(), ["a"])
        stale_revision = self.revision
        self.plan(original + [self.loop("c")])
        self.plan(original, revision=stale_revision, success=False)
        self.assertEqual(self.ready(), ["a", "c"])

    def test_plan_replacement_rejects_active_attempts(self):
        specs = [self.loop("a")]
        self.plan(specs)
        attempt = self.claim("a", bound=False)
        self.plan(specs + [self.loop("b")], success=False)
        self.reconcile(attempt)
        self.plan(specs + [self.loop("b")])
        self.assertEqual(self.ready(), ["a", "b"])

    def test_plan_preserves_unchanged_results_and_invalidates_changed_consumers(self):
        self.graph()
        for loop_id in ("build", "check", "other", "accept"):
            self.complete(loop_id)
        replaced = copy.deepcopy(self.specs)
        replaced[0]["goal"] = "Produce build with updated requirements"
        self.plan(replaced)
        self.assertEqual(self.ready(), ["build"])
        self.complete("build")
        self.assertEqual(self.ready(), ["check"])
        self.complete("check")
        self.complete("accept")
        self.main_op("finish")

    def test_plan_addition_keeps_passed_results_and_removal_keeps_history(self):
        self.plan([self.loop("a")])
        old = self.complete("a")
        before = self.main_op("history")["events"]
        self.plan([self.loop("a"), self.loop("accept", ["a"], 2, "acceptance")])
        self.assertEqual(self.ready(), ["accept"])
        self.complete("accept")
        self.plan([self.loop("accept", kind="acceptance")])
        after = self.main_op("history")["events"]
        self.assertEqual(after[:len(before)], before)
        self.assertIn(str(old["run"]), json.dumps(after))

    def test_added_work_requires_new_final_acceptance(self):
        specs = [self.loop("a"), self.loop("accept", ["a"], 2, "acceptance")]
        self.plan(specs)
        self.complete("a")
        self.complete("accept")
        before = self.main_op("status")
        self.plan(specs + [self.loop("b")], success=False)
        self.assertEqual(self.main_op("status"), before)
        self.plan([self.loop("a"), self.loop("b"),
                   self.loop("accept", ["a", "b"], 2, "acceptance")])
        self.assertEqual(self.ready(), ["b"])
        self.main_op("finish", success=False)
        self.complete("b")
        self.assertEqual(self.ready(), ["accept"])
        self.main_op("finish", success=False)
        self.complete("accept")
        self.main_op("finish")

    def test_completed_task_plan_change_reopens_for_resume(self):
        self.plan([self.loop("accept", kind="acceptance")])
        self.complete("accept")
        self.main_op("finish")
        self.plan([self.loop("new"), self.loop("accept", ["new"], 2, "acceptance")])
        self.assertEqual(self.main_op("status")["state"], "paused")
        self.main_op("resume", note="New plan confirmed")
        self.assertEqual(self.ready(), ["new"])
        self.complete("new")
        self.complete("accept")
        self.main_op("finish")

    def test_removing_final_acceptance_also_reopens_completed_task(self):
        self.plan([self.loop("a"), self.loop("accept", ["a"], 2, "acceptance")])
        self.complete("a")
        self.complete("accept")
        self.main_op("finish")
        self.plan([self.loop("a"), self.loop("b")])
        self.assertEqual(self.main_op("status")["state"], "paused")
        self.main_op("resume", note="Replan in progress")
        self.assertEqual(self.ready(), ["b"])
        self.main_op("finish", success=False)

    def test_blueprint_revision_invalidates_passed_results(self):
        self.plan([self.loop("work"), self.loop("accept", ["work"], 2, "acceptance")])
        self.complete("work")
        self.complete("accept")
        stale_revision = self.revision
        updated = {"goal": "Updated task goal", "acceptance": ["Updated task criterion"]}
        changed = self.main_op("blueprint", revision=self.revision,
                               blueprint=updated, reason="Task requirements changed")
        self.revision = changed["revision"]
        self.assertNotEqual(self.revision, stale_revision)
        self.main_op("blueprint", revision=stale_revision, blueprint=self.blueprint,
                     reason="Stale edit", success=False)
        self.assertEqual(self.main_op("status")["blueprint"], updated)
        self.main_op("finish", success=False)
        self.assertEqual(self.ready(), ["work"])
        self.complete("work")
        self.assertEqual(self.ready(), ["accept"])

    def test_blueprint_change_rejects_old_worker_and_verifier_snapshots(self):
        self.plan([self.loop("a"), self.loop("b")])
        worker = self.claim("a")
        verifier = self.review(self.submit(self.claim("b")))
        updated = {"goal": "New blueprint", "acceptance": ["New acceptance"]}
        changed = self.main_op("blueprint", revision=self.revision, blueprint=updated,
                               reason="Requirements updated")
        self.revision = changed["revision"]
        self.invoke("submit", {"summary": "stale", "artifacts": [str(self.file("old"))],
                               "conditions": {}}, access=worker["access"], success=False)
        self.verify(verifier, success=False)
        for attempt in (worker, verifier):
            self.assert_retry_reserved(attempt)
            self.reconcile(attempt)
        self.assertEqual(self.ready(), ["a", "b"])

    def test_history_is_append_only_retains_retries_and_never_logs_tokens(self):
        self.plan([self.loop("work")])
        before = self.main_op("history")["events"]
        self.assertIsInstance(before, list)
        failed = self.review(self.submit(self.claim("work")))
        self.verify(failed, passed=False)
        middle = self.main_op("history")["events"]
        self.assertEqual(middle[:len(before)], before)
        self.assertGreater(len(middle), len(before))
        self.reconcile(failed)
        fresh = self.complete("work")
        after = self.main_op("history")["events"]
        self.assertEqual(after[:len(middle)], middle)
        self.assertGreater(len(after), len(middle))
        encoded = json.dumps(after)
        for attempt in (failed, fresh):
            self.assertIn(str(attempt["run"]), encoded)
            self.assertNotIn(attempt["packet"]["token"], encoded)
            self.assertNotIn(attempt["review_response"]["packet"]["token"], encoded)
        main_packet = json.loads(self.main.read_text(encoding="utf-8"))
        if "token" in main_packet:
            self.assertNotIn(main_packet["token"], encoded)

    def test_conditions_reject_self_check_and_keep_only_reproduction_facts(self):
        self.plan([self.loop("work")])
        attempt = self.claim("work")
        submission = {"summary": "Implementation conclusion", "artifacts": [str(self.file("output"))]}
        for invalid in ({"self_check": "all checks passed"}, {"commands": "not a list"}, []):
            self.invoke("submit", {**submission, "conditions": invalid}, access=attempt["access"], success=False)
        facts = {"source": "/local/input", "source_version": "v1", "environment": "local", "commands": ["python3 check.py"]}
        self.invoke("submit", {**submission, "conditions": facts}, access=attempt["access"])
        response = self.invoke("review-packet", access=attempt["access"])
        self.assertEqual(response["packet"]["context"]["conditions"], facts)
        self.assertNotIn("Implementation conclusion", json.dumps(response))

    def test_request_json_file_and_unknown_operation(self):
        request_file = self.file(json.dumps({"task": self.task}), ".json")
        status = self.invoke("status", data_file=request_file)
        self.assertEqual(status["task"], self.task)
        self.invoke("nonexistent-operation", success=False)


if __name__ == "__main__":
    unittest.main()
