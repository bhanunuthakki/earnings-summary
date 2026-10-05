"""Portable tests of the normal runtime owner; Windows APIs are synthetic.

Operations disposition: no surface change. This optional in-memory channel
adds no operator action, scheduler entry, persistent receipt, or native retry.
Default call traces below retain the existing runtime behavior.
"""

from __future__ import annotations

import ctypes
import unittest
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from typing import Protocol, cast
from unittest.mock import patch

import runtime.job_runtime as job_runtime
from runtime.job_runtime import (
    NativeHandleObservation,
    NativeHandleObservations,
    NativeObservationRetentionError,
)


class WindowsJobForTest(Protocol):
    def close(self) -> None: ...


class WindowsJobFactory(Protocol):
    def __call__(self, handle: int, *, native_observations: object = None) -> WindowsJobForTest: ...

    def create_for_process(
        self, pid: int, *, native_observations: object = None
    ) -> WindowsJobForTest: ...


class RuntimeTestAPI(Protocol):
    windows_job: WindowsJobFactory
    resume_process_threads: Callable[..., None]
    process_is_in_job: Callable[..., bool]
    release_source_handles: Callable[..., None]


_RUNTIME_MEMBERS = {
    "windows_job": "_WindowsKillOnCloseJob",
    "resume_process_threads": "_resume_process_threads",
    "process_is_in_job": "_process_is_in_job",
    "release_source_handles": "_release_source_handles",
}
_runtime_members = {
    public: vars(job_runtime)[private] for public, private in _RUNTIME_MEMBERS.items()
}
assert all(callable(member) for member in _runtime_members.values())
runtime_test_api = cast(RuntimeTestAPI, SimpleNamespace(**_runtime_members))


class BooleanValue(Protocol):
    value: int


class ThreadEntry(Protocol):
    thread_id: int
    owner_process_id: int


class NativeError(OSError):
    def __init__(self, code: int, message: str = "synthetic native error") -> None:
        super().__init__(code, message)
        self.winerror = code


class Function:
    def __init__(self, kernel: Kernel, name: str) -> None:
        self.kernel, self.name = kernel, name

    def __call__(self, *args: object) -> int:
        return self.kernel.call(self.name, args)


class Kernel:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.last_error = 0
        self.open_count = 0
        self.assign_result = 1
        self.assign_error = 31
        self.create_result = 40
        self.open_fail = False
        self.resume_result = 1
        self.close_result = 1
        self.close_errors: dict[int, NativeError] = {}
        self.close_returns: dict[int, int] = {}
        self.close_last_error = 99
        self.pid = 321
        self.dll_loads = 0
        for name in (
            "CreateJobObjectW",
            "SetInformationJobObject",
            "OpenProcess",
            "AssignProcessToJobObject",
            "TerminateProcess",
            "IsProcessInJob",
            "CloseHandle",
            "CreateToolhelp32Snapshot",
            "Thread32First",
            "Thread32Next",
            "OpenThread",
            "ResumeThread",
        ):
            setattr(self, name, Function(self, name))

    def dll(self, *_args: object, **_kwargs: object) -> Kernel:
        self.dll_loads += 1
        return self

    def call(self, name: str, args: tuple[object, ...]) -> int:
        self.calls.append((name, args[:2] if name in ("CloseHandle", "ResumeThread") else ()))
        if name == "CreateJobObjectW":
            return self.create_result
        if name == "SetInformationJobObject":
            return 1
        if name == "OpenProcess":
            self.open_count += 1
            if self.open_fail:
                self.last_error = 5
                return 0
            return 41 if self.open_count == 1 else 42
        if name == "IsProcessInJob":
            cast(BooleanValue, getattr(args[2], "_obj")).value = 1
            return 1
        if name == "AssignProcessToJobObject":
            if not self.assign_result:
                self.last_error = self.assign_error
            return self.assign_result
        if name == "TerminateProcess":
            self.last_error = 77
            return 1
        if name == "CloseHandle":
            handle = cast(int, args[0])
            self.last_error = self.close_last_error
            if handle in self.close_errors:
                raise self.close_errors[handle]
            return self.close_returns.get(handle, self.close_result)
        if name == "CreateToolhelp32Snapshot":
            return 60
        if name == "Thread32First":
            entry = cast(ThreadEntry, getattr(args[1], "_obj"))
            entry.thread_id = 555
            entry.owner_process_id = self.pid
            return 1
        if name == "Thread32Next":
            return 0
        if name == "OpenThread":
            return 61
        if name == "ResumeThread":
            if self.resume_result == 0xFFFFFFFF:
                self.last_error = 32
            return self.resume_result
        raise AssertionError(name)

    def context(self) -> ExitStack:
        stack = ExitStack()
        stack.enter_context(patch.object(job_runtime.os, "name", "nt"))
        stack.enter_context(patch.object(ctypes, "WinDLL", side_effect=self.dll, create=True))
        stack.enter_context(
            patch.object(ctypes, "get_last_error", side_effect=lambda: self.last_error, create=True)
        )
        stack.enter_context(patch.object(ctypes, "WinError", new=NativeError, create=True))
        return stack

    def closed(self) -> list[int]:
        return [cast(int, args[0]) for name, args in self.calls if name == "CloseHandle"]


class NativeObservationTests(unittest.TestCase):
    def buffer(self) -> NativeHandleObservations:
        return NativeHandleObservations()

    def fill(self, buffer: NativeHandleObservations, count: int) -> None:
        for index in range(count):
            buffer.retain(
                NativeHandleObservation(
                    index + 1,
                    "acquire",
                    "OpenProcess",
                    "synthetic",
                    1000 + index,
                    1.0,
                    "synthetic request",
                    2.0,
                    "synthetic return",
                    1000 + index,
                    0,
                    None,
                )
            )

    def test_default_constructor_call_order_and_returns_match_existing_contract(self) -> None:
        k = Kernel()
        with k.context():
            job = runtime_test_api.windows_job.create_for_process(k.pid)
            self.assertEqual(vars(job)["_handle"], 40)
            self.assertIsNone(job.close())
        self.assertEqual(
            k.calls,
            [
                ("CreateJobObjectW", ()),
                ("SetInformationJobObject", ()),
                ("OpenProcess", ()),
                ("OpenProcess", ()),
                ("IsProcessInJob", ()),
                ("CloseHandle", (42,)),
                ("AssignProcessToJobObject", ()),
                ("CloseHandle", (41,)),
                ("CloseHandle", (40,)),
            ],
        )

    def test_observed_constructor_separate_membership_dll_and_final_close(self) -> None:
        k = Kernel()
        buffer = self.buffer()
        with k.context():
            job = runtime_test_api.windows_job.create_for_process(k.pid, native_observations=buffer)
            self.assertIsNone(job.close())
        self.assertEqual(
            [(f.operation, f.reference_role, f.handle, f.raw_return) for f in buffer.facts],
            [
                ("acquire", "owned-job", 40, 40),
                ("acquire", "assignment-process", 41, 41),
                ("acquire", "membership-query-process", 42, 42),
                ("close", "membership-query-process", 42, 1),
                ("close", "assignment-process", 41, 1),
                ("close", "owned-job", 40, 1),
            ],
        )
        self.assertEqual(k.dll_loads, 3)
        self.assertIsNone(buffer.unretained)
        self.assertEqual([f.ordinal for f in buffer.facts], list(range(1, 7)))
        self.assertTrue(all(f.requested_monotonic <= f.returned_monotonic for f in buffer.facts))

    def test_immutable_fixed_buffer_and_no_arbitrary_callback_or_subclass(self) -> None:
        k = Kernel()
        buffer = self.buffer()
        with k.context():
            job = runtime_test_api.windows_job.create_for_process(k.pid, native_observations=buffer)
        with self.assertRaises(FrozenInstanceError):
            setattr(buffer.facts[0], "handle", 123)
        with self.assertRaises(AttributeError):
            setattr(buffer, "callback", lambda: None)

        class Sub(NativeHandleObservations):
            pass

        for value in (lambda: None, Sub()):
            with k.context(), self.assertRaises(TypeError):
                runtime_test_api.windows_job.create_for_process(k.pid, native_observations=value)
        self.assertIsNotNone(job)

    def test_zero_handle_close_does_not_load_dll_or_call_native(self) -> None:
        k = Kernel()
        job = runtime_test_api.windows_job(0, native_observations=self.buffer())
        with k.context():
            self.assertIsNone(job.close())
        self.assertEqual(k.calls, [])
        self.assertEqual(k.dll_loads, 0)

    def test_false_close_bool_and_native_error_are_factual_not_success(self) -> None:
        k = Kernel()
        k.close_result = 0
        buffer = self.buffer()
        job = runtime_test_api.windows_job(40, native_observations=buffer)
        with k.context():
            self.assertIsNone(job.close())
            self.assertIsNone(job.close())
        fact = buffer.facts[0]
        self.assertEqual((fact.raw_return, fact.last_error), (0, 99))
        self.assertEqual(k.closed(), [40])
        self.assertEqual(vars(job)["_handle"], 0)

    def test_constructor_retention_overflow_closes_acquired_job_once(self) -> None:
        k = Kernel()
        buffer = self.buffer()
        self.fill(buffer, 256)
        with k.context(), self.assertRaises(NativeObservationRetentionError) as failure:
            runtime_test_api.windows_job.create_for_process(k.pid, native_observations=buffer)
        self.assertEqual(failure.exception.fact.operation, "acquire")
        self.assertEqual(len(buffer.facts), 256)
        self.assertIs(buffer.unretained, failure.exception.fact)
        self.assertEqual(k.closed(), [40])
        assert buffer.unretained is not None
        self.assertEqual(buffer.unretained.handle, 40)
        self.assertEqual(
            tuple(row[:3] for row in buffer.cleanup_failures),
            (("owned-job", 40, "NativeObservationRetentionError"),),
        )
        fact = buffer.cleanup_failures[0][3]
        assert fact is not None
        self.assertEqual(fact.raw_return, 1)
        self.assertEqual(fact.api, "CloseHandle")
        self.assertNotIn("OpenProcess", [name for name, _ in k.calls])

    def test_partial_constructor_retention_failure_attempts_each_reference_once(self) -> None:
        k = Kernel()
        buffer = self.buffer()
        self.fill(buffer, 255)
        with k.context(), self.assertRaises(NativeObservationRetentionError):
            runtime_test_api.windows_job.create_for_process(k.pid, native_observations=buffer)
        self.assertEqual(k.closed(), [40, 41])
        self.assertEqual(len(buffer.cleanup_failures), 2)

    def test_native_assignment_error_sample_precedes_termination_and_cleanup(self) -> None:
        k = Kernel()
        k.assign_result = 0
        k.close_errors[40] = NativeError(9)
        buffer = self.buffer()
        with k.context(), self.assertRaises(NativeError) as failure:
            runtime_test_api.windows_job.create_for_process(k.pid, native_observations=buffer)
        self.assertEqual(failure.exception.winerror, 31)
        self.assertEqual(k.closed(), [42, 40, 41])
        self.assertEqual(
            tuple(row[:3] for row in buffer.cleanup_failures), (("owned-job", 40, "NativeError"),)
        )
        failed = next(f for f in buffer.facts if f.operation == "close" and f.handle == 40)
        self.assertEqual(failed.raised_type, "NativeError")
        self.assertIsNone(failed.raw_return)

    def test_native_failure_wins_if_buffer_retention_also_fails(self) -> None:
        k = Kernel()
        k.open_fail = True
        buffer = self.buffer()
        self.fill(buffer, 255)
        with k.context(), self.assertRaises(NativeError) as failure:
            runtime_test_api.windows_job.create_for_process(k.pid, native_observations=buffer)
        self.assertEqual(failure.exception.winerror, 5)
        self.assertEqual(k.closed(), [40])
        assert buffer.unretained is not None
        self.assertEqual(buffer.unretained.raw_return, 0)

    def test_success_path_close_retention_failure_does_not_return_unowned_job(self) -> None:
        k = Kernel()
        buffer = self.buffer()
        self.fill(buffer, 252)
        with k.context(), self.assertRaises(NativeObservationRetentionError):
            runtime_test_api.windows_job.create_for_process(k.pid, native_observations=buffer)
        self.assertEqual(k.closed(), [42, 41, 40])
        self.assertEqual(len(set(k.closed())), 3)

    def test_default_and_observed_resume_native_call_order_match(self) -> None:
        traces: list[list[tuple[str, tuple[object, ...]]]] = []
        buffer = self.buffer()
        for observations in (None, buffer):
            k = Kernel()
            with k.context():
                if observations is None:
                    self.assertIsNone(runtime_test_api.resume_process_threads(k.pid))
                else:
                    self.assertIsNone(
                        runtime_test_api.resume_process_threads(
                            k.pid, native_observations=observations
                        )
                    )
            traces.append(k.calls)
        expected = [
            ("CreateToolhelp32Snapshot", ()),
            ("Thread32First", ()),
            ("OpenThread", ()),
            ("ResumeThread", (61,)),
            ("CloseHandle", (61,)),
            ("Thread32Next", ()),
            ("CloseHandle", (60,)),
        ]
        self.assertEqual(traces, [expected, expected])
        self.assertEqual(
            [(f.operation, f.reference_role, f.handle) for f in buffer.facts],
            [
                ("acquire", "thread-snapshot", 60),
                ("acquire", "resume-thread", 61),
                ("close", "resume-thread", 61),
                ("close", "thread-snapshot", 60),
            ],
        )

    def test_resume_error_preserved_and_both_references_close_if_thread_close_raises(self) -> None:
        k = Kernel()
        k.resume_result = 0xFFFFFFFF
        k.close_errors[61] = NativeError(88)
        buffer = self.buffer()
        with k.context(), self.assertRaises(NativeError) as failure:
            runtime_test_api.resume_process_threads(k.pid, native_observations=buffer)
        self.assertEqual(failure.exception.winerror, 32)
        self.assertEqual(k.closed(), [61, 60])
        self.assertEqual(
            tuple(row[:3] for row in buffer.cleanup_failures),
            (("resume-thread", 61, "NativeError"),),
        )

    def test_resume_acquisition_retention_failure_attempts_thread_and_snapshot(self) -> None:
        k = Kernel()
        buffer = self.buffer()
        self.fill(buffer, 255)
        with k.context(), self.assertRaises(NativeObservationRetentionError):
            runtime_test_api.resume_process_threads(k.pid, native_observations=buffer)
        self.assertEqual(k.closed(), [61, 60])
        self.assertNotIn("ResumeThread", [name for name, _ in k.calls])

    def test_query_failure_preserves_error_and_closes_its_exact_reference(self) -> None:
        k = Kernel()
        buffer = self.buffer()
        self.fill(buffer, 256)
        with k.context(), self.assertRaises(NativeObservationRetentionError):
            runtime_test_api.process_is_in_job(k.pid, native_observations=buffer)
        self.assertEqual(k.closed(), [41])
        self.assertNotIn("IsProcessInJob", [name for name, _ in k.calls])

    def test_generic_fact_storage_failure_still_closes_known_job(self) -> None:
        k = Kernel()
        buffer = self.buffer()
        with (
            k.context(),
            patch.object(NativeHandleObservations, "retain", side_effect=MemoryError),
            self.assertRaises(MemoryError),
        ):
            runtime_test_api.windows_job.create_for_process(k.pid, native_observations=buffer)
        self.assertEqual(k.closed(), [40])
        self.assertEqual(buffer.retention_error_type, "MemoryError")

    def test_native_failure_wins_over_generic_fact_storage_failure(self) -> None:
        k = Kernel()
        k.open_fail = True
        buffer = self.buffer()
        with (
            k.context(),
            patch.object(NativeHandleObservations, "retain", side_effect=MemoryError),
            self.assertRaises(NativeError) as failure,
        ):
            runtime_test_api.process_is_in_job(k.pid, native_observations=buffer)
        self.assertEqual(failure.exception.winerror, 5)
        self.assertEqual(buffer.retention_error_type, "MemoryError")

    def test_default_close_false_bool_and_exception_keep_existing_contract(self) -> None:
        for close_error in (None, NativeError(88)):
            with self.subTest(close_error=close_error):
                k = Kernel()
                k.close_result = 0
                job = runtime_test_api.windows_job(40)
                if close_error is not None:
                    k.close_errors[40] = close_error
                with k.context():
                    if close_error is None:
                        self.assertIsNone(job.close())
                    else:
                        with self.assertRaises(NativeError) as failure:
                            job.close()
                        self.assertIs(failure.exception, close_error)
                    self.assertIsNone(job.close())
                self.assertEqual(k.closed(), [40])
                self.assertEqual(vars(job)["_handle"], 0)

    def test_default_cleanup_error_precedence_keeps_existing_contract(self) -> None:
        k = Kernel()
        k.assign_result = 0
        k.close_errors[40] = NativeError(9)
        with k.context(), self.assertRaises(NativeError) as failure:
            runtime_test_api.windows_job.create_for_process(k.pid)
        self.assertEqual(failure.exception.winerror, 9)
        self.assertEqual(k.closed(), [42, 40, 41])

        k = Kernel()
        k.resume_result = 0xFFFFFFFF
        k.close_errors[61] = NativeError(88)
        with k.context(), self.assertRaises(NativeError) as failure:
            runtime_test_api.resume_process_threads(k.pid)
        self.assertEqual(failure.exception.winerror, 88)
        self.assertEqual(k.closed(), [61, 60])

    def test_final_close_attempt_survives_pre_call_sampling_failure(self) -> None:
        k = Kernel()
        buffer = self.buffer()
        job = runtime_test_api.windows_job(40, native_observations=buffer)
        sampling_error = MemoryError("synthetic request clock storage failure")
        with (
            k.context(),
            patch.object(job_runtime, "_handle_request", side_effect=sampling_error),
            self.assertRaises(MemoryError) as failure,
        ):
            job.close()
        self.assertIs(failure.exception, sampling_error)
        self.assertEqual(k.closed(), [40])
        self.assertEqual(vars(job)["_handle"], 0)
        self.assertEqual(buffer.retention_error_type, "MemoryError")
        self.assertEqual(buffer.facts, ())
        with k.context():
            self.assertIsNone(job.close())
        self.assertEqual(k.closed(), [40])

    def test_each_known_cleanup_attempt_survives_pre_call_sampling_failure(self) -> None:
        k = Kernel()
        buffer = self.buffer()
        references = (("owned-job", 40), ("assignment-process", 41), ("resume-thread", 61))
        with (
            k.context(),
            patch.object(job_runtime, "_handle_request", side_effect=MemoryError),
            self.assertRaises(MemoryError),
        ):
            runtime_test_api.release_source_handles(k, references, buffer)
        self.assertEqual(k.closed(), [40, 41, 61])
        self.assertEqual(buffer.retention_error_type, "MemoryError")
        self.assertEqual(buffer.facts, ())
        self.assertEqual(
            tuple(row[:3] for row in buffer.cleanup_failures),
            tuple((role, handle, "MemoryError") for role, handle in references),
        )

    def test_native_close_error_wins_over_pre_call_sampling_failure(self) -> None:
        k = Kernel()
        native_error = NativeError(88)
        k.close_errors[40] = native_error
        buffer = self.buffer()
        job = runtime_test_api.windows_job(40, native_observations=buffer)
        with (
            k.context(),
            patch.object(job_runtime, "_handle_request", side_effect=MemoryError),
            self.assertRaises(NativeError) as failure,
        ):
            job.close()
        self.assertIs(failure.exception, native_error)
        self.assertEqual(k.closed(), [40])
        self.assertEqual(buffer.retention_error_type, "MemoryError")
        self.assertEqual(buffer.facts, ())


if __name__ == "__main__":
    unittest.main()
